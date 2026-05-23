#!/usr/bin/env python3
"""Pull a Modal-built task back to the local repo.

After `modal run infra/modal/recipe_app.py::generate_one_task --task-id task_3`
finishes, the artifacts live in the `recipe-artifacts-part2` Volume. This
script syncs them down:

  1. `modal volume get recipe-artifacts-part2 /<task_id>/ recipe/runs/<task_id>/`
     → pulls design.json + source/ + screenshots/ + ground_truth/ + _packaged/
  2. Copies the oneshot packaged variant from
     `recipe/runs/<task_id>/_packaged/<task_id>-oneshot/` into
     `tasks/<task_id>-oneshot/` — that's the canonical Harbor task dir
     that `eval/job.yaml` references.

Idempotent: re-running pulls fresh artifacts from the Volume and replaces
the local `tasks/<task_id>-*/` dirs (any stale local files are removed).

Usage:

    python scripts/pull_task.py task_3
    python scripts/pull_task.py task_3 task_4 task_5      # multiple in one go
    python scripts/pull_task.py task_3 --dry-run          # preview commands
    python scripts/pull_task.py task_3 --no-stage-tasks   # only pull to recipe/runs/, skip the tasks/ copy

Requires: `modal` CLI on PATH (`pip install modal && modal token new`).
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
RUNS_DIR = REPO_ROOT / "recipe" / "runs"
TASKS_DIR = REPO_ROOT / "tasks"
VOLUME = "recipe-artifacts-part2"
VARIANTS = ("oneshot",)


def _stage_to_tasks(task_id: str, *, dry_run: bool) -> int:
    """Copy `_packaged/<task_id>-{variant}/` → `tasks/<task_id>-{variant}/`."""
    packaged = RUNS_DIR / task_id / "_packaged"
    if not packaged.is_dir():
        print(f"warn: {packaged} does not exist; skipping tasks/ stage", file=sys.stderr)
        return 0
    staged = 0
    for variant in VARIANTS:
        src = packaged / f"{task_id}-{variant}"
        dst = TASKS_DIR / f"{task_id}-{variant}"
        if not src.is_dir():
            print(f"warn: {src} not found in packaged output; skipping {variant}", file=sys.stderr)
            continue
        print(f"  copy {src.relative_to(REPO_ROOT)} → {dst.relative_to(REPO_ROOT)}")
        if not dry_run:
            if dst.exists():
                shutil.rmtree(dst)
            shutil.copytree(src, dst)
        staged += 1
    return staged


def _pull_one(task_id: str, *, dry_run: bool, stage_tasks: bool) -> int:
    """Pull one task_id from the Volume to local. Returns 0 on success."""
    local_dir = RUNS_DIR / task_id
    RUNS_DIR.mkdir(parents=True, exist_ok=True)

    # Wipe any prior local copy. `modal volume get` will recreate the
    # `task_id/` subdir under RUNS_DIR (we pass the parent as target since
    # Modal mirrors the remote path inside the local destination).
    if local_dir.exists():
        print(f"  wiping local {local_dir.relative_to(REPO_ROOT)} before pull")
        if not dry_run:
            shutil.rmtree(local_dir)

    # `modal volume get <vol> <remote> <local-parent>` — Modal nests the
    # remote dir's basename under <local-parent>, so for remote /task_3/
    # we pass RUNS_DIR (recipe/runs/) and Modal creates recipe/runs/task_3/.
    cmd = ["modal", "volume", "get", VOLUME, f"/{task_id}/", str(RUNS_DIR)]
    print("→", " ".join(cmd))
    if not dry_run:
        try:
            subprocess.run(cmd, check=True)
        except subprocess.CalledProcessError as e:
            print(f"error: `modal volume get` failed for {task_id} ({e.returncode})",
                  file=sys.stderr)
            return e.returncode

    if stage_tasks:
        TASKS_DIR.mkdir(parents=True, exist_ok=True)
        _stage_to_tasks(task_id, dry_run=dry_run)

    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__.split("\n")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("task_ids", nargs="+", help="one or more task IDs, e.g. task_3 task_4")
    ap.add_argument("--dry-run", action="store_true",
                    help="print commands without executing")
    ap.add_argument("--no-stage-tasks", action="store_true",
                    help="only sync to recipe/runs/<task_id>/; don't copy _packaged/* into tasks/")
    args = ap.parse_args()

    if shutil.which("modal") is None:
        print(
            "error: `modal` CLI not found on PATH.\n"
            "  install: pip install modal\n"
            "  auth:    modal token new",
            file=sys.stderr,
        )
        return 2

    fails = 0
    for tid in args.task_ids:
        if not tid.startswith("task_") or not tid[5:].isdigit():
            print(f"error: '{tid}' is not a valid task_id (expected 'task_N')",
                  file=sys.stderr)
            fails += 1
            continue
        rc = _pull_one(tid, dry_run=args.dry_run, stage_tasks=not args.no_stage_tasks)
        if rc != 0:
            fails += 1

    print(f"\npulled {len(args.task_ids) - fails}/{len(args.task_ids)} task(s) "
          f"from Modal volume '{VOLUME}'")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
