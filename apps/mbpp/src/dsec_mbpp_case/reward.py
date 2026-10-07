"""verl's async execution-reward hook using the existing scheduled DSec SDK.

Only the guest runs candidate Python. verl owns generation, TITO and GRPO.
Unknown actions and invalid verifier envelopes raise rather than earn zero.
"""

import base64
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import time

from framework_profile import FrameworkProfile
from scheduled_dsec import ScheduledDSecClient, ScheduledOutcomeUnknown
from dsec_mbpp_case.dataset import SOURCE_SHA256

MAX_CODE_BYTES = 32768
MARKER = "DSEC_MBPP_VERDICT "

# The parent is a guest-side verifier, the child is candidate code + assertions.
# Child output is captured separately and cannot impersonate the parent envelope.
# This is not a security boundary against malicious candidates sharing a VM UID.
DRIVER = r'''
import base64, json, math, os, resource, signal, subprocess, sys, tempfile, uuid
p = json.loads(base64.b64decode(sys.argv[1]))
def limits():
    resource.setrlimit(resource.RLIMIT_AS, (256 * 1024**2, 256 * 1024**2))
    resource.setrlimit(resource.RLIMIT_FSIZE, (1024**2, 1024**2))
    n = math.ceil(p['timeout']) + 1
    resource.setrlimit(resource.RLIMIT_CPU, (n, n))
with tempfile.TemporaryDirectory(prefix='dsec-mbpp-') as directory:
    with tempfile.TemporaryFile() as log, tempfile.TemporaryFile() as completed:
        # Require completion after every assertion: exit(0) alone is not a pass.
        nonce = uuid.uuid4().hex
        script = 'import os\nns = {}\n'
        for source in [p['code'], p['test_setup_code'], *p['test_list']]:
            script += 'exec(compile(' + repr(source) + ', "mbpp", "exec"), ns)\n'
        script += 'os.write(' + str(completed.fileno()) + ', ' + repr(nonce.encode()) + ')\n'
        child = subprocess.Popen([sys.executable, '-I', '-c', script], cwd=directory,
                                 stdout=log, stderr=log, start_new_session=True,
                                 preexec_fn=limits, pass_fds=(completed.fileno(),))
        timed_out = False
        try:
            child.wait(timeout=p['timeout'])
        except subprocess.TimeoutExpired:
            timed_out = True
        finally:
            try: os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError: pass
            child.wait()
        log.seek(0, 2)
        log.seek(max(0, log.tell() - 4000))
        output = log.read().decode('utf-8', 'replace')
        completed.seek(0)
        tests_completed = completed.read(128) == nonce.encode()
    verdict = {'schema': 'dsec.mbpp.verdict.v1', 'task_id': p['task_id'],
               'score': int(child.returncode == 0 and not timed_out and tests_completed),
               'candidate_exit_code': child.returncode, 'candidate_timed_out': timed_out,
               'tests_completed': tests_completed,
               'output': output, 'test_count': len(p['test_list'])}
print('DSEC_MBPP_VERDICT ' + json.dumps(verdict))
'''


def extract_code(solution):
    if not isinstance(solution, str):
        raise TypeError("solution_str must be text")
    final = solution.rsplit("</think>", 1)[-1]
    if "<think>" in final:
        return None
    blocks = re.findall(r"```([^\n]*)\n(.*?)```", final, re.S)
    if len(blocks) != 1 or final.count("```") != 2 or blocks[0][0].strip() not in ("python", "py", ""):
        return None
    code = blocks[0][1].strip()
    return code if code and len(code.encode()) <= MAX_CODE_BYTES else None


def verifier_command(code, ground_truth, timeout):
    payload = dict(ground_truth, code=code, timeout=timeout)
    encoded = base64.b64encode(json.dumps(payload).encode()).decode()
    return "python3 -I -c " + shlex.quote(DRIVER) + " " + shlex.quote(encoded)


def read_verdict(result, task_id):
    if (result.get("exit_code") != 0 or result.get("timed_out") is not False or
            result.get("truncated") is not False):
        raise RuntimeError("DSec MBPP verifier execution failed or is incomplete")
    lines = result.get("output", "").splitlines()
    if len(lines) != 1 or not lines[0].startswith(MARKER):
        raise RuntimeError("DSec MBPP verifier envelope is missing or ambiguous")
    d = json.loads(lines[0][len(MARKER):])
    if (d.get("schema") != "dsec.mbpp.verdict.v1" or
            type(d.get("task_id")) is not int or d["task_id"] != task_id or
            type(d.get("score")) is not int or d["score"] not in (0, 1) or
            d.get("test_count") != 3 or type(d.get("candidate_exit_code")) is not int or
            type(d.get("candidate_timed_out")) is not bool or
            type(d.get("tests_completed")) is not bool or
            d["score"] != int(d["candidate_exit_code"] == 0 and not d["candidate_timed_out"] and d["tests_completed"])):
        raise RuntimeError("DSec MBPP verifier returned inconsistent evidence")
    return d


