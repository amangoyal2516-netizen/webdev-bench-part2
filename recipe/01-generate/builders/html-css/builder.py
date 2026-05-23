"""HTML/CSS builder — two-pass + per-page parallel.

For a given task_id (under recipe/runs/<task_id>/ with a design.json):

  1. PASS 1 — design system call: one Claude call with the
     `design_system.md` prompt + the cached asset menu. Returns a JSON
     envelope with styles_css, design_notes, and assets_picked (the full
     asset set the site will use).

  2. PASS 2..N — per-page calls in parallel: one Claude call per page
     with the `page.md` prompt + the cached asset menu. Each receives the
     page spec + design system outputs and returns RAW HTML only.

  3. Vendor assets declared in assets_picked, write source/<page>.html
     and source/styles.css, run the structural validator.

Per-call meta + transcripts are persisted to _builder_meta.json and
_builder_transcript.json next to design.json.

CLI:
    python recipe/01-generate/builders/html-css/builder.py --task task_1
    python recipe/01-generate/builders/html-css/builder.py --task task_2 --no-save

Reads ANTHROPIC_API_KEY from the environment.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import anthropic
import jsonschema

# Sibling modules live in the same dir; package path is unimportable
# (leading-digit dirs `01-generate`, `html-css`).
HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import asset_menu as menu  # noqa: E402
from validators import validate  # noqa: E402

DESIGN_SYSTEM_PROMPT = HERE / "prompts" / "design_system.md"
PAGE_PROMPT = HERE / "prompts" / "page.md"
DESIGN_SYSTEM_SCHEMA = HERE / "schemas" / "design-system.schema.json"
DESIGN_SCHEMA_PATH = HERE.parent.parent / "schemas" / "design-doc.schema.json"
# parents: 0=builders, 1=01-generate, 2=recipe, 3=webdev-bench
REPO_ROOT = HERE.parents[3]
DEFAULT_RUNS_DIR = REPO_ROOT / "recipe" / "runs"

DEFAULT_MODEL = "claude-opus-4-7"
# Publicly documented Opus 4.x max output (no beta headers). Headroom over
# what we observe (~10-15 k design CSS, ~8-12 k pages) for safety margin.
DEFAULT_MAX_TOKENS_DESIGN = 32_000
DEFAULT_MAX_TOKENS_PAGE = 32_000
DEFAULT_MAX_ITERATIONS = 2

# Reference patterns the page validator parses out of the HTML. Tolerate
# an optional `#fragment` or `?query` after the extension (e.g.
# `library-big.svg#icon` for SVG sprite use).
_HTML_PHOTO_RE  = re.compile(r'["\']\./assets/photos/([^"\'/?#.]+)(?:\.\w+)?(?:[?#][^"\']*)?["\']')
_HTML_ICON_RE   = re.compile(r'["\']\./assets/icons/([^"\'/?#.]+)(?:\.\w+)?(?:[?#][^"\']*)?["\']')
_HTML_AVATAR_RE = re.compile(r'["\']\./assets/avatars/([^"\'/?#.]+)(?:\.\w+)?(?:[?#][^"\']*)?["\']')

_JSON_BLOCK = re.compile(r"```(?:json)?\s*(\{.*\})\s*```", re.DOTALL)
_FENCE_RE = re.compile(r"```(?:html)?\s*(.*?)\s*```", re.DOTALL)


def _display_path(p: Path) -> str:
    """For meta logs: relative-to-repo when running locally, absolute when
    running under a Modal Volume mount (e.g. /cache/recipe/task_3/...).
    """
    try:
        return str(p.relative_to(REPO_ROOT))
    except ValueError:
        return str(p)


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def load_design(path: Path) -> dict[str, Any]:
    schema = json.loads(DESIGN_SCHEMA_PATH.read_text())
    doc = json.loads(path.read_text())
    jsonschema.Draft202012Validator(schema).validate(doc)
    return doc


def extract_json(text: str) -> dict[str, Any]:
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    m = _JSON_BLOCK.search(text)
    if m:
        return json.loads(m.group(1))
    raise ValueError(
        f"response was not valid JSON and contained no ```json fence. "
        f"first 200 chars: {text[:200]!r}"
    )


def extract_html(text: str) -> str:
    """Strip whitespace and, defensively, any ```html fence the model added."""
    text = text.strip()
    if text.startswith("```"):
        m = _FENCE_RE.match(text)
        if m:
            text = m.group(1).strip()
    return text


