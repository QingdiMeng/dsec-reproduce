"""Native Edge dispatch uses existing ownership, journal and VM lifecycle."""
from contextlib import contextmanager
from dsec.contracts.errors import SandboxError, CommandOutcomeUnknown
from dsec.contracts.native import SESSION_ACTIONS, FILE_ACTIONS, STREAM_FEATURE
from dsec.contracts.sandbox import UnsupportedCapability
from dsec.runtime.isolation.proxy import guest_proxy_command
from dsec.runtime.sessions.native import NativeChannel


@contextmanager
def native_operation(server, sandbox_id, args, request_id):
    values = dict(args)
    backend, action = values.pop("backend"), values.pop("action")
    if action not in SESSION_ACTIONS | FILE_ACTIONS:
        raise ValueError("Unknown native action")
    if action in ("run", "stream"):
        values["operation_id"] = request_id
    if backend == "container":
        runtime = server.container_runtime
        runtime._journal()  # Require the existing container owner lock.
        kind, spec = runtime._spec(values)
        values.pop("kind", None)
        values.pop("spec", None)
        if kind != "container":
            raise UnsupportedCapability("Legacy TB2 server images need a native agent upgrade")
        with runtime._operation(sandbox_id):
            entry = runtime._entry(kind, spec, sandbox_id)
            endpoint = entry.backend.root / sandbox_id / "native.sock"
            if not endpoint.exists():
                raise UnsupportedCapability("Container has no native v1 agent")
        channel = NativeChannel(endpoint)
        channel.prepare(action, **values)
        channel.capabilities()
        if action == "stream" and STREAM_FEATURE not in channel.capabilities():
            raise UnsupportedCapability("Upgrade guest for native streaming")
        yield channel, values
        return
    if backend != "microvm":
        raise ValueError("Unknown native backend")
    manager = server.manager
    sandbox = manager.sandboxes.get(sandbox_id)
    if sandbox is None:
        raise SandboxError("Unknown sandbox")
    channel = NativeChannel(sandbox.vm.vsock, vsock=True,
                            max_timeout_ms=manager.command_timeout_ms(sandbox.environment_id))
    channel.prepare(action, **values)
    if action in ("run", "stream"):
        values["command"] = guest_proxy_command(values["command"], manager.egress_proxy_url,
                                                manager.egress_proxy_bypass_hosts)
        channel.prepare(action, **values)
    with sandbox.lock:
        sandbox._check()
        if sandbox.reserved or sandbox.baseline_sealed:
            raise SandboxError("Sandbox is reserved or sealed")
        if sandbox.state == "PAUSED":
            sandbox._restore()
        if sandbox.state != "RUNNING":
            raise SandboxError("Sandbox is not running")
        sandbox.native_inflight[request_id] = action
        try:
            sandbox._persist()
        except Exception:
            sandbox.native_inflight.pop(request_id, None)
            raise
    # A command does not hold the sandbox-wide lifecycle lock. Distinct native
    # sessions can execute concurrently; pause sees the durable activity fence.
    try:
        features = channel.capabilities()
        if action == "stream" and STREAM_FEATURE not in features:
            raise UnsupportedCapability("Upgrade guest for native streaming")
        yield channel, values
    except CommandOutcomeUnknown:
        # The guest may still be executing. Retire it before releasing the
        # activity fence; neither pause nor a replacement shell may hide it.
        with sandbox.lock:
            sandbox._fail("native_operation_outcome_unknown")
        raise
    finally:
        with sandbox.lock:
            sandbox.native_inflight.pop(request_id, None)
            sandbox._touch()


def dispatch_native(server, sandbox_id, args, request_id):
    if args.get("action") == "stream":
        raise ValueError("Use native_stream to start a streaming command")
    with native_operation(server, sandbox_id, args, request_id) as (channel, values):
        return channel.call(args["action"], **values)
