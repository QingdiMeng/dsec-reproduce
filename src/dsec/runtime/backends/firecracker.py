"""Same-host, trusted microVM prototype; serialized operations only."""
import http.client
import json
from pathlib import Path
import socket
import subprocess
import time
from dsec.contracts.execution import ShellRequest
from dsec.runtime.sessions.channel import VsockCommandChannel

class UnixHTTP(http.client.HTTPConnection):
    def __init__(self, path, timeout=15):
        super().__init__("localhost", timeout=timeout)
        self.path = str(path)
    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.path)

class MicroVM:
    def __init__(self, binary, directory, *, max_timeout_ms=30000):
        self.binary = str(binary)
        self.directory = Path(directory)
        self.max_timeout_ms = max_timeout_ms
        self.api_path = self.directory / "api.sock"
        self.vsock = self.directory / "v.sock"
        self.process = None
        self.log = None
        self.state = "STOPPED"
        self.start_launcher = None
        self.last_boot_phases = None

    def api(self, method, path, body=None, *, timeout=15):
        conn = UnixHTTP(self.api_path, timeout=timeout)
        try:
            conn.request(method, path, json.dumps(body) if body is not None else None,
                         {"Content-Type": "application/json"})
            resp = conn.getresponse(); data = resp.read()
            if resp.status not in (200, 204):
                raise RuntimeError(f"{method} {path}: {resp.status}: {data.decode()}")
            return json.loads(data) if data else None
        finally:
            conn.close()

    def start_process(self, name):
        if self.process is not None:
            raise RuntimeError("Process already exists")
        self.api_path.unlink(missing_ok=True)
        self.vsock.unlink(missing_ok=True)
        self.log = (self.directory / name).open("wb")
        if self.start_launcher is None:
            self.process = subprocess.Popen([self.binary, "--api-sock", str(self.api_path)],
                                            stdin=subprocess.DEVNULL, stdout=self.log,
                                            stderr=subprocess.STDOUT)
        else:
            self.process = self.start_launcher.start(self.binary, self.api_path,
                                                     self.log, name)
        if getattr(self, "on_process_started", None):
            self.on_process_started()
        deadline = time.monotonic()+10
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError("VMM exited; inspect log")
            if self.api_path.exists():
                self.state = "UNCONFIGURED"
                return
            time.sleep(.02)
        raise TimeoutError("API socket not ready")

    def boot(self, kernel, disk, *, memory_profile="default", data=None, work=None,
             memory_mib=None, tap_name=None, cpu_count=1, network=None,
             verifier_disk=None, track_dirty_pages=False, layer_disks=None,
             free_page_reporting=False, verifier_dax=False,
             readonly_pmem_disks=None, layer_dax_indices=None):
        e3 = memory_profile != "default"
        layer_disks = tuple(layer_disks or ())
        readonly_pmem_disks = tuple(readonly_pmem_disks or ())
        layer_dax_indices = frozenset(layer_dax_indices or ())
        if not isinstance(free_page_reporting, bool):
            raise ValueError("free_page_reporting must be boolean")
        if not isinstance(verifier_dax, bool) or (verifier_dax and verifier_disk is None):
            raise ValueError("verifier_dax requires a verifier disk and boolean flag")
        if any(not isinstance(i, int) or i < 0 or i >= len(layer_disks)
               for i in layer_dax_indices):
            raise ValueError("DAX layer index is outside the EROFS layer stack")
        balloon_enabled = free_page_reporting or memory_profile in ("damon_fpr", "dax_damon_fpr")
        if e3 and (data is None or work is None):
            raise ValueError("E3 memory profile requires shared data and private work disks")
        if e3 and layer_disks:
            raise ValueError("E3 data drives and TB2 EROFS layer drives cannot be combined")
        # Firecracker's x86 legacy GSI pool permits 17 VirtIO devices in this
        # configuration.  Count all devices, not just the EROFS layer drives:
        # the verifier disk can push an otherwise bootable task over the limit.
        virtio_devices = (1 + len(layer_disks) + 1 + int(balloon_enabled) +
                          int(verifier_disk is not None) +
                          int(bool(network or tap_name)) + len(readonly_pmem_disks))
        if virtio_devices > 17:
            raise ValueError(
                f"Firecracker x86 VirtIO/GSI limit: {virtio_devices} devices "
                f"(root=1, EROFS={len(layer_disks)}, vsock=1, balloon={int(balloon_enabled)}, "
                f"verifier={int(verifier_disk is not None)}, "
                f"network={int(bool(network or tap_name))}, "
                f"extra_pmem={len(readonly_pmem_disks)}); maximum is 17. "
                "Merge one or more readonly layers before boot.")
        if e3 and verifier_disk is not None:
            raise ValueError("E3 data disk and TB2 verifier disk cannot share /dev/vdb")
        if not isinstance(cpu_count, int) or isinstance(cpu_count, bool) or not 1 <= cpu_count <= 32:
            raise ValueError("cpu_count must be an integer in 1..32")
        stage = "vmm_process"
        stage_started = time.monotonic()
        phases = {}

        def advance(next_stage):
            nonlocal stage, stage_started
            now = time.monotonic()
            phases[stage] = round(now-stage_started, 6)
            stage, stage_started = next_stage, now

        self.start_process("boot.log")
        advance("machine_config")
        machine_config = {"vcpu_count":cpu_count,
                          "mem_size_mib":memory_mib or (512 if e3 else 256),
                          "smt":False}
        if track_dirty_pages:
            machine_config["track_dirty_pages"] = True
        self.api("PUT", "/machine-config", machine_config)
        advance("boot_source")
        boot_args = "console=ttyS0 reboot=k panic=1 pci=off root=/dev/vda rw init=/dsec-init"
        if network:
            boot_args += (f" dsec_guest_ip={network['guest_cidr']}"
                          f" dsec_gateway={network['gateway']} dsec_dns={network['dns']}")
            tap_name = network["tap"]
        self.api("PUT", "/boot-source", {"kernel_image_path":str(kernel),
                                          "boot_args":boot_args})
        advance("drives")
        self.api("PUT", "/drives/rootfs", {"drive_id":"rootfs", "path_on_host":str(disk),
                 "is_root_device":True, "is_read_only":False})
        if verifier_disk is not None and verifier_dax:
            self.api("PUT", "/pmem/verifier", {"id":"verifier",
                     "path_on_host":str(verifier_disk),
                     "root_device":False, "read_only":True})
        for index, layer in enumerate(layer_disks):
            if index in layer_dax_indices:
                self.api("PUT", f"/pmem/layer_{index}",
                         {"id":f"layer_{index}", "path_on_host":str(layer),
                          "root_device":False, "read_only":True})
            else:
                self.api("PUT", f"/drives/layer_{index}",
                         {"drive_id":f"layer_{index}", "path_on_host":str(layer),
                          "is_root_device":False, "is_read_only":True})
        if verifier_disk is not None and not verifier_dax:
            self.api("PUT", "/drives/verifier", {"drive_id":"verifier",
                     "path_on_host":str(verifier_disk), "is_root_device":False,
                     "is_read_only":True})
        for index, image in enumerate(readonly_pmem_disks):
            self.api("PUT", f"/pmem/readonly_{index}",
                     {"id":f"readonly_{index}", "path_on_host":str(image),
                      "root_device":False, "read_only":True})
        if e3:
            dax = memory_profile in ("dax", "dax_damon_fpr")
            if dax:
                self.api("PUT", "/pmem/data", {"id":"data", "path_on_host":str(data),
                         "root_device":False, "read_only":True})
            else:
                self.api("PUT", "/drives/data", {"drive_id":"data", "path_on_host":str(data),
                         "is_root_device":False, "is_read_only":True})
            self.api("PUT", "/drives/writable", {"drive_id":"writable", "path_on_host":str(work),
                     "is_root_device":False, "is_read_only":False})
        if balloon_enabled:
            self.api("PUT", "/balloon", {"amount_mib":0, "deflate_on_oom":False,
                                          "free_page_reporting":True,
                                          "stats_polling_interval_s":5})
        advance("network_vsock")
        if tap_name:
            self.api("PUT", "/network-interfaces/eth0",
                     {"iface_id":"eth0", "host_dev_name":tap_name,
                      "guest_mac":network["mac"] if network else "06:00:ac:1e:6e:02"})
        self.api("PUT", "/vsock", {"guest_cid":3, "uds_path":str(self.vsock)})
        advance("instance_start")
        self.api("PUT", "/actions", {"action_type":"InstanceStart"})
        self.state = "RUNNING"
        advance("guest_ready")
        self.wait_ready()
        advance("guest_config")
        if e3:
            self.configure_e3(memory_profile)
        if verifier_disk is not None:
            block_layers = len(layer_disks) - len(layer_dax_indices)
            self.configure_verifier(chr(ord("b") + block_layers), dax=verifier_dax)
        advance("done")
        self.last_boot_phases = phases

    def configure_verifier(self, device_letter="b", *, dax=False):
        device = "/dev/pmem0" if dax else f"/dev/vd{device_letter}"
        options = "ro,noload,dax=always" if dax else "ro,noload"
        command = ("mkdir -p /mnt/dsec-verifier && "
                   f"mount -t ext4 -o {options} {device} /mnt/dsec-verifier && "
                   "test -x /mnt/dsec-verifier/bin/uv && "
                   "test -x /mnt/dsec-verifier/bin/uvx && "
                   "/mnt/dsec-verifier/bin/uvx --version && "
                   "grep ' /mnt/dsec-verifier ' /proc/mounts")
        result = self.execute(command, timeout_ms=30000)
        if (result["exit_code"] or result["timed_out"] or
                "uvx 0.9.5" not in result["output"] or
                ("dax=always" in result["output"]) != dax):
            raise RuntimeError("TB2 verifier artifact mount failed: " + result["output"][-500:])
        return result["output"]

    def configure_e3(self, memory_profile):
        dax = memory_profile in ("dax", "dax_damon_fpr")
        device = "/dev/pmem0" if dax else "/dev/vdb"
        work_device = "/dev/vdb" if dax else "/dev/vdc"
        opts = "ro,dax" if dax else "ro"
        command = ("mkdir -p /mnt/data /mnt/work; "
                   f"mount -t ext4 -o {opts} {device} /mnt/data; "
                   f"mount -t ext4 -o rw {work_device} /mnt/work")
        result = self.execute(command, timeout_ms=30000)
        if result["exit_code"] or result["timed_out"]:
            raise RuntimeError("E3 disk mounts failed: " + result["output"][-500:])
        if memory_profile in ("damon_fpr", "dax_damon_fpr"):
            command = ("p=/sys/module/damon_reclaim/parameters; "
                       "echo 1000000 > $p/min_age; echo 1000 > $p/wmarks_high; "
                       "echo 999 > $p/wmarks_mid; echo 0 > $p/wmarks_low; echo Y > $p/enabled")
            result = self.execute(command, timeout_ms=30000)
            if result["exit_code"] or result["timed_out"]:
                raise RuntimeError("E3 DAMON/FPR activation failed: " + result["output"][-500:])
        return self.memory_probe(memory_profile)

    def memory_probe(self, memory_profile):
        if memory_profile == "default":
            return {"profile": "default"}
        dax = memory_profile in ("dax", "dax_damon_fpr")
        fpr = memory_profile in ("damon_fpr", "dax_damon_fpr")
        result = self.execute("grep ' /mnt/data ' /proc/mounts; "
                              "grep ' /mnt/work ' /proc/mounts; "
                              "p=/sys/module/damon_reclaim/parameters; "
                              "cat $p/enabled $p/kdamond_pid", timeout_ms=5000)
        if result["exit_code"] or result["timed_out"]:
            raise RuntimeError("E3 guest memory probe failed")
        lines = result["output"].splitlines()
        if len(lines) < 4 or ("dax=always" in lines[0]) != dax or " /mnt/work ext4 rw," not in lines[1]:
            raise RuntimeError("E3 DAX/private disk mount mismatch: " + result["output"][-500:])
        enabled = lines[2] == "Y" and int(lines[3]) > 0
        if enabled != fpr:
            raise RuntimeError("E3 DAMON/FPR state mismatch: " + result["output"][-500:])
        return {"profile": memory_profile, "dax": dax, "damon_fpr": fpr,
                "data_mount": lines[0], "work_mount": lines[1], "damon_enabled": enabled}

    def wait_ready(self):
        deadline = time.monotonic()+15
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError("VMM exited before guest readiness; inspect log")
            try:
                # Only this side-effect-free readiness probe may be retried.
                result = self.execute("true", timeout_ms=500)
                if result["exit_code"] == 0:
                    return
            except (OSError, EOFError):
                pass
            time.sleep(.05)
        raise TimeoutError("Guest agent not ready")

    def execute(self, command, timeout_ms=5000, output_limit=65536):
        if self.state != "RUNNING":
            raise RuntimeError(f"Cannot execute in {self.state}")
        return VsockCommandChannel(self.vsock, max_timeout_ms=self.max_timeout_ms,
            socket_factory=lambda *args: socket.socket(*args)).execute(
                ShellRequest(command, timeout_ms, output_limit))

    def pause(self):
        self.api("PATCH", "/vm", {"state":"Paused"}); self.state = "PAUSED"

    def resume(self):
        self.api("PATCH", "/vm", {"state":"Resumed"}); self.state = "RUNNING"

    def restore(self, snapshot, memory, *, track_dirty_pages=False):
        self.start_process("restore.log")
        self.api("PUT", "/snapshot/load", {"snapshot_path":str(snapshot),
                 "mem_backend":{"backend_path":str(memory), "backend_type":"File"},
                 "resume_vm":False, "track_dirty_pages":track_dirty_pages})
        self.state = "PAUSED"
        self.resume(); self.wait_ready()

    def restore_fork(self, snapshot, memory, disk, *, work=None, tap=None):
        """Rebind writable drives while paused; never run against the source disk."""
        self.start_process("restore.log")
        args = {"snapshot_path": str(snapshot),
                "mem_backend": {"backend_path": str(memory), "backend_type": "File"},
                "resume_vm": False, "vsock_override": {"uds_path": str(self.vsock)}}
        if tap:
            args["network_overrides"] = [{"iface_id": "eth0", "host_dev_name": tap}]
        self.api("PUT", "/snapshot/load", args)
        self.state = "PAUSED"
        self.api("PATCH", "/drives/rootfs", {"drive_id": "rootfs", "path_on_host": str(disk)})
        if work:
            self.api("PATCH", "/drives/writable", {"drive_id": "writable", "path_on_host": str(work)})
        self.resume()
        self.wait_ready()

    def stop(self):
        if self.process is not None:
            if self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.process.kill(); self.process.wait(timeout=5)
            self.process = None
        if self.log:
            self.log.close(); self.log = None
        self.state = "STOPPED"
