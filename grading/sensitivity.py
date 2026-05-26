"""Sensitivity test harness for `animation_fidelity` (part 2).

Two construct-validity checks, both against a single task fixture:

  1. Oracle calibration — score the canonical source (`solution/source/`)
     against the rebaselined ground truth. Should land at ~1.0 on Track A
     (locally captured strips compared against locally captured strips
     means SSIM ceiling) and Track B (judge sees identical strips).

  2. Per-corruption tests — apply a targeted perturbation to the canonical
     source that the design predicts will break the animation in one
     specific way, then verify both tracks drop accordingly.

Animation-specific corruptions (see CORRUPTIONS below):
  - `strip_animation`   — remove `animation:` rule  → grader can't find an animated element → 0
  - `static_at_final`   — set `animation: none` on the class → element rendered at final state → all panels identical → 0
  - `reverse_keyframes` — swap `from`/`to` in @keyframes → motion direction reversed
  - `slow_10x`          — multiply duration by 10 → strip captures only the beginning of the animation
  - `fast_10x`          — divide duration by 10 → animation settles by panel 1
  - `wrong_widget`      — move animation class to a different element → IoU vs widget bbox fails → 0

Fixture layout this harness expects (part 2's `tasks/<task>-oneshot/` shape):

    <fixture>/
      solution/source/{*.html, styles.css, assets/}
      environment/ground_truth/screenshots/desktop/<page>/{full,motion-strip}.png
      tests/ground_truth/widget/<page>.json
      tests/ground_truth/design.json

These get merged into a single unified GT dir at test time, then
rebaselined locally so SSIM ceiling is not bounded by cross-machine
Chromium drift.

Usage:
    python part2/grading/sensitivity.py
    python part2/grading/sensitivity.py --fixture tasks/task_3-oneshot
    python part2/grading/sensitivity.py --track-b --judge-pages home
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

# part2/grading/sensitivity.py → repo root is two levels up
PART2_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = PART2_ROOT.parent
sys.path.insert(0, str(PART2_ROOT))

from grading.criteria import animation_fidelity  # noqa: E402


# ---------------------------------------------------------------------------
# Fixture wiring — merge part 2's three-tree GT layout into one dir
# ---------------------------------------------------------------------------


def unify_ground_truth(fixture: Path, tmp: Path, *, screenshots_src: Path | None = None) -> Path:
    """Build a single GT dir from a part 2 task fixture.

    `screenshots_src` defaults to the original fixture's screenshots/
    directory; pass a different dir to substitute locally-rebaselined
    screenshots.
    """
    gt = tmp / "gt"
    gt.mkdir()

    # tests/ground_truth/* — widget, design.json, bboxes, etc.
    tests_gt = fixture / "tests" / "ground_truth"
    if tests_gt.is_dir():
        for child in tests_gt.iterdir():
            (gt / child.name).symlink_to(child.resolve())

    # screenshots/ — defaults to the fixture's, can be overridden
    shots = screenshots_src or (fixture / "environment" / "ground_truth" / "screenshots")
    if shots.exists():
        (gt / "screenshots").symlink_to(shots.resolve())

    return gt


def copy_source(source: Path, dest: Path) -> Path:
    """Mirror source/ under dest/agent. Symlink assets/ (read-only) and
    copy HTML/CSS (small, mutable)."""
    out = dest / "agent"
    out.mkdir(parents=True)
    for child in source.iterdir():
        if child.name == "assets":
            (out / "assets").symlink_to(child.resolve())
        else:
            shutil.copy2(child, out / child.name)
    return out


def rebaseline_locally(source: Path, fixture: Path, tmp: Path) -> Path:
    """Re-capture screenshots + motion strips locally using part 2's
    capture.py against the canonical source. Returns a directory holding
    fresh `screenshots/<vp>/<page>/{full,motion-strip}.png` (subset
    relevant to animation_fidelity).

    We import capture.py and call its `process_page` so the same code
    path that built the canonical reference rebaselines the local copy.
    """
    sys.path.insert(0, str(PART2_ROOT / "recipe" / "02-capture"))
    import capture  # type: ignore[import-not-found]
    from playwright.sync_api import sync_playwright

    run_dir = tmp / "rebaselined"
    run_source = run_dir / "source"
    run_dir.mkdir()
    run_source.mkdir()
    for child in source.iterdir():
        if child.name == "assets":
            (run_source / "assets").symlink_to(child.resolve())
        else:
            shutil.copy2(child, run_source / child.name)

    pages = sorted(p.stem for p in run_source.glob("*.html"))

    # capture.py expects design.json and widget metadata at the run-dir
    # level, mirroring `recipe/runs/task_N/`. We symlink them in from
    # tests/ground_truth/.
    tests_gt = fixture / "tests" / "ground_truth"
    if (tests_gt / "design.json").exists():
        (run_dir / "design.json").symlink_to((tests_gt / "design.json").resolve())
    if (tests_gt / "widget").is_dir():
        (run_dir / "ground_truth").mkdir(exist_ok=True)
        (run_dir / "ground_truth" / "widget").symlink_to((tests_gt / "widget").resolve())

    animations = capture.load_animations(run_dir)

    print(f"[rebaseline] capturing {len(pages)} pages × motion strips locally ...")
    t = time.time()
    with sync_playwright() as p:
        browser = p.chromium.launch()
        for page_name in pages:
            capture.process_page(
                browser, run_source, run_dir, page_name,
                animations.get(page_name),
                do_screenshots=True, do_motion_strip=True, do_precompute=False,
            )
        browser.close()
    print(f"[rebaseline] done in {time.time() - t:.1f}s")
    return run_dir / "screenshots"


# ---------------------------------------------------------------------------
# Corruptions
# ---------------------------------------------------------------------------


_CLASS_RE = re.compile(r"\.wdvb-anim-([a-z-]+)-([a-z-]+)")  # matches .wdvb-anim-<type>-<page>


def _rewrite_html(agent: Path, transform: Callable[[str], str]) -> None:
    """Apply a string transform to every HTML file under `agent`."""
    for html_path in agent.glob("*.html"):
        s = html_path.read_text()
        html_path.write_text(transform(s))


def corrupt_strip_animation(agent: Path) -> None:
    """Remove the `animation:` declaration on every `.wdvb-anim-*` class
    rule. The class still exists in the DOM, but computed `animationName`
    is `none` → animation_fidelity can't find an animated widget."""
    pat = re.compile(r"(\.wdvb-anim-[a-z-]+-[a-z-]+\s*\{[^}]*)animation\s*:[^;}]+;?", re.IGNORECASE)
    _rewrite_html(agent, lambda s: pat.sub(r"\1", s))


