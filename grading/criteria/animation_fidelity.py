"""animation_fidelity — per-panel SSIM between agent's and reference's
5-panel motion strips.

Each generated task carries one load-triggered entrance animation per
page on a small above-the-fold widget. Capture stitches 5 viewport-band
crops at t=0/25/50/75/100 % of the animation duration into
`ground_truth/screenshots/<vp>/<page>/motion-strip.png`. This grader
re-runs the same strip-generation algorithm against the agent's
rendered HTML and scores the resulting strip against the reference.

Mechanism:
    For each page (desktop viewport only — motion fidelity is a single-
    viewport measurement):
      1. Load reference widget metadata from <gt>/widget/<page>.json
         (saved by capture.py): {selector, bbox, type, duration_ms, easing}.
      2. Render the agent's <page>.html via Playwright.
      3. Find the agent's animated element: query every DOM element's
         computed style, keep the ones where `animationName !== 'none'`,
         pick the one with the highest IoU against the reference widget
         bbox.
      4. If no element is found (or IoU < 0.05): score 0 for this page
         — the agent has no entrance animation matching the reference.
      5. Generate the agent's motion strip the same way capture.py did:
         reload to fire the animation fresh, sample 5 frames at the
         reference's `duration_ms` offsets, crop each to a horizontal
         band centred on the agent's widget bbox, stitch left-to-right.
      6. Pre-check: if all 5 of the agent's panels are byte-identical
         (no animation actually played), return 0 directly. This catches
         the "static replica skipped the animation" case without
         polluting the SSIM signal.
      7. Otherwise compute per-panel SSIM, downsampling both panels to
         a common 720 px width (matches `layout_structure`'s convention,
         smooths anti-aliasing noise).
      8. Per-page score: early-weighted mean
            [0.30, 0.25, 0.20, 0.15, 0.10]
         — earlier panels (where reference is mid-animation) carry more
         weight than later ones (where reference has settled and any
         agent with a correct static design trivially matches).

    Overall score: mean across pages.

Trade-offs accepted:
    - Sensitive to per-pixel layout drift in the band region — same
      band geometry on agent + reference is the only normalisation.
    - SSIM is conservative on animations with small footprints
      (e.g. a 50-px badge sliding 120 px); the per-panel diff is mostly
      static surrounding context. The early-weighting + zero-out-on-
      identical-panels heuristic mitigates this.
    - Doesn't directly measure direction/type/duration; Track B
      `animation_fidelity` covers those via atomic judge questions on
      the same strip pair.

Usage:
    python grading/criteria/animation_fidelity.py <agent_output_dir> <ground_truth_dir>
    python grading/criteria/animation_fidelity.py <agent_output_dir> <ground_truth_dir> --pages home,about
"""

from __future__ import annotations

import argparse
import io
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

DESKTOP_VIEWPORT = (1440, 900)
N_FRAMES = 5
BAND_PAD_PX = 80
PANEL_WEIGHTS = (0.30, 0.25, 0.20, 0.15, 0.10)  # must sum to 1.0
SSIM_WIDTH = 720
IDENTICAL_PIXEL_THRESHOLD = 0  # bytes-equal means identical


# ---------------------------------------------------------------------------
# Bbox + band helpers — mirror capture.py's geometry so reference and agent
# strips are crop-compatible.
# ---------------------------------------------------------------------------


