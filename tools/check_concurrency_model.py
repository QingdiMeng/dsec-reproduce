"""Exhaustively check bounded TLA+ models and expected unsafe counterexamples.

Developer-only tool: never imported by the runtime or installed in its wheel.
No random simulation, state constraints, or silent checker/dependency skips.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import urllib.request


TLC_VERSION = "1.7.4"
TLC_URL = f"https://github.com/tlaplus/tlaplus/releases/download/v{TLC_VERSION}/tla2tools.jar"
TLC_SHA256 = "936a262061c914694dfd669a543be24573c45d5aa0ff20a8b96b23d01e050e88"
CASES = (
    ("lifecycle", "NativeLifecycle", "none", None),
    ("signal", "ShutdownSignal", "FALSE", None),
    ("queued-cancel", "NativeLifecycle", "queued_cancel", "TerminalNoNewExecution"),
    ("stale-failure", "NativeLifecycle", "stale_failure", "StoppedIsFinal"),
    ("old-incarnation", "NativeLifecycle", "stale_failure", "CallbackIsFenced"),
    ("unknown-replay", "NativeLifecycle", "unknown_replay", "UnknownNeverReadmitted"),
    ("unfenced-start", "NativeLifecycle", "unfenced_start", "StartIsFenced"),
    ("signal-overwrite", "ShutdownSignal", "TRUE", "StopIsMonotonic"),
    ("queue-target", "QueueCancellation", "none", None),
    ("callback-target", "NativeCallbackFence", "none", None),
    ("container-target", "ContainerNativeGate", "none", None),
    ("queue-lost-cancel", "QueueCancellation", "lost_cancel", "PendingCancelIsAcknowledged"),
    ("queue-redispatch", "QueueCancellation", "redispatch", "QueuedCancellationPreventsStart"),
    ("callback-stop", "NativeCallbackFence", "unfenced", "StoppedIsFinal"),
    ("callback-replacement", "NativeCallbackFence", "unfenced", "OldCallbackCannotRetireReplacement"),
    ("container-missing-guard", "ContainerNativeGate", "missing_guard", "StopExcludesNativeActivity"),
)


def checked_jar(path):
    if hashlib.sha256(path.read_bytes()).hexdigest() != TLC_SHA256:
        raise ValueError("TLC jar checksum mismatch; refusing to execute it")
    return path.resolve()


def fetch_jar(cache):
    cache.mkdir(parents=True, exist_ok=True)
    target = cache / f"tla2tools-{TLC_VERSION}.jar"
    if target.exists():
        return checked_jar(target)
    with urllib.request.urlopen(TLC_URL, timeout=30) as response:
        data = response.read(16 * 1024**2 + 1)
    if len(data) > 16 * 1024**2 or hashlib.sha256(data).hexdigest() != TLC_SHA256:
        raise ValueError("Downloaded TLC jar size/checksum mismatch")
    # Download before publication; no partially written executable is reused.
    with tempfile.NamedTemporaryFile(dir=cache, delete=False) as writer:
        staged = Path(writer.name)
        writer.write(data)
    try:
        staged.replace(target)
    finally:
        staged.unlink(missing_ok=True)
    return checked_jar(target)


def run_case(java, jar, models, out, name, module, fault, expected, timeout):
    case = out / name
    case.mkdir()
    shutil.copyfile(models / f"{module}.tla", case / f"{module}.tla")
    config = (models / f"{module}.cfg").read_text()
    if module != "ShutdownSignal" and expected:
        config = config.replace('Fault = "none"', f'Fault = "{fault}"')
        # Check one property so the expected counterexample cannot be masked
        # by a different invariant failure or parsing/configuration error.
        config = config[:config.index("INVARIANT")] + f"INVARIANT {expected}\n"
    elif module == "ShutdownSignal":
        config = config.replace("UnsafeAssignment = FALSE", f"UnsafeAssignment = {fault}")
    (case / f"{module}.cfg").write_text(config)
    command = [java, "-Xmx1g", "-XX:+UseParallelGC", "-jar", str(jar),
               "-workers", "1", "-seed", "1", "-fp", "0",
               "-metadir", str(case / "states"), module]
    try:
        result = subprocess.run(command, cwd=case, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        (case / "tlc.log").write_bytes((exc.stdout or b"") + (exc.stderr or b""))
        raise RuntimeError(f"{name}: exhaustive search did not finish within {timeout}s") from exc
    log = result.stdout + result.stderr
    (case / "tlc.log").write_text(log)
    if expected:
        passed = result.returncode == 12 and f"Invariant {expected} is violated" in log
    else:
        passed = result.returncode == 0 and "Model checking completed. No error has been found." in log
    counts = re.search(r"([\d,]+) states generated, ([\d,]+) distinct states found", log)
    item = dict(case=name, passed=passed, expected_violation=expected,
                exit_code=result.returncode, log=str(case / "tlc.log"))
    if counts:
        item.update(generated=int(counts[1].replace(",", "")),
                    distinct=int(counts[2].replace(",", "")))
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
    parser.add_argument("--timeout", type=int, default=180)
    args = parser.parse_args()
    if args.timeout < 1:
        parser.error("--timeout must be positive")
    java = shutil.which(args.java)
    if java is None:
        parser.error("Java 11+ is required; model validation cannot be skipped")
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=False)  # Preserve prior counterexample evidence.
    jar = checked_jar(args.jar) if args.jar else fetch_jar(args.cache)
    models = Path(__file__).resolve().parents[1] / "verification"
    results = [run_case(java, jar, models, out, *case, args.timeout) for case in CASES]
    report = dict(tlc_version=TLC_VERSION, tlc_sha256=TLC_SHA256,
                  runner_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                  bounds=dict(requests=2, incarnations=2), checks=results,
                  model_sha256={p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                                for p in sorted(models.iterdir()) if p.suffix in (".tla", ".cfg")},
                  boundary="Finite-model safety checking; no runtime refinement or liveness proof")
    (out / "report.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    raise SystemExit(0 if all(item["passed"] for item in results) else 1)


if __name__ == "__main__":
    main()
