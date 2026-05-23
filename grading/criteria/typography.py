"""typography — area-weighted match of font-family + font-size per text node.

Per plan.md §3: `getComputedStyle().fontFamily + fontSize` per text node,
area-weighted match against `ground_truth/typography/{page}.json`.

Per-node score:
    1.0  same family AND |font_size_diff| ≤ 2 px
    0.5  same family OR same size (not both)
    0.0  neither

Matching: greedy by tag (same tag preferred; cross-tag matches get a 0.5×
penalty). Area-weighted mean across all GT nodes per page.

Family normalization: take the first family in the comma-separated
font-family list, strip quotes, lowercase. So `"Inter", sans-serif` and
`'Inter'` and `Inter` all normalise to `inter`.

Usage:
    python grading/criteria/typography.py <agent_output_dir> <ground_truth_dir> [page1,...]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

_FONT_SIZE_TOLERANCE_PX = 2.0

_JS_TYPOGRAPHY = """
() => {
    const out = [];
    const walker = document.createTreeWalker(
        document.body, NodeFilter.SHOW_TEXT,
        { acceptNode: n => n.textContent && n.textContent.trim() ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_REJECT }
    );
    const seen = new Set();
    while (walker.nextNode()) {
        const node = walker.currentNode;
        const el = node.parentElement;
        if (!el || seen.has(el)) continue;
        seen.add(el);
        const r = el.getBoundingClientRect();
        if (r.width === 0 || r.height === 0) continue;
        const cs = getComputedStyle(el);
        out.push({
            tag: el.tagName.toLowerCase(),
            font_family: cs.fontFamily,
            font_size_px: parseFloat(cs.fontSize),
            font_weight: cs.fontWeight,
            line_height: cs.lineHeight,
            area: r.width * r.height,
            text_len: node.textContent.trim().length,
        });
    }
    return out;
}
"""


def normalize_family(family: str) -> str:
    """Drop quotes and the fallback list, lowercase."""
    if not family:
        return ""
    return family.split(",")[0].strip().strip("'\"").lower()


def node_pair_score(agent: dict, gt: dict) -> float:
    af, gf = normalize_family(agent["font_family"]), normalize_family(gt["font_family"])
    family_match = af == gf
    size_match = abs(agent["font_size_px"] - gt["font_size_px"]) <= _FONT_SIZE_TOLERANCE_PX
    if family_match and size_match:
        return 1.0
    if family_match or size_match:
        return 0.5
    return 0.0


def match_and_score(agent_typo: list[dict], gt_typo: list[dict]) -> float:
    """Greedy area-weighted matching, GT nodes pick best agent match in
    descending-area order. Cross-tag matches get a 0.5× penalty."""
    if not gt_typo:
        return 1.0 if not agent_typo else 0.0

    weighted_sum = 0.0
    total_area = 0.0
    used: set[int] = set()
    gt_sorted = sorted(enumerate(gt_typo), key=lambda x: -x[1]["area"])

    for _, gt_node in gt_sorted:
        best_idx = -1
        best_score = -1.0
        for ai, ag in enumerate(agent_typo):
            if ai in used:
                continue
            raw = node_pair_score(ag, gt_node)
            s = raw if ag["tag"] == gt_node["tag"] else raw * 0.5
            if s > best_score:
                best_score = s
                best_idx = ai
        if best_idx >= 0:
            used.add(best_idx)
        weighted_sum += max(0.0, best_score) * gt_node["area"]
        total_area += gt_node["area"]

    return weighted_sum / total_area if total_area > 0 else 0.0


def score(
    agent_output_dir: Path | str,
    ground_truth_dir: Path | str,
    pages: list[str],
) -> dict[str, Any]:
    from playwright.sync_api import sync_playwright

    agent_dir = Path(agent_output_dir)
    gt_typo_dir = Path(ground_truth_dir) / "typography"

    per_page: dict[str, dict[str, Any]] = {}
    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(viewport={"width": 1440, "height": 900})
        page = ctx.new_page()

        for page_name in pages:
            agent_html = agent_dir / f"{page_name}.html"
            gt_json = gt_typo_dir / f"{page_name}.json"
            if not agent_html.exists() or not gt_json.exists():
                per_page[page_name] = {"score": 0.0, "detail": "missing input"}
                continue

            gt_typo = json.loads(gt_json.read_text())
            page.goto(f"file://{agent_html.resolve()}")
            page.wait_for_load_state("networkidle")
            try:
                page.evaluate("() => (document.fonts && document.fonts.ready) ? document.fonts.ready : null")
            except Exception:
                pass
            agent_typo = page.evaluate(_JS_TYPOGRAPHY)

            s = match_and_score(agent_typo, gt_typo)
            per_page[page_name] = {
                "score": s,
                "agent_nodes": len(agent_typo),
                "gt_nodes": len(gt_typo),
            }

        browser.close()

    if not per_page:
        return {"score": 0.0, "per_page": {}, "detail": "no pages to score"}

    mean = sum(p["score"] for p in per_page.values()) / len(per_page)
    return {
        "score": mean,
        "per_page": per_page,
        "detail": "area-weighted (font-family + font-size) match, mean across pages",
    }


def _cli() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("agent_output_dir")
    ap.add_argument("ground_truth_dir")
    ap.add_argument("--pages", default=None)
    args = ap.parse_args()

    pages = (
        [p.strip() for p in args.pages.split(",") if p.strip()]
        if args.pages
        else sorted(p.stem for p in Path(args.agent_output_dir).glob("*.html"))
    )
    if not pages:
        print(f"error: no pages discovered in {args.agent_output_dir}", file=sys.stderr)
        return 2

    print(json.dumps(score(args.agent_output_dir, args.ground_truth_dir, pages), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
