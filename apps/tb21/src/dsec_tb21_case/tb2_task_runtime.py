"""Read the pinned TB2 task's OCI working directory from its Dockerfile."""

from __future__ import annotations

from pathlib import Path
import re


def task_workdir(task_dir: Path) -> str:
    dockerfile = Path(task_dir) / "environment" / "Dockerfile"
    lines = dockerfile.read_text().splitlines()
    values = [line.strip().split(None, 1)[1].strip()
              for line in lines if re.match(r"^\s*WORKDIR\s+", line, re.IGNORECASE)]
    if not values:
        raise ValueError(f"No WORKDIR in {dockerfile}")
    workdir = values[-1]
    if not re.fullmatch(r"/[A-Za-z0-9._/-]+", workdir) or ".." in Path(workdir).parts:
        raise ValueError(f"Unsupported OCI WORKDIR in {dockerfile}: {workdir!r}")
    return workdir
