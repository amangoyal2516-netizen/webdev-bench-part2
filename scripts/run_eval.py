#!/usr/bin/env python3
"""End-to-end eval: discover tasks → build JobConfig → harbor run → render report.

Single-command replacement for the manual `bash eval/run.sh` +
`python eval/reports/render_report.py` two-step. The JobConfig YAML is
generated in-memory from the actual contents of `--tasks-dir`, so the
task list can't drift out of sync with what's on disk.

Usage:

    python scripts/run_eval.py --tasks-dir tasks/
    python scripts/run_eval.py --tasks-dir batches/alpha/ --quick
    python scripts/run_eval.py --tasks-dir tasks/ --variants oneshot
    python scripts/run_eval.py --tasks-dir tasks/ --tasks task_3,task_8
    python scripts/run_eval.py --tasks-dir tasks/ --n-attempts 3 --concurrent 16

Outputs:
  - jobs/<job-name>/            Harbor's raw output (per-trial dirs, artifacts)
  - eval/reports/<job-name>.html  Self-contained visual report
  - eval/_runs/<job-name>.yaml  Generated JobConfig (kept for reproducibility)

Requires: `harbor` with the `[modal]` extra installed
(`uv tool install 'harbor[modal]'`), `ANTHROPIC_API_KEY` in env.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
JOBS_DIR = REPO_ROOT / "jobs"
REPORTS_DIR = REPO_ROOT / "eval" / "reports"
RUNS_CONFIG_DIR = REPO_ROOT / "eval" / "_runs"

# Defaults tuned for the campaign we run today. See plan.md §8 / eval/job.yaml.
DEFAULT_AGENT = "claude-code"
DEFAULT_MODEL = "claude-opus-4-7"
DEFAULT_CONCURRENT = 32
DEFAULT_N_ATTEMPTS = 1

# Representative trio for --quick (analytics / marketing / editorial spread).
QUICK_TASKS = ("task_3", "task_13", "task_29")
QUICK_VARIANT = "oneshot"


# ── pre-flight ──────────────────────────────────────────────────────────────


def _preflight(tasks_dir: Path) -> None:
    """Fail fast on missing prerequisites."""
    if shutil.which("harbor") is None:
        sys.exit(
            "error: `harbor` CLI not found on PATH.\n"
            "  install: uv tool install 'harbor[modal]'"
        )
    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("error: ANTHROPIC_API_KEY not set in environment")
    if not tasks_dir.is_dir():
        sys.exit(f"error: --tasks-dir {tasks_dir} does not exist")
    # Harbor's modal env needs the modal extra; quick way to detect it.
    r = subprocess.run(
        ["harbor", "run", "--help"], capture_output=True, text=True,
    )
    if r.returncode != 0 or "modal" not in r.stdout:
        sys.exit(
            "error: `harbor run` doesn't list 'modal' as an environment option.\n"
            "  install the extra: uv tool install --force 'harbor[modal]'"
        )


# ── task discovery ──────────────────────────────────────────────────────────


def _discover_tasks(
    tasks_dir: Path,
    variants: tuple[str, ...],
    explicit_tasks: list[str] | None,
    quick: bool,
) -> list[Path]:
    """Glob `<tasks_dir>/task_*-{variant}/` and filter."""
    all_tasks: list[Path] = []
    for child in sorted(tasks_dir.iterdir()):
        if not child.is_dir():
            continue
        m = re.fullmatch(r"(task_\d+)-(oneshot|iter)", child.name)
        if not m:
            continue
        all_tasks.append(child)

    if quick:
        return [
            p for p in all_tasks
            if any(p.name == f"{tid}-{QUICK_VARIANT}" for tid in QUICK_TASKS)
        ]

    filtered = [
        p for p in all_tasks
        if any(p.name.endswith(f"-{v}") for v in variants)
    ]

    if explicit_tasks:
        wanted = {t.strip() for t in explicit_tasks if t.strip()}
        filtered = [p for p in filtered if any(p.name.startswith(f"{t}-") for t in wanted)]

    return filtered


# ── job-name resolution (dodge Harbor's stale-job-dir trap) ─────────────────


def _resolve_job_name(explicit: str | None) -> str:
    """Default to a timestamped name. If --job-name is given, fail loudly
    on collision rather than silently overriding existing state (Harbor
    refuses to resume with a different config anyway)."""
    if explicit:
        if (JOBS_DIR / explicit).exists():
            sys.exit(
                f"error: jobs/{explicit}/ already exists. Harbor 0.7.1 refuses to\n"
                f"  resume with a different config. Either:\n"
                f"    - rm -rf jobs/{explicit} (lose history)\n"
                f"    - choose a new --job-name\n"
                f"    - drop --job-name to get an auto-timestamped one"
            )
        return explicit
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"webdev-bench-{ts}"


# ── JobConfig assembly ──────────────────────────────────────────────────────


def _build_job_config(
    job_name: str,
    n_attempts: int,
    n_concurrent: int,
    env_type: str,
    agent: str,
    model: str,
    run_track_b: bool,
    tasks: list[Path],
) -> dict:
    """Mirror harbor.models.job.config:JobConfig field names exactly.
    Pre-validated downstream via JobConfig.model_validate."""
    verifier_env: dict[str, str] = {}
    if run_track_b:
        verifier_env["RUN_TRACK_B"] = "1"
        verifier_env["ANTHROPIC_API_KEY"] = "${ANTHROPIC_API_KEY}"

    return {
        "job_name": job_name,
        "n_attempts": n_attempts,
        "n_concurrent_trials": n_concurrent,
        "environment": {"type": env_type},
        "agents": [{
            "name": agent,
            "model_name": model,
            "env": {"ANTHROPIC_API_KEY": "${ANTHROPIC_API_KEY}"},
        }],
        "verifier": {"env": verifier_env},
        "tasks": [{"path": str(p.relative_to(REPO_ROOT)) if p.is_relative_to(REPO_ROOT) else str(p)}
                  for p in tasks],
    }


def _find_harbor_site_packages() -> Path | None:
    """Locate harbor's site-packages when it's installed via `uv tool`
    (which keeps each tool in its own venv invisible to system Python)."""
    harbor_bin = shutil.which("harbor")
    if not harbor_bin:
        return None
    # `which harbor` typically → ~/.local/bin/harbor (symlink) → uv-tool venv.
    real = Path(harbor_bin).resolve()
    # Walk up: <tool>/bin/harbor → <tool>/bin → <tool>/lib/python3.X/site-packages
    tool_root = real.parent.parent
    candidates = sorted(tool_root.glob("lib/python*/site-packages"))
    return candidates[0] if candidates else None


def _validate_config(config: dict) -> None:
    """Catch typos before invoking Harbor (zero-cost smoke)."""
    try:
        from harbor.models.job.config import JobConfig  # noqa: F401
    except ImportError:
        sp = _find_harbor_site_packages()
        if sp is None:
            sys.exit(
                "error: cannot import `harbor.models.job.config` and could "
                "not auto-discover harbor's site-packages dir.\n"
                "  install: uv tool install --force 'harbor[modal]'"
            )
        sys.path.insert(0, str(sp))
        try:
            from harbor.models.job.config import JobConfig  # noqa: F811
        except ImportError as e:
            sys.exit(f"error: failed to import harbor.models.job.config even after path injection: {e}")

    from harbor.models.job.config import JobConfig
    try:
        JobConfig.model_validate(config)
    except Exception as e:
        sys.exit(f"error: generated JobConfig failed schema validation: {e}")


# ── harbor invocation ───────────────────────────────────────────────────────


def _persist_config(config: dict, job_name: str) -> Path:
    RUNS_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    p = RUNS_CONFIG_DIR / f"{job_name}.yaml"
    p.write_text(yaml.safe_dump(config, sort_keys=False))
    return p


def _run_harbor(config_path: Path) -> int:
    cmd = ["harbor", "run", "-c", str(config_path)]
    print("→", " ".join(cmd))
    print(f"  (config: {config_path.relative_to(REPO_ROOT)})\n")
    # Stream stdout/stderr to console so the user sees the per-trial
    # progress bar Harbor draws.
    return subprocess.run(cmd, cwd=str(REPO_ROOT)).returncode


# ── report rendering ────────────────────────────────────────────────────────


def _render_report(job_name: str) -> Path | None:
    """Import the renderer and produce the HTML report. Returns the
    output path on success, None if the renderer couldn't run."""
    job_dir = JOBS_DIR / job_name
    if not job_dir.is_dir():
        print(f"warn: {job_dir} missing; skipping report render", file=sys.stderr)
        return None
    # Import locally so a missing Pillow doesn't break the rest of the script.
    sys.path.insert(0, str(REPO_ROOT))
    try:
        from eval.reports.render_report import load_trials, render_job
    except Exception as e:
        print(f"warn: could not import renderer: {e}", file=sys.stderr)
        return None

    trials = load_trials(job_dir)
    if not trials:
        print(f"warn: no trials found in {job_dir}; skipping render", file=sys.stderr)
        return None

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    out = REPORTS_DIR / f"{job_name}.html"
    html = render_job(trials, job_name, "desktop")
    out.write_text(html)
    size_kb = out.stat().st_size / 1024
    print(f"✓ report rendered: {out.relative_to(REPO_ROOT)}  ({size_kb:.0f} KB, {len(trials)} trial(s))")
    return out


