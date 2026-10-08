"""Identity-checked VMM accounting called from the sandbox manager process."""

import json

from dsec.observability.meters import _process_sample
from dsec.runtime.lifecycle import SandboxError


def sandbox_resource_sample(manager, sandbox_id, identity_reader,
                            process_sampler=_process_sample):
    """Read only a registered live VMM, using its persisted process identity."""
    with manager.lock:
        sb = manager.sandboxes.get(sandbox_id)
    if (sb is None or getattr(sb, "reserved", False) or
            sb.state != "RUNNING" or sb.vm.process is None):
        raise SandboxError("No running sandbox to meter")
    expected = json.loads((sb.directory / "registry.json").read_text()).get("process")
    pid = sb.vm.process.pid
    if (not isinstance(expected, dict) or expected.get("pid") != pid or
            identity_reader(pid) != expected):
        raise SandboxError("VMM resource identity mismatch")
    return {"pid": pid, "sample": process_sampler(pid, int(expected["start_ticks"]))}
