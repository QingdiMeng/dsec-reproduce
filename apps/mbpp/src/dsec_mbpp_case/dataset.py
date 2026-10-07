"""Prepare the pinned original MBPP splits without reference solutions."""

import argparse
import hashlib
import json
from pathlib import Path

SOURCE_REVISION = "e49bbfe381c9c0e564b937f1c4e163a2273c65cc"
SOURCE_SHA256 = "ccf64ceae9c5403bf50a044cb6d505bfd2a2963ee58338ba268fd65beab92a9f"
SPLITS = {"prompt": (1, 10), "test": (11, 510),
          "validation": (511, 600), "train": (601, 974)}
SYSTEM_PROMPT = (
    "Solve the Python programming task. Return your complete implementation in "
    "one python code block. Include any required imports. The code will run in "
    "an isolated Python environment and must pass the supplied tests."
)


def load_tasks(path):
    raw = Path(path).read_bytes()
    if hashlib.sha256(raw).hexdigest() != SOURCE_SHA256:
        raise ValueError("MBPP source differs from the pinned original dataset")
    tasks = [json.loads(line) for line in raw.splitlines()]
    if sorted(task["task_id"] for task in tasks) != list(range(1, 975)):
        raise ValueError("MBPP task IDs are incomplete or duplicated")
    return tasks


def row(task):
    task_id = task["task_id"]
    tests = task["test_list"]
    if len(tests) != 3 or not all(isinstance(x, str) and x for x in tests):
        raise ValueError("Original MBPP requires three tests per task")
    ground_truth = {"schema": "dsec.mbpp.tests.v1", "task_id": task_id,
                    "source_sha256": SOURCE_SHA256,
                    "test_setup_code": task["test_setup_code"], "test_list": tests}
    return {
        "data_source": "mbpp-dsec",
        "prompt": [{"role": "system", "content": SYSTEM_PROMPT},
                   {"role": "user", "content": task["text"] +
                    "\n\nYour code should pass these tests:\n" + "\n".join(tests)}],
        "ability": "code",
        "reward_model": {"style": "rule", "ground_truth": json.dumps(ground_truth)},
        "extra_info": {"task_id": task_id, "source_revision": SOURCE_REVISION},
    }


def stage(source, output, *, parquet=False):
    tasks = load_tasks(source)
    output = Path(output)
    if output.exists():
        raise FileExistsError("Use a fresh MBPP output directory")
    if parquet:
        import pyarrow as pa
        import pyarrow.parquet as pq
    output.mkdir(parents=True, mode=0o700)
    files, counts = {}, {}
    for split, (low, high) in SPLITS.items():
        rows = [row(task) for task in tasks if low <= task["task_id"] <= high]
        for item in rows:
            item["extra_info"]["split"] = split
        path = output / (split + ".jsonl")
        path.write_text("".join(json.dumps(item, ensure_ascii=False) + "\n" for item in rows))
        files[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
        if parquet:
            path = output / (split + ".parquet")
            pq.write_table(pa.Table.from_pylist(rows), path)
            files[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
        counts[split] = len(rows)
    manifest = {"schema": 1, "source_revision": SOURCE_REVISION,
                "source_sha256": SOURCE_SHA256, "counts": counts, "files": files,
                "scoring": "all three original test_list assertions pass; challenge tests excluded",
                "reference_solutions_included": False}
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--parquet", action="store_true")
    args = parser.parse_args()
    print(json.dumps(stage(args.input, args.out, parquet=args.parquet)))


if __name__ == "__main__":
    main()