def _iou(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> float:
    """IoU on (x, y, w, h) tuples."""
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    x1 = max(ax, bx)
    y1 = max(ay, by)
    x2 = min(ax + aw, bx + bw)
    y2 = min(ay + ah, by + bh)
    if x2 <= x1 or y2 <= y1:
        return 0.0
    inter = (x2 - x1) * (y2 - y1)
    union = aw * ah + bw * bh - inter
    return inter / union if union > 0 else 0.0


def _band_from_rect(
    rect: dict[str, float] | tuple[float, float, float, float],
    viewport_w: int,
    viewport_h: int,
    *,
    pad_px: int = BAND_PAD_PX,
) -> tuple[int, int, int, int] | None:
    """Same algorithm as capture.py's _band_from_rect."""
    if rect is None:
        return None
    if isinstance(rect, dict):
        y, h = float(rect["y"]), float(rect["h"])
    else:
        _, y, _, h = rect
        y, h = float(y), float(h)
    cy = y + h / 2.0
    half_h = h / 2.0 + pad_px
    y0 = max(0, int(cy - half_h))
    y1 = min(viewport_h, int(cy + half_h))
    if y1 <= y0:
        return None
    return (0, y0, viewport_w, y1)


# ---------------------------------------------------------------------------
# Agent-side detection
# ---------------------------------------------------------------------------


_JS_ANIMATED_ELEMENTS = """
() => {
    const out = [];
    for (const el of document.querySelectorAll('*')) {
        const cs = getComputedStyle(el);
        if (!cs) continue;
        const name = cs.animationName;
        if (!name || name === 'none' || name === 'initial' || name === 'inherit') continue;
        const r = el.getBoundingClientRect();
        if (r.width === 0 || r.height === 0) continue;
        out.push({
            x: r.x, y: r.y, w: r.width, h: r.height,
            animation_name: name,
            tag: el.tagName.toLowerCase(),
            cls: (typeof el.className === 'string' && el.className) ? el.className : null,
        });
    }
    return out;
}
"""


def find_agent_widget(
    page,
    reference_bbox: tuple[float, float, float, float],
    *,
    min_iou: float = 0.05,
) -> dict[str, Any] | None:
    """Query the agent's rendered page for animated elements. Pick the one
    whose bbox has the highest IoU against `reference_bbox` (in viewport
    coords). Returns the element record (with `iou` added) or None if
    no element matches above the threshold."""
    try:
        elems = page.evaluate(_JS_ANIMATED_ELEMENTS)
    except Exception:
        return None
    if not elems:
        return None
    best = None
    best_iou = 0.0
    for elem in elems:
        bbox = (elem["x"], elem["y"], elem["w"], elem["h"])
        cur = _iou(reference_bbox, bbox)
        if cur > best_iou:
            best_iou = cur
            best = elem
    if best is None or best_iou < min_iou:
        return None
    best["iou"] = best_iou
    return best


# ---------------------------------------------------------------------------
# Strip generation against the agent's HTML
# ---------------------------------------------------------------------------


def make_agent_motion_strip(
    page,
    html_url: str,
    reference_widget: dict[str, Any],
    viewport: tuple[int, int] = DESKTOP_VIEWPORT,
    *,
    n_frames: int = N_FRAMES,
):
    """Reload the page (fresh load animation), find the agent's animated
    element by IoU against the reference widget bbox, sample n_frames at
    `t = 0/25/50/75/100 %` of reference duration_ms, crop each to a
    horizontal band centred on the agent's widget bbox, stitch
    left-to-right.

    Returns `(strip_image_or_None, info_dict)`:
      info_dict = {"found": bool, "iou": float | None, "agent_bbox": [x,y,w,h] | None}
    """
    from PIL import Image  # noqa: PLC0415 — lazy import

    vp_w, vp_h = viewport
    duration_ms = int(reference_widget.get("duration_ms") or 1300)
    ref_bbox_list = reference_widget["bbox"]
    ref_bbox = (
        float(ref_bbox_list[0]),
        float(ref_bbox_list[1]),
        float(ref_bbox_list[2]),
        float(ref_bbox_list[3]),
    )

    # Reload so the load animation fires fresh. Don't wait for networkidle
    # — animations start at DOMContentLoaded, which reload() returns at.
    try:
        page.goto(html_url, wait_until="domcontentloaded")
    except Exception:
        page.goto(html_url)

    # Wait for the load animation to settle so getBoundingClientRect()
    # returns the layout (not transformed) position.
    page.wait_for_timeout(max(duration_ms + 200, 1500))

    agent_widget = find_agent_widget(page, ref_bbox)
    if agent_widget is None:
        return None, {"found": False, "iou": None, "agent_bbox": None}

    agent_bbox_dict = {
        "x": agent_widget["x"], "y": agent_widget["y"],
        "w": agent_widget["w"], "h": agent_widget["h"],
    }
    band = _band_from_rect(agent_bbox_dict, vp_w, vp_h)
    if band is None:
        band = (0, 0, vp_w, vp_h)

    # Reload again to fire the animation fresh, then sample at offsets.
    try:
        page.goto(html_url, wait_until="domcontentloaded")
    except Exception:
        page.goto(html_url)
    # Give the page a moment to lay out before the first sample so
    # `getBoundingClientRect` was meaningful (we already used it before
    # the reload; layout should be deterministic on the second reload).
    page.wait_for_timeout(30)

    offsets_ms = [
        int(duration_ms * (i / (n_frames - 1))) if n_frames > 1 else duration_ms
        for i in range(n_frames)
    ]
    crops = []
    prev_t = 0
    for target_t in offsets_ms:
        delta = max(target_t - prev_t, 0)
        if delta > 0:
            page.wait_for_timeout(delta)
        prev_t = target_t
        try:
            shot_bytes = page.screenshot(full_page=False)
        except Exception:
            continue
        img = Image.open(io.BytesIO(shot_bytes))
        x0, y0, x1, y1 = band
        crops.append(img.crop((x0, y0, x1, y1)).copy())

    if not crops:
        return None, {"found": True, "iou": agent_widget["iou"], "agent_bbox": [
            agent_widget["x"], agent_widget["y"], agent_widget["w"], agent_widget["h"]
        ]}

    strip_h = max(c.height for c in crops)
    total_w = sum(c.width for c in crops)
    strip = Image.new("RGB", (total_w, strip_h), color=(255, 255, 255))
    cursor_x = 0
    for c in crops:
        strip.paste(c, (cursor_x, 0))
        cursor_x += c.width
    return strip, {
        "found": True,
        "iou": agent_widget["iou"],
        "agent_bbox": [agent_widget["x"], agent_widget["y"], agent_widget["w"], agent_widget["h"]],
    }


# ---------------------------------------------------------------------------
# Panel SSIM
# ---------------------------------------------------------------------------


def _split_into_panels(strip, n_panels: int) -> list[Any]:
    """Split a stitched strip into `n_panels` equal-width crops."""
    w, h = strip.size
    panel_w = w // n_panels
    panels = []
    for i in range(n_panels):
        x0 = i * panel_w
        x1 = (i + 1) * panel_w if i < n_panels - 1 else w
        panels.append(strip.crop((x0, 0, x1, h)))
    return panels


def _all_panels_identical(panels: list[Any]) -> bool:
    """True if every panel's bytes are equal to the first panel's bytes —
    meaning the agent's animation produced zero motion across all 5 frames."""
    if len(panels) < 2:
        return True
    first_bytes = panels[0].tobytes()
    for p in panels[1:]:
        if p.tobytes() != first_bytes:
            return False
    return True


def _ssim_panel_pair(panel_a, panel_b, *, common_width: int = SSIM_WIDTH) -> float:
    """Resize both panels to a common width preserving each panel's
    aspect ratio (heights may differ slightly post-resize), then pad the
    shorter one's bottom to match the taller, then compute multi-channel
    SSIM. Returns SSIM in [0, 1], clamped non-negative."""
    import numpy as np  # noqa: PLC0415
    from PIL import Image  # noqa: PLC0415
    from skimage.metrics import structural_similarity as ssim  # noqa: PLC0415

    def _resize_to_width(img, w_target: int):
        w0, h0 = img.size
        if w0 == 0 or h0 == 0:
            return img
        h_target = max(1, int(round(h0 * (w_target / w0))))
        return img.resize((w_target, h_target), Image.LANCZOS)

    def _dominant_bg(img):
        arr = np.asarray(img.convert("RGB"))
        if arr.size == 0:
            return (255, 255, 255)
        # Sample the top edge (~10 px) for a stable background colour.
        top = arr[:max(1, min(10, arr.shape[0])), :, :]
        med = np.median(top.reshape(-1, 3), axis=0).astype(np.uint8)
        return tuple(int(v) for v in med)

    a = _resize_to_width(panel_a, common_width)
    b = _resize_to_width(panel_b, common_width)
    h_max = max(a.size[1], b.size[1])
    bg = _dominant_bg(a)
    if a.size[1] < h_max:
        padded = Image.new("RGB", (common_width, h_max), bg)
        padded.paste(a, (0, 0))
        a = padded
    if b.size[1] < h_max:
        padded = Image.new("RGB", (common_width, h_max), _dominant_bg(b))
        padded.paste(b, (0, 0))
        b = padded
    arr_a = np.asarray(a.convert("RGB"))
    arr_b = np.asarray(b.convert("RGB"))
    if arr_a.shape != arr_b.shape:
        # Last-resort align — pad smaller to match larger.
        H = max(arr_a.shape[0], arr_b.shape[0])
        W = max(arr_a.shape[1], arr_b.shape[1])
        def _pad(arr):
            out = np.full((H, W, 3), 255, dtype=np.uint8)
            out[: arr.shape[0], : arr.shape[1], :] = arr
            return out
        arr_a = _pad(arr_a)
        arr_b = _pad(arr_b)
    score = ssim(arr_a, arr_b, channel_axis=2, data_range=255)
    return max(0.0, float(score))


def _weighted_panel_mean(panel_scores: list[float], weights: tuple[float, ...] = PANEL_WEIGHTS) -> float:
    """Weighted mean over panel scores. If lengths mismatch, fall back to
    uniform mean."""
    if len(panel_scores) != len(weights):
        return sum(panel_scores) / len(panel_scores) if panel_scores else 0.0
    return sum(s * w for s, w in zip(panel_scores, weights))


# ---------------------------------------------------------------------------
# Per-page scoring
# ---------------------------------------------------------------------------


def _load_widget_meta(gt_dir: Path, page_name: str) -> dict[str, Any] | None:
    p = gt_dir / "widget" / f"{page_name}.json"
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text())
    except Exception:
        return None


