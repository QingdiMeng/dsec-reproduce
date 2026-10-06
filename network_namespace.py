"""Unprivileged client for the root-owned per-microVM network helper."""
import json
import os
from pathlib import Path
import re
import select
import signal
import subprocess


class NetnsNetworkManager:
    def __init__(self, helper="/usr/local/libexec/dsec-netns-helper", *, max_slots=8,
                 dns="192.168.0.1", dax_binary=None):
        if not isinstance(max_slots, int) or isinstance(max_slots, bool) or not 1 <= max_slots <= 32768:
            raise ValueError("Invalid netns slot count")
        self.helper = str(helper)
        self.max_slots = max_slots
        self.dns = dns
        self.dax_binary = Path(dax_binary).resolve() if dax_binary is not None else None

    def _call(self, operation, sid, *args):
        try:
            result = subprocess.run(["sudo", "-n", self.helper, operation, sid,
                                     *(str(arg) for arg in args)],
                                    check=True, capture_output=True, text=True, timeout=30)
        except subprocess.CalledProcessError as exc:
            raise RuntimeError(f"Network helper {operation} failed: {exc.stderr[-1000:]}") from exc
        return json.loads(result.stdout)

    @staticmethod
    def slot_number(slot):
        if not isinstance(slot, str) or not re.fullmatch(r"ns-(0|[1-9][0-9]*)", slot):
            raise ValueError("Invalid netns slot identity")
        return int(slot[3:])

    def allocate(self, occupied):
        for index in range(self.max_slots):
            slot = f"ns-{index}"
            if slot not in occupied:
                return slot
        return None

    def ensure(self, sid, slot):
        number = self.slot_number(slot)
        if number >= self.max_slots:
            raise ValueError("Netns slot outside configured capacity")
        response = self._call("ensure", sid, number)
        if response.get("slot") != number or response.get("namespace") != "dsec-" + sid:
            raise RuntimeError("Network helper returned mismatched namespace")
        return self._network_config(number)

    def inspect(self, sid, slot):
        number = self.slot_number(slot)
        response = self._call("inspect", sid)
        if response.get("slot") != number or response.get("namespace") != "dsec-" + sid:
            raise RuntimeError("Persisted network namespace identity mismatch")
        return response

    def list(self):
        result = subprocess.run(["sudo", "-n", self.helper, "list"],
                                check=True, capture_output=True, text=True, timeout=30)
        return json.loads(result.stdout)

    def release(self, sid, slot):
        self.slot_number(slot)
        return self._call("release", sid)

    def attest(self, sid, slot, pid):
        self.slot_number(slot)
        result = self._call("attest", sid, pid)
        if result.get("pid") != pid:
            raise RuntimeError("Attested VMM PID mismatch")
        return result

    def launcher(self, sid, slot):
        self.slot_number(slot)
        return NetnsLauncher(self, sid, slot)

    def _network_config(self, number):
        return {"tap":"tap0", "guest_cidr":"169.254.110.2/30",
                "gateway":"169.254.110.1", "dns":self.dns,
                "mac":f"06:00:ac:1e:{(number >> 8) & 255:02x}:{number & 255:02x}"}


class NetnsLauncher:
    def __init__(self, manager, sid, slot):
        self.manager, self.sid, self.slot = manager, sid, slot

    def start(self, binary, api_path, log, log_name):
        self.manager.ensure(self.sid, self.slot)
        request_log = (log_name + "@dax39" if Path(binary).resolve() == self.manager.dax_binary
                       else log_name)
        result = self.manager._call("launch", self.sid, request_log)
        pid = result["pid"]
        if not isinstance(pid, int) or pid <= 1:
            raise RuntimeError("Invalid VMM PID from network helper")
        if result.get("namespace_attested") is not True:
            raise RuntimeError("Root helper did not attest the VMM network namespace")
        # AttachedProcess has pidfd-based signalling and strict executable,
        # argv, UID, and start-time validation. Import lazily to avoid a cycle.
        from durable_manager import AttachedProcess
        attester = lambda candidate: self.manager.attest(self.sid, self.slot, candidate)
        try:
            return AttachedProcess(result["identity"], binary, api_path,
                                   attester=attester)
        except Exception:
            # The helper may have launched a different pinned Firecracker build.
            # It is still our newly created process, but AttachedProcess will
            # rightly refuse to adopt it. Kill only after reattesting its exact
            # PID, start time, namespace, UID, and API socket; use pidfd to
            # avoid signalling a reused PID. Then normal create cleanup can
            # release the namespace rather than stranding an orphan VMM.
            saved = result["identity"]
            if (saved.get("pid") == pid and saved.get("uid") == os.getuid() and
                    saved.get("argv", [])[1:] == ["--api-sock", str(api_path)] and
                    attester(pid) == saved):
                fd = os.pidfd_open(pid)
                try:
                    if attester(pid) == saved:
                        signal.pidfd_send_signal(fd, signal.SIGTERM)
                        if not select.select([fd], [], [], 5)[0]:
                            signal.pidfd_send_signal(fd, signal.SIGKILL)
                            select.select([fd], [], [], 5)
                finally:
                    os.close(fd)
            raise
