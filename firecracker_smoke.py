"""Install pinned Firecracker locally and boot a trusted, networkless test VM twice."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import time

BASE = Path.home() / "dsec-reproduce" / "firecracker"
VERSION = "v1.17.0"
RELEASE_SHA = "06094a1108ae9e82aa4c23a775aa92758f53f1175d422270d9d6162cb9ade558"
ALPINE = "sha256:d9e853e87e55526f6b2917df91a2115c36dd7c696a35be12163d44e6e2a4b6bc"
KERNEL_KEY = "firecracker-ci/20260923-6f82ac4cf331-0/x86_64/vmlinux-6.1.186"

def run(args, **kwargs):
    return subprocess.run(args, check=True, **kwargs)

def sha(path):
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()

def download(url, path):
    if not path.exists():
        temp = path.with_suffix(path.suffix + ".part")
        run(["curl", "-fL", "--connect-timeout", "10", "--max-time", "180", "--retry", "2", "-o", str(temp), url])
        temp.rename(path)

def main():
    BASE.mkdir(parents=True, exist_ok=True)
    os.chmod(BASE, 0o700)
    archive = BASE / f"firecracker-{VERSION}-x86_64.tgz"
    release_url = f"https://github.com/firecracker-microvm/firecracker/releases/download/{VERSION}/{archive.name}"
    download(release_url, archive)
    assert sha(archive) == RELEASE_SHA, "Release checksum mismatch"
    release = BASE / f"release-{VERSION}-x86_64"
    with tarfile.open(archive) as tar:
        tar.extractall(BASE, filter="data")
    binary = release / f"firecracker-{VERSION}-x86_64"
    version = run([str(binary), "--version"], capture_output=True, text=True).stdout.strip()
    print(version, flush=True)
    kernel = BASE / "vmlinux-6.1.186"
    kernel_url = "https://s3.amazonaws.com/spec.ccfc.min/" + KERNEL_KEY
    download(kernel_url, kernel)
    download(kernel_url + ".config", BASE / "guest-kernel.config")
    testdir = BASE / ("smoke-" + time.strftime("%Y%m%d-%H%M%S"))
    testdir.mkdir()
    root = testdir / "rootfs"
    root.mkdir()
    cid = run(["docker", "create", "--network", "none", ALPINE, "/bin/true"], capture_output=True, text=True).stdout.strip()
    try:
        with (testdir / "rootfs.tar").open("wb") as out:
            run(["docker", "export", cid], stdout=out)
    finally:
        run(["docker", "rm", cid], stdout=subprocess.DEVNULL)
    run(["tar", "--no-same-owner", "-xf", str(testdir / "rootfs.tar"), "-C", str(root)])
    init = root / "dsec-init"
    init.write_text('''#!/bin/sh
set -eu
mount -t proc proc /proc
mount -t sysfs sysfs /sys
echo DSEC_GUEST_KERNEL=$(uname -r)
echo DSEC_GUEST_UID=$(id -u)
count=0
if [ -f /dsec-counter ]; then count=$(cat /dsec-counter); fi
count=$((count + 1))
echo "$count" > /dsec-counter
sync
echo DSEC_PERSIST_COUNT=$count
echo DSEC_GUEST_SMOKE_PASS
reboot -f
''')
    init.chmod(0o755)
    disk = testdir / "rootfs.ext4"
    with disk.open("wb") as out:
        out.truncate(64 * 1024**2)
    run(["mkfs.ext4", "-q", "-F", "-d", str(root), str(disk)])
    config = {"boot-source": {"kernel_image_path": str(kernel),
              "boot_args": "console=ttyS0 reboot=k panic=1 pci=off root=/dev/vda rw init=/dsec-init"},
              "drives": [{"drive_id": "rootfs", "path_on_host": str(disk), "is_root_device": True, "is_read_only": False}],
              "machine-config": {"vcpu_count": 1, "mem_size_mib": 256, "smt": False}}
    cfg = testdir / "config.json"
    cfg.write_text(json.dumps(config, indent=2))
    results = []
    for expected in (1, 2):
        log = testdir / f"boot-{expected}.log"
        start = time.monotonic()
        with log.open("wb") as out:
            proc = subprocess.run([str(binary), "--no-api", "--config-file", str(cfg)],
                                  stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT, timeout=45)
        text = log.read_text(errors="replace")
        assert proc.returncode == 0, f"VMM failed: see {log}"
        assert "DSEC_GUEST_SMOKE_PASS" in text, f"Guest did not pass: see {log}"
        assert f"DSEC_PERSIST_COUNT={expected}" in text, f"Persistence failed: see {log}"
        results.append({"boot": expected, "returncode": proc.returncode, "wall_seconds": time.monotonic()-start,
                        "markers": [line for line in text.splitlines() if line.startswith("DSEC_")]})
        print(json.dumps(results[-1]), flush=True)
    manifest = {"firecracker_version": version, "release_url": release_url, "release_sha256": RELEASE_SHA,
                "kernel_url": kernel_url, "kernel_sha256": sha(kernel), "alpine_image_id": ALPINE,
                "host_kernel": os.uname().release, "results": results,
                "scope": "Trusted guest; no jailer or network; cold boot and disk persistence only, not memory snapshot restore."}
    (testdir / "result.json").write_text(json.dumps(manifest, indent=2))
    shutil.copy2(testdir / "result.json", BASE / "latest-result.json")
    shutil.rmtree(root)
    (testdir / "rootfs.tar").unlink()
    print("RESULT=" + str(testdir / "result.json"), flush=True)

if __name__ == "__main__":
    main()