def html_well_formed_enough(text: str) -> str | None:
    """Return None if OK, else an error string describing the issue."""
    if not text.lower().startswith("<!doctype"):
        return f"output did not start with <!doctype html> (starts with {text[:60]!r})"
    if "</html>" not in text.lower():
        return "output did not contain a closing </html> tag"
    return None


def design_system_asset_errors(assets_picked: dict[str, Any]) -> list[str]:
    """Every photo/font/icon/avatar in assets_picked must exist in the
    on-disk pool. Catches design-system hallucinations (e.g., picking a
    Lucide icon name that has been renamed) before pages run against a
    bogus menu."""
    errs: list[str] = []
    for pid in assets_picked.get("photos") or []:
        if menu.resolve_photo(pid) is None:
            errs.append(f"unknown-photo: '{pid}' not in photo pool")
    for entry in assets_picked.get("fonts") or []:
        family = entry.get("family")
        slug = menu.font_slug(family) if family else None
        for w in entry.get("weights") or []:
            if menu.resolve_font(slug, int(w)) is None:
                errs.append(f"unknown-font: family='{family}' weight={w} not in font pool")
    for name in assets_picked.get("icons") or []:
        if menu.resolve_icon(name) is None:
            errs.append(f"unknown-icon: '{name}' not in icon pool")
    for aid in assets_picked.get("avatars") or []:
        if menu.resolve_avatar(aid) is None:
            errs.append(f"unknown-avatar: '{aid}' not in avatar pool")
    seen: set[str] = set()
    out: list[str] = []
    for e in errs:
        if e not in seen:
            seen.add(e)
            out.append(e)
    return out


def page_ref_errors(html: str, assets_picked: dict[str, Any]) -> list[str]:
    """Cheap pre-validation: every photo/icon/avatar this page references must
    be in the design-system's `assets_picked`. Catches hallucinated asset
    names before vendoring runs and the build hard-fails."""
    errs: list[str] = []
    ok_photos  = set(assets_picked.get("photos")  or [])
    ok_icons   = set(assets_picked.get("icons")   or [])
    ok_avatars = set(assets_picked.get("avatars") or [])
    for pid in _HTML_PHOTO_RE.findall(html):
        if pid not in ok_photos:
            errs.append(f"unknown-photo: '{pid}' not in assets_picked.photos")
    for name in _HTML_ICON_RE.findall(html):
        if name not in ok_icons:
            errs.append(f"unknown-icon: '{name}' not in assets_picked.icons")
    for aid in _HTML_AVATAR_RE.findall(html):
        if aid not in ok_avatars:
            errs.append(f"unknown-avatar: '{aid}' not in assets_picked.avatars")
    # Deduplicate while preserving order; up to ~10 distinct issues retained.
    seen: set[str] = set()
    out: list[str] = []
    for e in errs:
        if e not in seen:
            seen.add(e)
            out.append(e)
        if len(out) >= 10:
            break
    return out


# ---------------------------------------------------------------------------
# Pass 1 — design system
# ---------------------------------------------------------------------------


def build_system_blocks(prompt_path: Path) -> list[dict[str, Any]]:
    """Cached asset menu, then role-specific prompt.

    Putting the menu first means the cached prefix is just `[menu]`. Every
    builder call (design-system + every page + every design) shares that
    same prefix, so the cache hits across the entire run.
    """
    return [
        {
            "type": "text",
            "text": f"## Asset menu\n\n{menu.asset_menu()}",
            "cache_control": {"type": "ephemeral"},
        },
        {"type": "text", "text": prompt_path.read_text()},
    ]


def make_design_system_user_msg(design: dict[str, Any]) -> str:
    return (
        "## Design doc\n\n```json\n"
        + json.dumps(design, indent=2)
        + "\n```\n\n"
        "Produce the design-system envelope now. Return only the JSON object."
    )


