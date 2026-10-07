"""Compare complete native verl pre/post MBPP evaluations without dropping zeros."""
import argparse
from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path
import random
import statistics

from .dataset import SOURCE_SHA256


def read_scores(path, expected_ids, samples_per_task):
    scores = defaultdict(list)
    records = []
    for line in path.read_text().splitlines():
        item = json.loads(line)
        truth = json.loads(item["gts"]) if isinstance(item["gts"], str) else item["gts"]
        if truth["source_sha256"] != SOURCE_SHA256 or item["score"] not in (0, 1):
            raise ValueError("Unrecognized dataset identity or reward")
        scores[truth["task_id"]].append(item["score"])
        records.append(item)
    if set(scores) != set(expected_ids) or any(len(s) != samples_per_task for s in scores.values()):
        raise ValueError("Incomplete, duplicate or mismatched task/sample coverage: " + str(path))
    return scores, records


def summarize(scores):
    return dict(tasks=len(scores), samples=sum(map(len, scores.values())),
                passed=sum(map(sum, scores.values())),
                pass_at_1=statistics.mean(sum(s)/len(s) for s in scores.values()),
                pass_at_8=statistics.mean(bool(sum(s)) for s in scores.values()))


def compare(run):
    launch = json.loads((run / "launch.json").read_text())
    scope = launch["scope"]
    if not scope["full_epochs"] or scope["evaluation_split"] != "test" or scope["evaluation_tasks"] != 500:
        raise ValueError("Comparison requires a full-epoch run and all 500 original test tasks")
    steps, epochs = scope["steps"], scope["epochs"]
    before, pre_records = read_scores(run / "validation/0.jsonl", range(11, 511), 8)
    after, post_records = read_scores(run / f"validation/{steps}.jsonl", range(11, 511), 8)
    training = defaultdict(list)
    training_records = []
    for step in range(1, steps + 1):
        for line in (run / f"rollouts/{step}.jsonl").read_text().splitlines():
            item = json.loads(line)
            truth = json.loads(item["gts"])
            if truth["source_sha256"] != SOURCE_SHA256 or item["score"] not in (0, 1):
                raise ValueError("Invalid training reward")
            training[truth["task_id"]].append(item["score"])
            training_records.append(item)
    if set(training) != set(range(601, 975)) or any(len(s) != 8*epochs for s in training.values()):
        raise ValueError("Training did not cover every original train task in every epoch")
    stop_reasons = {}
    reward_sources = {}
    for name, records in [("before", pre_records), ("train", training_records), ("after", post_records)]:
        stops, sources = Counter(), Counter()
        for item in records:
            receipt = json.loads((run / "execution-evidence" / (item["dsec_rollout_id"] + ".json")).read_text())
            truth = json.loads(item["gts"])
            if (receipt.get("error") or receipt.get("cleanup_error") or receipt["score"] != item["score"] or
                    receipt["task_id"] != truth["task_id"]):
                raise ValueError("Failed or inconsistent execution evidence")
            if receipt["reward_source"] == "execution" and receipt.get("cleanup") != "stopped":
                raise ValueError("Created sandbox was not stopped")
            generation = json.loads((run / "generation-evidence" / (receipt["generation_id"] + ".json")).read_text())
            n = len(generation["response_ids"])
            if (generation.get("error") or len(generation["response_mask"]) != n or
                    (generation["response_logprobs"] is not None and len(generation["response_logprobs"]) != n)):
                raise ValueError("Incomplete generation token/mask evidence")
            stops[generation["finish_reason"]] += 1
            sources[receipt["reward_source"]] += 1
        stop_reasons[name], reward_sources[name] = dict(stops), dict(sources)
    # Pair by task; model samples are independent draws, not matched RNG streams.
    deltas = [sum(after[i])/8 - sum(before[i])/8 for i in range(11, 511)]
    rng = random.Random(42)
    bootstrap = sorted(statistics.mean(rng.choices(deltas, k=500)) for _ in range(10000))
    transitions = Counter((bool(sum(before[i])), bool(sum(after[i]))) for i in range(11, 511))
    report = dict(schema="dsec.mbpp.training.comparison.v1", scope=scope,
                  scoring="three original public assertions; challenge tests excluded",
                  before=summarize(before), after=summarize(after),
                  training=dict(tasks=len(training), samples=len(training_records),
                      passed=sum(map(sum, training.values())),
                      mean_sample_reward=statistics.mean(x["score"] for x in training_records)),
                  pass_at_1_delta=statistics.mean(deltas),
                  pass_at_1_delta_task_bootstrap_95ci=[bootstrap[249], bootstrap[9749]],
                  newly_solved=transitions[(False, True)], newly_unsolved=transitions[(True, False)],
                  stop_reasons=stop_reasons, reward_sources=reward_sources,
                  evidence_linkage_valid=True,
                  evaluation_rng="same parameters and engine seed; independent draws, no bitwise determinism claim",
                  evaluation_sha256={str(p.relative_to(run)):hashlib.sha256(p.read_bytes()).hexdigest()
                      for p in [run / "validation/0.jsonl", run / f"validation/{steps}.jsonl"]})
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    result = compare(args.run)
    with args.out.open("x") as stream:
        json.dump(result, stream, indent=2)
    print(json.dumps(result))
