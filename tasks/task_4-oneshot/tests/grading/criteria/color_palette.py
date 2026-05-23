"""color_palette — k-means in LAB on agent screenshot, EMD vs ground-truth palette.

Per plan.md §3: k-means (k=8, fixed seed, LAB color space) on the rendered
screenshot → Earth-Mover's Distance vs ground-truth palette. Score is
`1 - EMD / MAX_DIST` (clamped to [0, 1]).

EMD-approximation: Hungarian assignment over the 8 × 8 LAB-distance cost
matrix (Euclidean ≈ CIEDE76), summed with weights = min(agent_weight,
gt_weight) per matched pair. Fast, deterministic, and visually adequate
for our k=8 palette size.

Usage:
    python grading/criteria/color_palette.py <agent_output_dir> <ground_truth_dir> [page1,...]
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

PALETTE_K = 8
PALETTE_SAMPLES = 5000
MAX_LAB_DIST = 50.0  # Above this, similarity is 0; below, linear toward 1.


def compute_palette(screenshot_path: Path, k: int = PALETTE_K, n_samples: int = PALETTE_SAMPLES) -> list[dict]:
    """Duplicate of capture.compute_palette; keeps grader self-contained
    so it has no path-hacking import from recipe/02-capture/."""
    from PIL import Image
    import numpy as np
    from skimage.color import rgb2lab, lab2rgb
    from sklearn.cluster import KMeans

    img = np.array(Image.open(screenshot_path).convert("RGB"), dtype=np.float32) / 255.0
    pixels = img.reshape(-1, 3)
    rng = np.random.default_rng(42)
    if len(pixels) > n_samples:
        pixels = pixels[rng.choice(len(pixels), n_samples, replace=False)]

    lab = rgb2lab(pixels.reshape(1, -1, 3)).reshape(-1, 3)
    km = KMeans(n_clusters=k, random_state=42, n_init=10).fit(lab)
    sizes = np.bincount(km.labels_, minlength=k) / len(km.labels_)
    centers_rgb = np.clip(lab2rgb(km.cluster_centers_.reshape(1, -1, 3)).reshape(-1, 3), 0, 1)

    palette = []
    for i in range(k):
        r, g, b = (centers_rgb[i] * 255).astype(int)
        palette.append({
            "hex": f"#{r:02x}{g:02x}{b:02x}",
            "weight": float(sizes[i]),
            "lab": [float(v) for v in km.cluster_centers_[i]],
        })
    palette.sort(key=lambda p: -p["weight"])
    return palette


def palette_emd_approx(p1: list[dict], p2: list[dict]) -> float:
    """Approximate EMD between two weighted palettes via Hungarian assignment.

    Returns mean weighted LAB distance over matched pairs. 0 = identical.
    """
    import numpy as np
    from scipy.optimize import linear_sum_assignment

    if not p1 or not p2:
        return MAX_LAB_DIST

    lab1 = np.array([c["lab"] for c in p1])
    lab2 = np.array([c["lab"] for c in p2])
    w1 = np.array([c["weight"] for c in p1])
    w2 = np.array([c["weight"] for c in p2])

    cost = np.linalg.norm(lab1[:, None] - lab2[None, :], axis=2)  # n × m
    n, m = cost.shape
    size = max(n, m)
    padded = np.full((size, size), 1e6)
    padded[:n, :m] = cost
    row_ind, col_ind = linear_sum_assignment(padded)

    total_dist = 0.0
    total_w = 0.0
    for r, c in zip(row_ind, col_ind):
        if r < n and c < m:
            w = min(w1[r], w2[c])
            total_dist += cost[r, c] * w
            total_w += w
    return total_dist / total_w if total_w > 0 else MAX_LAB_DIST


def emd_to_similarity(emd: float) -> float:
    """Clamp 1 - emd / MAX_LAB_DIST to [0, 1]."""
    return max(0.0, min(1.0, 1.0 - emd / MAX_LAB_DIST))


def score(
    agent_output_dir: Path | str,
    ground_truth_dir: Path | str,
    pages: list[str],
) -> dict[str, Any]:
    from playwright.sync_api import sync_playwright

    agent_dir = Path(agent_output_dir)
    gt_palette_dir = Path(ground_truth_dir) / "palette"

    per_page: dict[str, dict[str, Any]] = {}
    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(viewport={"width": 1440, "height": 900})
        page = ctx.new_page()

        for page_name in pages:
            agent_html = agent_dir / f"{page_name}.html"
            gt_json = gt_palette_dir / f"{page_name}.json"
            if not agent_html.exists() or not gt_json.exists():
                per_page[page_name] = {"score": 0.0, "detail": "missing input"}
                continue

            gt_palette = json.loads(gt_json.read_text())

            page.goto(f"file://{agent_html.resolve()}")
            page.wait_for_load_state("networkidle")
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tf:
                page.screenshot(path=tf.name, full_page=True)
                agent_palette = compute_palette(Path(tf.name))

            emd = palette_emd_approx(agent_palette, gt_palette)
            sim = emd_to_similarity(emd)
            per_page[page_name] = {
                "score": sim,
                "emd_lab": round(emd, 3),
                "agent_top_hex": [c["hex"] for c in agent_palette[:3]],
                "gt_top_hex": [c["hex"] for c in gt_palette[:3]],
            }

        browser.close()

    if not per_page:
        return {"score": 0.0, "per_page": {}, "detail": "no pages to score"}

    mean = sum(p["score"] for p in per_page.values()) / len(per_page)
    return {
        "score": mean,
        "per_page": per_page,
        "detail": f"1 - Hungarian-EMD(LAB) / {MAX_LAB_DIST}, mean across pages",
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
