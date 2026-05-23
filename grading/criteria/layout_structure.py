"""layout_structure — SSIM on rendered full-page screenshots.

Per the "keep it simple" discussion: directly compare what the eye sees.
Render the agent's HTML at each viewport to a full-page PNG, load the
reference's pre-captured full-page PNG, pad the shorter to match the
taller (so the agent gets penalised for missing vertical content), then
compute the multi-channel structural similarity index.

Mechanism:
    For each (page, viewport):
      1. Render agent's <page>.html via Playwright at the viewport width,
         capturing a full_page screenshot (PNG bytes).
      2. Load reference PNG from
         <gt>/screenshots/<viewport>/<page>/full.png.
      3. Resize both to a common width (SSIM_WIDTH = 720 px), preserving
         each image's own aspect ratio. Smooths over single-pixel font
         antialiasing jitter without flattening the layout.
      4. Pad the shorter image's bottom with its dominant background
         colour to match the taller's height. Padding with background
         means "agent's page ended here — the rest is empty" rather
         than stretching the agent's content vertically.
      5. SSIM between the two same-shape arrays (channel_axis=2).
    Per-page score = mean across viewports.
    Overall       = mean across pages.

Trade-offs accepted (vs Hungarian-IoU on DOM bboxes):
    - Sensitive to small color drift. Mitigated by downsample to 720px.
    - No element-level matching; the score is a pure pixel-similarity
      readout. We rely on `component_presence` and the Track B judge for
      element-level signal.
    - Cannot distinguish "agent dropped a section" from "agent compressed
      a section." Both register as pixel divergence in that band, which
      is the right behaviour for design-replication grading.

Usage:
    python grading/criteria/layout_structure.py <agent_output_dir> <ground_truth_dir>
    python grading/criteria/layout_structure.py <agent_output_dir> <ground_truth_dir> --viewports desktop
    python grading/criteria/layout_structure.py <agent_output_dir> <ground_truth_dir> --pages home,about
"""

from __future__ import annotations

import argparse
import io
import json
import sys
from pathlib import Path
from typing import Any

VIEWPORT_SIZES = {
    "desktop": (1440, 900),
    "tablet": (768, 1024),
    "mobile": (375, 812),
}

# Downsample both images to this width before computing SSIM. 720 was
# chosen as a compromise: high enough to retain spacing/proportion signal,
# low enough to smooth over single-pixel font antialiasing differences.
SSIM_WIDTH = 720


def _dominant_bg_color(arr: "np.ndarray") -> tuple[int, int, int]:
    """Sample the four corners and return the mode RGB. Robust to the
    common case where one corner has a logo or rounded corner — the other
    three usually agree on the page background."""
    import numpy as np
    h, w = arr.shape[:2]
    corners = np.stack([
        arr[0, 0], arr[0, w - 1],
        arr[h - 1, 0], arr[h - 1, w - 1],
    ], axis=0)
    # Pick the corner colour that appears most often (mode by row).
    # Tie-break: top-left.
    unique, counts = np.unique(corners, axis=0, return_counts=True)
    return tuple(int(c) for c in unique[counts.argmax()])


def _pad_to_height(arr: "np.ndarray", target_h: int, bg: tuple[int, int, int]) -> "np.ndarray":
    """Pad the bottom of `arr` with `bg` until it reaches `target_h`."""
    import numpy as np
    h, w = arr.shape[:2]
    if h >= target_h:
        return arr[:target_h]
    pad_h = target_h - h
    pad_block = np.full((pad_h, w, 3), bg, dtype=arr.dtype)
    return np.concatenate([arr, pad_block], axis=0)


def _resize_to_width(arr: "np.ndarray", target_w: int) -> "np.ndarray":
    """Resize preserving aspect ratio via PIL.LANCZOS (the standard
    high-quality downsample). Returns uint8 array."""
    from PIL import Image
    h, w = arr.shape[:2]
    if w == target_w:
        return arr
    target_h = max(1, round(h * target_w / w))
    img = Image.fromarray(arr).resize((target_w, target_h), Image.LANCZOS)
    return _np_array_from_pil(img)


def _np_array_from_pil(img) -> "np.ndarray":
    """PIL → numpy uint8 RGB."""
    import numpy as np
    return np.asarray(img.convert("RGB"))


