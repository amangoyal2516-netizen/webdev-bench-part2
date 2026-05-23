#!/usr/bin/env python3
"""Generate N tasks end-to-end: Modal fan-out → pull → stage.

Single-command replacement for the multi-step recipe pipeline. Each
batch lives in its own `--output-dir`, so multiple batches can coexist
without colliding on local IDs. Modal-side ID collisions are handled
by a targeted pre-wipe of the volume slots we're about to write into.

Output layout under `<output-dir>` (the "batch namespace"):

    <output-dir>/
    ├── _workspaces/task_N/            full per-task workspace
    │   ├── design.json
    │   ├── source/                    raw HTML/CSS
    │   ├── screenshots/               reference PNGs
    │   ├── ground_truth/              precomputed grader inputs
    │   ├── _packaged/                 source for the staged dirs below
    │   └── _builder_*.json
    └── task_N-oneshot/                packaged Harbor task (eval-ready)

`scripts/run_eval.py --tasks-dir <output-dir>` reads the same dir
naturally — its `task_*-*/` glob skips `_workspaces/`.

Usage:

    python scripts/generate_tasks.py --count 30
    python scripts/generate_tasks.py --count 5 --output-dir batches/alpha/
    python scripts/generate_tasks.py --count 5 --output-dir batches/alpha/      # extends
    python scripts/generate_tasks.py --ids task_2,task_7 --output-dir batches/alpha/  # retry

Requires: `modal` CLI on PATH (`modal token new`), `ANTHROPIC_API_KEY` in env.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
VOLUME = "recipe-artifacts-part2"
ASSET_POOLS_VOLUME = "asset-pools"
VARIANTS = ("oneshot", "iter")

# `generate_all_tasks` prints these markers per task — we parse stdout
# to know which IDs to pull. Robust to extra prefix logging from Modal.
_OK_RE = re.compile(r"✓\s+(task_\d+)")
_FAIL_RE = re.compile(r"✗\s+(task_\d+)\s*:\s*(.+)")


# ── pre-flight ──────────────────────────────────────────────────────────────


def _preflight() -> None:
    """Fail fast on missing prerequisites. Cheap checks before any spend."""
    if shutil.which("modal") is None:
        sys.exit(
            "error: `modal` CLI not found on PATH.\n"
            "  install: uv tool install modal\n"
            "  auth:    modal token new"
        )
    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("error: ANTHROPIC_API_KEY not set in environment")
    # `modal volume create` is idempotent (already-exists is treated as
    # success per scripts/seed_modal_volumes.py:91-95). One round-trip
    # but it's fast (~1s) and saves the "Volume not found" failure mode.
    print("→ ensuring asset-pools volume exists…")
    r = subprocess.run(
        ["modal", "volume", "create", ASSET_POOLS_VOLUME],
        capture_output=True, text=True,
    )
    if r.returncode != 0 and "already exists" not in (r.stderr + r.stdout):
        print(r.stdout, file=sys.stderr)
        print(r.stderr, file=sys.stderr)
        sys.exit(f"error: failed to ensure {ASSET_POOLS_VOLUME} volume")


# ── id resolution ───────────────────────────────────────────────────────────


def _existing_ids_under(output_dir: Path) -> set[int]:
    """Parse N out of every `task_N-{oneshot,iter}` packaged dir in `output_dir`.
    `_workspaces/` is correctly ignored (leading underscore)."""
    if not output_dir.is_dir():
        return set()
    out: set[int] = set()
    for child in output_dir.iterdir():
        if not child.is_dir():
            continue
        m = re.fullmatch(r"task_(\d+)-(?:oneshot|iter)", child.name)
        if m:
            out.add(int(m.group(1)))
    return out


def _resolve_start_id(output_dir: Path) -> int:
    existing = _existing_ids_under(output_dir)
    return (max(existing) + 1) if existing else 1


# ── modal-side housekeeping ─────────────────────────────────────────────────


def _wipe_modal_slots(task_ids: list[str]) -> None:
    """Pre-emptively delete the volume slots we're about to write into.
    Treats 'not found' as success so the call is idempotent."""
    print(f"→ wiping {len(task_ids)} target slot(s) on {VOLUME} (collision-safety)…")
    for tid in task_ids:
        r = subprocess.run(
            ["modal", "volume", "rm", VOLUME, f"/{tid}", "--recursive"],
            capture_output=True, text=True,
        )
        # Idempotent: doesn't-exist is success here.
        ok = (
            r.returncode == 0
            or "not found" in r.stderr.lower()
            or "no such" in r.stderr.lower()
        )
        if not ok:
            print(r.stdout, file=sys.stderr)
            print(r.stderr, file=sys.stderr)
            sys.exit(f"error: failed to wipe /{tid} on {VOLUME}")


# ── modal fan-out ───────────────────────────────────────────────────────────


def _fanout(count: int, start_id: int, ids: str | None) -> tuple[list[str], list[tuple[str, str]]]:
    """Invoke `generate_all_tasks` on Modal and parse the success/failure
    list from stdout. Returns (ok_ids, [(failed_id, reason), ...])."""
    cmd = ["modal", "run", "-m", "infra.modal.recipe_app::generate_all_tasks"]
    if ids:
        cmd += ["--ids", ids]
    else:
        cmd += ["--count", str(count), "--start-id", str(start_id)]
    print("→", " ".join(cmd))
    print(f"  (this fans out to {count if not ids else len(ids.split(','))} parallel Modal sandboxes; "
          f"expect ~10-20 min wall)")

    # Stream output line-by-line and parse markers as we go so the user
    # sees progress in real time.
    ok_ids: list[str] = []
    failures: list[tuple[str, str]] = []
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1,
        cwd=str(REPO_ROOT),
    )
    assert proc.stdout is not None
    for line in proc.stdout:
        sys.stdout.write(line)
        sys.stdout.flush()
        m_ok = _OK_RE.search(line)
        if m_ok:
            ok_ids.append(m_ok.group(1))
            continue
        m_fail = _FAIL_RE.search(line)
        if m_fail:
            failures.append((m_fail.group(1), m_fail.group(2).strip()))
    rc = proc.wait()
    if rc != 0 and not (ok_ids or failures):
        sys.exit(f"error: `modal run` failed (exit {rc}) and produced no task markers")
    return ok_ids, failures


# ── pull + stage (logic borrowed from scripts/pull_task.py, parameterised) ──


def _pull_one_into(task_id: str, output_dir: Path) -> bool:
    """Pull `<task_id>` from the Modal volume into `<output_dir>/_workspaces/`
    and stage the packaged variants into `<output_dir>/<task_id>-{variant}/`.
    Returns True on success."""
    workspaces_dir = output_dir / "_workspaces"
    workspaces_dir.mkdir(parents=True, exist_ok=True)
    local_dir = workspaces_dir / task_id

    # Wipe any prior local copy so `modal volume get` writes cleanly.
    if local_dir.exists():
        shutil.rmtree(local_dir)

    cmd = ["modal", "volume", "get", VOLUME, f"/{task_id}/", str(workspaces_dir)]
    print(f"→ pull {task_id}: {' '.join(cmd)}")
    r = subprocess.run(cmd)
    if r.returncode != 0:
        print(f"  ✗ `modal volume get` failed for {task_id}", file=sys.stderr)
        return False

    # Stage packaged variants into <output_dir>/<task_id>-{variant}/
    packaged = local_dir / "_packaged"
    if not packaged.is_dir():
        print(f"  warn: {packaged.relative_to(REPO_ROOT)} missing; nothing to stage",
              file=sys.stderr)
        return False
    staged = 0
    for variant in VARIANTS:
        src = packaged / f"{task_id}-{variant}"
        dst = output_dir / f"{task_id}-{variant}"
        if not src.is_dir():
            print(f"  warn: {src.relative_to(REPO_ROOT)} not found; skipping {variant}",
                  file=sys.stderr)
            continue
        if dst.exists():
            shutil.rmtree(dst)
        shutil.copytree(src, dst)
        staged += 1
        print(f"  stage {variant} → {dst.relative_to(REPO_ROOT)}")
    return staged > 0


# ── CLI ─────────────────────────────────────────────────────────────────────


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__.split("\n")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--count", type=int, default=None,
                    help="number of new tasks to generate (auto-numbered from existing in --output-dir)")
    ap.add_argument("--start-id", type=int, default=None,
                    help="override the auto-derived start_id (escape hatch)")
    ap.add_argument("--ids", default=None,
                    help="comma-separated explicit IDs to regenerate (e.g. 'task_2,task_7'); skips --count")
    ap.add_argument("--output-dir", default="tasks",
                    help="batch namespace; tasks land here. Default: tasks/")
    ap.add_argument("--dry-run", action="store_true",
                    help="show what would be done; do not call Modal")
    args = ap.parse_args()

    if not args.count and not args.ids:
        sys.exit("error: pass either --count N or --ids task_X,task_Y,…")
    if args.count and args.ids:
        sys.exit("error: --count and --ids are mutually exclusive")

    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    # Decide which task IDs we're going to materialize this run.
    if args.ids:
        target_ids = [s.strip() for s in args.ids.split(",") if s.strip()]
        for tid in target_ids:
            if not re.fullmatch(r"task_\d+", tid):
                sys.exit(f"error: invalid id '{tid}' (expected 'task_N')")
    else:
        start_id = args.start_id if args.start_id is not None else _resolve_start_id(output_dir)
        target_ids = [f"task_{start_id + i}" for i in range(args.count)]
        print(f"→ resolved start_id={start_id} → generating {target_ids}")

    if args.dry_run:
        print(f"[dry-run] would generate {len(target_ids)} task(s): {target_ids}")
        print(f"[dry-run] output-dir: {output_dir}")
        return 0

    _preflight()
    _wipe_modal_slots(target_ids)

    # Modal fan-out.
    t0 = time.time()
    if args.ids:
        ok_ids, failures = _fanout(count=0, start_id=0, ids=args.ids)
    else:
        ok_ids, failures = _fanout(count=args.count, start_id=start_id, ids=None)
    gen_elapsed = time.time() - t0

    # Pull each successful task into the batch dir.
    print(f"\n→ pulling {len(ok_ids)} successful task(s) into {output_dir.relative_to(REPO_ROOT) if output_dir.is_relative_to(REPO_ROOT) else output_dir}")
    pulled = 0
    pull_failures: list[str] = []
    for tid in ok_ids:
        if _pull_one_into(tid, output_dir):
            pulled += 1
        else:
            pull_failures.append(tid)
    total_elapsed = time.time() - t0

    # Summary table.
    print("\n" + "=" * 72)
    print(f"  generation: {len(ok_ids)} ok / {len(failures)} failed  ({gen_elapsed/60:.1f} min)")
    print(f"  pull:       {pulled} ok / {len(pull_failures)} failed")
    print(f"  total wall: {total_elapsed/60:.1f} min")
    print(f"  output:     {output_dir}")
    print("=" * 72)

    if failures:
        print("\nfailed task generations:")
        for tid, reason in failures:
            print(f"  ✗ {tid}: {reason[:100]}")
        print(f"\n  retry with: python {sys.argv[0]} "
              f"--ids {','.join(t for t, _ in failures)} --output-dir {args.output_dir}")
    if pull_failures:
        print("\nfailed pulls (task generated but not pulled):")
        for tid in pull_failures:
            print(f"  ✗ {tid}")

    return 0 if (not failures and not pull_failures) else 1


if __name__ == "__main__":
    sys.exit(main())
