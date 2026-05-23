"""image_content_fidelity — agent images compared to ground truth at matched bboxes.

Mechanism:
    1. Take a full-page screenshot of the agent's HTML at desktop viewport.
    2. For each <img> in the rendered DOM, record bbox + crop the
       screenshot region + compute two signals:
         - pHash (DCT perceptual hash, 64 bits) — structural content.
         - Mean CIELAB color of the crop — catches content swaps where
           structure is preserved but pixels aren't.
    3. Greedy-match each ground-truth image to the nearest agent image by
       bbox-center Euclidean distance.
    4. Per-image similarity = mean of pHash similarity and color match.
       Both signals are needed — pHash alone is fooled by small icons
       (e.g. a 1×1 grey PNG stretched to 20×20 has near-uniform pHash
       similar to a thin-stroke lucide icon at the same size). Mean
       color delta catches that case.
    5. Per-page aggregate = sqrt(area)-weighted mean across matched
       images, so a hero photo outweighs a swarm of icons (instead of
       icon-heavy pages letting tiny-image noise dominate the score).
    6. Overall = mean across pages.

Edge cases:
    - GT has 0 images, agent has 0     → 1.0
    - GT has 0 images, agent has some  → 0.0 (unexpected images)
    - GT image has no matched agent    → 0.0 contribution
    - Crop fails (bbox outside)        → 0.0 contribution
    - Old ground_truth without lab_mean → falls back to pHash-only

Usage:
    python grading/criteria/image_content_fidelity.py <agent_output_dir> <ground_truth_dir> [page1,...]
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

_JS_IMAGES = """
() => {
    const out = [];
    const sx = window.scrollX, sy = window.scrollY;
    for (const img of document.querySelectorAll('img')) {
        const r = img.getBoundingClientRect();
        if (r.width === 0 || r.height === 0) continue;
        out.push({
            src: img.getAttribute('src'),
            alt: img.getAttribute('alt') || '',
            x: r.x + sx, y: r.y + sy, w: r.width, h: r.height,
        });
    }
    return out;
}
"""


# LAB color match ramp. JND in CIELAB is ~2-3 units; clearly different
# colors are >10. We allow up to LAB_SOFT_START of drift without penalty
# (covers Playwright rendering noise across runs) and fully penalise past
# LAB_HARD_END (which catches solid-grey vs near-white-icon cases easily).
LAB_SOFT_START = 5.0
LAB_HARD_END = 50.0


def phash_hamming_similarity(hash_a: str | None, hash_b: str | None) -> float:
    """1 - hamming(a, b) / bits; both hashes are hex strings from imagehash."""
    if not hash_a or not hash_b:
        return 0.0
    import imagehash
    try:
        a = imagehash.hex_to_hash(hash_a)
        b = imagehash.hex_to_hash(hash_b)
    except Exception:
        return 0.0
    bits = a.hash.size  # 64 for 8×8 pHash
    return max(0.0, 1.0 - (a - b) / bits)


def lab_distance(a: list[float] | None, b: list[float] | None) -> float | None:
    """Euclidean distance in CIELAB. None if either side is missing."""
    if a is None or b is None or len(a) != 3 or len(b) != 3:
        return None
    return ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2) ** 0.5


def color_match(d_lab: float | None) -> float | None:
    """1.0 if d_lab ≤ SOFT_START, 0.0 if ≥ HARD_END, linear between.
    None if d_lab is unavailable (caller falls back to pHash-only)."""
    if d_lab is None:
        return None
    if d_lab <= LAB_SOFT_START:
        return 1.0
    if d_lab >= LAB_HARD_END:
        return 0.0
    return 1.0 - (d_lab - LAB_SOFT_START) / (LAB_HARD_END - LAB_SOFT_START)


def combined_similarity(agent_sig: dict, gt_sig: dict) -> float:
    """Mean of pHash similarity and color match (when both are available).
    Falls back to pHash-only when `lab_mean` is missing on either side
    (backwards compat with old ground-truth artefacts)."""
    p_sim = phash_hamming_similarity(agent_sig.get("phash"), gt_sig.get("phash"))
    c_sim = color_match(lab_distance(agent_sig.get("lab_mean"), gt_sig.get("lab_mean")))
    if c_sim is None:
        return p_sim
    return (p_sim + c_sim) / 2.0


def crop_signatures(screenshot_path: Path, bbox: dict) -> dict | None:
    """Crop the bbox region from a full-page screenshot; return
    `{"phash": str, "lab_mean": [L, a, b]}` or None if the crop fails."""
    from PIL import Image
    import imagehash
    import numpy as np

    img = Image.open(screenshot_path).convert("RGB")
    iw, ih = img.size
    x = max(0, int(round(bbox["x"])))
    y = max(0, int(round(bbox["y"])))
    x2 = min(iw, x + max(1, int(round(bbox["w"]))))
    y2 = min(ih, y + max(1, int(round(bbox["h"]))))
    if x2 <= x or y2 <= y:
        return None
    try:
        crop = img.crop((x, y, x2, y2))
        ph = str(imagehash.phash(crop))
        # See capture.py::compute_image_signatures for the LAB encoding
        # rationale (L scaled, a/b reinterpreted as signed int8).
        lab_buf = np.asarray(crop.convert("LAB"))
        lab_mean = [
            float(lab_buf[..., 0].astype(np.float32).mean()) * 100.0 / 255.0,
            float(lab_buf[..., 1].view(np.int8).astype(np.float32).mean()),
            float(lab_buf[..., 2].view(np.int8).astype(np.float32).mean()),
        ]
        return {"phash": ph, "lab_mean": lab_mean}
    except Exception:
        return None


def match_by_center(agent_images: list[dict], gt_images: list[dict]) -> list[tuple[int, int]]:
    """Greedy nearest-center matching, GT-side initiates. Returns
    [(gt_idx, agent_idx_or_-1), …] for every GT image in order."""
    matched: list[tuple[int, int]] = []
    used: set[int] = set()
    for gi, gt in enumerate(gt_images):
        gcx, gcy = gt["x"] + gt["w"] / 2, gt["y"] + gt["h"] / 2
        best_idx = -1
        best_dist = float("inf")
        for ai, ag in enumerate(agent_images):
            if ai in used:
                continue
            acx, acy = ag["x"] + ag["w"] / 2, ag["y"] + ag["h"] / 2
            d = ((acx - gcx) ** 2 + (acy - gcy) ** 2) ** 0.5
            if d < best_dist:
                best_dist = d
                best_idx = ai
        if best_idx >= 0:
            used.add(best_idx)
        matched.append((gi, best_idx))
    return matched


def score(
    agent_output_dir: Path | str,
    ground_truth_dir: Path | str,
    pages: list[str],
) -> dict[str, Any]:
    from playwright.sync_api import sync_playwright

    agent_dir = Path(agent_output_dir)
    gt_images_dir = Path(ground_truth_dir) / "images"

    per_page: dict[str, dict[str, Any]] = {}
    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(viewport={"width": 1440, "height": 900})
        page = ctx.new_page()

        for page_name in pages:
            agent_html = agent_dir / f"{page_name}.html"
            gt_json = gt_images_dir / f"{page_name}.json"
            if not agent_html.exists() or not gt_json.exists():
                per_page[page_name] = {"score": 0.0, "detail": "missing input"}
                continue

            gt_images = json.loads(gt_json.read_text())
            page.goto(f"file://{agent_html.resolve()}")
            page.wait_for_load_state("networkidle")
            agent_images = page.evaluate(_JS_IMAGES)

            # No-images edge cases
            if not gt_images:
                per_page[page_name] = {
                    "score": 1.0 if not agent_images else 0.0,
                    "gt_images": 0,
                    "agent_images": len(agent_images),
                    "detail": "gt has no images",
                }
                continue

            # Take the full-page screenshot once per page
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tf:
                page.screenshot(path=tf.name, full_page=True)
                shot_path = Path(tf.name)

            sims: list[float] = []
            weights: list[float] = []  # sqrt(area_px), so hero photos > icons
            for gi, ai in match_by_center(agent_images, gt_images):
                gt = gt_images[gi]
                if gt.get("phash") is None:
                    continue  # gt entry had no signatures; skip
                area = max(1.0, float(gt.get("w", 0)) * float(gt.get("h", 0)))
                w = area ** 0.5
                if ai < 0:
                    sims.append(0.0)
                    weights.append(w)
                    continue
                agent_sig = crop_signatures(shot_path, agent_images[ai])
                if agent_sig is None:
                    sims.append(0.0)
                    weights.append(w)
                    continue
                sims.append(combined_similarity(agent_sig, gt))
                weights.append(w)

            total_w = sum(weights)
            page_score = (sum(s * w for s, w in zip(sims, weights)) / total_w) if total_w > 0 else 0.0
            per_page[page_name] = {
                "score": page_score,
                "matched": sum(1 for s in sims if s > 0),
                "gt_images": len(gt_images),
                "agent_images": len(agent_images),
            }

        browser.close()

    if not per_page:
        return {"score": 0.0, "per_page": {}, "detail": "no pages to score"}

    mean = sum(p["score"] for p in per_page.values()) / len(per_page)
    return {
        "score": mean,
        "per_page": per_page,
        "detail": "mean pHash Hamming similarity over center-matched <img> pairs",
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
