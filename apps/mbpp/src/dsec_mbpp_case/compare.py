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


class ReceiptIndex:
    """Match native train dumps, which omit custom reward fields, without losing duplicates."""
    def __init__(self, directory):
        self.by_id, self.by_content, self.used = {}, defaultdict(list), set()
        for path in sorted(directory.glob("*.json")):
            receipt = json.loads(path.read_text())
            if receipt["rollout_id"] != path.stem:
                raise ValueError("Receipt identity differs from filename")
            self.by_id[path.stem] = receipt
            if receipt.get("score") in (0, 1):
                digest = hashlib.sha256(receipt["raw_solution"].encode()).hexdigest()
                if digest != receipt["solution_sha256"]:
                    raise ValueError("Receipt output hash differs from saved solution")
                self.by_content[(receipt["task_id"], digest, receipt["score"])].append(path.stem)

    def take(self, item, *, allow_content_match=False):
        truth = json.loads(item["gts"]) if isinstance(item["gts"], str) else item["gts"]
        key = (truth["task_id"], hashlib.sha256(item["output"].encode()).hexdigest(), item["score"])
        identity = item.get("dsec_rollout_id")
        if identity is None:
            if not allow_content_match:
                raise ValueError("Evaluation row lacks a receipt identity")
            candidates = [rid for rid in self.by_content[key] if rid not in self.used]
            if not candidates:
                raise ValueError("Native training output has no unused matching execution receipt")
            identity = candidates[0]
        if identity in self.used:
            raise ValueError("Execution receipt reused by multiple rows")
        receipt = self.by_id[identity]
        if (receipt["task_id"], receipt["solution_sha256"], receipt["score"]) != key:
            raise ValueError("Row output or score differs from execution receipt")
        self.used.add(identity)
        return receipt


def compare(run, post_run=None):
    launch = json.loads((run / "launch.json").read_text())
    scope = launch["scope"]
    if not scope["full_epochs"] or scope["evaluation_split"] != "test" or scope["evaluation_tasks"] != 500:
        raise ValueError("Comparison requires a full-epoch run and all 500 original test tasks")
    steps, epochs = scope["steps"], scope["epochs"]
    post_run = post_run or run
    if post_run != run:
        replacement = json.loads((post_run / "launch.json").read_text())
        checkpoint = replacement["scope"].get("evaluate_checkpoint")
        if (replacement["scope"].get("mode") != "evaluate_checkpoint" or not checkpoint or
                Path(checkpoint).resolve() != (run / f"checkpoints/global_step_{steps}").resolve()):
            raise ValueError("Replacement evaluation must restore this run's final checkpoint")
        old_settings = dict(x.split("=", 1) for x in launch["command"] if "=" in x)
        new_settings = dict(x.split("=", 1) for x in replacement["command"] if "=" in x)
        keys = ["actor_rollout_ref.model.path", "actor_rollout_ref.model.lora_rank",
                "actor_rollout_ref.model.lora_alpha", "actor_rollout_ref.model.target_modules",
                "data.val_files", "data.val_max_samples", "data.max_prompt_length", "data.max_response_length",
                "+data.apply_chat_template_kwargs.enable_thinking",
                "+actor_rollout_ref.rollout.engine_kwargs.sglang.random_seed",
                "actor_rollout_ref.rollout.max_model_len", "actor_rollout_ref.rollout.max_num_seqs",
                *["actor_rollout_ref.rollout.val_kwargs." + key for key in ("n", "do_sample", "temperature", "top_p", "top_k")]]
        if any(old_settings.get(key) != new_settings.get(key) for key in keys):
            raise ValueError("Replacement evaluation changed model, data or sampling limits")
        for name in ("reward.py", "verl_agent.py"):
            if (run / "provenance.json").exists() and (post_run / "provenance.json").exists():
                old = json.loads((run / "provenance.json").read_text())["case_source_sha256"][name]
                new = json.loads((post_run / "provenance.json").read_text())["case_source_sha256"][name]
                if old != new:
                    raise ValueError("Replacement evaluation changed scoring or agent implementation")
    before, pre_records = read_scores(run / "validation/0.jsonl", range(11, 511), 8)
    after, post_records = read_scores(post_run / f"validation/{steps}.jsonl", range(11, 511), 8)
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
    receipts = ReceiptIndex(run / "execution-evidence")
    post_receipts = receipts if post_run == run else ReceiptIndex(post_run / "execution-evidence")
    for name, records in [("before", pre_records), ("train", training_records), ("after", post_records)]:
        stops, sources = Counter(), Counter()
        for item in records:
            selected = post_receipts if name == "after" else receipts
            evidence_root = post_run if name == "after" else run
            receipt = selected.take(item, allow_content_match=name == "train")
            truth = json.loads(item["gts"])
            if (receipt.get("error") or receipt.get("cleanup_error") or receipt["score"] != item["score"] or
                    receipt["task_id"] != truth["task_id"]):
                raise ValueError("Failed or inconsistent execution evidence")
            if receipt["reward_source"] == "execution" and receipt.get("cleanup") != "stopped":
                raise ValueError("Created sandbox was not stopped")
            generation = json.loads((evidence_root / "generation-evidence" / (receipt["generation_id"] + ".json")).read_text())
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
                  training_receipt_linkage="task/output SHA-256/score multiset; duplicate outputs matched by count, not native uid",
                  replacement_evaluation=str(post_run) if post_run != run else None,
                  logprob_chunk_rows={"before":scope.get("logprob_chunk_size"),
                      "after":json.loads((post_run / "launch.json").read_text())["scope"].get("logprob_chunk_size")},
                  evaluation_rng="same parameters and engine seed; independent draws, no bitwise determinism claim",
                  evaluation_sha256={label:hashlib.sha256(path.read_bytes()).hexdigest()
                      for label, path in [("before", run / "validation/0.jsonl"),
                                          ("after", post_run / f"validation/{steps}.jsonl")]})
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--post-run", type=Path, help="complete replacement evaluation from the same final checkpoint")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    result = compare(args.run, args.post_run)
    with args.out.open("x") as stream:
        json.dump(result, stream, indent=2)
    print(json.dumps(result))
