"""Modal-parallel Track B (and full reward) grading.

Track B is the LLM-as-judge grader on a 1-5 anchored scale. Running it
locally (via `scripts/regrade_job.py`) is
gated by Anthropic API latency × the number of batched judge calls per
trial — observed at ~45 min for 20 trials at `--concurrent 4`.

This module ports the SAME runner to Modal so each trial's grading runs
inside its own Modal sandbox in parallel. With 20 concurrent containers
each making ~80 batched judge calls of their own, the wall-clock time
drops to roughly the longest single-trial run (~10-15 min for a heavy
trial) instead of accumulating across waves.

How to use:

    # Modal-parallel regrade of a finished job:
    modal run -m infra.modal.judge_app::regrade --job-dir jobs/<job-name>/

    # Track A only (skip Track B, no API spend):
    modal run -m infra.modal.judge_app::regrade --job-dir jobs/<job-name>/ --no-run-track-b

Architecture:
    1. `score_trial` is a Modal Function: receives a tarball of the
       agent's output + a slim tarball of ground_truth (screenshots +
       design.json), runs `grading.aggregator.compute_reward` inside the
       container, returns the rich result dict. Container mounts the
       `judge-cache` Volume so cache hits accumulate across trials.
    2. `regrade` is a local entry point that walks `job_dir`, tars the
       inputs for each trial, fans out via `score_trial.spawn(...)`,
       collects results, and writes the new `grading.json` /
       `reward.json` back to each trial's `verifier/` directory.
       Preserves originals as `.original-<ts>.json` (same convention as
       `scripts/regrade_job.py`).
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import sys
import tarfile
import time
import tomllib
from datetime import datetime
from pathlib import Path
from typing import Any

import modal

from .app import SECRETS, app
from .images import base_verifier
from .volumes import judge_cache


# ─── single-trial Modal Function ─────────────────────────────────────────


def cache_key(
    question_id: str,
    ref_screenshot_hash: str,
    agent_screenshot_hash: str,
    agent_dom_hash: str,
    judge_model: str,
) -> str:
    """Stable cache key for one (question, rollout, judge) triple. Mirrors
    `grading/judge/client.py::cache_key` so the local cache and the Modal
    `judge-cache` Volume key the same entries (SCALE_VERSION is also
    folded in by the canonical implementation)."""
    payload = "|".join(
        [question_id, ref_screenshot_hash, agent_screenshot_hash, agent_dom_hash, judge_model]
    )
    return hashlib.sha256(payload.encode()).hexdigest()


@app.function(
    image=base_verifier,
    secrets=[SECRETS["anthropic-key"]],
    volumes={"/cache/judge": judge_cache},
    timeout=60 * 30,
    retries=modal.Retries(max_retries=1, backoff_coefficient=1.0, initial_delay=10),
)
def score_trial(
    agent_tar_bytes: bytes,
    gt_tar_bytes: bytes,
    weights_toml: str,
    task_config: dict[str, Any],
    pages: list[str],
    run_track_b: bool = True,
    judge_model: str = "claude-opus-4-7",
) -> dict[str, Any]:
    """Score one trial — Track A always, Track B when requested.

    Inputs are passed as in-memory tarballs to avoid coupling to Modal
    Volume layout. The grading code (already baked into `base_verifier`
    at /repo/grading) runs verbatim against an extracted workspace.

    Returns the full reward dict from `grading.aggregator.compute_reward`.
    """
    # /repo is where base_verifier mounts the grading + recipe trees.
    if "/repo" not in sys.path:
        sys.path.insert(0, "/repo")

    import tempfile
    from grading.aggregator import compute_reward

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)

        agent_dir = tmp / "agent_output"
        gt_dir = tmp / "ground_truth"
        agent_dir.mkdir()
        gt_dir.mkdir()

        with tarfile.open(fileobj=io.BytesIO(agent_tar_bytes), mode="r:gz") as tf:
            tf.extractall(agent_dir)
        with tarfile.open(fileobj=io.BytesIO(gt_tar_bytes), mode="r:gz") as tf:
            tf.extractall(gt_dir)

        # Symlink screenshots/ where the judge runner expects them. (test.sh
        # does the same trick at /tests/ground_truth/screenshots → /workspace/reference.)
        # Inside this tarball-extracted layout the screenshots are already at
        # ground_truth/screenshots/, so nothing extra to wire — leaving this
        # comment so future readers know why the path "just works."

        weights = tomllib.loads(weights_toml).get("scoring", {}).get("weights", {})
        weights = {k: float(v) for k, v in weights.items()}

        result = compute_reward(
            agent_output_dir=agent_dir,
            ground_truth_dir=gt_dir,
            weights=weights,
            pages=pages,
            task_config=task_config,
            run_track_b=run_track_b,
            judge_cache_dir=Path("/cache/judge"),
        )
        return result


# ─── local-side orchestration ────────────────────────────────────────────


def _make_tarball(src: Path, *, ignore: set[str] | None = None) -> bytes:
    """tar.gz a directory tree → bytes. Files in `ignore` (basename match)
    are skipped — keeps the upload small by dropping bulky artifacts we
    don't need on the verifier side."""
    ignore = ignore or set()
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for p in sorted(src.rglob("*")):
            if p.name in ignore:
                continue
            if any(part in ignore for part in p.parts):
                continue
            arcname = p.relative_to(src)
            tf.add(p, arcname=str(arcname), recursive=False)
    return buf.getvalue()