def _load_reference_strip(gt_dir: Path, page_name: str):
    """Reference strip lives under `screenshots/desktop/<page>/motion-strip.png`."""
    from PIL import Image  # noqa: PLC0415
    p = gt_dir / "screenshots" / "desktop" / page_name / "motion-strip.png"
    if not p.is_file():
        return None
    return Image.open(p).convert("RGB")


def score_page(
    agent_html: Path,
    widget_meta: dict[str, Any],
    reference_strip,
    *,
    browser=None,
    viewport: tuple[int, int] = DESKTOP_VIEWPORT,
) -> dict[str, Any]:
    """Score one page. Caller can pass an existing `browser` (Playwright
    Chromium instance) to amortise startup across pages; otherwise we
    launch our own.

    Returns `{score: float, detail: str, found: bool, iou: float | None,
    panel_scores: list[float] | None}`.
    """
    from playwright.sync_api import sync_playwright  # noqa: PLC0415

    own_browser = browser is None
    pw = None
    if own_browser:
        pw = sync_playwright().__enter__()
        browser = pw.chromium.launch()

    try:
        ctx = browser.new_context(viewport={"width": viewport[0], "height": viewport[1]})
        page = ctx.new_page()
        html_url = f"file://{agent_html.resolve()}"
        strip, info = make_agent_motion_strip(
            page, html_url, widget_meta, viewport=viewport, n_frames=N_FRAMES,
        )
        ctx.close()

        if strip is None:
            return {
                "score": 0.0,
                "detail": "no animated element matched the reference widget bbox",
                "found": info["found"],
                "iou": info["iou"],
                "panel_scores": None,
            }

        agent_panels = _split_into_panels(strip, N_FRAMES)
        if _all_panels_identical(agent_panels):
            return {
                "score": 0.0,
                "detail": "agent's 5 motion-strip panels are identical — no animation played",
                "found": True,
                "iou": info["iou"],
                "panel_scores": [1.0] * N_FRAMES,  # they're identical to each other, not to ref
            }

        ref_panels = _split_into_panels(reference_strip, N_FRAMES)
        panel_scores = [
            _ssim_panel_pair(ap, rp)
            for ap, rp in zip(agent_panels, ref_panels)
        ]
        page_score = _weighted_panel_mean(panel_scores)
        return {
            "score": page_score,
            "detail": (
                f"panel SSIMs early-weighted; iou={info['iou']:.3f}"
            ),
            "found": True,
            "iou": info["iou"],
            "panel_scores": panel_scores,
        }
    finally:
        if own_browser and pw is not None:
            pw.__exit__(None, None, None)