# ── summary ─────────────────────────────────────────────────────────────────


def _print_summary(job_name: str, elapsed_s: float) -> None:
    """Pull headline scores from each trial's reward.json."""
    job_dir = JOBS_DIR / job_name
    rows: list[tuple[str, float | None, float | None, float | None]] = []
    if job_dir.is_dir():
        import json
        for trial in sorted(job_dir.iterdir()):
            if not trial.is_dir() or "__" not in trial.name:
                continue
            rj = trial / "verifier" / "reward.json"
            if not rj.is_file():
                rows.append((trial.name, None, None, None))
                continue
            d = json.loads(rj.read_text())
            rows.append((
                trial.name,
                d.get("score_objective"),
                d.get("score_judge"),
                d.get("gate"),
            ))

    print("\n" + "=" * 78)
    print(f"  job:         {job_name}")
    print(f"  trials:      {len(rows)}")
    print(f"  wall time:   {elapsed_s/60:.1f} min")
    print("=" * 78)
    if rows:
        print(f"  {'trial':<40s}  {'Track A':>8s}  {'Track B':>8s}  {'gate':>4s}  flag")
        print(f"  {'-'*40}  {'-'*8}  {'-'*8}  {'-'*4}  ----")
        for name, a, b, g in rows:
            fmt = lambda v: f"{v:.4f}" if isinstance(v, (int, float)) else "  —   "
            delta_flag = ""
            if isinstance(a, (int, float)) and isinstance(b, (int, float)):
                if abs(a - b) > 0.1:
                    delta_flag = f"|Δ|={abs(a-b):.2f} ⚠"
            print(f"  {name:<40s}  {fmt(a):>8s}  {fmt(b):>8s}  {fmt(g):>4s}  {delta_flag}")
    print("=" * 78)