def corrupt_static_at_final(agent: Path) -> None:
    """Override `animation: none` on every `.wdvb-anim-*` class rule.
    Element renders at its default (final) state for the lifetime of the
    page → all 5 motion-strip panels are byte-identical → grader's
    'all panels identical' detector zeros the score."""
    pat = re.compile(r"(\.wdvb-anim-[a-z-]+-[a-z-]+\s*\{[^}]*animation\s*:)[^;}]+", re.IGNORECASE)
    _rewrite_html(agent, lambda s: pat.sub(r"\1 none", s))


def corrupt_reverse_keyframes(agent: Path) -> None:
    """Swap `from` and `to` blocks inside every @keyframes wdvb-anim-…
    rule. A slide-up that rose from below now sinks; a scale-up that
    grew now shrinks. Mid-strip frames diverge visibly from the reference."""

    kf_pat = re.compile(
        r"(@keyframes\s+wdvb-anim-[a-z-]+-[a-z-]+\s*\{\s*)"
        r"from\s*(\{[^}]*\})\s*"
        r"to\s*(\{[^}]*\})",
        re.IGNORECASE,
    )

    def sub(m: re.Match[str]) -> str:
        prefix, from_block, to_block = m.group(1), m.group(2), m.group(3)
        return f"{prefix}from {to_block} to {from_block}"

    _rewrite_html(agent, lambda s: kf_pat.sub(sub, s))


def corrupt_slow_10x(agent: Path) -> None:
    """Multiply every animation duration by 10. At reference-strip
    sample offsets (which cover 0..duration_ref ms), the agent's
    animation has barely progressed."""
    pat = re.compile(r"animation\s*:([^;}]+?)(\d+)ms", re.IGNORECASE)

    def sub(m: re.Match[str]) -> str:
        body, ms = m.group(1), int(m.group(2))
        return f"animation:{body}{ms * 10}ms"

    _rewrite_html(agent, lambda s: pat.sub(sub, s))


