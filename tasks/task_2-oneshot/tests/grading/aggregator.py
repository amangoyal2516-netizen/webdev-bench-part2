"""Dual-track aggregator — runs Track A always, Track B optionally.

Each rollout produces **two scores side-by-side**, never fused.

    Track A (objective)
        raw_a   = weighted_mean(7 deterministic sub-scores)
        final_a = raw_a × framework_gate

    Track B (LLM-as-judge, optional)
        raw_b   = weighted_mean(7 LLM-judge sub-scores)         ← same weights
        final_b = raw_b × framework_gate                        ← same gate

The seven criteria, the weights, and the framework gate are all shared
between the two tracks — so disagreements between `score_objective` and
`score_judge` isolate *measurement noise* rather than *prioritisation
noise* (the whole point of dual-track grading).

Output schema:

    {
      "score_objective": <final_a>,
      "score_judge":     <final_b | null>,        ← null if --run-track-b not set
      "gate":            <1.0 | 0.3>,
      "raw_score_objective": <raw_a>,
      "raw_score_judge":     <raw_b | null>,
      "per_criterion": {<name>: {"objective": …, "judge": … | null}, …},
      "per_criterion_detail": {<name>: {"objective": {…}, "judge": {…} | null}, …},
      "framework_compliance": {"score": …, "violations": [...]},
      "metadata": {"task_id": …, "variant": …, "elapsed_s": …, "track_b_run": bool}
    }

Usage:

    # Track A only (fast, no API key needed)
    python grading/aggregator.py \\
        --agent-output recipe/runs/_test/source \\
        --ground-truth recipe/runs/_test/ground_truth \\
        --weights tasks/_template/tests/_weights.toml \\
        --pages home,about,contact

    # Both tracks (Track B costs API calls; cache writes to grading/judge/_cache/)
    python grading/aggregator.py \\
        --agent-output recipe/runs/_test/source \\
        --ground-truth recipe/runs/_test/ground_truth \\
        --weights tasks/_template/tests/_weights.toml \\
        --pages home,about,contact \\
        --run-track-b
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import tomllib
from pathlib import Path
from typing import Any

TRACK_A_CRITERIA: tuple[str, ...] = (
    "layout_structure",
    "component_presence",
    "color_palette",
    "typography",
    "image_content_fidelity",
    "visible_text_fidelity",
    "animation_fidelity",
)


def load_weights(weights_path: Path) -> dict[str, float]:
    """Read tests/_weights.toml's [scoring.weights] section."""
    data = tomllib.loads(weights_path.read_text())
    weights = data.get("scoring", {}).get("weights", {})
    return {k: float(v) for k, v in weights.items()}


def load_pages(task_config_path: Path | None, pages_csv: str | None, agent_dir: Path) -> list[str]:
    """Resolve the canonical page list from --task-config, --pages, or
    autodiscovery from agent HTML files."""
    if task_config_path is not None:
        return list(json.loads(task_config_path.read_text())["pages"])
    if pages_csv is not None:
        return [p.strip() for p in pages_csv.split(",") if p.strip()]
    return sorted(p.stem for p in agent_dir.glob("*.html"))


def load_task_config(task_config_path: Path | None, pages: list[str]) -> dict[str, Any]:
    """Use provided task_config.json or synthesise a minimal one."""
    if task_config_path is not None and task_config_path.exists():
        return json.loads(task_config_path.read_text())
    return {
        "task_id": "_local",
        "variant": "dev",
        "allowed_frameworks": ["html-css"],
        "viewports": ["desktop", "tablet", "mobile"],
        "pages": pages,
        "judge_model": "claude-opus-4-7",
    }


def run_criterion(name: str, agent_dir: Path, gt_dir: Path, pages: list[str]) -> dict[str, Any]:
    """Import grading.criteria.<name> and call its score() function."""
    try:
        mod = __import__(f"grading.criteria.{name}", fromlist=[name])
    except ImportError as e:
        return {"score": 0.0, "error": f"import: {e}"}
    if not hasattr(mod, "score"):
        return {"score": 0.0, "error": "criterion module has no score() function"}
    try:
        return mod.score(agent_dir, gt_dir, pages)
    except Exception as e:
        return {"score": 0.0, "error": f"{type(e).__name__}: {e}"}


def _weighted_mean(per_criterion: dict[str, float], weights: dict[str, float]) -> float:
    total_w = sum(weights.get(c, 0.0) for c in TRACK_A_CRITERIA)
    if total_w <= 0:
        return 0.0
    return sum(
        weights.get(c, 0.0) * per_criterion.get(c, 0.0)
        for c in TRACK_A_CRITERIA
    ) / total_w


