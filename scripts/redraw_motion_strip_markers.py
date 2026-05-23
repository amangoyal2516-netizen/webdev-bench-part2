#!/usr/bin/env python3
"""Backfill the red widget-marker onto existing motion strips.

Why this exists
---------------
`recipe/02-capture/capture.py` was patched to draw a red rectangle
outline around the animated widget on each panel of the motion strip.
Existing tasks were generated before that change. Re-running the full
capture pipeline would also regenerate full.png + slices, which is
wasteful since the source HTML didn't change.

This script post-processes the existing motion-strip.png files in
place: it re-queries the widget bbox per viewport via Playwright (the
widget JSON only stores the desktop bbox), then draws 5 red rectangle
outlines on the existing strip — one per panel, at the widget's
settled position relative to the band. Idempotent (skip if the strip
already shows a red marker pixel where we expect one).

Usage:

    python3 scripts/redraw_motion_strip_markers.py
    python3 scripts/redraw_motion_strip_markers.py --tasks task_1,task_3
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from PIL import Image, ImageDraw
from playwright.async_api import async_playwright

REPO_ROOT = Path(__file__).resolve().parent.parent

VIEWPORTS: dict[str, tuple[int, int]] = {
    "desktop": (1440, 900),
    "tablet": (768, 1024),
    "mobile": (375, 812),
}

PAD_PX = 80
N_FRAMES = 5
MARKER_RGB = (255, 0, 0)
MARKER_WIDTH = 3


def _band_from_rect(
    rect: dict, viewport_w: int, viewport_h: int, *, pad_px: int = PAD_PX,
) -> tuple[int, int, int, int] | None:
    """Mirror of recipe/02-capture/capture.py:_band_from_rect."""
    if rect is None:
        return None
    cy = rect["y"] + rect["h"] / 2.0
    half_h = rect["h"] / 2.0 + pad_px
    y0 = max(0, int(cy - half_h))
    y1 = min(viewport_h, int(cy + half_h))
    if y1 <= y0:
        return None
    return (0, y0, viewport_w, y1)


def _already_marked(strip_path: Path, in_panel_marker_xy: tuple[int, int]) -> bool:
    """Cheap idempotency check: sample a few pixels along the expected
    rectangle outline. Avoid re-drawing if the strip already carries the
    marker."""
    try:
        img = Image.open(strip_path).convert("RGB")
    except Exception:
        return False
    mx, my = in_panel_marker_xy
    # Sample 3 pixels on the top edge of the marker outline.
    for dx in (0, 1, 2):
        try:
            px = img.getpixel((mx + dx, my))
        except IndexError:
            return False
        # Marker is solid red — accept any pixel where R >> G,B.
        if px[0] >= 200 and px[1] <= 80 and px[2] <= 80:
            return True
    return False


def _draw_markers(
    strip_path: Path,
    widget_rect: dict,
    band: tuple[int, int, int, int],
    n_frames: int = N_FRAMES,
) -> None:
    """Draw `n_frames` red rectangle outlines on the existing strip, one
    per panel. Overwrites the file in place."""
    img = Image.open(strip_path).convert("RGB")
    draw = ImageDraw.Draw(img)
    panel_w = band[2] - band[0]
    mx = widget_rect["x"]
    my = widget_rect["y"] - band[1]
    mw, mh = widget_rect["w"], widget_rect["h"]
    for i in range(n_frames):
        x0 = int(i * panel_w + mx)
        y0 = int(my)
        x1 = int(x0 + mw)
        y1 = int(y0 + mh)
        draw.rectangle((x0, y0, x1, y1), outline=MARKER_RGB, width=MARKER_WIDTH)
    img.save(strip_path)


async def _query_widget_at_viewport(
    browser, html_path: Path, viewport: tuple[int, int],
    selector: str, duration_ms: int,
) -> dict | None:
    """Open the HTML at `viewport`, wait for the animation to settle,
    return the widget's getBoundingClientRect()."""
    ctx = await browser.new_context(viewport={"width": viewport[0], "height": viewport[1]})
    page = await ctx.new_page()
    try:
        await page.goto(f"file://{html_path}")
        await page.wait_for_load_state("networkidle")
        await page.wait_for_timeout(max(duration_ms + 200, 1500))
        rect = await page.evaluate(
            "(s) => { const el = document.querySelector(s); "
            "if (!el) return null; const r = el.getBoundingClientRect(); "
            "return {x: r.x, y: r.y, w: r.width, h: r.height}; }",
            selector,
        )
    finally:
        await ctx.close()
    if not rect or rect.get("w", 0) <= 0 or rect.get("h", 0) <= 0:
        return None
    return {k: float(rect[k]) for k in ("x", "y", "w", "h")}


