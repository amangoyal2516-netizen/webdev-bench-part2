#!/usr/bin/env python3
"""Render a Harbor job's results as a self-contained visual HTML report.

The output is a single HTML file with a tabbed UI:

  ┌────────────────────────────────────────────────────────────────┐
  │ webdev-bench eval report · <job-name>                          │
  │ Track A mean … · Track B mean … · trials: N (ok / failed)      │
  ├────────────────────────────────────────────────────────────────┤
  │ [Dashboard] [task_3-oneshot] [task_13-oneshot] [task_29-…] …   │
  ├────────────────────────────────────────────────────────────────┤
  │                                                                │
  │  Dashboard (default):                                          │
  │    - one row per trial: A / B / |Δ| / gate / status            │
  │    - per-criterion means across all completed trials           │
  │                                                                │
  │  Per-trial tabs:                                               │
  │    - agent info, scoreboard, per-criterion bars                │
  │    - reference vs. agent screenshot pairs per page             │
  │    (failed trials show the error reason + any captured logs)   │
  │                                                                │
  └────────────────────────────────────────────────────────────────┘

Tabs are pure CSS (hidden radio inputs + `:checked ~ .panels`) so the
report is self-contained — no JS dependency.

Usage:
    python eval/reports/render_report.py jobs/<job-name>/
    python eval/reports/render_report.py jobs/<job-name>/ --output report.html
    python eval/reports/render_report.py jobs/<job-name>/ --viewport tablet
"""

from __future__ import annotations

import argparse
import atexit
import base64
import io
import json
import re
import sys
from pathlib import Path
from typing import Any

try:
    from PIL import Image
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

CRITERIA = [
    "layout_structure",
    "component_presence",
    "color_palette",
    "typography",
    "image_content_fidelity",
    "visible_text_fidelity",
    "animation_fidelity",
]

VIEWPORT_SIZES = {
    "desktop": (1440, 900),
    "tablet": (768, 1024),
    "mobile": (375, 812),
}

THUMB_WIDTH = 600
JPEG_QUALITY = 75

# Per-viewport thumbnail width (px). Mobile/tablet thumbnails embed at
# roughly their native render width — no point uploading a 1440-wide
# thumbnail of a 375-wide mobile screenshot. Smaller thumbs => smaller
# self-contained HTML (a 3-trial report with 3 viewports drops from
# ~16 MB to ~5 MB).
VIEWPORT_THUMB_WIDTH = {
    "desktop": 600,
    "tablet": 400,
    "mobile": 300,
}


# ── Playwright browser pool (lazy, shared across all renders) ────────────────


_browser_state: dict[str, Any] = {"playwright": None, "browser": None}


def _get_browser():
    if _browser_state["browser"] is not None:
        return _browser_state["browser"]
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None
    pw = sync_playwright().start()
    browser = pw.chromium.launch()
    _browser_state["playwright"] = pw
    _browser_state["browser"] = browser
    atexit.register(_close_browser)
    return browser


def _close_browser() -> None:
    if _browser_state["browser"] is not None:
        try:
            _browser_state["browser"].close()
        except Exception:
            pass
        _browser_state["browser"] = None
    if _browser_state["playwright"] is not None:
        try:
            _browser_state["playwright"].stop()
        except Exception:
            pass
        _browser_state["playwright"] = None


def _render_html_to_png(html_path: Path, png_path: Path, viewport: str) -> bool:
    if png_path.exists():
        return True
    browser = _get_browser()
    if browser is None:
        return False
    w, h = VIEWPORT_SIZES.get(viewport, (1440, 900))
    try:
        ctx = browser.new_context(viewport={"width": w, "height": h})
        page = ctx.new_page()
        page.goto(f"file://{html_path.resolve()}")
        try:
            page.wait_for_load_state("networkidle", timeout=10_000)
        except Exception:
            pass
        png_path.parent.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(png_path), full_page=True)
        ctx.close()
        return True
    except Exception as e:
        print(f"  warn: render {viewport}/{html_path.stem} failed: {type(e).__name__}: {e}", file=sys.stderr)
        return False


# ── animated WebP helpers (desktop animation cells in the report) ───────────


ANIM_TARGET_WIDTH = 600  # px — match the desktop screenshot thumb width
ANIM_N_FRAMES = 5
ANIM_PAD_PX = 80  # mirrors recipe/02-capture/capture.py:_band_from_rect


def _band_from_rect(
    rect: dict, viewport_w: int, viewport_h: int, pad_px: int = ANIM_PAD_PX,
) -> tuple[int, int, int, int] | None:
    """Mirror of recipe/02-capture/capture.py:_band_from_rect — full
    viewport width × widget_h+2·pad centred on the widget's row. Used to
    crop agent frames to the same band as the reference."""
    if rect is None:
        return None
    cy = rect["y"] + rect["h"] / 2.0
    half_h = rect["h"] / 2.0 + pad_px
    y0 = max(0, int(cy - half_h))
    y1 = min(viewport_h, int(cy + half_h))
    if y1 <= y0:
        return None
    return (0, y0, viewport_w, y1)


def _load_widget_meta(task_path: Path, page: str) -> dict | None:
    """Read per-page widget JSON staged into the packaged task at
    tests/ground_truth/widget/<page>.json by recipe/03-package/."""
    p = task_path / "tests" / "ground_truth" / "widget" / f"{page}.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except Exception:
        return None


