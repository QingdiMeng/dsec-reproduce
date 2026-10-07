"""Verify execution rewards and compare concurrency with a fixed fixture cohort.

Fixtures verify system behavior, not MBPP model accuracy. No model code runs on
the host. Each invocation refuses to overwrite evidence from an earlier run.
"""
import argparse
import asyncio
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import statistics
import time

from .dataset import SOURCE_SHA256
from .reward import compute_score
from work_scheduler import ProcHostSampler


TRUTH = {"schema": "dsec.mbpp.tests.v1", "task_id": 601,
         "source_sha256": SOURCE_SHA256, "test_setup_code": "",
         "test_list": ["assert add(1,2)==3", "assert add(-1,1)==0", "assert add(0,0)==0"]}
FIXTURES = [("correct", "def add(a,b): return a+b", 1),
            ("wrong", "def add(a,b): return 0", 0),
            ("timeout", "while True: pass", 0),
            ("early_exit", "import os; os._exit(0)", 0),
            ("isolated_file", "from pathlib import Path\nassert not Path('/tmp/dsec-mbpp-sentinel').exists()\nPath('/tmp/dsec-mbpp-sentinel').write_text('private')\ndef add(a,b): return a+b", 1)]


def percentile(values, q):
    return sorted(values)[min(len(values)-1, int(q*(len(values)-1)))]


async def verify(args):
    if args.candidates:
        cohort = [json.loads(line) for line in args.candidates.read_text().splitlines() if line.strip()]
        if not cohort or len(cohort) > 4096:
            raise ValueError("Candidate cohort must contain 1..4096 records")
        for item in cohort:
            if item.get("expected_score") not in (0, 1) or not isinstance(item.get("solution_str"), str):
                raise ValueError("Candidate records require solution_str, ground_truth and expected_score")
            if not isinstance(item.get("ground_truth"), dict):
                raise ValueError("Candidate ground_truth must be the prepared MBPP test specification")
    else:
        cohort = [dict(solution_str="```python\n"+FIXTURES[-1][1]+"\n```", ground_truth=TRUTH,
                       expected_score=1) for _ in range(args.samples)]
    args.out.mkdir(parents=True, exist_ok=False)
    common = dict(worker_socket=args.worker_socket, environment_id=args.environment_id,
                  candidate_timeout=.25)
    results = []
    for name, code, expected in FIXTURES:
        d = await compute_score("mbpp-dsec", "```python\n"+code+"\n```", TRUTH,
                                evidence_dir=args.out / "fixtures", **common)
        if d["score"] != expected:
            raise AssertionError(f"Fixture {name}: expected {expected}, got {d['score']}")
        results.append(dict(fixture=name, **d))
    levels = []
    for concurrency in args.concurrency:
        samples = []
        monitor_stop = asyncio.Event()
        sampler = ProcHostSampler(args.out, args.host_interface, args.disk_device) if args.host_interface else None
        baseline = sampler.sample() if sampler else None
        async def monitor():
            while True:
                try:
                    await asyncio.wait_for(monitor_stop.wait(), .25)
                except TimeoutError:
                    pass
                samples.append(asdict(sampler.sample()))
                if monitor_stop.is_set():
                    return
        monitor_task = asyncio.create_task(monitor()) if sampler else None
        semaphore = asyncio.Semaphore(concurrency)
        durations = []
        async def one(item):
            async with semaphore:
                start = time.monotonic()
                # Model candidates retain the production ten-second child limit.
                limits = dict(common, candidate_timeout=10 if args.candidates else .25)
                d = await compute_score("mbpp-dsec", item["solution_str"], item["ground_truth"],
                                        evidence_dir=args.out / f"concurrency-{concurrency}", **limits)
                durations.append(time.monotonic()-start)
                d["expected_score"] = item["expected_score"]
                return d
        start = time.monotonic()
        batch = await asyncio.gather(*(one(item) for item in cohort), return_exceptions=True)
        elapsed = time.monotonic()-start
        if monitor_task:
            monitor_stop.set()
            await monitor_task
            (args.out / f"host-concurrency-{concurrency}.jsonl").write_text(
                "".join(json.dumps(s) + "\n" for s in samples))
        errors = [str(d) for d in batch if isinstance(d, BaseException)]
        passed = sum(isinstance(d, dict) and d["score"] == d["expected_score"] for d in batch)
        executed = sum(isinstance(d, dict) and d["dsec_reward_source"] == "execution" for d in batch)
        level = dict(concurrency=concurrency, samples=len(cohort), passed=passed,
                     executed=executed, errors=errors, wall_seconds=elapsed, throughput=len(cohort)/elapsed,
                     execution_throughput=executed/elapsed,
                     p50_seconds=statistics.median(durations) if durations else None,
                     p95_seconds=percentile(durations,.95) if durations else None)
        if samples:
            level["host"] = dict(scope="whole host, sampled every 0.25s; not per-sandbox attribution",
                interface=args.host_interface, disk_device=args.disk_device,
                sample_count=len(samples), memory_available_baseline_mb=baseline.memory_available_mb,
                memory_available_min_mb=min(s["memory_available_mb"] for s in samples),
                disk_available_min_mb=min(s["disk_available_mb"] for s in samples),
                **{field+"_peak": max(s[field] for s in samples) for field in
                   ("cpu_utilization", "cpu_iowait", "network_mbps", "disk_io_mbps", "disk_busy")})
        levels.append(level)
        print(json.dumps(level), flush=True)
        if errors or passed != len(cohort):
            break
    report = dict(schema="dsec.mbpp.acceptance.v1", cohort="supplied generated candidates" if args.candidates else "synthetic isolation fixture, same at every level",
                  candidate_sha256=hashlib.sha256(args.candidates.read_bytes()).hexdigest() if args.candidates else None,
                  model_accuracy=False, fixtures=results, concurrency=levels,
                  status="passed" if all(x["passed"] == len(cohort) for x in levels) else "failed")
    (args.out / "summary.json").write_text(json.dumps(report, indent=2))
    if report["status"] != "passed":
        raise RuntimeError("Execution concurrency acceptance failed; inspect saved receipts")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker-socket", required=True)
    parser.add_argument("--environment-id", required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--samples", type=int, default=32)
    parser.add_argument("--candidates", type=Path, help="fixed JSONL cohort; solution_str, prepared ground_truth, expected_score")
    parser.add_argument("--concurrency", type=int, nargs="+", default=[8,16,32])
    parser.add_argument("--host-interface", help="optional whole-host Linux resource sampling on this interface")
    parser.add_argument("--disk-device", help="optional /sys/class/block device for host disk I/O sampling")
    args = parser.parse_args()
    if not 1 <= args.samples <= 4096 or not all(1 <= x <= 128 for x in args.concurrency):
        parser.error("samples must be 1..4096 and concurrency 1..128")
    asyncio.run(verify(args))