def ssim_score(agent_png_bytes: bytes, ref_png_path: Path) -> tuple[float, dict[str, Any]]:
    """Pure-function core: return (SSIM, debug_dims) for an agent PNG
    (as bytes) vs a reference PNG (as a file). Caller handles rendering."""
    import numpy as np
    from PIL import Image
    from skimage.metrics import structural_similarity

    agent_arr = _np_array_from_pil(Image.open(io.BytesIO(agent_png_bytes)))
    ref_arr = _np_array_from_pil(Image.open(ref_png_path))

    # Downsample both to a common width, each preserving its own aspect.
    agent_arr = _resize_to_width(agent_arr, SSIM_WIDTH)
    ref_arr = _resize_to_width(ref_arr, SSIM_WIDTH)

    # Pad the shorter to match the taller's height, using each image's
    # own dominant background so the padded band reads as "page ended."
    target_h = max(agent_arr.shape[0], ref_arr.shape[0])
    if agent_arr.shape[0] < target_h:
        agent_arr = _pad_to_height(agent_arr, target_h, _dominant_bg_color(agent_arr))
    if ref_arr.shape[0] < target_h:
        ref_arr = _pad_to_height(ref_arr, target_h, _dominant_bg_color(ref_arr))

    score_value = float(structural_similarity(
        agent_arr, ref_arr, channel_axis=2, data_range=255,
    ))
    return score_value, {
        "agent_h_px": int(agent_arr.shape[0]),
        "ref_h_px": int(ref_arr.shape[0]),
        "common_w_px": int(agent_arr.shape[1]),
    }


def score(
    agent_output_dir: Path | str,
    ground_truth_dir: Path | str,
    pages: list[str],
    *,
    viewports: list[str] | None = None,
) -> dict[str, Any]:
    """Score every (page × viewport) combination; aggregate to overall mean."""
    from playwright.sync_api import sync_playwright

    if viewports is None:
        viewports = list(VIEWPORT_SIZES.keys())

    agent_dir = Path(agent_output_dir)
    gt_shots_dir = Path(ground_truth_dir) / "screenshots"

    per_page: dict[str, dict[str, Any]] = {}
    with sync_playwright() as p:
        browser = p.chromium.launch()

        for page_name in pages:
            agent_html = agent_dir / f"{page_name}.html"
            if not agent_html.exists():
                per_page[page_name] = {"score": 0.0, "detail": f"agent html missing: {agent_html}"}
                continue

            vp_scores: dict[str, dict[str, Any]] = {}
            for vp in viewports:
                if vp not in VIEWPORT_SIZES:
                    vp_scores[vp] = {"score": 0.0, "detail": f"unknown viewport {vp!r}"}
                    continue
                ref_png = gt_shots_dir / vp / page_name / "full.png"
                if not ref_png.exists():
                    vp_scores[vp] = {"score": 0.0, "detail": f"gt screenshot missing: {ref_png}"}
                    continue

                w, h = VIEWPORT_SIZES[vp]
                ctx = browser.new_context(viewport={"width": w, "height": h})
                page = ctx.new_page()
                page.goto(f"file://{agent_html.resolve()}")
                page.wait_for_load_state("networkidle")
                try:
                    page.evaluate("() => (document.fonts && document.fonts.ready) ? document.fonts.ready : null")
                except Exception:
                    pass
                agent_png = page.screenshot(full_page=True)
                ctx.close()

                vp_score, dims = ssim_score(agent_png, ref_png)
                vp_scores[vp] = {"score": vp_score, **dims}

            if vp_scores:
                page_mean = sum(v["score"] for v in vp_scores.values()) / len(vp_scores)
            else:
                page_mean = 0.0
            per_page[page_name] = {"score": page_mean, "viewports": vp_scores}

        browser.close()

    if not per_page:
        return {"score": 0.0, "per_page": {}, "detail": "no pages to score"}

    mean = sum(p["score"] for p in per_page.values()) / len(per_page)
    return {
        "score": mean,
        "per_page": per_page,
        "detail": f"SSIM on full-page screenshots at {SSIM_WIDTH}px width, mean across {len(viewports)} viewport(s) and {len(per_page)} page(s)",
    }


def _cli() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("agent_output_dir")
    ap.add_argument("ground_truth_dir")
    ap.add_argument("--pages", default=None)
    ap.add_argument(
        "--viewports",
        default=None,
        help="comma-separated subset of desktop,tablet,mobile (default: all three)",
    )
    args = ap.parse_args()

    pages = (
        [p.strip() for p in args.pages.split(",") if p.strip()]
        if args.pages
        else sorted(p.stem for p in Path(args.agent_output_dir).glob("*.html"))
    )
    if not pages:
        print(f"error: no pages discovered in {args.agent_output_dir}", file=sys.stderr)
        return 2

    viewports = (
        [v.strip() for v in args.viewports.split(",") if v.strip()]
        if args.viewports
        else None
    )

    result = score(
        args.agent_output_dir,
        args.ground_truth_dir,
        pages,
        viewports=viewports,
    )
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