def _encode_frames_as_webp_data_uri(
    frames: list, duration_ms: int, target_width: int = ANIM_TARGET_WIDTH,
) -> str | None:
    """Encode `frames` (list of PIL.Image) as a looping animated WebP
    base64 data URI. Per-frame duration is `duration_ms / len(frames)`
    so the loop plays at the animation's authored speed."""
    if not HAS_PIL or not frames:
        return None
    try:
        # Scale all to target_width keeping aspect ratio.
        scaled = []
        for f in frames:
            if f.width != target_width:
                ratio = target_width / f.width
                new_size = (target_width, max(1, int(f.height * ratio)))
                scaled.append(f.convert("RGB").resize(new_size, Image.LANCZOS))
            else:
                scaled.append(f.convert("RGB"))
        per_frame_ms = max(50, duration_ms // len(scaled))
        buf = io.BytesIO()
        scaled[0].save(
            buf, format="WebP", save_all=True, append_images=scaled[1:],
            duration=per_frame_ms, loop=0, quality=70, method=4,
        )
        return f"data:image/webp;base64,{base64.b64encode(buf.getvalue()).decode()}"
    except Exception as e:
        print(f"  warn: WebP encode failed: {type(e).__name__}: {e}", file=sys.stderr)
        return None


def _slice_motion_strip(strip_path: Path, n_frames: int = ANIM_N_FRAMES) -> list:
    """Slice an existing reference motion-strip.png (5 panels stitched
    horizontally) back into a list of PIL.Image frames."""
    if not HAS_PIL or not strip_path.exists():
        return []
    try:
        img = Image.open(strip_path).convert("RGB")
        panel_w = img.width // n_frames
        return [img.crop((i * panel_w, 0, (i + 1) * panel_w, img.height))
                for i in range(n_frames)]
    except Exception as e:
        print(f"  warn: slice {strip_path}: {type(e).__name__}: {e}", file=sys.stderr)
        return []


def _capture_agent_animation_frames(
    html_path: Path, viewport: tuple[int, int],
    band: tuple[int, int, int, int], duration_ms: int,
    n_frames: int = ANIM_N_FRAMES,
) -> list:
    """Render the agent's HTML at `viewport`, retrigger any entrance
    animations, sample `n_frames` screenshots at evenly-spaced offsets
    through `duration_ms`, crop each to `band`. Returns a list of
    PIL.Image frames (band-cropped). Empty list on failure."""
    if not HAS_PIL:
        return []
    browser = _get_browser()
    if browser is None:
        return []
    try:
        ctx = browser.new_context(viewport={"width": viewport[0], "height": viewport[1]})
        page = ctx.new_page()
        page.goto(f"file://{html_path.resolve()}")
        try:
            page.wait_for_load_state("networkidle", timeout=10_000)
        except Exception:
            pass
        # Wait past any initial animation so we can cleanly retrigger.
        page.wait_for_timeout(max(duration_ms + 200, 1500))
        # Retrigger every animated element by toggling its anim/wdvb-anim
        # classes — restarts the keyframe from t=0. Works for both the
        # reference's `wdvb-anim-*` and agent variations like `anim-*`.
        page.evaluate("""() => {
            const targets = [];
            for (const el of document.querySelectorAll('*')) {
                const cs = window.getComputedStyle(el);
                if (cs.animationName && cs.animationName !== 'none') {
                    targets.push(el);
                }
            }
            for (const el of targets) {
                const all = [...el.classList];
                const anim = all.filter(c => /anim|wdvb/i.test(c));
                for (const c of anim) el.classList.remove(c);
                void el.offsetWidth;  // force reflow
                for (const c of anim) el.classList.add(c);
            }
        }""")
        page.wait_for_timeout(20)
        offsets_ms = [int(duration_ms * (i / (n_frames - 1))) if n_frames > 1 else duration_ms
                      for i in range(n_frames)]
        frames = []
        prev = 0
        for t in offsets_ms:
            delta = max(t - prev, 0)
            if delta > 0:
                page.wait_for_timeout(delta)
            prev = t
            try:
                shot = page.screenshot(full_page=False)
            except Exception:
                continue
            img = Image.open(io.BytesIO(shot)).convert("RGB")
            frames.append(img.crop(band))
        ctx.close()
        return frames
    except Exception as e:
        print(f"  warn: agent anim capture {html_path.stem}: {type(e).__name__}: {e}", file=sys.stderr)
        return []


def make_reference_animation_webp(
    task_path: Path, page: str, viewport: str, duration_ms: int,
) -> str | None:
    """Build the reference's animated WebP for the per-page cell. Slices
    the already-captured motion-strip.png back into 5 panels and
    re-encodes them as an animated WebP. No re-render needed."""
    strip = (task_path / "environment" / "ground_truth" / "screenshots"
             / viewport / page / "motion-strip.png")
    frames = _slice_motion_strip(strip)
    if not frames:
        return None
    target_w = VIEWPORT_THUMB_WIDTH.get(viewport, THUMB_WIDTH)
    return _encode_frames_as_webp_data_uri(frames, duration_ms, target_w)


def make_agent_animation_webp(
    trial_dir: Path, task_path: Path, page: str, viewport: str,
) -> str | None:
    """Build the agent's animated WebP. Loads the agent's HTML from
    artifacts/output, re-renders in Playwright at the target viewport,
    crops each frame to the same band as the reference (so the two
    GIFs are visually comparable), encodes as animated WebP."""
    html = trial_dir / "artifacts" / "output" / f"{page}.html"
    if not html.exists():
        return None
    meta = _load_widget_meta(task_path, page)
    if meta is None:
        return None
    duration_ms = int(meta.get("duration_ms") or 1300)
    bbox = meta.get("bbox") or []
    if len(bbox) != 4:
        return None
    ref_rect = {"x": bbox[0], "y": bbox[1], "w": bbox[2], "h": bbox[3]}
    vp_w, vp_h = VIEWPORT_SIZES.get(viewport, (1440, 900))
    band = _band_from_rect(ref_rect, vp_w, vp_h)
    if band is None:
        return None
    frames = _capture_agent_animation_frames(html, (vp_w, vp_h), band, duration_ms)
    if not frames:
        return None
    target_w = VIEWPORT_THUMB_WIDTH.get(viewport, THUMB_WIDTH)
    return _encode_frames_as_webp_data_uri(frames, duration_ms, target_w)


def render_animation_cell(label: str, webp_uri: str | None) -> str:
    """A figure cell holding an animated WebP. Same shape as
    render_screenshot_cell so they line up in the same .pair grid."""
    if webp_uri is None:
        return (
            f'<figure class="ss missing">'
            f'<figcaption>{label}</figcaption>'
            f'<div class="placeholder">no animation</div>'
            f"</figure>"
        )
    return (
        f'<figure class="ss">'
        f'<figcaption>{label}</figcaption>'
        f'<img src="{webp_uri}" loading="lazy" />'
        f"</figure>"
    )


# ── data loading ────────────────────────────────────────────────────────────


def load_trials(job_dir: Path) -> list[dict[str, Any]]:
    """Find every trial subdir of `job_dir` and load whatever artifacts exist.

    Includes failed trials (no reward.json) — they get a `failed_reason`
    field and surface on the dashboard / their own tab with the error.
    """
    trials: list[dict[str, Any]] = []
    for child in sorted(job_dir.iterdir()):
        if not child.is_dir():
            continue
        if child.name.startswith("_") or child.name in {"agent", "verifier", "artifacts"}:
            continue
        # Heuristic: trial dirs always have a config.json. Job-level
        # files like result.json / lock.json are siblings, not subdirs,
        # so this guards against any other directory shape ending up here.
        config_path = child / "config.json"
        if not config_path.exists():
            continue

        trial: dict[str, Any] = {"trial_dir": child, "name": child.name}
        trial["config"] = json.loads(config_path.read_text())
        reward_path = child / "verifier" / "reward.json"
        grading_path = child / "verifier" / "grading.json"
        if reward_path.exists():
            trial["reward"] = json.loads(reward_path.read_text())
        if grading_path.exists():
            trial["grading"] = json.loads(grading_path.read_text())
        if "reward" not in trial:
            trial["failed_reason"] = _failure_reason(child)
        trials.append(trial)
    return trials


def _failure_reason(trial_dir: Path) -> str:
    """Best-effort: grep the trial.log for a 'failed:' or 'timed out' line."""
    log = trial_dir / "trial.log"
    if not log.is_file():
        return "trial.log missing"
    try:
        text = log.read_text(errors="replace")
    except Exception:
        return "trial.log unreadable"
    for line in reversed(text.splitlines()):
        if "failed:" in line or "timed out" in line or "TimeoutError" in line:
            return line.strip()
    return "completed without writing reward.json (no explicit failure line in trial.log)"


def task_path_from_trial(trial: dict[str, Any]) -> Path | None:
    config = trial.get("config", {})
    task_path = (config.get("task") or {}).get("path")
    if not task_path:
        return None
    p = Path(task_path)
    if not p.is_absolute():
        p = (Path.cwd() / p).resolve()
    return p if p.exists() else None


def discover_pages(task_path: Path, viewport: str) -> list[str]:
    base = task_path / "environment" / "ground_truth" / "screenshots" / viewport
    if not base.is_dir():
        return []
    return sorted(p.name for p in base.iterdir() if p.is_dir())


def find_reference_screenshot(task_path: Path, page: str, viewport: str) -> Path | None:
    p = task_path / "environment" / "ground_truth" / "screenshots" / viewport / page / "full.png"
    return p if p.exists() else None


def find_agent_screenshot(trial_dir: Path, page: str, viewport: str) -> Path | None:
    pre = trial_dir / "artifacts" / "screenshots" / viewport / f"{page}.png"
    if pre.exists():
        return pre
    html = trial_dir / "artifacts" / "output" / f"{page}.html"
    if not html.exists():
        return None
    cache = trial_dir / "artifacts" / "_rendered_screenshots" / viewport / f"{page}.png"
    if cache.exists():
        return cache
    return cache if _render_html_to_png(html, cache, viewport) else None


# ── image embedding ─────────────────────────────────────────────────────────


def thumb_data_uri(png_path: Path | None, target_width: int = THUMB_WIDTH) -> str | None:
    if png_path is None or not png_path.exists():
        return None
    if not HAS_PIL:
        data = base64.b64encode(png_path.read_bytes()).decode()
        return f"data:image/png;base64,{data}"
    try:
        with Image.open(png_path) as img:
            img = img.convert("RGB")
            if img.width > target_width:
                ratio = target_width / img.width
                img = img.resize((target_width, int(img.height * ratio)), Image.LANCZOS)
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=JPEG_QUALITY, optimize=True)
            data = base64.b64encode(buf.getvalue()).decode()
            return f"data:image/jpeg;base64,{data}"
    except Exception as e:
        print(f"  warn: thumbnail failed for {png_path}: {type(e).__name__}: {e}", file=sys.stderr)
        return None


# ── small rendering helpers ─────────────────────────────────────────────────


def _sanitize_id(s: str) -> str:
    """Make an arbitrary string safe to use as an HTML id / CSS selector
    target. Replaces anything that isn't [A-Za-z0-9_-] with '_'."""
    return re.sub(r"[^A-Za-z0-9_-]", "_", s)


def _classify(v: float | None) -> str:
    if v is None:
        return "dim"
    if v >= 0.85:
        return "good"
    if v >= 0.5:
        return "ok"
    return "bad"


def _fmt(v: float | None, decimals: int = 4) -> str:
    return f"{v:.{decimals}f}" if isinstance(v, (int, float)) else "—"


def _score_bar_inline(v: float | None) -> str:
    """A compact inline score bar (used in dashboard cells)."""
    if v is None:
        return '<span class="dim">—</span>'
    pct = max(0.0, min(1.0, v)) * 100
    cls = _classify(v)
    return (
        f'<span class="inline-bar"><span class="inline-fill {cls}" style="width:{pct:.1f}%"></span>'
        f'<span class="inline-label">{v:.3f}</span></span>'
    )


def render_screenshot_cell(label: str, png_path: Path | None, target_width: int = THUMB_WIDTH) -> str:
    src = thumb_data_uri(png_path, target_width=target_width)
    if src is None:
        return (
            f'<figure class="ss missing">'
            f'<figcaption>{label}</figcaption>'
            f'<div class="placeholder">no screenshot</div>'
            f"</figure>"
        )
    return (
        f'<figure class="ss">'
        f'<figcaption>{label}</figcaption>'
        f'<img src="{src}" loading="lazy" />'
        f"</figure>"
    )


