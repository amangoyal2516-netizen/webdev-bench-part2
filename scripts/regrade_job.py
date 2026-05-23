#!/usr/bin/env python3
"""Re-grade a finished job's preserved agent outputs without re-running agents.

Reads each trial's `artifacts/output/` (the agent's HTML/CSS, preserved
per task.toml's `artifacts = [{ source = "/workspace/output", ... }]`)
and runs the current `grading/aggregator.py` against it. Overwrites the
trial's reward.json + grading.json with fresh numbers, then optionally
re-renders the HTML report.

Why this works fast:
  - Track A is fully deterministic — no caching needed.
  - Track B's per-question cache (`grading/judge/_cache/`, keyed on
    `(question_id, ref_image_hash, agent_image_hash, judge_model)`) makes
    unchanged questions a no-op. Only new or edited (re-versioned)
    questions fire fresh API calls.

Iteration loop the script enables:
  1. Edit a criterion in `grading/criteria/<name>.py`, or
     edit/add a question in `grading/judge/question_packs/<crit>.json`
  2. Run:  python scripts/regrade_job.py jobs/<job-name>/
  3. See updated scores + diff against the previous run

Usage:
    python scripts/regrade_job.py jobs/<job-name>/
    python scripts/regrade_job.py jobs/<job-name>/ --no-track-b
    python scripts/regrade_job.py jobs/<job-name>/ --concurrent 4
    python scripts/regrade_job.py jobs/<job-name>/ --backup
    python scripts/regrade_job.py jobs/<job-name>/ --no-render
    python scripts/regrade_job.py jobs/<job-name>/ --render-all-tasks
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
AGGREGATOR = REPO_ROOT / "grading" / "aggregator.py"
DEFAULT_WEIGHTS = REPO_ROOT / "tasks" / "_template" / "tests" / "_weights.toml"


# ── task-path resolution ────────────────────────────────────────────────────


def _resolve_task_dir(trial_dir: Path, tasks_dir: Path) -> Path | None:
    """Find the task this trial ran on.

    Strategy:
      1. Trial's config.json records the path Harbor used. Prefer this.
      2. If that's stale (e.g. tasks/ got renamed since the run), fall
         back to parsing the trial dir name (`task_<N>-<variant>__<id>`)
         and looking up `tasks_dir/<task_<N>-<variant>>/`.
    """
    config_path = trial_dir / "config.json"
    if config_path.is_file():
        try:
            cfg = json.loads(config_path.read_text())
            recorded = (cfg.get("task") or {}).get("path")
            if recorded:
                p = Path(recorded)
                if not p.is_absolute():
                    p = REPO_ROOT / p
                if p.is_dir():
                    return p
        except Exception:
            pass

    m = re.match(r"(task_\d+-(?:oneshot|iter))__\w+", trial_dir.name)
    if m:
        candidate = tasks_dir / m.group(1)
        if candidate.is_dir():
            return candidate
    return None


def _ensure_screenshots_symlink(task_dir: Path) -> bool:
    """Mirror the runtime fix from tasks/_template/tests/test.sh — make
    `tests/ground_truth/screenshots` resolve to the reference PNGs that
    were COPYed into the env image. Inside Docker, test.sh does this via
    `ln -s /workspace/reference /tests/ground_truth/screenshots` because
    Harbor stages the env Dockerfile's `COPY ground_truth/screenshots/.`
    output at `/workspace/reference/`. Locally, the same screenshots
    live at `<task>/environment/ground_truth/screenshots/`."""
    tests_gt = task_dir / "tests" / "ground_truth"
    env_screenshots = task_dir / "environment" / "ground_truth" / "screenshots"
    if not env_screenshots.is_dir():
        return False
    tests_gt.mkdir(parents=True, exist_ok=True)
    link = tests_gt / "screenshots"
    if link.is_symlink() or link.exists():
        return True  # already there (link or real dir from a prior package)
    link.symlink_to(env_screenshots.resolve())
    return True


# ── single-trial regrade ────────────────────────────────────────────────────


def _regrade_trial(
    trial_dir: Path,
    tasks_dir: Path,
    run_track_b: bool,
    backup: bool,
    judge_cache_dir: str | None,
    weights_path: str,
) -> dict:
    """Re-grade one trial. Returns a result dict for the summary."""
    name = trial_dir.name
    agent_output = trial_dir / "artifacts" / "output"
    if not agent_output.is_dir() or not any(agent_output.glob("*.html")):
        return {"trial": name, "status": "skip",
                "reason": "no agent HTML in artifacts/output (agent failed or never produced output)"}

    task_dir = _resolve_task_dir(trial_dir, tasks_dir)
    if task_dir is None:
        return {"trial": name, "status": "skip",
                "reason": "could not resolve task dir (config.json path stale and no fallback match)"}

    _ensure_screenshots_symlink(task_dir)
    gt_dir = task_dir / "tests" / "ground_truth"
    if not gt_dir.is_dir():
        return {"trial": name, "status": "skip", "reason": f"missing {gt_dir}"}

    verifier_dir = trial_dir / "verifier"
    verifier_dir.mkdir(parents=True, exist_ok=True)
    grading_path = verifier_dir / "grading.json"
    reward_path = verifier_dir / "reward.json"

    # Capture old values for delta reporting
    old_a = old_b = None
    if reward_path.is_file():
        try:
            old = json.loads(reward_path.read_text())
            old_a = old.get("score_objective")
            old_b = old.get("score_judge")
        except Exception:
            pass

    # Backup if requested
    if backup:
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        for src in (grading_path, reward_path):
            if src.is_file():
                shutil.copy2(src, src.with_name(src.stem + f".original-{ts}" + src.suffix))

    # Run the aggregator
    cmd = [
        sys.executable, str(AGGREGATOR),
        "--agent-output", str(agent_output),
        "--ground-truth", str(gt_dir),
        "--weights", weights_path,
        "--output", str(grading_path),
    ]
    if run_track_b:
        cmd.append("--run-track-b")
    if judge_cache_dir:
        cmd += ["--judge-cache-dir", judge_cache_dir]

    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, cwd=str(REPO_ROOT), env=os.environ,
        )
    except Exception as e:
        return {"trial": name, "status": "fail",
                "reason": f"aggregator subprocess error: {type(e).__name__}: {e}"}

    if proc.returncode != 0:
        # Surface the last few lines of stderr — the actual cause
        tail = "\n".join(proc.stderr.strip().splitlines()[-6:])
        return {"trial": name, "status": "fail",
                "reason": f"aggregator exit {proc.returncode}: {tail[:400]}"}

    # Project the rich grading.json into the flat reward.json that
    # Harbor's schema accepts — mirrors the post-process block in
    # tasks/_template/tests/test.sh so re-grades and fresh runs produce
    # byte-compatible reward.json files.
    if grading_path.is_file():
        g = json.loads(grading_path.read_text())
        rewards: dict[str, float] = {}
        for key in ("score_objective", "score_judge",
                    "raw_score_objective", "raw_score_judge", "gate"):
            v = g.get(key)
            if isinstance(v, (int, float)):
                rewards[key] = float(v)
        for crit, scores in (g.get("per_criterion") or {}).items():
            for track in ("objective", "judge"):
                v = (scores or {}).get(track)
                if isinstance(v, (int, float)):
                    rewards[f"{crit}__{track}"] = float(v)
        reward_path.write_text(json.dumps(rewards, indent=2))

    new = json.loads(reward_path.read_text()) if reward_path.is_file() else {}
    new_a, new_b = new.get("score_objective"), new.get("score_judge")
    return {
        "trial": name,
        "status": "ok",
        "old_a": old_a, "old_b": old_b,
        "new_a": new_a, "new_b": new_b,
    }


# ── CLI ─────────────────────────────────────────────────────────────────────


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__.split("\n")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("job_dir", help="path to jobs/<job-name>/")
    ap.add_argument("--tasks-dir", default="tasks",
                    help="dir to look up task ground-truth in (default: tasks/)")
    ap.add_argument("--weights", default=str(DEFAULT_WEIGHTS),
                    help="path to _weights.toml (default: tasks/_template/tests/_weights.toml)")
    ap.add_argument("--no-track-b", action="store_true",
                    help="skip Track B (Track A only — much faster, no API spend)")
    ap.add_argument("--judge-cache-dir", default=None,
                    help="override judge verdict cache dir (default: grading/judge/_cache/)")
    ap.add_argument("--backup", action="store_true",
                    help="save original verifier/{grading,reward}.json to .original-<ts>.json "
                         "before overwriting")
    ap.add_argument("--concurrent", type=int, default=1,
                    help="trials to regrade in parallel (default: 1; "
                         "bump cautiously to avoid hitting judge-API rate limits)")
    ap.add_argument("--no-render", action="store_true",
                    help="skip the post-regrade HTML report render")
    ap.add_argument("--render-all-tasks", action="store_true",
                    help="pass --all-tasks to the renderer (Diversity tab = full dataset)")
    args = ap.parse_args()

    job_dir = Path(args.job_dir).resolve()
    if not job_dir.is_dir():
        sys.exit(f"error: {job_dir} is not a directory")
    tasks_dir = Path(args.tasks_dir).resolve()
    if not tasks_dir.is_dir():
        sys.exit(f"error: {tasks_dir} is not a directory")

    run_track_b = not args.no_track_b
    if run_track_b and not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("error: ANTHROPIC_API_KEY not set (needed for Track B; pass --no-track-b to skip)")

    if not AGGREGATOR.is_file():
        sys.exit(f"error: aggregator missing at {AGGREGATOR}")

    trials = sorted(
        d for d in job_dir.iterdir()
        if d.is_dir() and "__" in d.name and (d / "config.json").is_file()
    )
    if not trials:
        sys.exit(f"error: no trial subdirs (matching `*__*`) under {job_dir}")

    rel_job = job_dir.relative_to(REPO_ROOT) if job_dir.is_relative_to(REPO_ROOT) else job_dir
    print(f"→ re-grading {len(trials)} trial(s) from {rel_job}")
    print(f"  tasks_dir:   {tasks_dir.relative_to(REPO_ROOT) if tasks_dir.is_relative_to(REPO_ROOT) else tasks_dir}")
    print(f"  Track B:     {'on' if run_track_b else 'OFF (Track A only)'}")
    print(f"  concurrent:  {args.concurrent}")
    print(f"  backup:      {'yes' if args.backup else 'no (overwrite in place)'}\n")

    t0 = time.time()

    def _one(trial: Path) -> dict:
        print(f"  ⋯ {trial.name}")
        r = _regrade_trial(
            trial, tasks_dir, run_track_b, args.backup,
            args.judge_cache_dir, args.weights,
        )
        # Tight per-trial line so the user sees progress
        if r["status"] == "ok":
            da = (r["new_a"] - r["old_a"]) if isinstance(r.get("new_a"), (int, float)) and isinstance(r.get("old_a"), (int, float)) else None
            db = (r["new_b"] - r["old_b"]) if isinstance(r.get("new_b"), (int, float)) and isinstance(r.get("old_b"), (int, float)) else None
            da_str = f"{da:+.4f}" if da is not None else " n/a "
            db_str = f"{db:+.4f}" if db is not None else " n/a "
            print(f"    ✓ {trial.name}  ΔA={da_str}  ΔB={db_str}")
        else:
            print(f"    ✗ {trial.name}  [{r['status']}] {r['reason'][:120]}")
        return r

    if args.concurrent <= 1:
        results = [_one(t) for t in trials]
    else:
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.concurrent) as pool:
            results = list(pool.map(_one, trials))

    elapsed = time.time() - t0

    ok = [r for r in results if r["status"] == "ok"]
    skip = [r for r in results if r["status"] == "skip"]
    fail = [r for r in results if r["status"] == "fail"]

    print(f"\n=== regrade complete in {elapsed/60:.1f} min ===")
    print(f"  ✓ ok:   {len(ok)}/{len(results)}")
    print(f"  ⋯ skip: {len(skip)}")
    print(f"  ✗ fail: {len(fail)}")

    if ok:
        # Summary stats: mean shift per track
        def _mean(xs):
            return sum(xs) / len(xs) if xs else None
        da = _mean([r["new_a"] - r["old_a"] for r in ok
                    if isinstance(r["new_a"], (int, float)) and isinstance(r["old_a"], (int, float))])
        db = _mean([r["new_b"] - r["old_b"] for r in ok
                    if isinstance(r["new_b"], (int, float)) and isinstance(r["old_b"], (int, float))])
        if da is not None:
            print(f"\n  mean ΔTrack A across {len(ok)} ok trials: {da:+.4f}")
        if db is not None:
            print(f"  mean ΔTrack B across {len(ok)} ok trials: {db:+.4f}")

    if skip or fail:
        print("\n  skipped / failed:")
        for r in skip + fail:
            print(f"    {r['status']}: {r['trial']} — {r['reason'][:160]}")

    if args.no_render:
        return 0 if not fail else 1

    print(f"\n→ re-rendering report")
    cmd = [
        sys.executable, "eval/reports/render_report.py", str(job_dir),
        "--tasks-dir", str(tasks_dir),
    ]
    if args.render_all_tasks:
        cmd.append("--all-tasks")
    subprocess.run(cmd, cwd=str(REPO_ROOT))

    return 0 if not fail else 1


if __name__ == "__main__":
    sys.exit(main())
