"""component_presence — macro-block count agreement (purely geometric).

Mechanism (two sentences, per the Track-A design philosophy):

    1. From each page's bbox list (already extracted by capture.py for
       the ground truth; extracted on-the-fly here for the agent),
       count "macro-blocks" — block-level elements whose area is at
       least 1% and less than 95% of the page area, walked
       largest-first with spatial-containment de-duplication.
    2. Per-page score = `1 - |agent_count - gt_count| / max(agent, gt)`;
       overall score = mean across pages.

This is deliberately **agnostic of any author convention**: no
`data-component`, no class names, no IDs, no roles. Two HTML
implementations that look the same will yield similar macro-block
counts regardless of how their authors named or organised the source.

What it catches:
    - Agent that builds only a header / hero and skips the rest of the
      page (count too low).
    - Agent that pads with unrelated extra panels (count too high).

What it does NOT catch (other graders cover these):
    - Macro-blocks in the wrong locations  → `layout_structure`.
    - Wrong colors / typography / images   → those graders, respectively.
    - Subjective "is the filters sidebar there" gestalt → Track B's
      LLM judge.

Run desktop-only; multi-viewport tuning is `layout_structure`'s job.

Usage:
    python grading/criteria/component_presence.py <agent_output_dir> <ground_truth_dir>
    python grading/criteria/component_presence.py <agent_output_dir> <ground_truth_dir> --pages home,about
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

DESKTOP_W, DESKTOP_H = 1440, 900

# Block-level tags eligible to be a "macro" component container.
_MACRO_BLOCK_TAGS: frozenset[str] = frozenset({
    "div", "section", "article", "aside", "header", "footer", "nav", "main",
    "form", "table", "figure", "ul", "ol",
})

# Never counted (structural or non-visual elements).
_SKIP_TAGS: frozenset[str] = frozenset({
    "html", "body", "head", "script", "style", "noscript",
    "meta", "link", "title",
})

# An element is a macro-block candidate iff its area is in
# [MIN_AREA_FRAC, PAGE_WRAPPER_FRAC) of the page area. The upper bound
# strips out the html/body and any single <div class="app"> wrapper.
_MIN_AREA_FRAC = 0.01
_PAGE_WRAPPER_FRAC = 0.95

# Tolerance for "is bbox A contained in bbox B" — a few pixels of
# anti-aliasing slack so rounding doesn't break dedup.
_CONTAINMENT_TOL = 4.0

# Same JS evaluator as recipe/02-capture/capture.py — kept verbatim so
# agent-side extraction matches ground-truth extraction byte-for-byte.
_JS_BBOXES = """
() => {
    const out = [];
    const sx = window.scrollX, sy = window.scrollY;
    for (const el of document.querySelectorAll('*')) {
        const r = el.getBoundingClientRect();
        if (r.width === 0 || r.height === 0) continue;
        const cs = getComputedStyle(el);
        if (cs.display === 'none' || cs.visibility === 'hidden' || parseFloat(cs.opacity) === 0) continue;
        out.push({
            tag: el.tagName.toLowerCase(),
            id: el.id || null,
            cls: (typeof el.className === 'string' && el.className) ? el.className : null,
            role: el.getAttribute('role') || null,
            data_component: el.getAttribute('data-component') || null,
            x: r.x + sx,
            y: r.y + sy,
            w: r.width,
            h: r.height,
        });
    }
    return out;
}
"""


def _page_area(bboxes: list[dict[str, Any]]) -> float:
    """Use <html>'s bbox as the page area reference; fall back to <body>
    or the largest bbox."""
    for b in bboxes:
        if b["tag"] == "html":
            return max(1.0, b["w"] * b["h"])
    for b in bboxes:
        if b["tag"] == "body":
            return max(1.0, b["w"] * b["h"])
    if bboxes:
        return max(1.0, max(b["w"] * b["h"] for b in bboxes))
    return 1.0


def _contained(inner: dict[str, Any], outer: dict[str, Any], tol: float = _CONTAINMENT_TOL) -> bool:
    return (
        inner["x"] >= outer["x"] - tol
        and inner["y"] >= outer["y"] - tol
        and inner["x"] + inner["w"] <= outer["x"] + outer["w"] + tol
        and inner["y"] + inner["h"] <= outer["y"] + outer["h"] + tol
    )


def macro_blocks(bboxes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return the kept macro-block bboxes (largest-first, dedup by
    spatial containment)."""
    pa = _page_area(bboxes)
    lo = pa * _MIN_AREA_FRAC
    hi = pa * _PAGE_WRAPPER_FRAC
    candidates = [
        b for b in bboxes
        if b["tag"] not in _SKIP_TAGS
        and b["tag"] in _MACRO_BLOCK_TAGS
        and lo <= b["w"] * b["h"] < hi
    ]
    candidates.sort(key=lambda b: b["w"] * b["h"], reverse=True)
    kept: list[dict[str, Any]] = []
    for b in candidates:
        if any(_contained(b, k) for k in kept):
            continue
        kept.append(b)
    return kept