def render_score_bar_row(label: str, track_a: float | None, track_b: float | None) -> str:
    def bar(v: float | None) -> str:
        if v is None:
            return '<td class="score-cell dim">—</td>'
        pct = max(0.0, min(1.0, v)) * 100
        cls = _classify(v)
        return (
            f'<td class="score-cell">'
            f'<div class="bar"><div class="fill {cls}" style="width:{pct:.1f}%"></div>'
            f'<span class="bar-label">{v:.3f}</span></div>'
            f"</td>"
        )
    return f"<tr><th class='crit'>{label}</th>{bar(track_a)}{bar(track_b)}</tr>"


# ── dashboard ──────────────────────────────────────────────────────────────


def _task_summary_rows(task_id: str, task_trials: list[dict[str, Any]]) -> str:
    """Dashboard rows for one task: one row per variant, with the task name
    column visually merged via `rowspan` so variants visually group under
    a single task heading. The task cell is a label that switches to the
    per-task tab."""
    tab_id = f"tab-{_sanitize_id(task_id)}"
    n = len(task_trials)
    rows: list[str] = []
    for i, trial in enumerate(task_trials):
        variant = _extract_variant(trial["name"])
        reward = trial.get("reward") or {}
        a = reward.get("score_objective")
        b = reward.get("score_judge")
        g = reward.get("gate")
        delta = abs(a - b) if isinstance(a, (int, float)) and isinstance(b, (int, float)) else None

        status = '<span class="status-ok">✓ ok</span>' if reward else '<span class="status-bad">✗ failed</span>'
        delta_cell = (
            f'<td class="num delta-flag">{delta:.2f} ⚠</td>'
            if (delta is not None and delta > 0.1)
            else f'<td class="num">{_fmt(delta, 2) if delta is not None else "—"}</td>'
        )

        task_cell = (
            f'<th class="task-name" rowspan="{n}"><label for="{tab_id}">{task_id}</label></th>'
            if i == 0 else ""
        )
        # Subtle group border between tasks — applied to the last row of the group
        row_class = ' class="task-group-end"' if i == n - 1 else ""

        rows.append(
            f"<tr{row_class}>"
            f"{task_cell}"
            f'<td class="variant">{_display_variant(variant)}</td>'
            f"<td>{status}</td>"
            f'<td class="score-num {_classify(a)}">{_fmt(a)}</td>'
            f'<td class="score-num {_classify(b)}">{_fmt(b)}</td>'
            f"{delta_cell}"
            f'<td class="num">{_fmt(g, 2) if isinstance(g, (int, float)) else "—"}</td>'
            f"</tr>"
        )
    return "\n".join(rows)


def _per_criterion_mean_row(trials: list[dict[str, Any]], crit: str) -> str:
    """Compute mean of (Track A, Track B) for this criterion across all
    trials that have a reward.json. Reports |Δ| flag too."""
    a_vals: list[float] = []
    b_vals: list[float] = []
    for t in trials:
        pc = (t.get("grading") or {}).get("per_criterion") or {}
        cell = pc.get(crit) or {}
        if isinstance(cell.get("objective"), (int, float)):
            a_vals.append(cell["objective"])
        if isinstance(cell.get("judge"), (int, float)):
            b_vals.append(cell["judge"])

    a_mean = sum(a_vals) / len(a_vals) if a_vals else None
    b_mean = sum(b_vals) / len(b_vals) if b_vals else None
    delta = abs(a_mean - b_mean) if a_mean is not None and b_mean is not None else None
    delta_str = ""
    if delta is not None:
        delta_str = f"{delta:.2f} ⚠" if delta > 0.1 else f"{delta:.2f}"

    return f"""
    <tr>
      <th class="crit">{crit}</th>
      <td>{_score_bar_inline(a_mean)}</td>
      <td>{_score_bar_inline(b_mean)}</td>
      <td class="num delta-cell">{delta_str or '—'}</td>
    </tr>
    """


def render_dashboard_panel(trials: list[dict[str, Any]]) -> str:
    if not trials:
        return '<section class="panel" id="panel-dashboard"><p class="dim">no trials in this job</p></section>'

    grouped = _group_trials_by_task(trials)
    rows = "\n".join(_task_summary_rows(tid, ts) for tid, ts in grouped.items())
    crit_rows = "\n".join(_per_criterion_mean_row(trials, c) for c in CRITERIA)

    ok_count = sum(1 for t in trials if t.get("reward"))
    fail_count = len(trials) - ok_count

    return f"""
<section class="panel" id="panel-dashboard">
  <h2>Per-task summary</h2>
  <p class="muted small">
    {len(grouped)} task(s) · {ok_count} ok / {fail_count} failed trial(s).
    Variants grouped per task — click a task name to open its tab.
  </p>
  <table class="dashboard-table">
    <thead>
      <tr>
        <th>task</th>
        <th>variant</th>
        <th>status</th>
        <th class="num">Track A</th>
        <th class="num">Track B</th>
        <th class="num">|Δ|</th>
        <th class="num">gate</th>
      </tr>
    </thead>
    <tbody>{rows}</tbody>
  </table>

  <h2 class="section-h">Per-criterion means (across ok trials)</h2>
  <p class="muted small">
    |Δ| > 0.1 flagged for spot review.
    Large per-criterion |Δ| reveals where the two tracks disagree systematically.
  </p>
  <table class="crit-summary">
    <thead>
      <tr>
        <th>criterion</th>
        <th>Track A mean</th>
        <th>Track B mean</th>
        <th class="num">|Δ|</th>
      </tr>
    </thead>
    <tbody>{crit_rows}</tbody>
  </table>
</section>
"""


# ── per-trial panels ────────────────────────────────────────────────────────


def _extract_task_id(trial_name: str) -> str | None:
    """`task_1-oneshot__yJ7wdJ8` → `task_1`. Returns None if not in expected shape."""
    m = re.match(r"(task_\d+)-(?:oneshot|iter)", trial_name)
    return m.group(1) if m else None


def _extract_variant(trial_name: str) -> str:
    """`task_1-oneshot__yJ7wdJ8` → `oneshot`. Returns `?` if not parseable."""
    m = re.search(r"-(oneshot|iter)__", trial_name)
    return m.group(1) if m else "?"


def _display_variant(v: str) -> str:
    """Display name for a variant. Keeps internal IDs (`iter`) compact in
    paths and configs but presents the friendlier word in the UI."""
    return {"oneshot": "oneshot", "iter": "iterative"}.get(v, v)