def _resolve_task_dir(trial_dir: Path, tasks_root: Path) -> Path:
    """Map a trial dir back to its source task dir. Prefer the trial's
    `config.json`; fall back to parsing the trial name."""
    cfg = trial_dir / "config.json"
    if cfg.exists():
        try:
            d = json.loads(cfg.read_text())
            task_path = d.get("task_path") or d.get("task", {}).get("path")
            if task_path:
                tp = Path(task_path)
                if not tp.is_absolute():
                    tp = trial_dir.parent.parent / task_path
                if tp.is_dir():
                    return tp
        except Exception:
            pass
    # Fallback: parse "task_3-oneshot__abc123" → "task_3-oneshot"
    m = re.match(r"(task_\d+-(?:oneshot|iter))__", trial_dir.name)
    if m:
        candidate = tasks_root / m.group(1)
        if candidate.is_dir():
            return candidate
    raise FileNotFoundError(f"cannot resolve task dir for {trial_dir.name}")


def _project_to_reward(grading: dict[str, Any]) -> dict[str, Any]:
    """Mirror scripts/regrade_job.py's reward.json projection — flatten
    the rich grading.json into the Harbor-accepted key→float dict, and
    strip nulls (Harbor's pydantic schema rejects them)."""
    out = {
        "score_objective": grading["score_objective"],
        "score_judge": grading.get("score_judge"),
        "gate": grading["gate"],
        "raw_score_objective": grading["raw_score_objective"],
        "raw_score_judge": grading.get("raw_score_judge"),
    }
    # Strip nulls
    return {k: v for k, v in out.items() if v is not None}


def _backup_existing(path: Path, ts: str) -> None:
    if path.exists():
        backup = path.with_suffix(f".original-{ts}.json")
        # Keep the .json suffix at the very end
        backup = path.parent / f"{path.stem}.original-{ts}.json"
        path.rename(backup)