async def compute_score(data_source, solution_str, ground_truth, extra_info=None, *,
                        worker_socket=None, environment_id=None, evidence_dir=None,
                        candidate_timeout=10, **kwargs):
    """Native verl custom_reward_function; no verl import or algorithm patch."""
    if data_source != "mbpp-dsec":
        raise ValueError("This reward function only accepts the MBPP application")
    truth = json.loads(ground_truth) if isinstance(ground_truth, str) else ground_truth
    if (not isinstance(truth, dict) or truth.get("schema") != "dsec.mbpp.tests.v1" or
            truth.get("source_sha256") != SOURCE_SHA256 or
            type(truth.get("task_id")) is not int or not 1 <= truth["task_id"] <= 974 or
            len(truth.get("test_list", [])) != 3 or
            not all(isinstance(x, str) and x for x in truth["test_list"]) or
            not isinstance(truth.get("test_setup_code"), str)):
        raise ValueError("Invalid pinned MBPP test specification")
    if (extra_info or {}).get("task_id", truth["task_id"]) != truth["task_id"]:
        raise ValueError("Reward task identity differs from dataset row")
    if not isinstance(candidate_timeout, (int, float)) or isinstance(candidate_timeout, bool) or not 0 < candidate_timeout <= 20:
        raise ValueError("candidate_timeout must be in (0, 20] seconds")
    worker_socket = worker_socket or os.environ.get("DSEC_ROLLOUT_WORKER_SOCKET")
    environment_id = environment_id or os.environ.get("DSEC_MBPP_ENVIRONMENT_ID")
    evidence_dir = evidence_dir or os.environ.get("DSEC_MBPP_EVIDENCE_DIR")
    if not all([worker_socket, environment_id, evidence_dir]):
        raise ValueError("Provide a worker socket, published Python environment ID and evidence directory")
    profile = FrameworkProfile.from_dict({"environment": "erofs_layers", "environment_id": environment_id})
    profile.validate_runtime()
    rid = ScheduledDSecClient.new_rollout_id()
    started = time.monotonic()
    receipt = {"schema": "dsec.mbpp.execution.v1", "task_id": truth["task_id"],
               "rollout_id": rid, "source_sha256": SOURCE_SHA256,
               "generation_id": (extra_info or {}).get("dsec_generation_id"),
               "solution_sha256": hashlib.sha256(solution_str.encode()).hexdigest(),
               "profile": profile.as_dict(), "raw_solution": solution_str}
    output = Path(evidence_dir)
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    sandbox, client, unknown = None, None, False
    code = extract_code(solution_str)
    try:
        if code is None:
            receipt.update(score=0, reward_source="model_format", reason="expected_one_complete_python_block")
        else:
            client = ScheduledDSecClient(worker_socket)
            await client.open()
            admission_started = time.monotonic()
            sandbox = await client.create(
                task_id="mbpp-" + str(truth["task_id"]), rollout_id=rid, profile=profile,
                resources={"cpu": 1, "memory_mb": 512, "disk_mb": 256,
                           "network_mbps": 0, "api_episode_slots": 1}, ttl_running_stop=300)
            if sandbox.state == "QUEUED":
                receipt["queued"] = True
                queue_started = time.monotonic()
                await sandbox.wait_ready(timeout=180)
                receipt["queue_wait_seconds"] = time.monotonic() - queue_started
            receipt["create_and_admission_seconds"] = time.monotonic() - admission_started
            command = verifier_command(code, truth, candidate_timeout)
            execution_started = time.monotonic()
            result = await sandbox.run_shell(command, step_id=0, action_id="mbpp-test-v1",
                                             timeout_ms=30000, output_limit=65536)
            receipt["execution_seconds"] = time.monotonic() - execution_started
            receipt["execution"] = result
            receipt["verdict"] = read_verdict(result, truth["task_id"])
            receipt.update(score=receipt["verdict"]["score"], reward_source="execution")
    except ScheduledOutcomeUnknown as exc:
        unknown = True
        receipt.update(error=str(exc), cleanup_deferred="attach_and_reconcile_by_rollout_id")
        raise
    except Exception as exc:
        receipt["error"] = str(exc)
        raise
    finally:
        try:
            if sandbox is not None and not unknown:
                cleanup_started = time.monotonic()
                await sandbox.stop()
                receipt["cleanup_seconds"] = time.monotonic() - cleanup_started
                receipt["cleanup"] = "stopped"
        except Exception as exc:
            receipt["cleanup_error"] = str(exc)
            raise
        finally:
            if client is not None:
                await client.close()
            receipt["elapsed_seconds"] = time.monotonic() - started
            fd = os.open(output / (rid + ".json"), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as stream:
                json.dump(receipt, stream, ensure_ascii=False, indent=2)
    return {"score": float(receipt["score"]), "acc": float(receipt["score"]),
            "dsec_rollout_id": rid, "dsec_reward_source": receipt["reward_source"]}
