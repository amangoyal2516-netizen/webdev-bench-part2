"""visible_text_fidelity — token-set similarity on visible DOM textContent.

Per plan.md §3: normalized token-set similarity over `document.body.innerText`,
compared against `ground_truth/text/{page}.json` produced by capture.py.

Mechanism (v1, simple):
    1. Tokenize both texts: lowercase, strip punctuation, split on whitespace.
    2. Sørensen-Dice over the token sets: `2 * |A ∩ B| / (|A| + |B|)`.
    3. Average across pages.

Per plan.md the eventual richer form is Dice for short labels + ROUGE-L for
longer fields with the lower of the two governing — that's a refinement.
v1 uses a single combined Dice over the full page text.

Usage:
    python grading/criteria/visible_text_fidelity.py <agent_output_dir> <ground_truth_dir> [page1,page2,...]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any


_TOKEN_STRIP = re.compile(r"[^\w\s'-]")


def _tokenize(text: str) -> set[str]:
    """Lowercase, strip punctuation, split on whitespace, drop empties."""
    text = _TOKEN_STRIP.sub(" ", text.lower())
    return {t for t in text.split() if t}


def sorensen_dice(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return 2 * len(a & b) / (len(a) + len(b))


def score(
    agent_output_dir: Path | str,
    ground_truth_dir: Path | str,
    pages: list[str],
) -> dict[str, Any]:
    """For each page, compare agent's visible DOM text to ground truth's."""
    from playwright.sync_api import sync_playwright

    agent_dir = Path(agent_output_dir)
    gt_text_dir = Path(ground_truth_dir) / "text"

    per_page: dict[str, dict[str, Any]] = {}
    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(viewport={"width": 1440, "height": 900})
        page = ctx.new_page()

        for page_name in pages:
            agent_html = agent_dir / f"{page_name}.html"
            gt_json = gt_text_dir / f"{page_name}.json"

            if not agent_html.exists():
                per_page[page_name] = {"score": 0.0, "detail": f"agent html missing: {agent_html}"}
                continue
            if not gt_json.exists():
                per_page[page_name] = {"score": 0.0, "detail": f"ground truth missing: {gt_json}"}
                continue

            gt_text = json.loads(gt_json.read_text()).get("text", "")
            page.goto(f"file://{agent_html.resolve()}")
            page.wait_for_load_state("networkidle")
            agent_text = page.evaluate("() => (document.body.innerText || '').trim()")

            gt_toks = _tokenize(gt_text)
            ag_toks = _tokenize(agent_text)
            sim = sorensen_dice(ag_toks, gt_toks)
            per_page[page_name] = {
                "score": sim,
                "agent_tokens": len(ag_toks),
                "gt_tokens": len(gt_toks),
                "overlap": len(ag_toks & gt_toks),
            }

        browser.close()

    if not per_page:
        return {"score": 0.0, "per_page": {}, "detail": "no pages to score"}

    mean = sum(p["score"] for p in per_page.values()) / len(per_page)
    return {
        "score": mean,
        "per_page": per_page,
        "detail": "Sørensen-Dice over tokenized page text, mean across pages",
    }


def _cli() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("agent_output_dir")
    ap.add_argument("ground_truth_dir")
    ap.add_argument("--pages", default=None, help="comma-separated; default: every *.html in agent_output_dir")
    args = ap.parse_args()

    pages = (
        [p.strip() for p in args.pages.split(",") if p.strip()]
        if args.pages
        else sorted(p.stem for p in Path(args.agent_output_dir).glob("*.html"))
    )
    if not pages:
        print(f"error: no pages discovered in {args.agent_output_dir}", file=sys.stderr)
        return 2

    result = score(args.agent_output_dir, args.ground_truth_dir, pages)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