@app.local_entrypoint()
def regrade(
    job_dir: str,
    tasks_dir: str = "tasks",
    weights_path: str = "tasks/_template/tests/_weights.toml",
    run_track_b: bool = True,
    backup: bool = True,
    no_render: bool = False,
):
    """Modal-parallelised regrade of an existing job.

    Args:
        job_dir:     path to jobs/<job-name>/
        tasks_dir:   path to tasks/ (to resolve trial → task ground truth)
        weights_path: path to the _weights.toml the regrade should use
        run_track_b: also fire Track B (LLM judge). Costs ~$15-20 per
                     full canonical run when the cache is cold.
        backup:      preserve existing grading.json / reward.json as
                     .original-<ts>.json before overwriting
        no_render:   skip the post-regrade HTML report render
    """
    job = Path(job_dir).resolve()
    tasks_root = Path(tasks_dir).resolve()
    weights_toml = Path(weights_path).read_text()
    ts = datetime.utcnow().strftime("%Y%m%d-%H%M%S")

    # Discover trials
    trial_dirs = sorted(p for p in job.iterdir() if p.is_dir() and (p / "verifier").is_dir())
    if not trial_dirs:
        print(f"error: no trials found in {job}", file=sys.stderr)
        return

    print(f"job: {job}")
    print(f"trials: {len(trial_dirs)}")
    print(f"run_track_b: {run_track_b}  | backup: {backup}")
    print()
    print("=== preparing inputs (tarball + dispatch) ===")

    # Prepare per-trial inputs and spawn Modal calls
    in_flight: list[tuple[str, Path, modal.FunctionCall]] = []  # (name, trial_dir, call)
    t_start = time.time()
    for trial in trial_dirs:
        try:
            task_dir = _resolve_task_dir(trial, tasks_root)
        except FileNotFoundError as e:
            print(f"  SKIP {trial.name}: {e}")
            continue

        agent_dir = trial / "artifacts" / "output"
        if not agent_dir.is_dir():
            print(f"  SKIP {trial.name}: no artifacts/output/")
            continue

        # Slim ground_truth tarball — we only need screenshots/ and design.json
        # for Track A (SSIM) + Track B (judge). Other Track A criteria still
        # read more JSONs, so include the whole env/ground_truth/ tree.
        gt_dir = task_dir / "environment" / "ground_truth"
        if not gt_dir.is_dir():
            print(f"  SKIP {trial.name}: gt dir missing at {gt_dir}")
            continue

        # design.json + pages list come from the task's tests/ground_truth/
        tests_gt = task_dir / "tests" / "ground_truth"
        design_path = tests_gt / "design.json"
        if not design_path.exists():
            print(f"  SKIP {trial.name}: design.json missing")
            continue
        design = json.loads(design_path.read_text())
        pages = [p["name"] for p in design["pages"]]

        # Build the task_config the aggregator expects
        task_config = {
            "task_id": design.get("task_id") or task_dir.name.rsplit("-", 1)[0],
            "variant": task_dir.name.rsplit("-", 1)[-1],
            "allowed_frameworks": design.get("allowed_frameworks", ["html-css"]),
            "viewports": ["desktop", "tablet", "mobile"],
            "pages": pages,
            "judge_model": "claude-opus-4-7",
        }

        # Tar both inputs
        agent_tar = _make_tarball(agent_dir, ignore={"__pycache__"})
        # Also include the full tests/ground_truth so the aggregator finds
        # design.json, bboxes/, palette/, etc. (Track A reads several of these.)
        # We merge them under a single tarball by tarring a synthesised
        # temp tree — simpler: tar env/ground_truth (has screenshots/source/assets),
        # tar tests/ground_truth (has design.json + bboxes/palette/etc), then
        # extract both into ground_truth/ inside the function (last writer
        # wins for overlap, but the two trees don't overlap).
        # Cheapest: tar tests/ground_truth (smaller, ~MBs) into the gt tarball;
        # env/ground_truth/screenshots is referenced via the env tarball.
        # Reality check: env/ground_truth has only `screenshots/` and `source/assets/`
        # (per recipe/03-package/package.py). tests/ground_truth has the JSON
        # artifacts + design.json. Combine into a single ground_truth/ tree:

        # Build a fresh temp dir that merges both, then tar that.
        import shutil
        import tempfile
        with tempfile.TemporaryDirectory() as tmp_merge:
            merge = Path(tmp_merge) / "ground_truth"
            merge.mkdir()
            # Copy env/ground_truth/* (screenshots, source/assets)
            for child in gt_dir.iterdir():
                dest = merge / child.name
                if child.is_dir():
                    shutil.copytree(child, dest)
                else:
                    shutil.copy2(child, dest)
            # Overlay tests/ground_truth/* (design.json, bboxes/, palette/, …)
            for child in tests_gt.iterdir():
                dest = merge / child.name
                if dest.exists():
                    if dest.is_dir() and child.is_dir():
                        # merge dir contents
                        for item in child.iterdir():
                            shutil.copy2(item, dest / item.name) if item.is_file() else shutil.copytree(item, dest / item.name, dirs_exist_ok=True)
                    continue
                if child.is_dir():
                    shutil.copytree(child, dest)
                else:
                    shutil.copy2(child, dest)
            gt_tar = _make_tarball(merge, ignore={"__pycache__"})

        size_mb = (len(agent_tar) + len(gt_tar)) / 1e6
        print(f"  → spawn {trial.name}  (upload: {size_mb:.1f} MB)")
        call = score_trial.spawn(
            agent_tar_bytes=agent_tar,
            gt_tar_bytes=gt_tar,
            weights_toml=weights_toml,
            task_config=task_config,
            pages=pages,
            run_track_b=run_track_b,
            judge_model="claude-opus-4-7",
        )
        in_flight.append((trial.name, trial, call))

    if not in_flight:
        print("error: no spawnable trials", file=sys.stderr)
        return

    print()
    print(f"=== {len(in_flight)} trials in flight on Modal ===")
    print()

    ok, fail = 0, 0
    sum_delta_a, sum_delta_b, n_delta_b = 0.0, 0.0, 0
    for i, (name, trial, call) in enumerate(in_flight, 1):
        try:
            result = call.get()
        except Exception as e:
            fail += 1
            print(f"  ✗ {name}  ({type(e).__name__}: {e})")
            continue
        ok += 1

        # Compare to existing for delta reporting
        verifier_dir = trial / "verifier"
        verifier_dir.mkdir(parents=True, exist_ok=True)
        grading_path = verifier_dir / "grading.json"
        reward_path = verifier_dir / "reward.json"

        prev = None
        if grading_path.exists():
            try:
                prev = json.loads(grading_path.read_text())
            except Exception:
                prev = None

        delta_a = result["score_objective"] - (prev["score_objective"] if prev else 0.0)
        delta_b_str = " n/a "
        if result.get("score_judge") is not None and prev and prev.get("score_judge") is not None:
            delta_b = result["score_judge"] - prev["score_judge"]
            sum_delta_b += delta_b
            n_delta_b += 1
            delta_b_str = f"{delta_b:+.4f}"
        sum_delta_a += delta_a

        # Backup + write
        if backup:
            if grading_path.exists():
                grading_path.rename(verifier_dir / f"grading.original-{ts}.json")
            if reward_path.exists():
                reward_path.rename(verifier_dir / f"reward.original-{ts}.json")
        grading_path.write_text(json.dumps(result, indent=2) + "\n")
        reward_path.write_text(json.dumps(_project_to_reward(result), indent=2) + "\n")

        print(f"  ✓ {name:42s}  ΔA={delta_a:+.4f}  ΔB={delta_b_str}  ({i}/{len(in_flight)})")

    elapsed = (time.time() - t_start) / 60
    print()
    print(f"=== regrade complete in {elapsed:.1f} min ===")
    print(f"  ✓ ok:   {ok}/{len(in_flight)}")
    print(f"  ✗ fail: {fail}/{len(in_flight)}")
    if ok > 0:
        print()
        print(f"  mean ΔTrack A across {ok} ok trials: {sum_delta_a/ok:+.4f}")
        if n_delta_b > 0:
            print(f"  mean ΔTrack B across {n_delta_b} ok trials: {sum_delta_b/n_delta_b:+.4f}")

    if not no_render:
        print()
        print("=== rendering report ===")
        import subprocess
        try:
            subprocess.run(
                ["python3", "eval/reports/render_report.py", str(job)],
                check=True,
                cwd=str(Path(__file__).resolve().parents[2]),
            )
        except subprocess.CalledProcessError as e:
            print(f"  warn: report render failed: {e}", file=sys.stderr)


# ─── module-level smoke test (not a Modal entry point) ──────────────────


def _cli() -> int:
    """Local CLI used for `python -m infra.modal.judge_app --help` —
    informational only; the real entry point is `modal run -m
    infra.modal.judge_app::regrade ...`."""
    print(__doc__)
    print()
    print("This module is invoked via Modal, not directly. Try:")
    print("  modal run -m infra.modal.judge_app::regrade --job-dir jobs/<job-name>/")
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