def macro_block_count(bboxes: list[dict[str, Any]]) -> int:
    return len(macro_blocks(bboxes))


def _extract_agent_bboxes(html_path: Path) -> list[dict[str, Any]]:
    """Render the agent's HTML at desktop and pull the bbox list."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(viewport={"width": DESKTOP_W, "height": DESKTOP_H})
        page = ctx.new_page()
        page.goto(f"file://{html_path.resolve()}")
        page.wait_for_load_state("networkidle")
        bboxes = page.evaluate(_JS_BBOXES)
        browser.close()
    return bboxes


def _page_score(gt_count: int, agent_count: int) -> float:
    if gt_count == 0 and agent_count == 0:
        return 1.0
    return 1.0 - abs(gt_count - agent_count) / max(gt_count, agent_count)


def score(
    agent_output_dir: Path | str,
    ground_truth_dir: Path | str,
    pages: list[str],
) -> dict[str, Any]:
    """Mean macro-block count agreement across pages, desktop only."""
    agent_dir = Path(agent_output_dir)
    gt_dir = Path(ground_truth_dir)

    per_page: dict[str, dict[str, Any]] = {}
    for page_name in pages:
        gt_bboxes_path = gt_dir / "bboxes" / "desktop" / f"{page_name}.json"
        if not gt_bboxes_path.exists():
            per_page[page_name] = {"score": 0.0, "error": f"missing {gt_bboxes_path}"}
            continue
        gt_bboxes = json.loads(gt_bboxes_path.read_text())
        gt_count = macro_block_count(gt_bboxes)

        agent_html = agent_dir / f"{page_name}.html"
        if not agent_html.exists():
            per_page[page_name] = {
                "score": 0.0, "gt_count": gt_count, "agent_count": 0,
                "error": f"missing {agent_html}",
            }
            continue
        try:
            agent_bboxes = _extract_agent_bboxes(agent_html)
        except Exception as e:
            per_page[page_name] = {
                "score": 0.0, "gt_count": gt_count, "agent_count": 0,
                "error": f"{type(e).__name__}: {e}",
            }
            continue
        agent_count = macro_block_count(agent_bboxes)

        per_page[page_name] = {
            "score": _page_score(gt_count, agent_count),
            "gt_count": gt_count,
            "agent_count": agent_count,
        }

    if not per_page:
        return {"score": 0.0, "per_page": {}, "n_pages": 0}
    mean = sum(p["score"] for p in per_page.values()) / len(per_page)
    return {"score": mean, "per_page": per_page, "n_pages": len(per_page)}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("agent_output_dir")
    ap.add_argument("ground_truth_dir")
    ap.add_argument("--pages", default=None,
                    help="comma-separated (default: every *.html in agent-output-dir)")
    args = ap.parse_args()

    agent_dir = Path(args.agent_output_dir)
    pages = (
        [p.strip() for p in args.pages.split(",") if p.strip()]
        if args.pages
        else sorted(p.stem for p in agent_dir.glob("*.html"))
    )
    if not pages:
        print("error: no pages discovered", file=sys.stderr)
        return 2

    result = score(agent_dir, args.ground_truth_dir, pages)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
