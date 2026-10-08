"""Run the task's own test.sh and read its binary reward without OpenEnv.

Uses the existing /tests and /logs/verifier harness layout and reward marker.
Task tests, CTRF validation, and durable evidence export remain in the caller.
"""

import shlex

_VERIFY_TESTS_DIR = "/tests"
_VERIFIER_LOG_DIR = "/logs/verifier"
_REWARD_MARKER = "__TB2_REWARD__:"


def canonical_eval_cmd(workdir: str, timeout_s: float | None = None) -> str:
    run = f"bash {shlex.quote(_VERIFY_TESTS_DIR + '/test.sh')}"
    if timeout_s is not None:
        if timeout_s < 1:
            raise ValueError("Verifier timeout must be at least one second")
        run = ("if command -v timeout >/dev/null 2>&1; then "
               f"timeout {int(timeout_s)} {run}; else {run}; fi")
    log = shlex.quote(_VERIFIER_LOG_DIR + "/testsh.log")
    reward = shlex.quote(_VERIFIER_LOG_DIR + "/reward.txt")
    return (f"cd {shlex.quote(workdir)} && {run} > {log} 2>&1; "
            f"tail -c 20000 {log} 2>/dev/null; "
            f"echo {_REWARD_MARKER}$(cat {reward} 2>/dev/null)")


def parse_canonical_reward(output: str) -> float | None:
    for line in reversed(output.splitlines()):
        if _REWARD_MARKER in line:
            try:
                value = float(line.split(_REWARD_MARKER, 1)[1].strip())
            except ValueError:
                return None
            return value if value in (0.0, 1.0) else None
    return None
