"""recipe/02-capture/capture.py

Render every page in a recipe run dir at 3 viewports, save screenshots
(full-page + viewport-height slices), produce a 5-frame motion strip that
shows the page's load-triggered entrance animation playing, and extract
the grader's reference values (bboxes, palette, typography, images, text)
from the settled (animation-frozen) DOM.

Ground-truth extraction is done **once at recipe time**. The grader at
eval time reads these JSONs and re-extracts the agent's values to compare.

Expected input layout (created by the builder, with assets nested under source/):

    <run_dir>/
      design.json                                       ← read for per-page animation
      source/{home,about,…}.html
      source/styles.css
      source/assets/{photos,icons,fonts,avatars}/...    ← vendored, nested in source/

Output layout (created here):

    <run_dir>/
      screenshots/
        desktop/{home,…}/
          full.png                ← full-page settled-state PNG (primary static reference)
          001.png, 002.png, …     ← viewport-height slices of full.png
          motion-strip.png        ← 5-panel horizontal strip showing the load animation
                                    at t=0/25/50/75/100 % of duration_ms. Each panel is
                                    full viewport width × (widget_h + 2·pad) tall,
                                    centred on the widget's row — preserves L/R context.
        tablet/{…}/{full,001,…,motion-strip}.png
        mobile/{…}/{full,001,…,motion-strip}.png
      ground_truth/
        bboxes/{desktop,tablet,mobile}/{home,…}.json   ← settled-state bboxes
        typography/{home,…}.json               ← desktop computed styles, area-weighted (settled)
        text/{home,…}.json                     ← visible DOM textContent (settled)
        images/{home,…}.json                   ← per-<img> bbox + pHash (from desktop full.png)
        palette/{home,…}.json                  ← k-means in LAB on desktop full.png

Usage:

    python recipe/02-capture/capture.py recipe/runs/task_1/
    python recipe/02-capture/capture.py recipe/runs/task_1/ --pages home,about
    python recipe/02-capture/capture.py recipe/runs/task_1/ --no-screenshots
    python recipe/02-capture/capture.py recipe/runs/task_1/ --no-motion-strip
    python recipe/02-capture/capture.py recipe/runs/task_1/ --no-precompute

Heavy imports (Playwright, scipy, sklearn, scikit-image, imagehash, Pillow)
are deferred until first use so this module imports cheaply for --help.
Requires: pip install playwright Pillow numpy scipy scikit-learn scikit-image
          imagehash; then `playwright install chromium`.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

# 1440×900 / 768×1024 / 375×812 — common desktop/tablet/mobile breakpoints.
VIEWPORTS: dict[str, tuple[int, int]] = {
    "desktop": (1440, 900),
    "tablet": (768, 1024),
    "mobile": (375, 812),
}

PALETTE_K = 8            # k-means cluster count
PALETTE_SAMPLES = 5000   # subsample for speed on big screenshots


# ---------------------------------------------------------------------------
# DOM extractors — JS snippets evaluated inside the page.
# All coords are document-relative (scrollY-adjusted) so they line up with
# the full-page screenshot.
# ---------------------------------------------------------------------------


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

_JS_TYPOGRAPHY = """
() => {
    const out = [];
    const walker = document.createTreeWalker(
        document.body,
        NodeFilter.SHOW_TEXT,
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

_JS_TEXT = """
() => ({ text: (document.body.innerText || '').trim() })
"""

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
            x: r.x + sx,
            y: r.y + sy,
            w: r.width,
            h: r.height,
        });
    }
    return out;
}
"""


# ---------------------------------------------------------------------------
# Pillow / sklearn / imagehash work (lazy imports)
# ---------------------------------------------------------------------------


def compute_image_signatures(images_meta: list[dict], full_page_png: Path) -> list[dict]:
    """Crop each image's bbox out of the full-page screenshot and record
    two signals for the image_content_fidelity grader:

      - `phash` — DCT perceptual hash (structural content).
      - `lab_mean` — mean CIE-LAB color of the crop (catches content
        swaps where structure is preserved but pixels aren't — e.g. a
        1×1 grey PNG stretched over an icon bbox, which has near-uniform
        pHash similar to the original icon but a wildly different mean
        color).

    LAB values are standard CIELAB. Pillow's `convert("LAB")` packs L
    into 0–255 (scale ×2.55) and stores a/b unoffset as signed values
    in a uint8 buffer (so a negative chroma reads as e.g. 240 for -16).
    We reinterpret the uint8 buffer as int8 for a/b before averaging,
    then scale L back to 0–100.
    """
    from PIL import Image
    import imagehash
    import numpy as np

    img = Image.open(full_page_png).convert("RGB")
    img_w, img_h = img.size
    out = []
    for im in images_meta:
        x = max(0, int(round(im["x"])))
        y = max(0, int(round(im["y"])))
        w = max(1, int(round(im["w"])))
        h = max(1, int(round(im["h"])))
        x2 = min(img_w, x + w)
        y2 = min(img_h, y + h)
        if x2 <= x or y2 <= y:
            out.append({**im, "phash": None, "lab_mean": None, "phash_error": "bbox outside screenshot"})
            continue
        try:
            crop = img.crop((x, y, x2, y2))
            ph = str(imagehash.phash(crop))
            lab_buf = np.asarray(crop.convert("LAB"))  # uint8, shape HxWx3
            # L: scale 0-255 → 0-100; a/b: reinterpret as signed int8.
            L_mean = float(lab_buf[..., 0].astype(np.float32).mean()) * 100.0 / 255.0
            a_mean = float(lab_buf[..., 1].view(np.int8).astype(np.float32).mean())
            b_mean = float(lab_buf[..., 2].view(np.int8).astype(np.float32).mean())
            lab_mean = [round(L_mean, 2), round(a_mean, 2), round(b_mean, 2)]
            out.append({**im, "phash": ph, "lab_mean": lab_mean})
        except Exception as e:
            out.append({**im, "phash": None, "lab_mean": None, "phash_error": f"{type(e).__name__}: {e}"})
    return out


def compute_palette(full_page_png: Path, k: int = PALETTE_K, n_samples: int = PALETTE_SAMPLES) -> list[dict]:
    """k-means in LAB color space on a subsample of pixels.

    Returns a list of clusters sorted by weight (largest first), each with
    the centroid as `hex` (sRGB) and `lab` (CIE-LAB), plus a `weight`
    fraction of pixels assigned to that cluster.
    """
    from PIL import Image
    import numpy as np
    from skimage.color import rgb2lab, lab2rgb
    from sklearn.cluster import KMeans

    img = np.array(Image.open(full_page_png).convert("RGB"), dtype=np.float32) / 255.0
    pixels = img.reshape(-1, 3)

    rng = np.random.default_rng(42)
    if len(pixels) > n_samples:
        idx = rng.choice(len(pixels), n_samples, replace=False)
        pixels = pixels[idx]

    lab = rgb2lab(pixels.reshape(1, -1, 3)).reshape(-1, 3)

    km = KMeans(n_clusters=k, random_state=42, n_init=10)
    km.fit(lab)

    sizes = np.bincount(km.labels_, minlength=k) / len(km.labels_)
    centers_rgb = lab2rgb(km.cluster_centers_.reshape(1, -1, 3)).reshape(-1, 3)
    centers_rgb = np.clip(centers_rgb, 0.0, 1.0)

    palette = []
    for hex_idx in range(k):
        r, g, b = (centers_rgb[hex_idx] * 255).astype(int)
        palette.append({
            "hex": f"#{r:02x}{g:02x}{b:02x}",
            "weight": float(sizes[hex_idx]),
            "lab": [float(v) for v in km.cluster_centers_[hex_idx]],
        })
    palette.sort(key=lambda p: -p["weight"])
    return palette


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------


def discover_pages(source_dir: Path) -> list[str]:
    return sorted(p.stem for p in source_dir.glob("*.html"))


def _write(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n")


def _wait_for_ready(page) -> None:
    """Wait for network-idle + web-font loading so the screenshot/extraction is stable."""
    page.wait_for_load_state("networkidle")
    try:
        page.evaluate(
            "() => (document.fonts && document.fonts.ready) ? document.fonts.ready : null"
        )
    except Exception:
        # Some pages have no document.fonts; ignore.
        pass


# Freezes every animation and transition to its end state, so settled-frame
# extraction returns stable bboxes / typography / palette regardless of
# loop / scroll-trigger / hover state. Applied AFTER the video recording.
_FREEZE_CSS = (
    "*, *::before, *::after { "
    "animation-duration: 0.001s !important; "
    "animation-delay: 0s !important; "
    "animation-iteration-count: 1 !important; "
    "animation-fill-mode: forwards !important; "
    "transition: none !important; "
    "}"
)


def load_animations(run_dir: Path) -> dict[str, dict[str, Any]]:
    """Read design.json and return {page_name: animation_object}.

    Each page now has exactly one animation per the schema (Part 2). If
    design.json is missing or a page omits animations, the page renders
    with no animation driver — the video still captures the page as-is.
    """
    design_path = run_dir / "design.json"
    if not design_path.is_file():
        return {}
    try:
        design = json.loads(design_path.read_text())
    except Exception:
        return {}
    out: dict[str, dict[str, Any]] = {}
    for page in design.get("pages") or []:
        anims = page.get("animations") or []
        if anims:
            out[page["name"]] = anims[0]
    return out


def _target_selector(animation: dict[str, Any] | None, page_name: str) -> str | None:
    """Selector the page builder agrees to: `.wdvb-anim-<type>-<page>`."""
    if animation is None:
        return None
    atype = animation.get("type")
    if not atype:
        return None
    return f".wdvb-anim-{atype}-{page_name}"


def _retrigger_class_animation(page, selector: str) -> None:
    """Replay a CSS @keyframes animation by removing and re-adding the
    `wdvb-anim-*` class. Needed because by the time `_wait_for_ready`
    returned (~500–1000 ms after navigation via `networkidle`), a
    1000–2000 ms load animation has already started — possibly even
    completed. Toggling the class forces a reflow and restarts the
    animation from `t=0` so motion-frame timing is deterministic."""
    try:
        page.evaluate(
            """(s) => {
              for (const el of document.querySelectorAll(s)) {
                const cls = Array.from(el.classList).find(c => c.startsWith('wdvb-anim-'));
                if (!cls) continue;
                el.classList.remove(cls);
                void el.offsetWidth;  // force reflow so the re-add restarts the animation
                el.classList.add(cls);
              }
            }""",
            selector,
        )
    except Exception:
        pass


def _freeze_animations(page) -> None:
    """Inject CSS that snaps every animation/transition to its end state.
    Used immediately before ground-truth extraction so bboxes / palette /
    typography are stable regardless of trigger type."""
    try:
        page.add_style_tag(content=_FREEZE_CSS)
    except Exception:
        # If injection fails (closed page / navigation), the extraction
        # downstream just sees an in-flight animation; not fatal.
        pass


def slice_into_chunks(full_path: Path, viewport_height: int) -> int:
    """Slice a full-page PNG into viewport-height chunks for the agent's
    reference. Chunks are saved as 001.png, 002.png, … alongside
    `full.png`. Returns the number of chunks written.

    The last chunk is shorter than `viewport_height` if the page doesn't
    divide evenly. We slice rather than re-render because slicing is
    ~100× faster and gives bit-identical content to the full-page
    screenshot (no risk of dynamic content shifting between captures).
    """
    from PIL import Image  # noqa: PLC0415 — lazy import

    img = Image.open(full_path)
    w, h = img.size
    out_dir = full_path.parent
    n = 0
    for i, y0 in enumerate(range(0, h, viewport_height), start=1):
        y1 = min(y0 + viewport_height, h)
        chunk = img.crop((0, y0, w, y1))
        chunk.save(out_dir / f"{i:03d}.png")
        n += 1
    return n


def _query_widget_rect(page, selector: str) -> dict[str, float] | None:
    """Query the widget's current bbox via `getBoundingClientRect()`.
    Returns `{x, y, w, h}` (viewport-relative) or None if the selector
    matches nothing or matches an element with zero area.

    Callers MUST query at a known state — typically after waiting past
    the initial load animation so `animation-fill-mode: both` has held
    the widget at its end-state with no in-flight transform — otherwise
    `getBoundingClientRect()` reflects the transformed (possibly
    off-screen) position.
    """
    if not selector:
        return None
    try:
        rect = page.evaluate(
            "(s) => { const el = document.querySelector(s); "
            "if (!el) return null; const r = el.getBoundingClientRect(); "
            "return {x: r.x, y: r.y, w: r.width, h: r.height}; }",
            selector,
        )
    except Exception:
        return None
    if not rect or rect.get("w", 0) <= 0 or rect.get("h", 0) <= 0:
        return None
    return {
        "x": float(rect["x"]),
        "y": float(rect["y"]),
        "w": float(rect["w"]),
        "h": float(rect["h"]),
    }


def _band_from_rect(
    rect: dict[str, float] | None,
    viewport_w: int,
    viewport_h: int,
    *,
    pad_px: int = 80,
) -> tuple[int, int, int, int] | None:
    """Convert a widget rect into a horizontal band — full viewport
    width × (widget_h + 2*pad_px), centred on the widget's row, clamped
    to the viewport. The band preserves L/R context so the agent can see
    where the widget sits on the page, not just the widget in isolation.

    Returns None for a degenerate (zero-area / off-screen) rect.
    """
    if rect is None:
        return None
    cy = rect["y"] + rect["h"] / 2.0
    half_h = rect["h"] / 2.0 + pad_px
    y0 = max(0, int(cy - half_h))
    y1 = min(viewport_h, int(cy + half_h))
    if y1 <= y0:
        return None
    return (0, y0, viewport_w, y1)


def _make_motion_strip(
    page,
    animation: dict[str, Any] | None,
    page_name: str,
    out_path: Path,
    viewport_w: int,
    viewport_h: int,
    *,
    widget_rect: dict[str, float] | None = None,
    n_frames: int = 5,
) -> None:
    """Replay the page's load-triggered entrance animation and stitch
    `n_frames` viewport-band crops left-to-right into a single PNG at
    `out_path`. Frames are sampled at t = 0 % / 25 % / 50 % / 75 %
    / 100 % of `duration_ms` (for n_frames=5). The last frame is at the
    animation's end state.

    The crop is a horizontal band — full viewport width × widget_h + 2·pad_px,
    centred on the widget — so the agent sees both the moving element
    AND its surrounding row of context. If the selector finds nothing,
    falls back to full-viewport frames so packaging downstream still
    has the artifact.

    `widget_rect` is the pre-queried settled-state bbox. If None, the
    function queries it itself (after waiting past the load animation).
    Pass the cached value when the caller has already settled the page.
    """
    from PIL import Image  # noqa: PLC0415 — lazy import
    import io  # noqa: PLC0415

    out_path.parent.mkdir(parents=True, exist_ok=True)
    sel = _target_selector(animation, page_name)
    duration_ms = int((animation or {}).get("duration_ms") or 1300)

    # If the caller didn't pre-query, wait past the initial load animation
    # so animation-fill-mode: both has settled the widget, then query.
    # Querying during animation flight returns the transformed (possibly
    # off-screen) bbox, which collapses the band to a degenerate strip.
    if widget_rect is None and sel:
        page.wait_for_timeout(max(duration_ms + 200, 1500))
        widget_rect = _query_widget_rect(page, sel)

    band = _band_from_rect(widget_rect, viewport_w, viewport_h)
    if band is None:
        band = (0, 0, viewport_w, viewport_h)

    # Now replay the animation from t=0 so frame timings are deterministic.
    if sel:
        _retrigger_class_animation(page, sel)
        page.wait_for_timeout(20)  # reflow + restart

    # Sample n_frames at evenly-spaced offsets through duration_ms.
    offsets_ms = [
        int(duration_ms * (i / (n_frames - 1))) if n_frames > 1 else duration_ms
        for i in range(n_frames)
    ]
    crops: list[Image.Image] = []
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
        return

    # Stitch left-to-right. All crops share the same band geometry, so
    # heights match; widths sum.
    strip_h = max(c.height for c in crops)
    total_w = sum(c.width for c in crops)
    strip = Image.new("RGB", (total_w, strip_h), color=(255, 255, 255))
    cursor_x = 0
    for c in crops:
        strip.paste(c, (cursor_x, 0))
        cursor_x += c.width

    # Red marker outline around the widget's settled position on each
    # panel. The marker stays fixed even though the widget moves through
    # the animation states — disambiguates which element on the page is
    # the moving subject when multiple candidates sit within the band.
    if widget_rect is not None:
        from PIL import ImageDraw  # noqa: PLC0415 — lazy import
        draw = ImageDraw.Draw(strip)
        panel_w = band[2] - band[0]
        mx = widget_rect["x"]                 # band x0 is always 0
        my = widget_rect["y"] - band[1]       # offset from band y0
        mw, mh = widget_rect["w"], widget_rect["h"]
        for i in range(len(crops)):
            x0 = int(i * panel_w + mx)
            y0 = int(my)
            x1 = int(x0 + mw)
            y1 = int(y0 + mh)
            draw.rectangle((x0, y0, x1, y1), outline=(255, 0, 0), width=3)

    strip.save(out_path)


def process_page(
    browser,
    source_dir: Path,
    run_dir: Path,
    page_name: str,
    animation: dict[str, Any] | None,
    *,
    do_screenshots: bool,
    do_motion_strip: bool,
    do_precompute: bool,
) -> dict[str, Any]:
    """Render `page_name` at each viewport and produce the per-task
    reference artifacts:

      * `<run>/screenshots/<vp>/<page>/full.png` (full-page settled PNG)
      * `<run>/screenshots/<vp>/<page>/001.png`, `002.png`, … (slices)
      * `<run>/screenshots/<vp>/<page>/motion-strip.png` (5-panel strip)
      * `<run>/ground_truth/{bboxes,typography,text,images,palette}/…`
    """
    html_path = source_dir / f"{page_name}.html"
    if not html_path.exists():
        return {"page": page_name, "error": f"missing {html_path}"}

    result: dict[str, Any] = {"page": page_name, "viewports": {}}
    desktop_full: Path | None = None
    images_meta: list[dict] | None = None
    file_url = f"file://{html_path.resolve()}"

    for vp_name, (w, h) in VIEWPORTS.items():
        screenshots_dir = run_dir / "screenshots" / vp_name / page_name
        screenshots_dir.mkdir(parents=True, exist_ok=True)

        ctx = browser.new_context(viewport={"width": w, "height": h})
        page = ctx.new_page()
        page.goto(file_url)
        _wait_for_ready(page)

        # Wait past the initial load animation so animation-fill-mode:
        # both has settled the widget at its layout position. Then query
        # the widget's settled bbox once — used both by the motion-strip
        # band geometry AND by ground_truth/widget/<page>.json (consumed
        # by the animation_fidelity grader).
        widget_sel = _target_selector(animation, page_name) if animation else None
        widget_rect: dict[str, float] | None = None
        if widget_sel:
            duration_ms = int(animation.get("duration_ms") or 1300)
            page.wait_for_timeout(max(duration_ms + 200, 1500))
            widget_rect = _query_widget_rect(page, widget_sel)

        # Save per-page widget metadata once, from the desktop viewport
        # — the animation_fidelity grader's reference bbox.
        if (
            do_precompute
            and vp_name == "desktop"
            and animation is not None
            and widget_rect is not None
        ):
            _write(
                run_dir / "ground_truth" / "widget" / f"{page_name}.json",
                {
                    "selector": widget_sel,
                    "bbox": [
                        widget_rect["x"],
                        widget_rect["y"],
                        widget_rect["w"],
                        widget_rect["h"],
                    ],
                    "type": animation.get("type"),
                    "duration_ms": animation.get("duration_ms"),
                    "easing": animation.get("easing"),
                },
            )

        # Motion strip — pass the already-queried widget rect so we
        # don't re-wait + re-query.
        if do_motion_strip:
            strip_path = screenshots_dir / "motion-strip.png"
            _make_motion_strip(
                page, animation, page_name, strip_path, w, h,
                widget_rect=widget_rect,
            )
            result["viewports"].setdefault(vp_name, {})["motion_strip"] = strip_path.name

        # Freeze everything so the settled PNG + ground-truth JSON
        # extraction work against a stable DOM. Even if the strip
        # left the animation at its end state, a tail transition may
        # still be in-flight.
        _freeze_animations(page)
        page.wait_for_timeout(50)

        if do_precompute:
            bboxes = page.evaluate(_JS_BBOXES)
            _write(run_dir / "ground_truth" / "bboxes" / vp_name / f"{page_name}.json", bboxes)
            result["viewports"].setdefault(vp_name, {})["bboxes"] = len(bboxes)

            if vp_name == "desktop":
                typo = page.evaluate(_JS_TYPOGRAPHY)
                _write(run_dir / "ground_truth" / "typography" / f"{page_name}.json", typo)
                result["typography_nodes"] = len(typo)

                text = page.evaluate(_JS_TEXT)
                _write(run_dir / "ground_truth" / "text" / f"{page_name}.json", text)
                result["text_chars"] = len(text.get("text", ""))

                images_meta = page.evaluate(_JS_IMAGES)
                result["images_count"] = len(images_meta)

        # Full-page settled screenshot + viewport-height slices.
        if do_screenshots:
            full_path = screenshots_dir / "full.png"
            page.screenshot(path=str(full_path), full_page=True)
            n_chunks = slice_into_chunks(full_path, viewport_height=h)
            result["viewports"].setdefault(vp_name, {})["chunks"] = n_chunks
            if vp_name == "desktop":
                desktop_full = full_path

        page.close()
        ctx.close()

    # Image signatures + palette derive from the desktop full-page PNG.
    if do_precompute and desktop_full and images_meta is not None:
        images_with_signatures = compute_image_signatures(images_meta, desktop_full)
        _write(run_dir / "ground_truth" / "images" / f"{page_name}.json", images_with_signatures)

        palette = compute_palette(desktop_full)
        _write(run_dir / "ground_truth" / "palette" / f"{page_name}.json", palette)
        result["palette_size"] = len(palette)

    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__.split("\n")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("run_dir", help="path to a recipe/runs/task_N/ directory (must contain source/*.html)")
    ap.add_argument("--pages", default=None, help="comma-separated page names (default: every *.html in source/)")
    ap.add_argument("--no-screenshots", action="store_true", help="skip full.png + viewport slices")
    ap.add_argument("--no-motion-strip", action="store_true", help="skip motion-strip.png generation")
    ap.add_argument("--no-precompute", action="store_true", help="skip ground-truth extraction; only screenshots/strip")
    args = ap.parse_args()

    run_dir = Path(args.run_dir)
    source_dir = run_dir / "source"
    if not source_dir.is_dir():
        print(f"error: {source_dir} does not exist", file=sys.stderr)
        return 2

    pages = (
        [p.strip() for p in args.pages.split(",") if p.strip()]
        if args.pages
        else discover_pages(source_dir)
    )
    if not pages:
        print(f"error: no .html files in {source_dir}", file=sys.stderr)
        return 2

    do_screenshots = not args.no_screenshots
    do_motion_strip = not args.no_motion_strip
    do_precompute = not args.no_precompute
    if not (do_screenshots or do_motion_strip or do_precompute):
        print("error: nothing to do — all three pipelines disabled", file=sys.stderr)
        return 2

    animations = load_animations(run_dir)

    print(
        f"capturing {len(pages)} page(s) × {len(VIEWPORTS)} viewport(s) "
        f"from {source_dir}  (screenshots={do_screenshots}, "
        f"motion_strip={do_motion_strip}, precompute={do_precompute}, "
        f"animations_known={len(animations)})"
    )

    # Lazy-import Playwright so --help doesn't pay for it.
    from playwright.sync_api import sync_playwright

    t_total = time.time()
    with sync_playwright() as p:
        browser = p.chromium.launch()
        for page_name in pages:
            t_page = time.time()
            try:
                result = process_page(
                    browser, source_dir, run_dir, page_name,
                    animations.get(page_name),
                    do_screenshots=do_screenshots,
                    do_motion_strip=do_motion_strip,
                    do_precompute=do_precompute,
                )
            except Exception as e:
                print(f"  {page_name}: ERROR {type(e).__name__}: {e}", file=sys.stderr)
                continue
            elapsed = time.time() - t_page

            if "error" in result:
                print(f"  {page_name}: {result['error']}")
                continue

            vp_parts = []
            for vp, v in result.get("viewports", {}).items():
                pieces = []
                if "chunks" in v:
                    pieces.append(f"{v['chunks']}ch")
                if "motion_strip" in v:
                    pieces.append("strip")
                if "bboxes" in v:
                    pieces.append(f"{v['bboxes']}b")
                vp_parts.append(f"{vp}=[{','.join(pieces)}]")
            vp_str = ", ".join(vp_parts) or "no-output"
            extras = ""
            if do_precompute:
                extras = (
                    f" | typo={result.get('typography_nodes', '-')}"
                    f" text={result.get('text_chars', '-')}c"
                    f" imgs={result.get('images_count', '-')}"
                    f" palette={result.get('palette_size', '-')}"
                )
            print(f"  {page_name}: {elapsed:>4.1f}s | {vp_str}{extras}")
        browser.close()

    print(f"\ndone in {time.time() - t_total:.1f}s → {run_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