def corrupt_fast_10x(agent: Path) -> None:
    """Divide every animation duration by 10. Animation settles before
    the first strip sample fires → all 5 panels match the final state,
    matching the reference's panel 5 only."""
    pat = re.compile(r"animation\s*:([^;}]+?)(\d+)ms", re.IGNORECASE)

    def sub(m: re.Match[str]) -> str:
        body, ms = m.group(1), int(m.group(2))
        return f"animation:{body}{max(1, ms // 10)}ms"

    _rewrite_html(agent, lambda s: pat.sub(sub, s))


def corrupt_wrong_widget(agent: Path) -> None:
    """Move every `.wdvb-anim-*` class application from its intended
    element onto the page's <header> / topbar. Bbox IoU with the
    reference widget bbox collapses → grader returns 0."""
    from lxml import html as lxml_html

    for html_path in agent.glob("*.html"):
        tree = lxml_html.fromstring(html_path.read_bytes())
        anim_class = None
        # Find the first element carrying a wdvb-anim-* class
        for el in tree.iter():
            cls = el.get("class") or ""
            for token in cls.split():
                if token.startswith("wdvb-anim-"):
                    anim_class = token
                    break
            if anim_class:
                # Strip it from this element
                new = " ".join(t for t in cls.split() if t != anim_class)
                if new:
                    el.set("class", new)
                else:
                    del el.attrib["class"]
                break
        if not anim_class:
            continue
        # Re-attach to the first <header>, else <body>'s first child
        target = next(iter(tree.iter("header")), None)
        if target is None:
            body = next(iter(tree.iter("body")), None)
            target = next(iter(body), None) if body is not None else None
        if target is None:
            continue
        existing = target.get("class") or ""
        target.set("class", (existing + " " + anim_class).strip())
        html_path.write_bytes(b"<!doctype html>\n" + lxml_html.tostring(tree))


CORRUPTIONS: dict[str, tuple[Callable[[Path], None], str]] = {
    "strip_animation":   (corrupt_strip_animation,   "remove `animation:` rule entirely"),
    "static_at_final":   (corrupt_static_at_final,   "force `animation: none` so element sits at final state"),
    "reverse_keyframes": (corrupt_reverse_keyframes, "swap from/to in @keyframes — reverses direction"),
    "slow_10x":          (corrupt_slow_10x,          "10× longer duration — strip captures only the start"),
    "fast_10x":          (corrupt_fast_10x,          "10× shorter duration — animation settles before panel 1"),
    "wrong_widget":      (corrupt_wrong_widget,      "move animation class to the <header>; bbox IoU vs reference fails"),
}


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def run_track_a(agent_dir: Path, gt_dir: Path, pages: list[str]) -> dict[str, Any]:
    r = animation_fidelity.score(agent_dir, gt_dir, pages)
    return {
        "score": round(r["score"], 4),
        "per_page": {
            p: {
                "score": round(d["score"], 4),
                "detail": d.get("detail", ""),
                "found": d.get("found"),
                "iou": d.get("iou"),
            }
            for p, d in r.get("per_page", {}).items()
        },
    }


def run_track_b(
    agent_dir: Path, gt_dir: Path, pages: list[str], model: str
) -> dict[str, Any]:
    """Call part 2's judge runner directly on just animation_fidelity."""
    from grading.judge.client import JudgeClient
    from grading.judge.runner import _score_per_page_motion_strip

    client = JudgeClient(model=model)
    result = _score_per_page_motion_strip(
        client, "animation_fidelity", agent_dir, gt_dir, pages
    )
    return {
        "score": round(result["score"], 4) if result.get("score") is not None else None,
        "per_page": {
            p: {"score": round(d["score"], 4)} for p, d in result.get("per_page", {}).items()
            if isinstance(d, dict) and "score" in d
        },
    }


