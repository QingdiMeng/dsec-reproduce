"""Verify native SDK against an already deployed Edge and prepared guest.

Creates at most two sandboxes, uses no model/network, and stops both on exit.
This is functional acceptance; elapsed times are not performance benchmarks.
"""
import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import time
import traceback
import uuid

from dsec.contracts.sandbox import DSecContainerRunArgs, DSecMicroVMRunArgs
from dsec.sdk.client import DSecClient
from dsec.sdk.sandbox_transport import ServiceError


async def verify(socket, backend, environment, output):
    if output.exists():
        raise FileExistsError(output)
    report = dict(status="running", backend=backend, environment=environment,
                  checks=[], sandboxes=[], cleanup_errors=[])
    boxes = []
    client = DSecClient(socket)
    def passed(name, **details):
        report["checks"].append(dict(name=name, **details))
        output.write_text(json.dumps(report, indent=2)+"\n")
    async def result(session, command, **options):
        value = await session.run_shell(command, **options)
        assert value["exit_code"] == 0, value
        return value
    try:
        await client.open()
        report["features"] = sorted(client._features)
        for _ in range(2):
            if backend == "microvm":
                box = await client.run_microvm(DSecMicroVMRunArgs(environment_id=environment))
            else:
                box = await client.run_container(DSecContainerRunArgs(environment_id=environment))
            boxes.append(box)
            report["sandboxes"].append(box.id)
        box, other = boxes
        legacy = await box.run_shell("printf legacy")
        assert legacy["exit_code"] == 0 and legacy["output"] == "legacy", legacy
        passed("legacy_one_shot")
        session = await box.open_session()
        separate = await box.open_session()
        await result(session, "mkdir -p /tmp/native-sdk; cd /tmp/native-sdk; export NATIVE_STATE=kept")
        command = 'printf "%s:%s" "$PWD" "$NATIVE_STATE"'
        assert (await result(session, command))["stdout"] == "/tmp/native-sdk:kept"
        assert (await result(separate, 'printf "%s" "${NATIVE_STATE-unset}"'))["stdout"] == "unset"
        passed("persistent_cwd_env_and_session_isolation")
        data = b"\0\xff\r\n"*40000 + "binary中文".encode()
        transfer = uuid.uuid4().hex
        await box.write_file("/tmp/native-sdk/payload", data, request_id=transfer)
        await box.write_file("/tmp/native-sdk/payload", data, request_id=transfer)
        assert await box.read_file("/tmp/native-sdk/payload") == data
        try:
            await box.write_file("/tmp/native-sdk/payload", b"different", request_id=transfer)
            raise AssertionError("Conflicting transfer accepted")
        except ServiceError as exc:
            assert exc.kind == "RequestConflict", exc
        assert await box.read_file("/tmp/native-sdk/payload") == data
        isolated = await other.run_shell("test ! -e /tmp/native-sdk/payload")
        assert isolated["exit_code"] == 0, isolated
        passed("binary_chunked_atomic_deduplicated_files", bytes=len(data),
               sha256=hashlib.sha256(data).hexdigest())
        op = uuid.uuid4().hex
        once = "printf x >> /tmp/native-sdk/once"
        first = await result(session, once, request_id=op)
        assert await result(session, once, request_id=op) == first
        assert await box.read_file("/tmp/native-sdk/once") == b"x"
        passed("command_deduplication")
        began = time.monotonic()
        async def barrier(current, peer):
            return await result(current,
                f"touch /tmp/native-sdk/ready-{peer}; i=0; "
                f"while [ ! -e /tmp/native-sdk/ready-{3-peer} ]; do "
                "i=$((i+1)); [ \"$i\" -lt 400 ] || exit 1; sleep .01; done",
                timeout_ms=10000)
        # Each command must observe the other command's effect while it is
        # still executing. This proves overlap without a speed threshold.
        await asyncio.gather(barrier(session, 1), barrier(separate, 2))
        elapsed = time.monotonic()-began
        passed("different_sessions_overlap", elapsed_s=elapsed)
        stream_id = uuid.uuid4().hex
        stream = session.stream("printf first; sleep 1; printf last; printf error >&2",
                                request_id=stream_id)
        first_event = await anext(stream)
        assert first_event["type"] == "stdout" and first_event["data"] == b"first", first_event
        cursor = first_event["cursor"]
        await stream.aclose()
        await client.close()
        await client.open()
        attached = (await client.attach(box.id) if backend == "microvm" else
                    await client.attach_container(box.id, box._run_args)).attach_session(session.id)
        events = [event async for event in attached.events(stream_id, cursor=cursor)]
        assert b"".join(e["data"] for e in events if e["type"] == "stdout") == b"last", events
        assert b"".join(e["data"] for e in events if e["type"] == "stderr") == b"error", events
        assert events[-1]["type"] == "result" and events[-1]["result"]["exit_code"] == 0, events
        passed("stream_detach_reattach_ordered_stdout_stderr")
        cancel_id = uuid.uuid4().hex
        running = session.stream("printf started; sleep 10; printf unexpected", request_id=cancel_id,
                                 timeout_ms=15000)
        assert (await anext(running))["type"] == "stdout"
        if backend == "microvm":
            pause_id = uuid.uuid4().hex
            try:
                await box.pause(request_id=pause_id)
                raise AssertionError("Active native command was snapshotted")
            except ServiceError as exc:
                assert exc.kind == "ServiceBusy", exc
            assert (await client.lookup_request(pause_id))["state"] == "NOT_FOUND"
            passed("active_pause_rejected_before_effects")
        cancelled = await session.cancel(cancel_id)
        assert cancelled["cancel_requested"], cancelled
        remaining = [e async for e in running]
        assert remaining[-1]["result"]["cancelled"] and remaining[-1]["result"]["session_reset"], remaining
        passed("exact_operation_cancel")
        session = await box.open_session()
        bounded = await session.run_shell("while :; do printf 0123456789; done", timeout_ms=200,
                                          output_limit=1024)
        assert bounded["timed_out"] and bounded["truncated"] and bounded["session_reset"], bounded
        passed("continuous_output_bounded_timeout")
        session = await box.open_session()
        await result(session, "cd /tmp/native-sdk; export NATIVE_STATE=snapshot")
        if backend == "microvm":
            before = await box.status()
            await box.pause()
            assert (await box.status())["state"] == "PAUSED"
            await box.resume()
            after = await box.status()
            assert (await result(session, command))["stdout"] == "/tmp/native-sdk:snapshot"
            assert await box.read_file("/tmp/native-sdk/payload") == data
            passed("idle_session_snapshot_restore", before_pid=before.get("pid"), after_pid=after.get("pid"))
        await session.close()
        await separate.close()
        report["status"] = "passed"
    except BaseException as exc:
        report.update(status="failed", error=repr(exc), traceback=traceback.format_exc())
        raise
    finally:
        for box in reversed(boxes):
            try:
                await box.stop()
                if backend == "microvm":
                    assert (await box.status())["state"] == "STOPPED"
            except Exception as exc:
                report["cleanup_errors"].append(dict(sandbox=box.id, error=repr(exc)))
        await client.close()
        if report["cleanup_errors"]:
            report["status"] = "failed"
        output.write_text(json.dumps(report, indent=2)+"\n")
    if report["status"] != "passed":
        raise RuntimeError(report)
    return dict(status=report["status"], checks=len(report["checks"]), output=str(output))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--socket", required=True)
    parser.add_argument("--backend", choices=("microvm", "container"), default="microvm")
    parser.add_argument("--environment")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if args.backend == "container" and not args.environment:
        parser.error("Container acceptance requires an explicitly prepared environment")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    print(json.dumps(asyncio.run(verify(args.socket, args.backend, args.environment, args.out))))


if __name__ == "__main__":
    main()