async def process_task(browser, task_id: str) -> dict[str, int]:
    """Backfill markers for one task across all (viewport, page) combos."""
    workspace = REPO_ROOT / "tasks" / "_workspaces" / task_id
    packaged = REPO_ROOT / "tasks" / f"{task_id}-oneshot"
    task_cfg = json.loads((packaged / "task_config.json").read_text())
    pages: list[str] = task_cfg["pages"]
    widget_dir = workspace / "ground_truth" / "widget"
    src_dir = workspace / "source"

    stats = {"drawn": 0, "skipped": 0, "missing_widget": 0, "missing_strip": 0,
             "no_bbox": 0, "no_band": 0}

    for page_name in pages:
        widget_json = widget_dir / f"{page_name}.json"
        if not widget_json.exists():
            print(f"  [{task_id}/{page_name}] no widget JSON — skipping")
            stats["missing_widget"] += 1
            continue
        meta = json.loads(widget_json.read_text())
        selector = meta["selector"]
        duration_ms = int(meta.get("duration_ms") or 1300)
        html_path = src_dir / f"{page_name}.html"

        for vp_name, (vp_w, vp_h) in VIEWPORTS.items():
            # Both copies stay in sync: packaged + workspace.
            packaged_strip = (packaged / "environment" / "ground_truth"
                              / "screenshots" / vp_name / page_name / "motion-strip.png")
            workspace_strip = (workspace / "screenshots"
                               / vp_name / page_name / "motion-strip.png")
            if not packaged_strip.exists():
                stats["missing_strip"] += 1
                continue

            widget_rect = await _query_widget_at_viewport(
                browser, html_path, (vp_w, vp_h), selector, duration_ms,
            )
            if widget_rect is None:
                print(f"  [{task_id}/{vp_name}/{page_name}] no bbox — skipping")
                stats["no_bbox"] += 1
                continue
            band = _band_from_rect(widget_rect, vp_w, vp_h)
            if band is None:
                stats["no_band"] += 1
                continue

            mx = int(widget_rect["x"])
            my = int(widget_rect["y"] - band[1])
            if _already_marked(packaged_strip, (mx, my)):
                stats["skipped"] += 1
                continue

            _draw_markers(packaged_strip, widget_rect, band)
            if workspace_strip.exists():
                # Copy the marked strip to the workspace too.
                Image.open(packaged_strip).save(workspace_strip)
            stats["drawn"] += 1
            print(f"  [{task_id}/{vp_name}/{page_name}] marker drawn")

    return stats


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--tasks", default="task_1,task_2,task_3,task_4",
                    help="comma-separated task IDs to backfill (default: all 4)")
    args = ap.parse_args()
    task_ids = [t.strip() for t in args.tasks.split(",") if t.strip()]

    totals = {"drawn": 0, "skipped": 0, "missing_widget": 0,
              "missing_strip": 0, "no_bbox": 0, "no_band": 0}

    async with async_playwright() as p:
        browser = await p.chromium.launch()
        try:
            for tid in task_ids:
                print(f"\n=== {tid} ===")
                s = await process_task(browser, tid)
                for k, v in s.items():
                    totals[k] += v
                print(f"  → drawn={s['drawn']}, skipped={s['skipped']}, "
                      f"missing={s['missing_widget']+s['missing_strip']}, "
                      f"no_bbox={s['no_bbox']}")
        finally:
            await browser.close()

    print(f"\n=== totals ===")
    for k, v in totals.items():
        print(f"  {k}: {v}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