def run(
    fixture: Path,
    rebaseline: bool = True,
    track_b: bool = False,
    judge_pages: list[str] | None = None,
    judge_model: str = "claude-opus-4-7",
) -> dict[str, Any]:
    pages = sorted(p.stem for p in (fixture / "solution" / "source").glob("*.html"))
    judge_pages_eff = judge_pages or ([pages[0]] if pages else [])

    results: dict[str, Any] = {
        "fixture": str(fixture),
        "pages": pages,
        "rebaseline": rebaseline,
        "track_b": track_b,
        "judge_pages": judge_pages_eff if track_b else [],
        "judge_model": judge_model if track_b else None,
        "runs": {},
    }

    source = fixture / "solution" / "source"

    with tempfile.TemporaryDirectory() as tmpstr:
        tmp = Path(tmpstr)

        screenshots_src = None
        if rebaseline:
            screenshots_src = rebaseline_locally(source, fixture, tmp)
        gt = unify_ground_truth(fixture, tmp, screenshots_src=screenshots_src)

        # 1) Oracle
        print(f"[oracle] scoring canonical source ...")
        t = time.time()
        oracle_dir = copy_source(source, tmp / "oracle")
        oracle_a = run_track_a(oracle_dir, gt, pages)
        oracle_b: dict[str, Any] | None = None
        if track_b:
            tb = time.time()
            print(f"  [track-b] {len(judge_pages_eff)} page(s): {judge_pages_eff} ...")
            oracle_b = run_track_b(oracle_dir, gt, judge_pages_eff, judge_model)
            print(f"  [track-b] done in {time.time() - tb:.1f}s: track_b={oracle_b['score']}")
        print(f"  done in {time.time() - t:.1f}s: track_a={oracle_a['score']}")
        results["runs"]["oracle"] = {"track_a": oracle_a, "track_b": oracle_b}

        # 2) Corruptions
        for name, (fn, blurb) in CORRUPTIONS.items():
            print(f"[{name}] {blurb} ...")
            t = time.time()
            work = tmp / name
            work.mkdir()
            agent = copy_source(source, work)
            fn(agent)
            scores_a = run_track_a(agent, gt, pages)
            scores_b: dict[str, Any] | None = None
            if track_b:
                tb = time.time()
                scores_b = run_track_b(agent, gt, judge_pages_eff, judge_model)
                print(f"  [track-b] done in {time.time() - tb:.1f}s: track_b={scores_b['score']}")
            print(f"  done in {time.time() - t:.1f}s: track_a={scores_a['score']}")
            results["runs"][name] = {"track_a": scores_a, "track_b": scores_b, "blurb": blurb}

    return results


# ---------------------------------------------------------------------------
# Markdown writer
# ---------------------------------------------------------------------------