def make_design_system_correction(design: dict[str, Any], errs: list[str]) -> str:
    err_lines = "\n".join(f"  {i + 1}. {e}" for i, e in enumerate(errs[:10]))
    return (
        f"Your previous response failed {len(errs)} check(s):\n\n{err_lines}\n\n"
        "## Original design doc (still applies)\n\n```json\n"
        + json.dumps(design, indent=2)
        + "\n```\n\nReturn a corrected envelope as a single JSON object only — "
        "no prose, no markdown fences. Fix every listed error."
    )


def call_design_system(
    client: anthropic.Anthropic,
    design: dict[str, Any],
    *,
    model: str,
    max_tokens: int,
    max_iterations: int,
) -> dict[str, Any]:
    system_blocks = build_system_blocks(DESIGN_SYSTEM_PROMPT)
    schema = json.loads(DESIGN_SYSTEM_SCHEMA.read_text())
    transcript: list[dict[str, Any]] = [
        {"role": "user", "content": make_design_system_user_msg(design)}
    ]
    envelope: dict[str, Any] | None = None
    errors: list[str] = []
    iterations_used = 0
    total_in = total_out = 0
    total_cache_read = total_cache_create = 0
    t0 = time.time()

    for it in range(1, max_iterations + 1):
        iterations_used = it
        with client.messages.stream(
            model=model,
            max_tokens=max_tokens,
            system=system_blocks,
            messages=transcript,
        ) as stream:
            for _ in stream:
                pass
            resp = stream.get_final_message()
        total_in += resp.usage.input_tokens
        total_out += resp.usage.output_tokens
        total_cache_read += getattr(resp.usage, "cache_read_input_tokens", 0) or 0
        total_cache_create += getattr(resp.usage, "cache_creation_input_tokens", 0) or 0
        raw = resp.content[0].text
        transcript.append({"role": "assistant", "content": raw})

        try:
            envelope = extract_json(raw)
            jsonschema.Draft202012Validator(schema).validate(envelope)
        except (ValueError, jsonschema.ValidationError) as e:
            errors = [f"envelope-parse: {e}"]
            envelope = None
            if it < max_iterations:
                transcript.append(
                    {"role": "user", "content": make_design_system_correction(design, errors)}
                )
            continue

        # Schema passed — now check every picked asset exists in the pool.
        errors = design_system_asset_errors(envelope["assets_picked"])
        if not errors:
            break
        envelope = None
        if it < max_iterations:
            transcript.append(
                {"role": "user", "content": make_design_system_correction(design, errors)}
            )

    return {
        "envelope": envelope,
        "errors": errors,
        "iterations_used": iterations_used,
        "elapsed_s": round(time.time() - t0, 2),
        "input_tokens": total_in,
        "output_tokens": total_out,
        "cache_read_input_tokens": total_cache_read,
        "cache_creation_input_tokens": total_cache_create,
        "transcript": transcript,
    }


# ---------------------------------------------------------------------------
# Pass 2..N — per-page calls
# ---------------------------------------------------------------------------


def make_page_user_msg(
    design: dict[str, Any],
    page: dict[str, Any],
    design_system: dict[str, Any],
) -> str:
    page_names = [p["name"] for p in design["pages"]]
    # `mode` was removed from the schema in favour of an explicit `palette`
    # (see decision in author.md). Palette is conveyed implicitly via the
    # shared styles.css the page sees below.
    palette = design.get("palette") or {}
    return (
        f"## Site\n\n"
        f"description: {design['description']}\n"
        f"palette: {json.dumps(palette)}\n"
        f"all pages: {', '.join(page_names)}\n\n"
        f"## Target page\n\n```json\n{json.dumps(page, indent=2)}\n```\n\n"
        f"## Design notes\n\n{design_system['design_notes']}\n\n"
        f"## Shared styles.css\n\n```css\n{design_system['styles_css']}\n```\n\n"
        f"## assets_picked\n\n```json\n{json.dumps(design_system['assets_picked'], indent=2)}\n```\n\n"
        f"Produce the raw HTML for page '{page['name']}' now. "
        "Remember: start with <!doctype html>, end with </html>, nothing else."
    )