# ---------------------------------------------------------------------------
# Entry point (called by tests/animation_fidelity/check.py)
# ---------------------------------------------------------------------------


def score(
    agent_output_dir: Path | str,
    ground_truth_dir: Path | str,
    pages: list[str],
) -> dict[str, Any]:
    """Score animation fidelity across every page. Returns:

        {"score": float ∈ [0, 1],
         "per_page": {<page>: {"score": float, ...detail}},
         "detail": "<one-line mechanism summary>"}
    """
    from playwright.sync_api import sync_playwright  # noqa: PLC0415

    agent_dir = Path(agent_output_dir)
    gt_dir = Path(ground_truth_dir)

    per_page: dict[str, dict[str, Any]] = {}
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        for page_name in pages:
            agent_html = agent_dir / f"{page_name}.html"
            if not agent_html.is_file():
                per_page[page_name] = {
                    "score": 0.0,
                    "detail": f"agent HTML missing: {page_name}.html",
                }
                continue
            widget_meta = _load_widget_meta(gt_dir, page_name)
            if widget_meta is None:
                # No animation declared for this page in the reference —
                # neutral 1.0 (no expectation to meet) rather than 0.0.
                per_page[page_name] = {
                    "score": 1.0,
                    "detail": "no widget metadata in ground truth",
                }
                continue
            reference_strip = _load_reference_strip(gt_dir, page_name)
            if reference_strip is None:
                per_page[page_name] = {
                    "score": 0.0,
                    "detail": "reference motion-strip missing",
                }
                continue
            per_page[page_name] = score_page(
                agent_html, widget_meta, reference_strip, browser=browser,
            )
        browser.close()

    if per_page:
        overall = sum(p["score"] for p in per_page.values()) / len(per_page)
    else:
        overall = 0.0
    return {
        "score": overall,
        "per_page": per_page,
        "detail": (
            f"per-panel SSIM at {SSIM_WIDTH}px width, weights={PANEL_WEIGHTS}, "
            f"mean across {len(per_page)} page(s)"
        ),
    }


# ---------------------------------------------------------------------------
# CLI for ad-hoc invocation
# ---------------------------------------------------------------------------


def _main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("agent_output_dir")
    ap.add_argument("ground_truth_dir")
    ap.add_argument("--pages", default=None, help="comma-separated page names")
    args = ap.parse_args()

    gt = Path(args.ground_truth_dir)
    if args.pages:
        pages = [p.strip() for p in args.pages.split(",") if p.strip()]
    else:
        # Discover from widget/ dir if present, else from screenshots/desktop/
        widget_dir = gt / "widget"
        if widget_dir.is_dir():
            pages = sorted(p.stem for p in widget_dir.glob("*.json"))
        else:
            shots = gt / "screenshots" / "desktop"
            pages = sorted(p.name for p in shots.iterdir() if p.is_dir()) if shots.is_dir() else []

    result = score(args.agent_output_dir, args.ground_truth_dir, pages)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(_main())
