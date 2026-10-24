"""Build the small writable bootstrap disk for an OCI-layered microVM."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import subprocess
import tempfile


def build(manifest: Path, agent_source: Path, busybox: Path, output: Path,
          timeout_ms: int, network: bool, size_mb: int,
          bundle_map: Path | None = None, dax_layer_indices: tuple[int, ...] = (),
          verifier_dax: bool = False) -> None:
    spec = json.loads(manifest.read_text())
    layers = spec["layers"]
    if not 1 <= len(layers) <= 17:
        raise ValueError("Firecracker guest needs 1..17 layer drives")
    dax_layer_indices = tuple(sorted(set(dax_layer_indices)))
    if any(i < 0 or i >= len(layers) or not layers[i].get("dax")
           for i in dax_layer_indices):
        raise ValueError("DAX layer indices must be pinned in the layer manifest")
    if bundle_map is not None and dax_layer_indices:
        raise ValueError("Bundled and pmem EROFS layers cannot be combined")
    for layer in layers:
        if not Path(layer["erofs"]).is_file():
            raise FileNotFoundError(layer["erofs"])
    bundle = None
    if bundle_map is not None:
        bundle = json.loads(bundle_map.read_text())
        if (bundle.get("format") != 1 or bundle.get("image_id") != spec["image_id"] or
                len(bundle.get("layers", [])) != len(layers)):
            raise ValueError("EROFS bundle map does not match image manifest")
        for original, span in zip(layers, bundle["layers"]):
            if (original["erofs_sha256"] != span["erofs_sha256"] or
                    original["erofs_bytes"] != span["length_bytes"] or
                    span["offset_bytes"] % 4096):
                raise ValueError("EROFS bundle layer order or size differs")
    if output.exists():
        raise FileExistsError(output)
    if size_mb < 256:
        raise ValueError("size_mb must be at least 256")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="dsec-erofs-boot-", dir=output.parent) as temp:
        work = Path(temp)
        root = work / "root"
        for directory in ("bin", "proc", "sys", "dev", "layers", "newroot",
                          "upper", "work"):
            (root / directory).mkdir(parents=True)
        shutil.copy2(busybox, root / "bin/busybox")
        applets = ("sh", "mount", "mkdir", "cat", "losetup", "mknod") if bundle else (
            "sh", "mount", "mkdir", "cat")
        for applet in applets:
            (root / f"bin/{applet}").symlink_to("busybox")
        subprocess.run(["gcc", "-static", "-O2", "-Wall", "-Wextra", "-Werror",
                        f"-DDSEC_MAX_TIMEOUT_MS={timeout_ms}", "-o",
                        str(root / "upper/dsec-agent"), str(agent_source)], check=True)
        mount_lines = []
        for index in range(len(layers)):
            mount_lines.append(f"mkdir -p /layers/{index}")
            if bundle:
                offset = bundle["layers"][index]["offset_bytes"]
                mount_lines.extend((
                    f"[ -b /dev/loop{index} ] || mknod /dev/loop{index} b 7 {index}",
                    f"losetup -r -o {offset} /dev/loop{index} /dev/vdb",
                    f"mount -t erofs -o ro /dev/loop{index} /layers/{index}"))
            else:
                if index in dax_layer_indices:
                    pmem = int(verifier_dax) + dax_layer_indices.index(index)
                    mount_lines.append(
                        f"mount -t erofs -o ro,dax=always /dev/pmem{pmem} /layers/{index}")
                else:
                    block = index - sum(i < index for i in dax_layer_indices)
                    mount_lines.append(
                        f"mount -t erofs -o ro /dev/vd{chr(ord('b') + block)} /layers/{index}")
        lower = ":".join(f"/layers/{i}" for i in reversed(range(len(layers))))
        network_lines = ""
        if network:
            network_lines = """\
guest_ip= gateway= dns=
for arg in $(cat /proc/cmdline); do
  case $arg in
    dsec_guest_ip=*) guest_ip=${arg#*=} ;;
    dsec_gateway=*) gateway=${arg#*=} ;;
    dsec_dns=*) dns=${arg#*=} ;;
  esac
done
[ -n "$guest_ip" ] && [ -n "$gateway" ] && [ -n "$dns" ]
/bin/busybox ip link set lo up
/bin/busybox ip link set eth0 up
/bin/busybox ip addr add "$guest_ip" dev eth0
/bin/busybox ip route add default via "$gateway"
mkdir -p /newroot/etc
printf 'nameserver %s\\n' "$dns" > /newroot/etc/resolv.conf
"""
        init = root / "dsec-init"
        init.write_text("#!/bin/sh\nset -eu\n"
                        "export PATH=/bin:/usr/bin:/usr/local/bin:/sbin:/usr/sbin:/usr/local/sbin HOME=/root\n"
                        "mount -t proc proc /proc\n"
                        "mount -t sysfs sysfs /sys\n"
                        + "\n".join(mount_lines) + "\n"
                        + f"mount -t overlay overlay -o lowerdir={lower},upperdir=/upper,workdir=/work /newroot\n"
                        + "mkdir -p /newroot/proc /newroot/sys /newroot/dev /newroot/tmp\n"
                        + network_lines
                        + "mount --move /proc /newroot/proc\n"
                        + "mount --move /sys /newroot/sys\n"
                        + "mount --move /dev /newroot/dev\n"
                        + "exec /bin/busybox chroot /newroot /dsec-agent\n")
        init.chmod(0o755)
        disk = work / "boot.ext4"
        with disk.open("wb") as stream:
            stream.truncate(size_mb * 1024 * 1024)
        subprocess.run(["mkfs.ext4", "-q", "-F", "-d", str(root), str(disk)], check=True)
        shutil.move(disk, output)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--agent-source", type=Path, required=True)
    parser.add_argument("--busybox", type=Path, default=Path("/usr/bin/busybox"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--max-timeout-ms", type=int, default=900000)
    parser.add_argument("--size-mb", type=int, default=10240)
    parser.add_argument("--network", action="store_true")
    parser.add_argument("--bundle-map", type=Path)
    parser.add_argument("--dax-layer-index", type=int, action="append", default=[])
    parser.add_argument("--verifier-dax", action="store_true")
    args = parser.parse_args()
    build(args.manifest, args.agent_source, args.busybox, args.output,
          args.max_timeout_ms, args.network, args.size_mb, args.bundle_map,
          tuple(args.dax_layer_index), args.verifier_dax)


if __name__ == "__main__":
    main()