def make_page_correction(
    design: dict[str, Any],
    page: dict[str, Any],
    design_system: dict[str, Any],
    errors: list[str],
) -> str:
    err_lines = "\n".join(f"  - {e}" for e in errors[:10])
    base = make_page_user_msg(design, page, design_system)
    return (
        f"Your previous response had {len(errors)} issue(s):\n{err_lines}\n\n"
        "Return ONLY the raw HTML for this page: start with <!doctype html>, "
        "end with </html>, no JSON wrapper, no markdown fences, no commentary, "
        "no preamble or postamble. Reference only assets that appear in "
        "`assets_picked` exactly — do not invent photo IDs, icon names, or "
        "avatar IDs.\n\n"
        + base
    )


def call_page(
    client: anthropic.Anthropic,
    design: dict[str, Any],
    page: dict[str, Any],
    design_system: dict[str, Any],
    *,
    model: str,
    max_tokens: int,
    max_iterations: int,
) -> dict[str, Any]:
    system_blocks = build_system_blocks(PAGE_PROMPT)
    transcript: list[dict[str, Any]] = [
        {"role": "user", "content": make_page_user_msg(design, page, design_system)}
    ]
    html: str | None = None
    last_errors: list[str] = []
    retry_reason: str | None = None
    iterations_used = 0
    total_in = total_out = 0
    total_cache_read = total_cache_create = 0
    t0 = time.time()

    for it in range(1, max_iterations + 1):
        iterations_used = it
        with client.messages.stream(
            model=model,
            max_tokens=max_tokens,
            system=system_blocks,
            messages=transcript,
        ) as stream:
            for _ in stream:
                pass
            resp = stream.get_final_message()
        total_in += resp.usage.input_tokens
        total_out += resp.usage.output_tokens
        total_cache_read += getattr(resp.usage, "cache_read_input_tokens", 0) or 0
        total_cache_create += getattr(resp.usage, "cache_creation_input_tokens", 0) or 0
        raw = resp.content[0].text
        transcript.append({"role": "assistant", "content": raw})

        candidate = extract_html(raw)
        well_formed = html_well_formed_enough(candidate)
        if well_formed is not None:
            last_errors = [well_formed]
        else:
            last_errors = page_ref_errors(candidate, design_system["assets_picked"])

        if not last_errors:
            html = candidate
            break

        retry_reason = last_errors[0]
        if it < max_iterations:
            transcript.append(
                {"role": "user",
                 "content": make_page_correction(design, page, design_system, last_errors)}
            )

    return {
        "name": page["name"],
        "html": html,
        "ok": html is not None,
        "error": "; ".join(last_errors) if last_errors and html is None else None,
        "retry_reason": retry_reason if iterations_used > 1 and html else None,
        "iterations_used": iterations_used,
        "elapsed_s": round(time.time() - t0, 2),
        "input_tokens": total_in,
        "output_tokens": total_out,
        "cache_read_input_tokens": total_cache_read,
        "cache_creation_input_tokens": total_cache_create,
        "transcript": transcript,
    }


# ---------------------------------------------------------------------------
# Vendor assets
# ---------------------------------------------------------------------------


