"""Independent E1/E2 lifecycle audit; repair only provable UNKNOWN outcomes."""

import argparse
import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import time

from libdsec_compat import DSecClient
from request_journal import atomic_json


SID = re.compile(r"[0-9a-f]{32}")
PREFIXES = ("dsec-e1-", "dsec-e2-")


def docker_names(root):
    result = subprocess.run(["docker", "container", "ls", "-a", "--format", "{{.Names}}"],
                            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            timeout=30)
    if result.returncode:
        raise RuntimeError("Docker inventory failed: " + result.stderr[-500:])
    selected = []
    for name in result.stdout.splitlines():
        if not any(name.startswith(prefix) and SID.fullmatch(name[len(prefix):])
                   for prefix in PREFIXES):
            continue
        inspected = subprocess.run(["docker", "inspect", name], text=True,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
        if inspected.returncode:
            raise RuntimeError("Docker inventory changed during inspection: " + name)
        value = json.loads(inspected.stdout)[0]
        if any(mount.get("Destination") == "/dsec-private" and
               Path(mount.get("Source", "")).resolve().parent == root
               for mount in value.get("Mounts", [])):
            selected.append(name)
    return sorted(selected)


def private_ids(root):
    return sorted(path.name for path in root.iterdir()
                  if path.is_dir() and SID.fullmatch(path.name))


def rollout_refs(state_dir):
    if state_dir is None:
        return None
    refs = {}
    for path in Path(state_dir).glob("*.json"):
        value = json.loads(path.read_text())
        sid = value.get("sandbox_id")
        if sid:
            refs.setdefault(sid, []).append({"rollout_id": value["rollout_id"],
                                             "state": value["state"]})
        pending = value.get("pending") or {}
        if pending.get("operation") == "create" and pending.get("request_id"):
            refs.setdefault(pending["request_id"], []).append({
                "rollout_id": value["rollout_id"], "state": value["state"]})
    return refs


async def audit_once(root, *, state_dir=None):
    root = Path(root).resolve()
    configured = os.environ.get("DSEC_CONTAINER_ROOT")
    if configured and Path(configured).resolve() != root:
        raise ValueError("DSEC_CONTAINER_ROOT differs from audited root")
    os.environ["DSEC_CONTAINER_ROOT"] = str(root)
    journal = root / "lifecycle-requests"
    journal.mkdir(mode=0o700, parents=True, exist_ok=True)
    client = DSecClient(os.environ.get("DSEC_SOCKET", "/nonexistent"))
    proofs = []
    for path in sorted(journal.glob("*.json")):
        if not SID.fullmatch(path.stem):
            raise ValueError("Invalid lifecycle journal path: " + str(path))
        proofs.append(await client.lookup_container_request(path.stem))
    names = docker_names(root)
    private = private_ids(root)
    refs = rollout_refs(state_dir)
    known = set()
    stop_done = set()
    for proof in proofs:
        if proof.get("operation") == "create":
            known.add(proof["request_id"])
        elif proof.get("operation") == "stop" and proof.get("sandbox_id"):
            known.add(proof["sandbox_id"])
            if proof["state"] == "DONE":
                stop_done.add(proof["sandbox_id"])
    running_ids = {name[len(prefix):] for name in names for prefix in PREFIXES
                   if name.startswith(prefix)}
    private_set = set(private)
    anomalies = []
    for proof in proofs:
        if proof["state"] in ("UNKNOWN", "PENDING"):
            anomalies.append({"kind": "unresolved_request", "request_id": proof["request_id"],
                              "operation": proof.get("operation"), "state": proof["state"]})
    for sid in sorted(running_ids - known):
        anomalies.append({"kind": "container_without_request_record", "sandbox_id": sid})
    for sid in sorted(private_set - known):
        anomalies.append({"kind": "private_without_request_record", "sandbox_id": sid})
    for sid in sorted(private_set - running_ids):
        anomalies.append({"kind": "private_without_container", "sandbox_id": sid})
    for sid in sorted(running_ids - private_set):
        anomalies.append({"kind": "container_without_private", "sandbox_id": sid})
    for sid in sorted(stop_done & (running_ids | private_set)):
        anomalies.append({"kind": "committed_stop_has_resources", "sandbox_id": sid})
    if refs is not None:
        for sid in sorted(running_ids - set(refs)):
            anomalies.append({"kind": "container_not_in_worker_store", "sandbox_id": sid})
    report = {"status": "attention" if anomalies else "healthy",
              "checked_at": datetime.now(timezone.utc).isoformat(),
              "root": str(root), "request_counts": {
                  state: sum(proof["state"] == state for proof in proofs)
                  for state in ("PENDING", "DONE", "UNKNOWN")},
              "containers": names, "private_ids": private,
              "rollout_refs": refs, "anomalies": anomalies}
    atomic_json(root / "lifecycle-supervisor.json", report)
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=os.environ.get("DSEC_CONTAINER_ROOT"))
    parser.add_argument("--state-dir")
    parser.add_argument("--interval", type=float, default=30)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    if not args.root:
        parser.error("--root or DSEC_CONTAINER_ROOT is required")
    if not args.once and args.interval < 1:
        parser.error("--interval must be at least one second")
    previous = None
    while True:
        try:
            report = asyncio.run(audit_once(args.root, state_dir=args.state_dir))
            signature = (report["status"], json.dumps(report["anomalies"], sort_keys=True))
            if signature != previous or args.once:
                print(json.dumps({"status": report["status"], "checked_at": report["checked_at"],
                                  "anomalies": report["anomalies"]}), flush=True)
            previous = signature
        except Exception as exc:
            error = {"status": "error", "checked_at": datetime.now(timezone.utc).isoformat(),
                     "error": str(exc)}
            atomic_json(Path(args.root) / "lifecycle-supervisor.json", error)
            print(json.dumps(error), flush=True)
            previous = None
            if args.once:
                raise
        if args.once:
            return
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
