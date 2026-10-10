"""Replay real C-agent shutdown traces against the same TLA+ specification.

Controlled signals use compile-time-only probes in the real command path.
The historical assignment mutant is a required negative control, not a second
implementation. Python projects observations; TLC evaluates model transitions.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import time
import uuid

from check_concurrency_model import checked_jar, fetch_jar, TLC_SHA256


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from dsec.runtime.sessions.native import NativeChannel, NativeSessionReset
from dsec.contracts.errors import CommandOutcomeUnknown

RECORD = struct.Struct("=iiiii")
PHASES = {0: "RUN", 1: "ASSIGN", 2: "IDLE"}
FIXED_STATEMENT = "if(stop_run)ns_stopping=1;"
UNSAFE_STATEMENT = "ns_stopping=stop_run;"


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build(cc, out, unsafe):
    source = (ROOT / "guest_native.c").read_text()
    if source.count(FIXED_STATEMENT) != 1:
        raise ValueError("Code/model mapping changed: review the result-application boundary")
    if unsafe:
        source = source.replace(FIXED_STATEMENT, UNSAFE_STATEMENT, 1)
    target = out / ("unsafe.c" if unsafe else "fixed.c")
    target.write_text(source)
    binary = target.with_suffix("")
    result = subprocess.run([cc, "-DDSEC_NATIVE_STANDALONE", "-O2", "-Wall", "-Wextra",
        "-Werror", "-include", str(ROOT / "verification/native_shutdown_probe.h"),
        "-o", str(binary), str(target)], capture_output=True, text=True, timeout=60)
    (out / (binary.name + "-build.log")).write_text(result.stdout + result.stderr)
    if result.returncode:
        raise RuntimeError("Controlled agent build failed: " + result.stderr)
    return binary, target


def capture(binary, case, timing, command):
    trace = case / "observed.bin"
    fd = os.open(trace, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    env = dict(os.environ, DSEC_TEST_TRACE_FD=str(fd), DSEC_TEST_SIGNAL_TIMING=timing)
    result = None
    # Short UDS paths; case/evidence paths may exceed the host's socket limit.
    with tempfile.TemporaryDirectory(prefix="dsec-refine-", dir="/tmp") as tmp:
        endpoint = Path(tmp) / "agent.sock"
        with (case / "agent.log").open("wb") as log:
            process = subprocess.Popen([str(binary), "--unix", str(endpoint)],
                env=env, pass_fds=(fd,), start_new_session=True, stdout=log, stderr=log)
            os.close(fd)
            channel = NativeChannel(endpoint)
            sid = uuid.uuid4().hex
            try:
                deadline = time.monotonic() + 5
                while not endpoint.exists() and process.poll() is None and time.monotonic() < deadline:
                    time.sleep(.01)
                channel.capabilities()
                channel.call("open", session_id=sid)
                result = channel.call("run", session_id=sid, command=command, timeout_ms=3000)
                wanted = 3 if timing == "none" else 4
                deadline = time.monotonic() + 5
                while trace.stat().st_size < wanted * RECORD.size and time.monotonic() < deadline:
                    time.sleep(.01)
                raw = trace.read_bytes()
                if len(raw) != wanted * RECORD.size:
                    raise RuntimeError("Missing or unexpected controlled trace records")
            finally:
                try:
                    channel.call("close", session_id=sid)
                except (NativeSessionReset, CommandOutcomeUnknown, OSError):
                    pass
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=5)
                finally:
                    if process.poll() is None:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait(timeout=5)
                if process.returncode != 0:
                    raise RuntimeError(f"Agent cleanup failed: exit={process.returncode}")
    observed = []
    pids = set()
    for offset in range(0, len(raw), RECORD.size):
        pid, stop, signalled, phase, returned = RECORD.unpack_from(raw, offset)
        if any(value not in (0, 1) for value in (stop, signalled, returned)) or phase not in PHASES:
            raise ValueError("Invalid C observation")
        pids.add(pid)
        observed.append(dict(stop=bool(stop), signalled=bool(signalled),
                             phase=PHASES[phase], returned=bool(returned)))
    if len(pids) != 1:
        raise ValueError("A trace must describe exactly one session worker")
    (case / "observed.json").write_text(json.dumps(observed, indent=2) + "\n")
    (case / "command-result.json").write_text(json.dumps(result, indent=2) + "\n")
    return observed


def replay(java, jar, out, name, observed, unsafe, expected=None):
    case = out / name
    case.mkdir()
    for module in ("ShutdownSignal", "ShutdownReplay"):
        shutil.copyfile(ROOT / "verification" / f"{module}.tla", case / f"{module}.tla")
    records = []
    for state in observed:
        fields = [f'{key} |-> {json.dumps(value) if isinstance(value, str) else str(value).upper()}'
                  for key, value in state.items()]
        records.append("[" + ", ".join(fields) + "]")
    (case / "TraceInput.tla").write_text("---- MODULE TraceInput ----\nEXTENDS ShutdownReplay\n"
        + "Recorded == <<" + ",\n".join(records) + ">>\n====\n")
    (case / "TraceInput.cfg").write_text("SPECIFICATION ReplaySpec\nCONSTANTS\n"
        + f"    UnsafeAssignment = {str(unsafe).upper()}\n    Observed <- Recorded\n"
        + "CHECK_DEADLOCK FALSE\nINVARIANTS TraceConforms"
        + (" StopIsMonotonic" if expected != "TraceConforms" else "") + "\n")
    result = subprocess.run([java, "-Xmx512m", "-XX:+UseParallelGC", "-jar", str(jar),
        "-workers", "1", "-seed", "1", "-fp", "0", "-metadir", str(case / "states"),
        "TraceInput"], cwd=case, capture_output=True, text=True, timeout=60)
    log = result.stdout + result.stderr
    (case / "tlc.log").write_text(log)
    passed = (result.returncode == 12 and f"Invariant {expected} is violated" in log if expected
              else result.returncode == 0 and "Model checking completed. No error has been found." in log)
    item = dict(check=name, passed=passed, exit_code=result.returncode,
                expected_violation=expected, log=str(case / "tlc.log"))
    print(json.dumps(item, sort_keys=True), flush=True)
    if not passed:
        print(log[-12000:], flush=True)
    return item


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    dependency = parser.add_mutually_exclusive_group(required=True)
    dependency.add_argument("--jar", type=Path)
    dependency.add_argument("--fetch", action="store_true")
    parser.add_argument("--cache", type=Path, default=Path(tempfile.gettempdir()) / "dsec-tla")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--java", default="java")
    parser.add_argument("--cc", default="cc")
    args = parser.parse_args()
    java, cc = shutil.which(args.java), shutil.which(args.cc)
    if java is None or cc is None:
        parser.error("Java 11+ and a C compiler are required; correspondence checks cannot be skipped")
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=False)
    jar = checked_jar(args.jar) if args.jar else fetch_jar(args.cache)
    fixed, fixed_source = build(cc, out, False)
    unsafe, unsafe_source = build(cc, out, True)
    scenarios = (
        ("no-signal", "none", "printf done"),
        ("signal-before-return", "before_return", "printf done"),
        ("signal-between-return-and-apply", "between_return_and_apply", "printf done"),
        ("signal-after-apply", "after_apply", "printf done"),
        ("shell-exit", "none", "exit 7"),
    )
    checks = []
    for name, timing, command in scenarios:
        case = out / name
        case.mkdir()
        observed = capture(fixed, case, timing, command)
        checks.append(replay(java, jar, out, name + "-model", observed, False))
    case = out / "historical-assignment"
    case.mkdir()
    observed = capture(unsafe, case, "between_return_and_apply", "printf done")
    # The real mutant must match the old model and violate its safety property.
    checks.append(replay(java, jar, out, "historical-matches-unsafe", observed, True, "StopIsMonotonic"))
    # The SAME observations must be rejected by the repaired model, not just
    # accepted by a permissive Python checker or a separately reimplemented FSM.
    checks.append(replay(java, jar, out, "historical-rejected-by-safe", observed, False, "TraceConforms"))
    report = dict(checks=checks, tlc_sha256=TLC_SHA256,
        source_sha256=digest(ROOT / "guest_native.c"), probe_sha256=digest(ROOT / "verification/native_shutdown_probe.h"),
        fixed_build_source_sha256=digest(fixed_source), unsafe_build_source_sha256=digest(unsafe_source),
        fixed_binary_sha256=digest(fixed), unsafe_binary_sha256=digest(unsafe),
        model_sha256={name: digest(ROOT / "verification" / name)
                      for name in ("ShutdownSignal.tla", "ShutdownReplay.tla")},
        boundary="Controlled C-window trace conformance, not full runtime refinement or shutdown liveness")
    (out / "report.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    raise SystemExit(0 if all(item["passed"] for item in checks) else 1)


if __name__ == "__main__":
    main()