def compute_reward(
    agent_output_dir: Path,
    ground_truth_dir: Path,
    weights: dict[str, float],
    pages: list[str],
    task_config: dict[str, Any],
    *,
    run_track_b: bool = False,
    judge_cache_dir: Path | None = None,
) -> dict[str, Any]:
    """Run Track A always, Track B if requested; assemble the reward dict."""
    t0 = time.time()

    # ── Track A ───────────────────────────────────────────────────────
    detail_a: dict[str, dict[str, Any]] = {}
    for name in TRACK_A_CRITERIA:
        detail_a[name] = run_criterion(name, agent_output_dir, ground_truth_dir, pages)
    per_crit_a = {c: detail_a[c].get("score", 0.0) for c in TRACK_A_CRITERIA}
    raw_a = _weighted_mean(per_crit_a, weights)

    # ── Framework gate (shared between tracks) ────────────────────────
    from grading.gates import framework_compliance
    gate_result = framework_compliance.score(agent_output_dir, task_config)
    gate = gate_result["score"]

    final_a = raw_a * gate

    # ── Track B (optional) ────────────────────────────────────────────
    # Track B failures (e.g. Anthropic API `529 Overloaded` after retries)
    # MUST NOT kill the trial — Track A scores have value on their own.
    # On hard failure: set score_judge / raw_score_judge / per-criterion
    # judge values to None, which the test.sh post-process strips before
    # writing reward.json. Harbor sees a valid reward.json with Track A
    # only and marks the trial completed (not errored).
    detail_b: dict[str, dict[str, Any]] | None = None
    raw_b: float | None = None
    final_b: float | None = None
    if run_track_b:
        try:
            from grading.judge import runner as judge_runner
            detail_b = judge_runner.score_judge(
                agent_output_dir,
                ground_truth_dir,
                pages,
                model=task_config.get("judge_model", "claude-opus-4-7"),
                cache_dir=judge_cache_dir,
            )
            # Per-criterion scores may be None if a criterion's handler
            # itself caught an unrecoverable error. Treat None as "skip"
            # in the weighted mean rather than 0.0 (a 0.0 would mean
            # "the judge graded this and said it's terrible," which is
            # a false signal).
            usable = {
                c: detail_b[c]["score"]
                for c in TRACK_A_CRITERIA
                if isinstance(detail_b.get(c, {}).get("score"), (int, float))
            }
            if usable:
                per_crit_b = usable
                raw_b = _weighted_mean(per_crit_b, {c: weights[c] for c in usable})
                final_b = raw_b * gate
            # else: leave raw_b / final_b as None — all criteria failed.
        except Exception as e:
            import sys as _sys
            print(
                f"track-b runner crashed entirely: {type(e).__name__}: {e}",
                file=_sys.stderr,
            )
            detail_b = None  # signal complete failure to the per-criterion view below

    elapsed = round(time.time() - t0, 2)

    return {
        "score_objective": final_a,
        "score_judge": final_b,
        "gate": gate,
        "raw_score_objective": raw_a,
        "raw_score_judge": raw_b,
        "per_criterion": {
            c: {
                "objective": detail_a[c].get("score", 0.0),
                "judge": (
                    detail_b[c].get("score")
                    if detail_b and c in detail_b and isinstance(detail_b[c].get("score"), (int, float))
                    else None
                ),
            }
            for c in TRACK_A_CRITERIA
        },
        "per_criterion_detail": {
            c: {
                "objective": detail_a[c],
                "judge": (detail_b[c] if detail_b else None),
            }
            for c in TRACK_A_CRITERIA
        },
        "framework_compliance": gate_result,
        "metadata": {
            "task_id": task_config.get("task_id"),
            "variant": task_config.get("variant"),
            "elapsed_s": elapsed,
            "n_pages": len(pages),
            "n_criteria": len(TRACK_A_CRITERIA),
            "track_b_run": run_track_b,
            "judge_model": task_config.get("judge_model") if run_track_b else None,
        },
    }


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__.split("\n")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--agent-output", required=True, help="path to agent's HTML/CSS output")
    ap.add_argument("--ground-truth", required=True, help="path to ground_truth/ dir from capture")
    ap.add_argument("--weights", required=True, help="tests/_weights.toml")
    ap.add_argument("--task-config", default=None, help="task_config.json (optional; auto-synthesised if absent)")
    ap.add_argument("--pages", default=None, help="comma-separated page list (alt to --task-config)")
    ap.add_argument("--output", default=None, help="reward.json path (default stdout)")
    ap.add_argument(
        "--run-track-b",
        action="store_true",
        help="also compute Track B (LLM-as-judge) score. Requires ANTHROPIC_API_KEY.",
    )
    ap.add_argument(
        "--judge-cache-dir",
        default=None,
        help="override the judge verdict cache dir (default grading/judge/_cache/)",
    )
    args = ap.parse_args()

    # Make project root importable
    repo_root = Path(__file__).resolve().parent.parent
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

    if args.run_track_b and not os.environ.get("ANTHROPIC_API_KEY"):
        print("error: --run-track-b requires ANTHROPIC_API_KEY in environment", file=sys.stderr)
        return 2

    agent_dir = Path(args.agent_output)
    gt_dir = Path(args.ground_truth)
    weights_path = Path(args.weights)
    task_config_path = Path(args.task_config) if args.task_config else None

    pages = load_pages(task_config_path, args.pages, agent_dir)
    if not pages:
        print(
            "error: no pages resolved (provide --task-config, --pages, or *.html in agent-output)",
            file=sys.stderr,
        )
        return 2

    weights = load_weights(weights_path)
    task_config = load_task_config(task_config_path, pages)

    reward = compute_reward(
        agent_dir,
        gt_dir,
        weights,
        pages,
        task_config,
        run_track_b=args.run_track_b,
        judge_cache_dir=Path(args.judge_cache_dir) if args.judge_cache_dir else None,
    )

    output_json = json.dumps(reward, indent=2)
    if args.output:
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(output_json + "\n")
        # Brief summary line
        summary = (
            f"score_objective={reward['score_objective']:.4f} "
            f"(raw={reward['raw_score_objective']:.4f}, gate={reward['gate']:.2f})"
        )
        if reward["score_judge"] is not None:
            summary += (
                f"  |  score_judge={reward['score_judge']:.4f} "
                f"(raw={reward['raw_score_judge']:.4f})"
            )
        print(f"{summary}  →  {args.output}")
    else:
        print(output_json)

    return 0


if __name__ == "__main__":
    sys.exit(main())