def write_markdown(results: dict[str, Any], out_path: Path) -> None:
    oracle_a = results["runs"]["oracle"]["track_a"]
    oracle_b = results["runs"]["oracle"].get("track_b")
    track_b_enabled = bool(results.get("track_b"))

    def fmt(x: float | None) -> str:
        return "—" if x is None else f"{x:.3f}"

    try:
        fixture_disp = str(Path(results["fixture"]).resolve().relative_to(REPO_ROOT))
    except ValueError:
        fixture_disp = results["fixture"]

    lines: list[str] = []
    lines.append("# Sensitivity tests — `animation_fidelity` (part 2)")
    lines.append("")
    lines.append(
        f"_Fixture: `{fixture_disp}` ({len(results['pages'])} pages: "
        f"{', '.join(results['pages'])}). Regenerate with "
        "`python part2/grading/sensitivity.py --track-b --write-md part2/grading/SENSITIVITY.md`._"
    )
    lines.append("")
    if results.get("rebaseline", False):
        lines.append(
            "Ground truth was **locally rebaselined** — part 2's `recipe/02-capture/capture.py` "
            "was re-run against the fixture's `solution/source/` so the reference motion strips "
            "come from the same Playwright the grader uses. Oracle SSIM is not bounded by "
            "cross-machine Chromium drift."
        )
    else:
        lines.append(
            "_Scored against the fixture's pre-existing motion strips (no rebaseline). "
            "Track A oracle is bounded below by cross-machine Chromium drift._"
        )
    lines.append("")

    # ---- What the corruptions do ----
    lines.append("## Corruption catalogue")
    lines.append("")
    lines.append("`animation_fidelity` is a single-criterion grader; the matrix collapses to one column per track.")
    lines.append("")
    lines.append("| corruption | what it does | predicted effect on the grader |")
    lines.append("|---|---|---|")
    lines.append("| `strip_animation`   | Removes the `animation: ...` declaration from `.wdvb-anim-*` class rules | Grader's `find_agent_widget` finds no element with `animationName !== 'none'` → **score 0** |")
    lines.append("| `static_at_final`   | Sets `animation: none` on the class so the element renders at its default (final) state | All 5 motion-strip panels are byte-identical → \"no animation played\" detector → **score 0** |")
    lines.append("| `reverse_keyframes` | Swaps `from` and `to` blocks in `@keyframes wdvb-anim-…` | Motion direction reverses — mid-strip panels diverge from the reference, SSIM falls but not to 0 (start/end frames still close) |")
    lines.append("| `slow_10x`          | Multiplies every `animation: … <N>ms` by 10 | At reference duration offsets the animation has barely started → mid-frames look static → SSIM drops |")
    lines.append("| `fast_10x`          | Divides duration by 10 | Animation settles before panel 1 → all panels match the final state, missing mid-motion → SSIM drops |")
    lines.append("| `wrong_widget`      | Moves the `wdvb-anim-*` class onto the page's `<header>` | Bbox IoU vs the reference widget fails (< 0.05) → **score 0** |")
    lines.append("")

    # ---- Results table ----
    lines.append("## Results")
    lines.append("")
    cols = ["track", "oracle"] + list(CORRUPTIONS.keys())
    header = "| " + " | ".join(cols) + " |"
    sep = "|" + "|".join("---" for _ in cols) + "|"
    lines.append(header)
    lines.append(sep)

    # Track A row
    row = ["**Track A** (SSIM)", fmt(oracle_a["score"])]
    for name in CORRUPTIONS:
        s = results["runs"][name]["track_a"]["score"]
        delta = s - oracle_a["score"]
        row.append(f"{fmt(s)} ({delta:+.2f})")
    lines.append("| " + " | ".join(row) + " |")

    # Track B row
    if track_b_enabled and oracle_b is not None:
        row = ["**Track B** (judge)", fmt(oracle_b["score"])]
        for name in CORRUPTIONS:
            tb = results["runs"][name].get("track_b") or {}
            s = tb.get("score")
            if s is None or oracle_b.get("score") is None:
                row.append(fmt(s))
            else:
                delta = s - oracle_b["score"]
                row.append(f"{fmt(s)} ({delta:+.2f})")
        lines.append("| " + " | ".join(row) + " |")
    elif track_b_enabled:
        lines.append("| **Track B** (judge) | — | " + " | ".join("—" for _ in CORRUPTIONS) + " |")

    lines.append("")
    lines.append("_Each cell is `score (Δ vs oracle)`. Track A scope: every page in the fixture. Track B scope: a single page (default `home`) to bound API cost._")
    lines.append("")

    # ---- Side-by-side ----
    if track_b_enabled and oracle_b is not None:
        lines.append("## Track A vs Track B — agreement matrix")
        lines.append("")
        lines.append("| corruption | Track A Δ | Track B Δ | agree? |")
        lines.append("|---|---|---|---|")
        for name in CORRUPTIONS:
            a = results["runs"][name]["track_a"]["score"]
            tb = results["runs"][name].get("track_b") or {}
            b = tb.get("score")
            o_a = oracle_a["score"]
            o_b = oracle_b["score"]
            da = a - o_a if (a is not None and o_a is not None) else None
            db = b - o_b if (b is not None and o_b is not None) else None
            if da is None or db is None:
                agree = "—"
            elif da < -0.05 and db < -0.05:
                agree = "✓ both register"
            elif da < -0.05:
                agree = "⚠ only Track A"
            elif db < -0.05:
                agree = "⚠ only Track B"
            else:
                agree = "✗ neither registers"
            da_s = f"{da:+.2f}" if da is not None else "—"
            db_s = f"{db:+.2f}" if db is not None else "—"
            lines.append(f"| `{name}` | {da_s} | {db_s} | {agree} |")
        lines.append("")

    # ---- Per-page Track A detail ----
    lines.append("## Per-page Track A scores")
    lines.append("")
    scenarios = ["oracle"] + list(CORRUPTIONS.keys())
    header = "| page | " + " | ".join(scenarios) + " |"
    lines.append(header)
    lines.append("|" + "|".join("---" for _ in range(len(scenarios) + 1)) + "|")
    for page in results["pages"]:
        row = [f"`{page}`"]
        for s in scenarios:
            pp = results["runs"][s]["track_a"].get("per_page", {}).get(page, {})
            sc = pp.get("score")
            row.append(fmt(sc))
        lines.append("| " + " | ".join(row) + " |")
    lines.append("")

    # ---- Known interactions / honest notes ----
    lines.append("## Known interactions & caveats")
    lines.append("")
    lines.append("- **`strip_animation` and `static_at_final` collapse to the same observable behaviour on Track A** (no widget found / panels identical, both → 0.0). They differ in DOM state but produce identical strips. Track B's `anim_q7` (panel distinguishability) catches both, but the surrounding 6 questions still rate the fallback strip generously, so the Track B drop is more modest than Track A's.")
    lines.append("- **Track A applies a multiplicative `duration_factor`** in [0, 1] based on the agent's `animation-duration` vs the reference's `duration_ms`. Within ±25 % of the reference: factor 1.0. Beyond that the factor decays linearly on the ratio scale, so a 10 × duration mismatch caps the per-page score at ~0.125. This closes the pre-fix blind spot where `fast_10x` (animation settled before panel 1) scored near oracle because all panels looked like the reference's settled-state.")
    lines.append("- **`wrong_widget` zeros Track A by construction** (IoU gate), but Track B can still rate it partially positive (Δ ≈ −0.2) if the animation it sees looks valid in isolation. This asymmetry is intentional: Track A enforces \"the animation is *here*\", Track B asks \"is *an* animation present and well-shaped\".")
    lines.append("- The reference strip captures motion at `t = 0 / 25 / 50 / 75 / 100 %` of `duration_ms`. The Track A duration_factor compensates for the panel-SSIM metric's blindness to badly-timed animations; without it, an animation that ran 10× too fast or 10× too slow could still score highly on panel mean because mid-frames either matched the reference's start panel or its end panel.")
    lines.append("")
    lines.append("## Recent fixes")
    lines.append("")
    lines.append("- **Track A — duration factor** (`grading/criteria/animation_fidelity.py::duration_factor`). Multiplies the panel-SSIM mean by a [0, 1] factor reflecting how well the agent's `animation-duration` matches the reference. Lifted `fast_10x` Δ from −0.04 to −0.82.")
    lines.append("- **Track B — `anim_q7`** (`grading/judge/question_packs/animation_fidelity.json`). New question: \"Are the 5 panels in the agent's strip visually distinguishable from each other?\". Detects fallback strips and identical-panel renders. Lifted `strip_animation` / `static_at_final` Δ from +0.00 to −0.11.")
    lines.append("")

    lines.append("## What this does NOT prove")
    lines.append("")
    lines.append("- That a higher animation_fidelity score implies a *better* animation in absolute terms — only that it more faithfully replicates the reference.")
    lines.append("- That the per-frame panel offsets generalise — non-load-triggered or staggered animations would need a different sampling rule.")
    if not track_b_enabled:
        lines.append("- That Track A and Track B agree. Re-run with `--track-b` to populate the side-by-side comparison.")
    lines.append("")

    out_path.write_text("\n".join(lines))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--fixture", default="tasks/task_1-oneshot", help="part-2 task dir (default: tasks/task_1-oneshot)")
    ap.add_argument("--no-rebaseline", action="store_true", help="skip local rebaseline; score against the fixture's pre-existing motion strips")
    ap.add_argument("--track-b", action="store_true", help="also score every scenario with the Track B LLM judge")
    ap.add_argument("--judge-pages", default=None, help="comma-separated page subset for Track B (default: first page)")
    ap.add_argument("--judge-model", default="claude-opus-4-7")
    ap.add_argument("--write-md", default=None)
    ap.add_argument("--write-json", default=None)
    ap.add_argument("--from-json", default=None, help="re-render markdown from a saved JSON results file")
    args = ap.parse_args()

    fixture = (PART2_ROOT / args.fixture).resolve()
    if not fixture.is_dir():
        print(f"error: fixture not found: {fixture}", file=sys.stderr)
        return 2

    if args.from_json:
        results = json.loads(Path(args.from_json).read_text())
    else:
        judge_pages = (
            [p.strip() for p in args.judge_pages.split(",") if p.strip()]
            if args.judge_pages
            else None
        )
        if args.track_b and not os.environ.get("ANTHROPIC_API_KEY"):
            print("error: --track-b requires ANTHROPIC_API_KEY in env", file=sys.stderr)
            return 2
        results = run(
            fixture,
            rebaseline=not args.no_rebaseline,
            track_b=args.track_b,
            judge_pages=judge_pages,
            judge_model=args.judge_model,
        )

    print(json.dumps(results, indent=2, default=str))

    if args.write_json:
        Path(args.write_json).write_text(json.dumps(results, indent=2, default=str))
    if args.write_md:
        write_markdown(results, Path(args.write_md))
        print(f"\nwrote {args.write_md}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