def vendor_assets(assets_picked: dict[str, Any], task_dir: Path) -> list[str]:
    """Copy every picked asset into task_dir/source/assets/<kind>/.

    The HTML produced by the page calls references assets as
    `./assets/...` (relative to the .html files in source/) — so the
    vendored directory must sit alongside the .html files inside
    source/, not at the task-dir root.
    """
    errs: list[str] = []
    assets_root = task_dir / "source" / "assets"

    photo_dst = assets_root / "photos"
    for pid in assets_picked.get("photos") or []:
        rec = menu.resolve_photo(pid)
        if rec is None:
            errs.append(f"unknown-photo: '{pid}' is not in the photo pool")
            continue
        photo_dst.mkdir(parents=True, exist_ok=True)
        shutil.copy2(menu.asset_abs_path(rec), photo_dst / f"{pid}.jpg")

    for entry in assets_picked.get("fonts") or []:
        family = entry.get("family")
        slug = menu.font_slug(family) if family else None
        for w in entry.get("weights") or []:
            rec = menu.resolve_font(slug, int(w)) if slug else None
            if rec is None:
                errs.append(f"unknown-font: family='{family}' weight={w} not in font pool")
                continue
            src = menu.asset_abs_path(rec)
            # Preserve the per-family subdir layout under assets/fonts/.
            rel_under_pool = src.relative_to(menu.POOL_ROOT / "font-pool")
            dst = assets_root / "fonts" / rel_under_pool
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)

    icon_dst = assets_root / "icons"
    for name in assets_picked.get("icons") or []:
        rec = menu.resolve_icon(name)
        if rec is None:
            errs.append(f"unknown-icon: '{name}' is not in the icon pool")
            continue
        icon_dst.mkdir(parents=True, exist_ok=True)
        shutil.copy2(menu.asset_abs_path(rec), icon_dst / f"{name}.svg")

    av_dst = assets_root / "avatars"
    for aid in assets_picked.get("avatars") or []:
        rec = menu.resolve_avatar(aid)
        if rec is None:
            errs.append(f"unknown-avatar: '{aid}' is not in the avatar pool")
            continue
        av_dst.mkdir(parents=True, exist_ok=True)
        shutil.copy2(menu.asset_abs_path(rec), av_dst / f"{aid}.svg")

    return errs


def clear_outputs(task_dir: Path) -> None:
    for sub in ("source", "assets"):
        d = task_dir / sub
        if d.exists():
            shutil.rmtree(d)


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


def _strip_transcript(d: dict[str, Any]) -> dict[str, Any]:
    """Drop the transcript field for meta.json — kept separately in transcripts.json."""
    return {k: v for k, v in d.items() if k != "transcript"}