def _group_trials_by_task(trials: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Bucket trials by their base task id (`task_1`, `task_2`, …). Order:
    task ids sorted numerically, variants within each task ordered as
    [oneshot, iter, ...other]."""
    def _sort_key(name: str) -> tuple[int, str]:
        m = re.match(r"task_(\d+)", name)
        return (int(m.group(1)), name) if m else (10**6, name)

    buckets: dict[str, list[dict[str, Any]]] = {}
    for t in trials:
        tid = _extract_task_id(t["name"]) or t["name"]
        buckets.setdefault(tid, []).append(t)

    # Sort variants inside each task: oneshot → iter → other
    variant_order = {"oneshot": 0, "iter": 1}
    for tid in buckets:
        buckets[tid].sort(key=lambda t: variant_order.get(_extract_variant(t["name"]), 9))

    # Return as a dict ordered by task id
    return {tid: buckets[tid] for tid in sorted(buckets, key=_sort_key)}


def _gather_unique_tasks(
    trials: list[dict[str, Any]],
    fallback_tasks_dir: Path | None = None,
    prefer_dir: bool = False,
) -> list[tuple[str, Path]]:
    """Return [(base_task_id, task_path)] deduplicated and sorted by numeric ID.

    Default behaviour (Option B in the report's design):
      Diversity = tasks this eval ran on (extracted from trial configs).
      Self-contained per job; consistent with the rest of the report.

    `prefer_dir=True` flips to Option A (dataset overview): scan the
    given `fallback_tasks_dir` regardless of what the trials say. Useful
    for benchmark-overview reports.

    `fallback_tasks_dir` is also used as a graceful-degradation fallback
    when no trial paths resolve (e.g. rendering an old job whose task
    dirs got moved/renamed since the run).
    """
    def _sort_key(name: str) -> tuple[int, str]:
        m = re.match(r"task_(\d+)", name)
        return (int(m.group(1)), name) if m else (10**6, name)

    def _scan_dir(d: Path) -> dict[str, Path]:
        out: dict[str, Path] = {}
        for child in sorted(d.iterdir()):
            m = re.fullmatch(r"(task_\d+)-(?:oneshot|iter)", child.name)
            if m and m.group(1) not in out:
                out[m.group(1)] = child
        return out

    # Option A — explicit dataset view
    if prefer_dir and fallback_tasks_dir and fallback_tasks_dir.is_dir():
        seen = _scan_dir(fallback_tasks_dir)
        return sorted(seen.items(), key=lambda kv: _sort_key(kv[0]))

    # Option B (default) — trial-driven
    seen: dict[str, Path] = {}
    for trial in trials:
        tp = task_path_from_trial(trial)
        if not tp:
            continue
        base = re.sub(r"-(?:oneshot|iter)$", "", tp.name)
        if base not in seen:
            seen[base] = tp

    # Graceful degradation: trials resolved nothing (e.g. stale paths
    # after rename). Fall back to the dir so the panel isn't blank.
    if not seen and fallback_tasks_dir and fallback_tasks_dir.is_dir():
        seen = _scan_dir(fallback_tasks_dir)

    return sorted(seen.items(), key=lambda kv: _sort_key(kv[0]))


def _load_design_for_task(task_path: Path) -> dict[str, Any] | None:
    """Best-effort load of design.json from the packaged task's vendored
    ground_truth. Returns None if missing or unparseable."""
    p = task_path / "tests" / "ground_truth" / "design.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except Exception:
        return None


def render_diversity_panel(
    trials: list[dict[str, Any]],
    fallback_tasks_dir: Path | None = None,
    prefer_dir: bool = False,
) -> str:
    """Build the 'Diversity' tab — per-task horizontal carousel of full
    desktop-viewport reference screenshots. Each task section is its own
    scroll strip; user scrolls horizontally between pages of a single task,
    vertically within a slide to see the full top-to-bottom layout.

    By default, shows only tasks in this eval (trial-driven). Pass
    `prefer_dir=True` with `fallback_tasks_dir=...` for a dataset-wide view.
    """

    tasks = _gather_unique_tasks(
        trials, fallback_tasks_dir=fallback_tasks_dir, prefer_dir=prefer_dir,
    )
    if not tasks:
        return (
            '<section class="panel" id="panel-diversity">'
            '<p class="dim">no tasks discovered from job trials</p>'
            '</section>'
        )

    sections = []
    for base_id, task_path in tasks:
        design = _load_design_for_task(task_path)
        description = ""
        if design and isinstance(design.get("description"), str):
            description = design["description"].strip()

        pages = discover_pages(task_path, "desktop")
        slides = []
        for page in pages:
            ref = find_reference_screenshot(task_path, page, "desktop")
            # Thumb at 320 px wide — wide enough to make the layout
            # readable but slim enough that 60+ full-page screenshots
            # don't balloon the HTML beyond a few extra MB.
            src = thumb_data_uri(ref, target_width=320)
            if src:
                slides.append(
                    f'<figure class="diversity-slide">'
                    f'<figcaption>{page}</figcaption>'
                    f'<div class="diversity-slide-frame">'
                    f'<img src="{src}" loading="lazy" />'
                    f'</div></figure>'
                )
            else:
                slides.append(
                    f'<figure class="diversity-slide">'
                    f'<figcaption>{page}</figcaption>'
                    f'<div class="diversity-slide-frame"><div class="placeholder">no reference</div></div>'
                    f'</figure>'
                )

        desc_html = (
            f'<span class="muted"> — {description[:160]}{"…" if len(description) > 160 else ""}</span>'
            if description else ""
        )
        sections.append(f"""
<div class="diversity-task-section">
  <h3>{base_id}{desc_html}</h3>
  <div class="diversity-carousel">{''.join(slides)}</div>
</div>
""")

    return f"""
<section class="panel" id="panel-diversity">
  <h2>Reference website diversity</h2>
  <p class="muted small">Per task: a horizontal strip of full-page desktop screenshots (all pages, top-to-bottom). Scroll horizontally within each task to see its other pages; scroll within a slide to see the page top-to-bottom.</p>
  {''.join(sections)}
</section>
"""


def _verdict_cell(v: int | float | None) -> str:
    """Render a single verdict cell with a colour-graded class.

    1-5 scale → CSS class `v-1` (red) through `v-5` (green). Values outside
    [0, 5] still render but without a styled class. None → dim em-dash."""
    if v is None:
        return '<td class="v dim">—</td>'
    try:
        iv = int(v)
    except (TypeError, ValueError):
        return f'<td class="v">{v}</td>'
    # Clamp to known scale range; values outside [0, 5] still render
    # but won't have a styled class.
    if 0 <= iv <= 5:
        return f'<td class="v v-{iv}">{iv}</td>'
    return f'<td class="v">{iv}</td>'


def _gather_verdicts(judge_data: dict[str, Any]) -> list[tuple[str, str, str, int]]:
    """Walk a judge criterion's detail and yield flat (page, context, q_id, verdict) tuples.

    `context` distinguishes the inner dimension:
      - per_page / per_image: "" (no inner dimension)
      - per_page_per_viewport, per_non_desktop_viewport: "<viewport>"
      - per_component: "c<N>: <component description truncated>"
    """
    out: list[tuple[str, str, str, int]] = []
    pp = judge_data.get("per_page") or {}
    for page, page_data in pp.items():
        if "viewports" in page_data:
            for vp, vp_data in page_data["viewports"].items():
                for v in vp_data.get("verdicts", []):
                    out.append((page, vp, v["q_id"], v["verdict"]))
        elif "components" in page_data:
            for comp in page_data["components"]:
                ci = comp.get("component_idx", "?")
                cd = comp.get("component", "")
                ctx = f"c{ci}: {cd[:60]}{'…' if len(cd) > 60 else ''}"
                for v in comp.get("verdicts", []):
                    # Strip the "__cN" suffix so each component reuses the
                    # same set of base q_ids → grid columns line up.
                    qid = v["q_id"].split("__")[0]
                    out.append((page, ctx, qid, v["verdict"]))
        elif "verdicts" in page_data:
            for v in page_data["verdicts"]:
                out.append((page, "", v["q_id"], v["verdict"]))
    return out


def _question_legend(criterion: str) -> dict[str, str]:
    """Load the question pack and return {q_id: text} for the legend."""
    try:
        p = (REPO_ROOT_FOR_PACKS / f"{criterion}.json").read_text()
        pack = json.loads(p)
    except Exception:
        return {}
    return {q["id"]: q["text"] for q in pack.get("questions", [])}


REPO_ROOT_FOR_PACKS = (Path(__file__).resolve().parent.parent.parent / "grading" / "judge" / "question_packs")


def _render_criterion_judge_detail(criterion: str, judge_data: dict[str, Any]) -> str:
    """One criterion's per-question grid: rows × cols of ✓/✗ cells."""
    # Failed criterion (catch from runner.py per-criterion try/except)
    if isinstance(judge_data.get("score"), type(None)) or judge_data.get("error"):
        err = judge_data.get("error", "(no error message)")
        return (
            f'<div class="judge-crit-detail">'
            f'<h4>{criterion}</h4>'
            f'<p class="dim small">criterion failed during judging: <code>{err}</code></p>'
            f'</div>'
        )

    tuples = _gather_verdicts(judge_data)
    if not tuples:
        return (
            f'<div class="judge-crit-detail"><h4>{criterion}</h4>'
            f'<p class="dim small">scope: {judge_data.get("scope", "?")} — no verdicts recorded</p>'
            f'</div>'
        )

    # Stable ordering: q_ids in first-seen order; rows in (page, context) first-seen order.
    qid_order: list[str] = []
    for _, _, qid, _ in tuples:
        if qid not in qid_order:
            qid_order.append(qid)
    row_keys: list[tuple[str, str]] = []
    for page, ctx, _, _ in tuples:
        key = (page, ctx)
        if key not in row_keys:
            row_keys.append(key)
    cell: dict[tuple[str, str, str], int] = {(p, c, q): v for p, c, q, v in tuples}

    legend = _question_legend(criterion)

    # Build the legend list (q_id → text) — shown above the grid.
    legend_html = "".join(
        f'<li><code>{qid}</code> <span class="muted">{legend.get(qid, "(no text)")}</span></li>'
        for qid in qid_order
    )

    # Header row
    header_cells = "".join(
        f'<th class="qid-col" title="{legend.get(qid, "")}"><code>{qid}</code></th>'
        for qid in qid_order
    )
    # Body rows
    body_rows = []
    for page, ctx in row_keys:
        ctx_cell = f'<td class="ctx">{ctx}</td>' if any(c for _, c in row_keys) else ""
        cells = ""
        for qid in qid_order:
            v = cell.get((page, ctx, qid))
            cells += _verdict_cell(v)
        body_rows.append(f'<tr><th class="ctx page-cell">{page}</th>{ctx_cell}{cells}</tr>')

    ctx_header = '<th class="ctx-col">ctx</th>' if any(c for _, c in row_keys) else ""

    return f'''
<div class="judge-crit-detail">
  <h4>{criterion} <span class="muted small">— score {judge_data.get("score", 0):.3f}, scope: {judge_data.get("scope", "?")}</span></h4>
  <details class="judge-grid">
    <summary>per-question grid ({len(row_keys)} rows × {len(qid_order)} questions = {len(row_keys) * len(qid_order)} verdicts)</summary>
    <ul class="qid-legend">{legend_html}</ul>
    <table class="judge-grid-table">
      <thead><tr><th>page</th>{ctx_header}{header_cells}</tr></thead>
      <tbody>{"".join(body_rows)}</tbody>
    </table>
  </details>
</div>
'''


def _render_judge_detail(trial: dict[str, Any]) -> str:
    """All criteria's per-question verdict tables, wrapped in a single
    collapsible top-level `<details>` so the per-trial panel doesn't
    balloon by default."""
    grading = trial.get("grading") or {}
    detail = grading.get("per_criterion_detail") or {}
    if not detail:
        return ""
    sections = []
    for crit in CRITERIA:
        jd = (detail.get(crit) or {}).get("judge")
        if not jd:
            continue
        sections.append(_render_criterion_judge_detail(crit, jd))
    if not sections:
        return ""
    return f'''
<details class="judge-detail-wrap">
  <summary class="judge-detail-summary">▸ Per-question Track B verdicts (granular)</summary>
  <div class="judge-detail">{"".join(sections)}</div>
</details>
'''


def _render_viewport_subpanel(
    trial: dict[str, Any],
    task_path: Path | None,
    viewport: str,
    pages_by_viewport: dict[str, list[str]],
) -> str:
    """The pages-with-screenshot-pairs section for one (trial, viewport).

    Tags the panel div with a viewport-specific class so per-viewport CSS
    can constrain the displayed image width (mobile shouldn't fill the
    same grid cell as a desktop screenshot)."""
    sub_id = f"vp-panel-{_sanitize_id(trial['name'])}-{viewport}"
    target_w = VIEWPORT_THUMB_WIDTH.get(viewport, THUMB_WIDTH)
    pages = pages_by_viewport.get(viewport, [])
    if not pages:
        return f'<div class="vp-panel vp-{viewport}" id="{sub_id}"><p class="dim">no reference screenshots at {viewport}</p></div>'

    blocks = []
    for page in pages:
        ref = find_reference_screenshot(task_path, page, viewport) if task_path else None
        agent = find_agent_screenshot(trial["trial_dir"], page, viewport)
        anim_row = ""
        # Animated WebPs are heavy + slow to encode; only emit at the
        # desktop viewport (per user decision — animations look similar
        # across viewports and tablet/mobile would 3x the report size).
        if viewport == "desktop" and task_path is not None:
            meta = _load_widget_meta(task_path, page)
            duration_ms = int((meta or {}).get("duration_ms") or 1300)
            ref_anim = make_reference_animation_webp(task_path, page, viewport, duration_ms)
            agent_anim = make_agent_animation_webp(trial["trial_dir"], task_path, page, viewport)
            anim_row = (
                f'<div class="pair animations">'
                f'{render_animation_cell("reference (animation)", ref_anim)}'
                f'{render_animation_cell("agent (animation)", agent_anim)}'
                f'</div>'
            )
        blocks.append(
            f'<section class="page"><h4>{page}</h4>'
            f'{anim_row}'
            f'<div class="pair">{render_screenshot_cell("reference", ref, target_w)}'
            f'{render_screenshot_cell("agent", agent, target_w)}</div>'
            f'</section>'
        )
    return f'<div class="vp-panel vp-{viewport}" id="{sub_id}"><div class="pages">{"".join(blocks)}</div></div>'


def _render_trial_inner(trial: dict[str, Any], default_viewport: str) -> str:
    """Inner content of one variant block: scoreboard, criteria, judge
    detail, viewport sub-tabs. No outer panel section — composed into
    a per-task panel that may host multiple variants."""
    reward = trial.get("reward")
    if reward is None:
        return _render_failed_variant_inner(trial)

    grading = trial.get("grading") or {}
    config = trial.get("config") or {}
    agent_cfg = config.get("agent") or {}
    task_path = task_path_from_trial(trial)
    metadata = grading.get("metadata") or {}

    a = reward.get("score_objective")
    b = reward.get("score_judge")
    g = reward.get("gate")
    track_b_run = metadata.get("track_b_run", False)
    variant = _extract_variant(trial["name"])

    pc = grading.get("per_criterion") or {}
    rows = "".join(
        render_score_bar_row(c, (pc.get(c) or {}).get("objective"), (pc.get(c) or {}).get("judge"))
        for c in CRITERIA
    )

    # Viewport sub-tabs (desktop / tablet / mobile). Each TRIAL gets its own
    # radio group (name="vp-<trial>") — trial names are unique per (task,
    # variant, short-id), so two variants under one task panel don't
    # collide with each other's viewport selectors.
    viewports = ("desktop", "tablet", "mobile")
    pages_by_viewport = {
        vp: (discover_pages(task_path, vp) if task_path else []) for vp in viewports
    }
    trial_sanitized = _sanitize_id(trial["name"])
    vp_group = f"vp-{trial_sanitized}"

    vp_inputs = []
    vp_labels = []
    for vp in viewports:
        vp_input_id = f"vp-{trial_sanitized}-{vp}"
        checked = " checked" if vp == default_viewport else ""
        vp_inputs.append(f'<input type="radio" name="{vp_group}" class="vp-input" id="{vp_input_id}"{checked}>')
        vp_labels.append(f'<label class="vp-label" for="{vp_input_id}">{vp}</label>')

    vp_subpanels = "".join(
        _render_viewport_subpanel(trial, task_path, vp, pages_by_viewport) for vp in viewports
    )

    return f"""
<div class="variant-block">
  <div class="variant-header">
    <h3>{_display_variant(variant)} <span class="muted small">/ {trial['name']}</span></h3>
    <div class="agent-info muted small">
      {agent_cfg.get('name', '—')} • {agent_cfg.get('model_name', '—')}
      {f"• Track B: {metadata.get('judge_model', '—')}" if track_b_run else ""}
    </div>
  </div>

  <div class="scoreboard">
    <div class="headline {_classify(a)}">
      <div class="big">{_fmt(a)}</div><div class="label">Track A · objective</div>
    </div>
    <div class="headline {_classify(b)}">
      <div class="big">{_fmt(b)}</div><div class="label">Track B · judge</div>
    </div>
    <div class="headline {'good' if g == 1.0 else 'bad'}">
      <div class="big">{_fmt(g, 2)}</div><div class="label">framework gate</div>
    </div>
  </div>

  <table class="criteria">
    <thead><tr><th>criterion</th><th>Track A</th><th>Track B</th></tr></thead>
    <tbody>{rows}</tbody>
  </table>

  {_render_judge_detail(trial)}

  {''.join(vp_inputs)}
  <nav class="vp-bar">{''.join(vp_labels)}</nav>
  <div class="vp-panels">{vp_subpanels}</div>
</div>
"""


def _render_failed_variant_inner(trial: dict[str, Any]) -> str:
    """Failed-trial content block (no scoreboard — just the error reason)."""
    config = trial.get("config") or {}
    agent_cfg = config.get("agent") or {}
    task_path_str = (config.get("task") or {}).get("path", "?")
    reason = trial.get("failed_reason", "unknown")
    variant = _extract_variant(trial["name"])
    return f"""
<div class="variant-block failed">
  <div class="variant-header">
    <h3>{_display_variant(variant)} <span class="muted small">/ {trial['name']}</span> <span class="status-bad">✗ failed</span></h3>
    <div class="agent-info muted small">
      task: {task_path_str} • {agent_cfg.get('name','—')} • {agent_cfg.get('model_name','—')}
    </div>
  </div>
  <div class="failure-box">
    <div class="failure-label">failure reason</div>
    <pre class="failure-reason">{reason}</pre>
    <div class="failure-hint muted small">
      trial dir: <code>{trial['trial_dir']}</code>
    </div>
  </div>
</div>
"""


def render_task_panel(
    task_id: str,
    task_trials: list[dict[str, Any]],
    default_viewport: str,
) -> str:
    """Per-task panel hosting all variants for one task ID.

    Layout:
      [task header with description]
      [variant-comparison table — oneshot vs iter side by side]
      [variant blocks stacked — each is a full (scoreboard + criteria +
       judge detail + viewport sub-tabs + screenshots) section]
    """
    panel_id = f"panel-{_sanitize_id(task_id)}"

    # Description: pull from the first trial that has a resolvable task_path
    description = ""
    for trial in task_trials:
        tp = task_path_from_trial(trial)
        if tp:
            d = _load_design_for_task(tp)
            if d and isinstance(d.get("description"), str):
                description = d["description"].strip()
                break

    # Variant-comparison summary table at the top
    comp_rows = []
    for trial in task_trials:
        variant = _extract_variant(trial["name"])
        reward = trial.get("reward") or {}
        a = reward.get("score_objective")
        b = reward.get("score_judge")
        g = reward.get("gate")
        delta = abs(a - b) if isinstance(a, (int, float)) and isinstance(b, (int, float)) else None
        delta_str = (f"{delta:.2f}" + (" ⚠" if delta > 0.1 else "")) if delta is not None else "—"
        status = '<span class="status-ok">✓ ok</span>' if reward else '<span class="status-bad">✗ failed</span>'
        comp_rows.append(
            f"<tr>"
            f"<th>{_display_variant(variant)}</th>"
            f"<td>{status}</td>"
            f'<td class="score-num {_classify(a)}">{_fmt(a)}</td>'
            f'<td class="score-num {_classify(b)}">{_fmt(b)}</td>'
            f'<td class="num">{delta_str}</td>'
            f'<td class="num">{_fmt(g, 2) if isinstance(g,(int,float)) else "—"}</td>'
            f"</tr>"
        )

    comp_table = f"""
<table class="variant-compare-table">
  <thead>
    <tr>
      <th>variant</th>
      <th>status</th>
      <th class="num">Track A</th>
      <th class="num">Track B</th>
      <th class="num">|Δ|</th>
      <th class="num">gate</th>
    </tr>
  </thead>
  <tbody>{''.join(comp_rows)}</tbody>
</table>
"""

    # Variant sub-tabs — only if there's more than one variant. Each task
    # gets its own radio group (name="vt-<task>") so they don't collide
    # with the top-level tab radios or the per-trial viewport radios.
    if len(task_trials) > 1:
        sanitized_task = _sanitize_id(task_id)
        vt_inputs = []
        vt_labels = []
        vt_panels_html = []
        for i, trial in enumerate(task_trials):
            v = _extract_variant(trial["name"])
            input_id = f"vt-{sanitized_task}-{v}"
            sub_id = f"vt-panel-{sanitized_task}-{v}"
            checked = " checked" if i == 0 else ""
            vt_inputs.append(
                f'<input type="radio" name="vt-{sanitized_task}" class="vt-input" id="{input_id}"{checked}>'
            )
            vt_labels.append(
                f'<label class="vt-label" for="{input_id}">{_display_variant(v)}</label>'
            )
            vt_panels_html.append(
                f'<div class="vt-panel" id="{sub_id}">{_render_trial_inner(trial, default_viewport)}</div>'
            )
        variant_body = (
            f"{''.join(vt_inputs)}"
            f'<nav class="vt-bar">{"".join(vt_labels)}</nav>'
            f'<div class="vt-panels">{"".join(vt_panels_html)}</div>'
        )
    else:
        # Single variant — no sub-tabs.
        variant_body = (
            f'<div class="variant-blocks">'
            f'{_render_trial_inner(task_trials[0], default_viewport)}'
            f'</div>'
        )

    return f"""
<section class="panel task-panel" id="{panel_id}">
  <header class="task-panel-header">
    <h2>{task_id}{f' <span class="muted">— {description[:200]}{"…" if len(description) > 200 else ""}</span>' if description else ''}</h2>
  </header>
  {comp_table}
  {variant_body}
</section>
"""


# Note: `_render_failed_panel` was removed when render_trial_panel got
# refactored into _render_trial_inner + render_task_panel. The failed
# inner-block variant is `_render_failed_variant_inner` above.


# ── tab assembly ───────────────────────────────────────────────────────────


def _tab_inputs(panel_keys: list[str]) -> str:
    """Hidden radio inputs — one per tab; the first is checked by default."""
    out = []
    for i, key in enumerate(panel_keys):
        tab_id = f"tab-{_sanitize_id(key)}"
        checked = " checked" if i == 0 else ""
        out.append(f'<input type="radio" name="tabs" class="tab-input" id="{tab_id}"{checked}>')
    return "\n".join(out)


def _tab_bar(panel_keys: list[str], display_names: dict[str, str]) -> str:
    out = ['<nav class="tab-bar">']
    for key in panel_keys:
        tab_id = f"tab-{_sanitize_id(key)}"
        name = display_names.get(key, key)
        out.append(f'<label class="tab-label" for="{tab_id}">{name}</label>')
    out.append("</nav>")
    return "\n".join(out)


def _tab_visibility_css(panel_keys: list[str]) -> str:
    """Generated CSS rules — one per panel — that show the panel and
    highlight the active label when the matching radio is checked."""
    show_rules = []
    label_rules = []
    for key in panel_keys:
        tab = _sanitize_id(f"tab-{key}")
        panel = _sanitize_id(f"panel-{key}")
        show_rules.append(f'#{tab}:checked ~ .panels #{panel}')
        label_rules.append(f'#{tab}:checked ~ .tab-bar label[for="{tab}"]')
    return (
        ",\n".join(show_rules) + " { display: block; }\n\n"
        + ",\n".join(label_rules) + " { background: var(--surface-2); color: var(--text); font-weight: 500; border-bottom-color: var(--accent); }"
    )


def _variant_tab_visibility_css(grouped: dict[str, list[dict[str, Any]]]) -> str:
    """Generated CSS for the per-task variant sub-tabs (oneshot / iterative).

    Only emits rules for tasks that actually have multiple variants —
    single-variant tasks don't render sub-tabs at all.
    """
    show_rules = []
    label_rules = []
    for task_id, task_trials in grouped.items():
        if len(task_trials) <= 1:
            continue
        sanitized = _sanitize_id(task_id)
        for trial in task_trials:
            v = _extract_variant(trial["name"])
            input_id = f"vt-{sanitized}-{v}"
            panel_id = f"vt-panel-{sanitized}-{v}"
            show_rules.append(f'#{input_id}:checked ~ .vt-panels #{panel_id}')
            label_rules.append(f'#{input_id}:checked ~ .vt-bar label[for="{input_id}"]')
    if not show_rules:
        return ""
    return (
        ",\n".join(show_rules) + " { display: block; }\n\n"
        + ",\n".join(label_rules) + " { background: var(--surface-2); color: var(--text); font-weight: 500; }"
    )


def _viewport_visibility_css(trial_names: list[str]) -> str:
    """Generated CSS rules for the per-trial viewport sub-tabs.

    The viewport radios are *inside* each trial's panel section (siblings
    of `.vp-bar` and `.vp-panels`), so the `:checked ~` selector matches
    the sub-panel that shares the trial's namespace.
    """
    show_rules = []
    label_rules = []
    for name in trial_names:
        ts = _sanitize_id(name)
        for vp in ("desktop", "tablet", "mobile"):
            input_id = f"vp-{ts}-{vp}"
            subpanel_id = f"vp-panel-{ts}-{vp}"
            show_rules.append(f'#{input_id}:checked ~ .vp-panels #{subpanel_id}')
            label_rules.append(f'#{input_id}:checked ~ .vp-bar label[for="{input_id}"]')
    if not show_rules:
        return ""
    return (
        ",\n".join(show_rules) + " { display: block; }\n\n"
        + ",\n".join(label_rules) + " { background: var(--surface-2); color: var(--text); font-weight: 500; }"
    )


# ── stylesheet ─────────────────────────────────────────────────────────────


CSS_BASE = """
:root {
  --bg: #0f0f12;
  --surface: #1a1a1f;
  --surface-2: #232329;
  --text: #e6e6e8;
  --muted: #8a8a92;
  --good: #4ade80;
  --ok: #fbbf24;
  --bad: #f87171;
  --dim: #444;
  --accent: #60a5fa;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  background: var(--bg);
  color: var(--text);
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
  line-height: 1.5;
}
h1, h2, h3, h4 { margin: 0; }
code { font-family: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace; font-size: 0.9em; background: var(--surface-2); padding: 1px 5px; border-radius: 3px; }
.muted { color: var(--muted); font-weight: normal; }
.small { font-size: 0.85em; }
.dim { color: var(--dim); }
.num { text-align: right; font-variant-numeric: tabular-nums; }

header.job {
  padding: 16px 32px 0;
  background: var(--surface);
  border-bottom: 1px solid var(--surface-2);
  position: sticky; top: 0; z-index: 10;
}
header.job h1 { font-size: 1.1rem; margin-bottom: 8px; }
header.job .summary { display: flex; gap: 32px; color: var(--muted); font-size: 0.85rem; flex-wrap: wrap; margin-bottom: 16px; }
header.job .summary b { color: var(--text); font-variant-numeric: tabular-nums; }
.status-ok  { color: var(--good); font-weight: 500; }
.status-bad { color: var(--bad);  font-weight: 500; }

/* tabs ─ hidden radio inputs that drive panel show/hide */
.tab-input { position: absolute; left: -9999px; }
.tab-bar {
  display: flex; gap: 2px; padding: 0 32px;
  background: var(--surface);
  overflow-x: auto; white-space: nowrap;
  border-bottom: 1px solid var(--surface-2);
}
.tab-label {
  display: inline-block; padding: 10px 16px;
  color: var(--muted); cursor: pointer;
  border-radius: 6px 6px 0 0;
  border-bottom: 2px solid transparent;
  transition: background 0.1s, color 0.1s;
  font-size: 0.9rem;
  user-select: none;
}
.tab-label:hover { background: var(--surface-2); color: var(--text); }

/* panel show/hide — generated rules live in CSS_GENERATED appended below */
.panels {
  max-width: 1400px; margin: 0 auto; padding: 24px 32px;
}
.panel { display: none; }

/* dashboard table */
.dashboard-table, .crit-summary {
  width: 100%; border-collapse: collapse; margin: 12px 0 24px 0;
  font-size: 0.9rem; background: var(--surface); border-radius: 8px; overflow: hidden;
}
.dashboard-table th, .dashboard-table td,
.crit-summary th, .crit-summary td { padding: 8px 12px; text-align: left; border-bottom: 1px solid var(--surface-2); }
.dashboard-table thead th, .crit-summary thead th { font-size: 0.75rem; color: var(--muted); text-transform: uppercase; letter-spacing: 0.05em; font-weight: 500; background: var(--surface-2); }
.dashboard-table tbody tr:hover, .crit-summary tbody tr:hover { background: rgba(255,255,255,0.02); }
.task-name label { color: var(--text); cursor: pointer; font-weight: 500; }
.task-name label:hover { color: var(--accent); text-decoration: underline; }
.score-num { font-variant-numeric: tabular-nums; }
.score-num.good { color: var(--good); }
.score-num.ok   { color: var(--ok); }
.score-num.bad  { color: var(--bad); }
.score-num.dim  { color: var(--dim); }
.delta-flag { color: var(--bad); font-weight: 500; }

.section-h { margin: 32px 0 8px 0; font-size: 0.95rem; color: var(--muted); font-weight: normal; letter-spacing: 0.02em; }

/* inline score bar (used in crit-summary) */
.inline-bar {
  display: inline-block; position: relative;
  width: 200px; height: 18px;
  background: var(--surface-2); border-radius: 3px; overflow: hidden;
  vertical-align: middle;
}
.inline-fill { position: absolute; left: 0; top: 0; bottom: 0; opacity: 0.7; }
.inline-fill.good { background: var(--good); }
.inline-fill.ok   { background: var(--ok); }
.inline-fill.bad  { background: var(--bad); }
.inline-fill.dim  { background: var(--dim); }
.inline-label {
  position: absolute; inset: 0;
  display: flex; align-items: center; justify-content: flex-end;
  padding-right: 6px; font-size: 0.8rem; color: var(--text);
  font-variant-numeric: tabular-nums;
}

/* per-trial panel */
.trial-header h2 { font-size: 1.1rem; }
.trial-header .agent-info { color: var(--muted); font-size: 0.85rem; margin-top: 4px; }

.scoreboard { display: flex; gap: 16px; margin: 16px 0; }
.headline {
  flex: 1; text-align: center; padding: 12px;
  background: var(--surface); border-radius: 8px;
  border-left: 4px solid var(--dim);
}
.headline.good { border-left-color: var(--good); }
.headline.ok   { border-left-color: var(--ok); }
.headline.bad  { border-left-color: var(--bad); }
.headline.dim  { border-left-color: var(--dim); }
.headline .big { font-size: 1.6rem; font-variant-numeric: tabular-nums; font-weight: 600; }
.headline .label { color: var(--muted); font-size: 0.8rem; }

table.criteria { width: 100%; border-collapse: collapse; margin: 16px 0; font-size: 0.9rem; }
table.criteria th, table.criteria td { padding: 6px 10px; text-align: left; }
table.criteria th.crit { color: var(--muted); font-weight: normal; width: 200px; }
table.criteria thead th { font-size: 0.75rem; color: var(--muted); text-transform: uppercase; }
.score-cell .bar {
  position: relative; height: 22px;
  background: var(--surface-2); border-radius: 4px; overflow: hidden;
  min-width: 200px;
}
.score-cell .fill { position: absolute; left: 0; top: 0; bottom: 0; opacity: 0.7; }
.score-cell .fill.good { background: var(--good); }
.score-cell .fill.ok   { background: var(--ok); }
.score-cell .fill.bad  { background: var(--bad); }
.score-cell .bar-label {
  position: absolute; inset: 0;
  display: flex; align-items: center; justify-content: flex-end;
  padding-right: 8px;
  font-variant-numeric: tabular-nums; font-size: 0.85rem;
}
.score-cell.dim { color: var(--dim); padding-left: 8px; }

/* viewport sub-tabs (nested inside each per-trial panel) */
.vp-input { position: absolute; left: -9999px; }
.vp-bar {
  display: flex; gap: 2px; margin-top: 16px;
  background: var(--surface);
  border-bottom: 1px solid var(--surface-2);
}
.vp-label {
  display: inline-block; padding: 8px 14px;
  color: var(--muted); cursor: pointer;
  border-radius: 4px 4px 0 0;
  font-size: 0.8rem; user-select: none;
  text-transform: capitalize;
}
.vp-label:hover { background: var(--surface-2); color: var(--text); }
.vp-panel { display: none; }
.vp-panels { padding-top: 16px; }

.pages { display: grid; gap: 24px; margin-top: 16px; }
section.page h4 {
  font-size: 1.25rem; color: var(--text); font-weight: 600;
  margin-bottom: 12px; padding-bottom: 6px;
  border-bottom: 1px solid var(--surface-2);
  letter-spacing: 0.01em;
}
section.page .pair { display: grid; gap: 16px; }
figure.ss { margin: 0; }
figure.ss figcaption {
  font-size: 0.75rem; color: var(--muted);
  text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 4px;
}
figure.ss img { width: 100%; border-radius: 6px; background: white; display: block; }
figure.ss .placeholder {
  aspect-ratio: 16 / 10; background: var(--surface-2); border-radius: 6px;
  display: flex; align-items: center; justify-content: center;
  color: var(--dim); font-size: 0.85rem;
}

/* variant sub-tabs (one per task panel when >1 variant — oneshot / iterative) */
.vt-input { position: absolute; left: -9999px; }
.vt-bar {
  display: flex; gap: 4px; margin: 16px 0 0; padding: 0 4px;
  border-bottom: 1px solid var(--surface-2);
}
.vt-label {
  padding: 9px 18px; color: var(--muted); cursor: pointer;
  border-radius: 6px 6px 0 0; font-size: 0.9rem;
  user-select: none; text-transform: capitalize;
  border-bottom: 2px solid transparent;
  margin-bottom: -1px;
}
.vt-label:hover { background: var(--surface-2); color: var(--text); }
.vt-panel { display: none; }
.vt-panels { padding-top: 16px; }

/* per-task panel — hosts one or more variant blocks */
.task-panel { }
.task-panel-header h2 { font-size: 1.2rem; }
.task-panel-header .muted { font-size: 0.85rem; font-weight: normal; }

.variant-compare-table { width: 100%; border-collapse: collapse; margin: 16px 0 24px 0; font-size: 0.9rem; background: var(--surface); border-radius: 8px; overflow: hidden; }
.variant-compare-table th, .variant-compare-table td { padding: 8px 12px; text-align: left; border-bottom: 1px solid var(--surface-2); }
.variant-compare-table thead th { font-size: 0.75rem; color: var(--muted); text-transform: uppercase; letter-spacing: 0.05em; font-weight: 500; background: var(--surface-2); }
.variant-compare-table tbody th { color: var(--text); font-weight: 600; text-transform: capitalize; }

.variant-blocks { display: grid; gap: 32px; }
.variant-block { padding: 20px; background: var(--surface); border-radius: 12px; border-left: 4px solid var(--surface-2); }
.variant-block.failed { border-left-color: var(--bad); }
.variant-header { margin-bottom: 12px; }
.variant-header h3 { font-size: 1.05rem; font-weight: 600; text-transform: capitalize; }
.variant-header .agent-info { margin-top: 2px; }

/* Dashboard table — variant-grouping refinements */
.dashboard-table td.variant { text-transform: capitalize; font-size: 0.85rem; color: var(--muted); }
.dashboard-table tr.task-group-end td,
.dashboard-table tr.task-group-end th { border-bottom: 2px solid var(--surface-2); }
.dashboard-table .task-name { vertical-align: middle; }

/* diversity tab — horizontal scroll strips of full-page reference screenshots */
.diversity-task-section { margin-bottom: 36px; padding-bottom: 24px; border-bottom: 1px solid var(--surface-2); }
.diversity-task-section:last-child { border-bottom: none; }
.diversity-task-section h3 { font-size: 1.05rem; font-weight: 600; margin-bottom: 12px; color: var(--text); }
.diversity-task-section h3 .muted { font-size: 0.85rem; font-weight: normal; }
.diversity-carousel {
  display: flex; gap: 16px;
  overflow-x: auto;
  scroll-snap-type: x mandatory;
  padding: 4px 4px 16px 4px;
}
.diversity-slide {
  flex: 0 0 auto;
  width: 320px;
  scroll-snap-align: start;
  margin: 0;
}
.diversity-slide figcaption {
  font-size: 0.85rem; color: var(--muted);
  margin-bottom: 6px;
  font-weight: 500;
  letter-spacing: 0.02em;
}
.diversity-slide-frame {
  width: 100%; max-height: 720px;
  overflow-y: auto;
  border-radius: 6px;
  background: white;
  box-shadow: 0 2px 8px rgba(0,0,0,0.4);
}
.diversity-slide-frame img { width: 100%; display: block; border-radius: 6px; }
.diversity-slide-frame .placeholder {
  aspect-ratio: 9 / 16; background: var(--surface-2);
  display: flex; align-items: center; justify-content: center;
  color: var(--dim); font-size: 0.85rem; border-radius: 6px;
}
.diversity-carousel::-webkit-scrollbar { height: 10px; }
.diversity-carousel::-webkit-scrollbar-track { background: var(--surface-2); border-radius: 4px; }
.diversity-carousel::-webkit-scrollbar-thumb { background: var(--muted); border-radius: 4px; }
.diversity-carousel::-webkit-scrollbar-thumb:hover { background: var(--text); }

/* judge detail (per-question verdicts grid) */
.judge-detail-wrap { margin: 24px 0; background: var(--surface); border-radius: 8px; padding: 16px 20px; border-left: 4px solid var(--surface-2); }
.judge-detail-summary { cursor: pointer; color: var(--text); font-weight: 500; font-size: 0.95rem; user-select: none; }
.judge-detail-summary:hover { color: var(--accent); }
.judge-detail { margin-top: 16px; display: grid; gap: 20px; }
.judge-crit-detail h4 { font-size: 1rem; color: var(--text); margin-bottom: 8px; font-weight: 600; border-bottom: 1px solid var(--surface-2); padding-bottom: 4px; }
.judge-grid > summary { cursor: pointer; font-size: 0.85rem; color: var(--muted); margin: 4px 0 8px 0; }
.judge-grid > summary:hover { color: var(--text); }
.qid-legend { font-size: 0.8rem; list-style: none; padding: 0 0 12px 0; margin: 0; border-bottom: 1px dashed var(--surface-2); }
.qid-legend li { padding: 3px 0; }
.qid-legend code { font-size: 0.75rem; background: var(--surface-2); padding: 1px 6px; border-radius: 3px; }
.judge-grid-table { width: 100%; border-collapse: collapse; font-size: 0.78rem; margin-top: 8px; }
.judge-grid-table th, .judge-grid-table td { padding: 4px 8px; text-align: left; border-bottom: 1px solid var(--surface-2); }
.judge-grid-table thead th { color: var(--muted); font-weight: 500; font-size: 0.7rem; text-transform: uppercase; letter-spacing: 0.04em; vertical-align: bottom; }
.judge-grid-table .qid-col { writing-mode: horizontal-tb; text-align: center; min-width: 50px; cursor: help; }
.judge-grid-table .qid-col code { font-size: 0.7rem; background: transparent; padding: 0; }
.judge-grid-table .ctx { color: var(--muted); font-size: 0.78rem; font-style: italic; max-width: 280px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.judge-grid-table .page-cell { color: var(--text); font-weight: 500; }
.judge-grid-table .v { text-align: center; font-size: 0.95rem; font-weight: 600; min-width: 40px; }
.judge-grid-table .v.good { color: var(--good); }
.judge-grid-table .v.bad { color: var(--bad); }
.judge-grid-table .v.dim { color: var(--dim); }
/* 1-5 scale colour gradient. 1 = no match; 5 = perfect match. */
.judge-grid-table .v-0 { color: #c84a4a; }
.judge-grid-table .v-1 { color: #c84a4a; }
.judge-grid-table .v-2 { color: #d28a3d; }
.judge-grid-table .v-3 { color: #cbb04a; }
.judge-grid-table .v-4 { color: #8dbc4f; }
.judge-grid-table .v-5 { color: #4cb45c; }
.judge-grid-table tbody tr:hover { background: rgba(255,255,255,0.02); }

/* Per-viewport display caps — keep mobile/tablet small so they don't
   get stretched 2× to fill a desktop-width grid cell. */
.vp-desktop .pair { grid-template-columns: 1fr 1fr; max-width: 1400px; }
.vp-tablet  .pair { grid-template-columns: 400px 400px; gap: 24px; }
.vp-mobile  .pair { grid-template-columns: 300px 300px; gap: 32px; }
.vp-desktop figure.ss img { max-width: 700px; }
.vp-tablet  figure.ss img { max-width: 400px; }
.vp-mobile  figure.ss img { max-width: 300px; }

/* failed-trial panel */
.failure-box {
  margin: 16px 0; padding: 16px;
  background: var(--surface); border-left: 4px solid var(--bad); border-radius: 6px;
}
.failure-label { font-size: 0.75rem; color: var(--muted); text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 6px; }
.failure-reason {
  margin: 0; padding: 8px 12px;
  background: var(--surface-2); border-radius: 4px;
  font-family: ui-monospace, SFMono-Regular, monospace; font-size: 0.85rem;
  color: var(--text); white-space: pre-wrap; word-break: break-word;
}
.failure-hint { margin-top: 8px; }
"""


# ── top-level renderer ─────────────────────────────────────────────────────


def render_job(
    trials: list[dict[str, Any]],
    job_name: str,
    viewport: str,
    tasks_dir: Path | None = None,
    all_tasks: bool = False,
) -> str:
    """Render the full HTML page: header + tabs + panels."""

    # The first panel key is the default (Dashboard). After Dashboard +
    # Diversity, we get one tab per *task* (not per trial — variants are
    # aggregated under their task's tab).
    grouped = _group_trials_by_task(trials)
    task_ids = list(grouped.keys())
    panel_keys = ["dashboard", "diversity"] + task_ids
    display_names = {"dashboard": "Dashboard", "diversity": "Diversity"}
    for tid in task_ids:
        display_names[tid] = tid

    # Header summary
    ok_trials = [t for t in trials if t.get("reward")]
    a_vals = [t["reward"].get("score_objective") for t in ok_trials]
    a_vals = [v for v in a_vals if isinstance(v, (int, float))]
    b_vals = [t["reward"].get("score_judge") for t in ok_trials]
    b_vals = [v for v in b_vals if isinstance(v, (int, float))]

    def _stats(vs: list[float]) -> str:
        if not vs:
            return "<span class='dim'>—</span>"
        m = sum(vs) / len(vs)
        return f"<b>mean</b> {m:.3f}  <b>min</b> {min(vs):.3f}  <b>max</b> {max(vs):.3f}  (n={len(vs)})"

    summary = f"""
<div class="summary">
  <span>job: <b>{job_name}</b></span>
  <span>trials: <b>{len(trials)}</b> ({len(ok_trials)} ok, {len(trials) - len(ok_trials)} failed)</span>
  <span>Track A · {_stats(a_vals)}</span>
  <span>Track B · {_stats(b_vals)}</span>
  <span>viewport: <b>{viewport}</b></span>
</div>
"""

    # Generated CSS for tab visibility + variant sub-tabs + viewport sub-tabs
    css_generated = (
        _tab_visibility_css(panel_keys)
        + "\n\n"
        + _variant_tab_visibility_css(grouped)
        + "\n\n"
        + _viewport_visibility_css([t["name"] for t in trials])
    )

    # Panels: dashboard, diversity, then ONE per task (each task panel
    # hosts both variants). Diversity defaults to trial-driven (Option B);
    # `all_tasks=True` flips it to a dataset view of every task in `tasks_dir`.
    panels_html = [
        render_dashboard_panel(trials),
        render_diversity_panel(trials, fallback_tasks_dir=tasks_dir, prefer_dir=all_tasks),
    ]
    for tid, task_trials in grouped.items():
        panels_html.append(render_task_panel(tid, task_trials, viewport))

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>webdev-bench · {job_name}</title>
  <style>{CSS_BASE}
{css_generated}
</style>
</head>
<body>
{_tab_inputs(panel_keys)}
<header class="job">
  <h1>webdev-bench eval report</h1>
  {summary}
</header>
{_tab_bar(panel_keys, display_names)}
<div class="panels">
{''.join(panels_html)}
</div>
</body>
</html>"""


# ── CLI ─────────────────────────────────────────────────────────────────────


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("job_dir", help="path to jobs/<job-name>/")
    ap.add_argument("--output", "-o", default=None,
                    help="output HTML path (default: eval/reports/<job-name>.html)")
    ap.add_argument("--viewport", default="desktop", choices=["desktop", "tablet", "mobile"],
                    help="which viewport's screenshots to show in per-trial tabs (default: desktop)")
    ap.add_argument("--tasks-dir", default="tasks",
                    help="fallback tasks dir for the Diversity tab (used as graceful "
                         "fallback when trial paths don't resolve, or as the primary "
                         "source when --all-tasks is set). default: tasks/")
    ap.add_argument("--all-tasks", action="store_true",
                    help="Diversity tab shows EVERY task in --tasks-dir (dataset "
                         "overview, Option A) instead of only the tasks this eval ran "
                         "(default, Option B). Useful for benchmark-overview reports.")
    args = ap.parse_args()

    job_dir = Path(args.job_dir).resolve()
    if not job_dir.is_dir():
        print(f"error: {job_dir} is not a directory", file=sys.stderr)
        return 2

    trials = load_trials(job_dir)
    if not trials:
        print(f"error: no trials found under {job_dir}", file=sys.stderr)
        return 2

    out_path = Path(args.output) if args.output else (
        Path("eval/reports") / f"{job_dir.name}.html"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tasks_dir = Path(args.tasks_dir).resolve() if args.tasks_dir else None

    html = render_job(
        trials, job_dir.name, args.viewport,
        tasks_dir=tasks_dir,
        all_tasks=args.all_tasks,
    )
    out_path.write_text(html)

    size_kb = out_path.stat().st_size / 1024
    ok = sum(1 for t in trials if t.get("reward"))
    failed = len(trials) - ok
    print(f"✓ wrote {out_path}  ({size_kb:.0f} KB, {len(trials)} trial(s): {ok} ok, {failed} failed)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
