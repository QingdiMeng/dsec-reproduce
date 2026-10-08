"""Local durable registry. Uses pidfds to avoid signalling reused PIDs."""
import json
import os
from pathlib import Path
import select
import signal
import subprocess
from dsec.runtime.backends.firecracker import MicroVM
from dsec.runtime.lifecycle import Sandbox, SandboxManager, _fsync_directory
from dsec.runtime.registry_store import RegistryOperations, RegistryRecords, SandboxRegistry
from dsec.runtime.edge import initialize_edge

BOOT_ID = Path("/proc/sys/kernel/random/boot_id").read_text().strip()

def identity(pid):
    proc = Path(f"/proc/{pid}")
    fields = (proc/"stat").read_text().rsplit(")",1)[1].split()
    return {"pid":pid,"start_ticks":fields[19],"boot_id":BOOT_ID,
            "exe":str((proc/"exe").resolve(strict=True)),
            "argv":(proc/"cmdline").read_bytes().decode().strip("\0").split("\0"),
            "uid":proc.stat().st_uid}

class AttachedProcess:
    def __init__(self, saved, binary, api_path, *, attester=None):
        self.pid = saved["pid"]
        self.fd = os.pidfd_open(self.pid)
        try:
            current = attester(self.pid) if attester else identity(self.pid)
            if current != saved or current["uid"] != os.getuid() or current["exe"] != str(Path(binary).resolve()):
                raise RuntimeError("VMM process identity mismatch")
            if current["argv"] != [str(binary),"--api-sock",str(api_path)]:
                raise RuntimeError("Unexpected VMM command line")
        except Exception:
            os.close(self.fd); self.fd = None
            raise
    def poll(self):
        if self.fd is None:
            return 0
        return 0 if select.select([self.fd],[],[],0)[0] else None
    def terminate(self):
        if self.poll() is None:
            signal.pidfd_send_signal(self.fd, signal.SIGTERM)
    def kill(self):
        if self.poll() is None:
            signal.pidfd_send_signal(self.fd, signal.SIGKILL)
    def wait(self, timeout):
        if self.fd is not None and not select.select([self.fd],[],[],timeout)[0]:
            raise subprocess.TimeoutExpired("attached VMM",timeout)
        self.close()
        return 0
    def close(self):
        if self.fd is not None:
            os.close(self.fd); self.fd = None
    def __del__(self):
        if getattr(self,"fd",None) is not None:
            self.close()

def atomic_json(path, value):
    temp = path.with_suffix(".tmp")
    with temp.open("w") as stream:
        json.dump(value,stream,indent=2); stream.flush(); os.fsync(stream.fileno())
    os.replace(temp,path)
    fd = os.open(path.parent, os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)



def _detach_process(process):
    if isinstance(process, AttachedProcess):
        process.close()


def registry_operations():
    # Resolve native hooks at call time so existing fault/attestation hooks keep
    # their identity, while portable record handling has no Linux import effects.
    return RegistryOperations(
        boot_id=BOOT_ID,
        identity=lambda *args, **kwargs: identity(*args, **kwargs),
        attach_process=lambda *args, **kwargs: AttachedProcess(*args, **kwargs),
        process_entries=lambda: Path("/proc").iterdir(),
        detach_process=_detach_process,
        new_sandbox=lambda: Sandbox.__new__(Sandbox),
        new_microvm=lambda *args, **kwargs: MicroVM(*args, **kwargs),
        write_json=lambda *args: atomic_json(*args),
        sync_directory=lambda path: _fsync_directory(path))


def create_registry(root):
    return SandboxRegistry(root, registry_operations())


class DurableManager(SandboxManager):
    """Legacy constructor; the formal service uses composed open_edge()."""
    def __init__(self, root, binary, kernel, template, **kwargs):
        initialize_edge(self, root, binary, kernel, template, **kwargs)

    def _records(self):
        registry = getattr(self, 'registry', None)
        return registry.records if registry is not None else RegistryRecords(registry_operations())

    def _check_network_registry(self):
        return self._records().check_network_registry(self)

    def _reconcile_network_orphans(self):
        return self._records().reconcile_network_orphans(self)

    def _load(self):
        return self._records().load(self)

    def _prune_uncommitted(self, sandbox):
        return self._records().prune_uncommitted(self, sandbox)

    def _find_orphans(self):
        return self._records().find_orphans(self)