# ── CLI ─────────────────────────────────────────────────────────────────────


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__.split("\n")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--tasks-dir", default="tasks",
                    help="dir containing task_N-{oneshot,iter}/ subdirs (default: tasks/)")
    ap.add_argument("--variants", default="oneshot,iter",
                    help="comma-separated variant subset (default: oneshot,iter)")
    ap.add_argument("--tasks", default=None,
                    help="comma-separated task ID subset (e.g. 'task_3,task_8')")
    ap.add_argument("--quick", action="store_true",
                    help=f"smoke against 3 oneshot tasks: {','.join(QUICK_TASKS)}")
    ap.add_argument("--n-attempts", type=int, default=DEFAULT_N_ATTEMPTS,
                    help=f"attempts per task (default: {DEFAULT_N_ATTEMPTS})")
    ap.add_argument("--concurrent", type=int, default=DEFAULT_CONCURRENT,
                    help=f"max concurrent trials (default: {DEFAULT_CONCURRENT})")
    ap.add_argument("--agent", default=DEFAULT_AGENT,
                    help=f"Harbor agent name (default: {DEFAULT_AGENT})")
    ap.add_argument("--model", default=DEFAULT_MODEL,
                    help=f"agent model (default: {DEFAULT_MODEL})")
    ap.add_argument("--env", default="modal", choices=("modal", "docker"),
                    help="Harbor environment type (default: modal)")
    ap.add_argument("--no-track-b", action="store_true",
                    help="skip Track B (LLM-judge) — Track A only")
    ap.add_argument("--job-name", default=None,
                    help="explicit job name (default: webdev-bench-<timestamp>)")
    ap.add_argument("--dry-run", action="store_true",
                    help="build + validate config; print and exit without firing harbor")
    args = ap.parse_args()

    tasks_dir = Path(args.tasks_dir).resolve()
    _preflight(tasks_dir)

    variants = tuple(v.strip() for v in args.variants.split(",") if v.strip())
    explicit_tasks = (
        [t.strip() for t in args.tasks.split(",") if t.strip()]
        if args.tasks else None
    )
    tasks = _discover_tasks(tasks_dir, variants, explicit_tasks, args.quick)
    if not tasks:
        sys.exit(f"error: no tasks discovered in {tasks_dir} (variants={variants}, "
                 f"explicit={explicit_tasks}, quick={args.quick})")

    job_name = _resolve_job_name(args.job_name)

    config = _build_job_config(
        job_name=job_name,
        n_attempts=args.n_attempts,
        n_concurrent=args.concurrent,
        env_type=args.env,
        agent=args.agent,
        model=args.model,
        run_track_b=not args.no_track_b,
        tasks=tasks,
    )
    _validate_config(config)

    print(f"→ {len(tasks)} task(s) × {args.n_attempts} attempt(s) = "
          f"{len(tasks) * args.n_attempts} trial(s) on {args.env}")
    print(f"  job_name: {job_name}")
    print(f"  tasks:    {[t.name for t in tasks]}")
    print(f"  Track B:  {'on' if not args.no_track_b else 'off'}")

    if args.dry_run:
        print("\n[dry-run] generated config:")
        print(yaml.safe_dump(config, sort_keys=False))
        return 0

    config_path = _persist_config(config, job_name)
    t0 = time.time()
    rc = _run_harbor(config_path)
    elapsed = time.time() - t0

    # Always attempt the report — partial trials still have value.
    _render_report(job_name)
    _print_summary(job_name, elapsed)

    return rc


if __name__ == "__main__":
    sys.exit(main())