def build_one(
    design_path: Path,
    *,
    model: str = DEFAULT_MODEL,
    max_tokens_design: int = DEFAULT_MAX_TOKENS_DESIGN,
    max_tokens_page: int = DEFAULT_MAX_TOKENS_PAGE,
    max_iterations: int = DEFAULT_MAX_ITERATIONS,
    page_workers: int | None = None,
    save: bool = True,
) -> dict[str, Any]:
    design_path = design_path.resolve()
    task_dir = design_path.parent
    design = load_design(design_path)
    client = anthropic.Anthropic()

    transcripts: dict[str, Any] = {}
    t0 = time.time()

    # PASS 1 — design system
    clear_outputs(task_dir)
    try:
        ds = call_design_system(
            client, design,
            model=model, max_tokens=max_tokens_design, max_iterations=max_iterations,
        )
    except Exception as e:
        meta = {
            "design_path": _display_path(design_path),
            "model": model,
            "elapsed_s": round(time.time() - t0, 2),
            "design_system": {"crash": f"{type(e).__name__}: {e}"},
            "pages": {},
            "ok": False,
            "errors": [f"design-system-crash: {type(e).__name__}: {e}"],
        }
        if save:
            (task_dir / "_builder_meta.json").write_text(json.dumps(meta, indent=2) + "\n")
        raise
    transcripts["design_system"] = ds["transcript"]

    if ds["envelope"] is None:
        meta = {
            "design_path": _display_path(design_path),
            "model": model,
            "elapsed_s": round(time.time() - t0, 2),
            "design_system": _strip_transcript(ds),
            "pages": {},
            "ok": False,
            "errors": ds["errors"],
        }
        if save:
            (task_dir / "_builder_meta.json").write_text(json.dumps(meta, indent=2) + "\n")
            (task_dir / "_builder_transcript.json").write_text(json.dumps(transcripts, indent=2) + "\n")
        return {**meta, "task_dir": str(task_dir)}

    design_system = ds["envelope"]

    # Vendor + write styles.css before per-page calls so any rendering smoke
    # tests against an early page work.
    vendor_errs = vendor_assets(design_system["assets_picked"], task_dir)
    source_dir = task_dir / "source"
    source_dir.mkdir(parents=True, exist_ok=True)
    (source_dir / "styles.css").write_text(design_system["styles_css"])

    # PASS 2..N — per page in parallel
    workers = page_workers or len(design["pages"])
    pages_meta: dict[str, Any] = {}
    page_results: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(
                call_page,
                client, design, page, design_system,
                model=model, max_tokens=max_tokens_page, max_iterations=max_iterations,
            ): page["name"]
            for page in design["pages"]
        }
        for fut in as_completed(futures):
            pr = fut.result()
            page_results.append(pr)
            pages_meta[pr["name"]] = _strip_transcript(pr)
            transcripts[f"page:{pr['name']}"] = pr["transcript"]

    # Write every page that succeeded
    for pr in page_results:
        if pr["ok"]:
            (source_dir / f"{pr['name']}.html").write_text(pr["html"])

    # Final structural validation
    errors = vendor_errs + validate(task_dir, design, design_system["assets_picked"])
    page_failures = [pr["name"] for pr in page_results if not pr["ok"]]
    for n in page_failures:
        errors.append(f"page-call-failed: {n}: {pages_meta[n].get('error')}")

    elapsed = round(time.time() - t0, 2)
    ok = not errors

    meta = {
        "design_path": _display_path(design_path),
        "model": model,
        "elapsed_s": elapsed,
        "design_system": _strip_transcript(ds),
        "pages": pages_meta,
        "ok": ok,
        "errors": errors,
    }

    if save:
        (task_dir / "_builder_meta.json").write_text(json.dumps(meta, indent=2) + "\n")
        (task_dir / "_builder_transcript.json").write_text(json.dumps(transcripts, indent=2) + "\n")

    return {**meta, "task_dir": str(task_dir)}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__.split("\n")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--task", required=True, help="task id (e.g. task_1) under --runs-dir")
    ap.add_argument("--runs-dir", default=str(DEFAULT_RUNS_DIR))
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--max-tokens-design", type=int, default=DEFAULT_MAX_TOKENS_DESIGN)
    ap.add_argument("--max-tokens-page",   type=int, default=DEFAULT_MAX_TOKENS_PAGE)
    ap.add_argument("--max-iterations",    type=int, default=DEFAULT_MAX_ITERATIONS)
    ap.add_argument("--page-workers",      type=int, default=None,
                    help="parallel page workers (default: one per page)")
    ap.add_argument("--no-save", action="store_true")
    args = ap.parse_args()

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("error: ANTHROPIC_API_KEY not set", file=sys.stderr)
        return 2

    design_path = Path(args.runs_dir) / args.task / "design.json"
    if not design_path.is_file():
        print(f"error: design not found at {design_path}", file=sys.stderr)
        return 2

    result = build_one(
        design_path,
        model=args.model,
        max_tokens_design=args.max_tokens_design,
        max_tokens_page=args.max_tokens_page,
        max_iterations=args.max_iterations,
        page_workers=args.page_workers,
        save=not args.no_save,
    )

    status = "ok" if result["ok"] else f"FAILED ({len(result['errors'])} err)"
    ds = result["design_system"]
    n_pages_ok = sum(1 for v in result["pages"].values() if v["ok"])
    n_pages = len(result["pages"])
    print(
        f"[{args.task}] {status} | {result['elapsed_s']}s wall\n"
        f"  design-system: {ds['iterations_used']}i {ds['elapsed_s']}s "
        f"in={ds['input_tokens']} (read={ds['cache_read_input_tokens']}, "
        f"create={ds['cache_creation_input_tokens']}) out={ds['output_tokens']}\n"
        f"  pages: {n_pages_ok}/{n_pages} ok"
    )
    for name, p in result["pages"].items():
        marker = "✓" if p["ok"] else "✗"
        extra = f" (retry: {p['retry_reason']})" if p.get("retry_reason") else ""
        print(
            f"    {marker} {name:24s} {p['iterations_used']}i {p['elapsed_s']:>5}s "
            f"in={p['input_tokens']:>6} (read={p['cache_read_input_tokens']:>6}) "
            f"out={p['output_tokens']:>5}{extra}"
        )
    for e in result["errors"][:6]:
        print(f"  → {e}")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
