"""recipe/03-package/package.py

Stamp `recipe/runs/<task_id>/` into a Harbor task at `tasks/<task_id>-oneshot/`.

Ships oneshot-only — the iter variant + its render helper were removed
(the agent doesn't benefit from a per-page render check for the
animation-replication task; cleaner to keep a single variant).

What it does
------------
1. Validates the input run dir has everything we need:
   - `design.json` with `pages[]`, `description`, `allowed_frameworks`
   - `source/*.html` (one per page in design.json) plus a nested
     `source/assets/` (single-mount layout)
   - `screenshots/<vp>/<page>/full.png` + `motion-strip.png` per page
     (full + slices layout, plus the per-page motion strip)
   - `ground_truth/{bboxes,typography,text,images,palette}/…`
2. Copies `tasks/_template/` to `tasks/<task_id>-oneshot/`, substituting
   placeholders ({{ TASK_ID }}, {{ VARIANT }}, {{ DESIGN_DESCRIPTION }},
   {{ PAGES_LIST }}, {{ PAGES_JSON }}, {{ ALLOWED_FRAMEWORKS }},
   {{ ALLOWED_FRAMEWORKS_JSON }}, {{ BASE_IMAGE }}, {{ FONT_DECLARATIONS }},
   {{ ANIMATION_DURATIONS }}).
3. Copies the run's source/ (HTML + CSS + nested assets/), screenshots/,
   and pre-computed grader JSONs into the task's `ground_truth/`.

Usage
-----

    # Stamp the oneshot variant for one task
    python recipe/03-package/package.py recipe/runs/task_1

    # Overwrite existing
    python recipe/03-package/package.py recipe/runs/task_1 --force

    # Stamp every recipe run under recipe/runs/*
    python recipe/03-package/package.py recipe/runs/ --all
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_TEMPLATE_DIR = REPO_ROOT / "tasks" / "_template"
DEFAULT_TASKS_DIR = REPO_ROOT / "tasks"

VARIANTS: tuple[str, ...] = ("oneshot",)
VIEWPORTS: tuple[str, ...] = ("desktop", "tablet", "mobile")

# Files inside the template that should have `{{ PLACEHOLDER }}` substitution
# applied. Everything else is copied byte-for-byte.
TEMPLATABLE_SUFFIXES: tuple[str, ...] = (
    ".toml", ".md", ".json", ".sh", ".py",
    "Dockerfile",  # full filename — matched separately
)

# Paths (relative to template_dir) that documenting the scaffold itself —
# excluded from packaged tasks. The per-task ground_truth/README.md *is*
# shipped because it describes the task's own ground_truth/ contents.
TEMPLATE_SKIP_RELS: frozenset[Path] = frozenset({Path("README.md")})


# ─── validation ───────────────────────────────────────────────────────


class PackageError(RuntimeError):
    """Raised when the run dir can't be packaged (missing files, etc.)."""


def validate_run_dir(run_dir: Path) -> dict[str, Any]:
    """Sanity-check that `run_dir` looks like a completed recipe run.
    Returns the parsed design.json on success."""
    if not run_dir.is_dir():
        raise PackageError(f"not a directory: {run_dir}")

    design_path = run_dir / "design.json"
    if not design_path.exists():
        raise PackageError(f"missing design.json in {run_dir}")
    design = json.loads(design_path.read_text())

    for required in ("description", "pages", "allowed_frameworks"):
        if required not in design:
            raise PackageError(f"design.json missing '{required}'")

    pages = [p["name"] for p in design["pages"]]
    if not pages:
        raise PackageError("design.json has no pages")

    source_dir = run_dir / "source"
    if not source_dir.is_dir():
        raise PackageError(f"missing source/ in {run_dir} — has the builder run?")
    missing_html = [p for p in pages if not (source_dir / f"{p}.html").exists()]
    if missing_html:
        raise PackageError(f"source/ missing HTML for: {', '.join(missing_html)}")

    screenshots_dir = run_dir / "screenshots"
    if not screenshots_dir.is_dir():
        raise PackageError(f"missing screenshots/ — has capture.py run?")
    for vp in VIEWPORTS:
        for page in pages:
            full = screenshots_dir / vp / page / "full.png"
            if not full.exists():
                raise PackageError(f"missing screenshot: {full.relative_to(run_dir)}")
            strip = screenshots_dir / vp / page / "motion-strip.png"
            if not strip.exists():
                raise PackageError(f"missing motion strip: {strip.relative_to(run_dir)}")

    return design


# ─── substitution map ─────────────────────────────────────────────────


def _format_pages_list(pages: list[dict[str, Any]]) -> str:
    """Markdown bullet list with filename + one-line page description."""
    return "\n".join(
        f"- `{p['name']}.html` — {p.get('description', '').strip()}"
        for p in pages
    )


def _format_animation_durations(pages: list[dict[str, Any]]) -> str:
    """Markdown bullet list of per-page animation `duration_ms`.

    Duration is invisible in 5-still-frame motion strips — the agent
    can read direction, magnitude, and easing curve from frame spacing,
    but absolute time cannot be inferred from PNG stills. Leaking the
    authored duration per page keeps the timing sub-task fair. Type,
    easing, and the animated selector are NOT leaked — those remain
    inferable from the motion strip itself.
    """
    bullets = []
    for p in pages:
        anims = p.get("animations") or []
        if not anims:
            continue
        ms = anims[0].get("duration_ms")
        if ms is None:
            continue
        bullets.append(f"- `{p['name']}`: {int(ms)} ms")
    return "\n".join(bullets)


def _format_font_declarations(run_dir: Path | None) -> str:
    """Parse `solution/source/styles.css` for @font-face declarations and
    return a 'Fonts to declare' markdown section. Returns empty string
    when the source CSS has no @font-face rules (task uses system fonts
    only) or run_dir is missing.

    The grader's `typography` criterion reads `getComputedStyle().fontFamily`
    as an exact string. Font family names declared in @font-face don't
    always match the file basename (e.g. `'Fraunces'` declared while the
    file on disk is `source-serif-4-400.woff2`), so the agent can't reliably
    derive the family from filenames alone. Exposing the canonical
    declarations explicitly makes the typography sub-task fair.
    """
    if run_dir is None:
        return ""
    import re as _re
    css_path = run_dir / "source" / "styles.css"
    if not css_path.is_file():
        return ""
    css = css_path.read_text()
    faces = []
    for block in _re.findall(r"@font-face\s*\{([^}]+)\}", css):
        d: dict[str, str] = {}
        m = _re.search(r"font-family\s*:\s*['\"]?([^;'\"]+)['\"]?", block)
        if m: d["family"] = m.group(1).strip()
        m = _re.search(
            r"src\s*:\s*url\(['\"]?([^)'\"]+)['\"]?\)(?:\s*format\(['\"]?([^)'\"]+)['\"]?\))?",
            block,
        )
        if m:
            d["src"] = m.group(1).strip()
            d["fmt"] = m.group(2).strip() if m.group(2) else "woff2"
        m = _re.search(r"font-weight\s*:\s*([^;]+)", block)
        d["weight"] = (m.group(1).strip() if m else "400")
        m = _re.search(r"font-style\s*:\s*([^;]+)", block)
        d["style"] = (m.group(1).strip() if m else "normal")
        if "family" in d and "src" in d:
            faces.append(d)
    if not faces:
        return ""
    css_lines = "\n".join(
        f"@font-face {{ font-family: '{f['family']}'; "
        f"src: url('{f['src']}') format('{f['fmt']}'); "
        f"font-weight: {f['weight']}; "
        f"font-style: {f['style']}; }}"
        for f in faces
    )
    return (
        "## Fonts to declare\n\n"
        "The reference uses these `@font-face` rules. Use them as-is in your CSS — "
        "the family name is what the `typography` grader reads via `getComputedStyle()`, "
        "and it doesn't always match the filename:\n\n"
        "```css\n" + css_lines + "\n```\n"
    )


def build_substitutions(
    design: dict[str, Any],
    task_id: str,
    variant: str,
    run_dir: Path | None = None,
) -> dict[str, str]:
    pages_obj = design["pages"]
    pages_names = [p["name"] for p in pages_obj]
    allowed_frameworks: list[str] = list(design["allowed_frameworks"])

    return {
        "{{ TASK_ID }}":                  task_id,
        "{{ VARIANT }}":                  variant,
        "{{ DESIGN_DESCRIPTION }}":       design["description"].strip(),
        "{{ PAGES_LIST }}":               _format_pages_list(pages_obj),
        "{{ PAGES_JSON }}":               json.dumps(pages_names),
        "{{ ALLOWED_FRAMEWORKS }}":       ", ".join(allowed_frameworks),
        "{{ ALLOWED_FRAMEWORKS_JSON }}":  json.dumps(allowed_frameworks),
        "{{ BASE_IMAGE }}":               "base-html-css-oneshot",
        "{{ FONT_DECLARATIONS }}":        _format_font_declarations(run_dir),
        "{{ ANIMATION_DURATIONS }}":      _format_animation_durations(pages_obj),
    }


def _is_templatable(path: Path) -> bool:
    return path.suffix in TEMPLATABLE_SUFFIXES or path.name == "Dockerfile"


def _render_placeholders(text: str, subs: dict[str, str]) -> str:
    for placeholder, value in subs.items():
        text = text.replace(placeholder, value)
    return text


# ─── copy operations ──────────────────────────────────────────────────


def _copy_template_tree(template_dir: Path, dest: Path, subs: dict[str, str]) -> int:
    """Copy `template_dir` → `dest`. Files whose suffix is in
    TEMPLATABLE_SUFFIXES get placeholder substitution; everything else is
    copied byte-for-byte. Returns count of files written."""
    count = 0
    for src in template_dir.rglob("*"):
        rel = src.relative_to(template_dir)
        if rel in TEMPLATE_SKIP_RELS:
            continue
        target = dest / rel
        if src.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        if _is_templatable(src):
            target.write_text(_render_placeholders(src.read_text(), subs))
            # Preserve the executable bit on .sh scripts.
            if src.suffix == ".sh":
                target.chmod(0o755)
        else:
            shutil.copy2(src, target)
        count += 1
    return count


def _copy_task_artifacts(run_dir: Path, dest: Path, design: dict[str, Any]) -> dict[str, int]:
    """Lay out the packaged task per Harbor 0.7.x conventions:

      <dest>/
        solution/source/        ← Oracle's answer key (HTML/CSS/assets)
        tests/ground_truth/     ← verifier reference data + design.json
        tests/grading/          ← aggregator + criteria + gates
        ground_truth/screenshots/ ← full.png + slices + motion-strip.png
                                    per (vp, page). The Dockerfile COPYs
                                    this into /workspace/reference/ at
                                    build time — primary agent reference.
        ground_truth/source/    ← retained so the agent's env Dockerfile
                                    can COPY .../assets into /workspace/output/

    Returns counts per subtree for the summary line.
    """
    counts: dict[str, int] = {}

    def copy_subtree(src: Path, dst: Path, label: str) -> None:
        if not src.exists():
            counts[label] = 0
            return
        if dst.exists():
            shutil.rmtree(dst)
        shutil.copytree(src, dst)
        counts[label] = sum(1 for _ in dst.rglob("*") if _.is_file())

    # solution/source/ — Oracle answer key, mounted at /solution/source/.
    copy_subtree(run_dir / "source", dest / "solution" / "source", "solution/source")

    # tests/ground_truth/ — what test.sh reads at /tests/ground_truth/.
    tests_gt = dest / "tests" / "ground_truth"
    src_gt = run_dir / "ground_truth"
    for sub in ("bboxes", "typography", "text", "images", "palette", "widget"):
        copy_subtree(src_gt / sub, tests_gt / sub, f"tests/ground_truth/{sub}")
    if (run_dir / "design.json").is_file():
        tests_gt.mkdir(parents=True, exist_ok=True)
        shutil.copy2(run_dir / "design.json", tests_gt / "design.json")
        counts["tests/ground_truth/design.json"] = 1

    # tests/grading/ — project-wide grading package the aggregator needs.
    grading_src = REPO_ROOT / "grading"
    if grading_src.is_dir():
        dst = dest / "tests" / "grading"
        if dst.exists():
            shutil.rmtree(dst)
        shutil.copytree(
            grading_src, dst,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache"),
        )
        counts["tests/grading"] = sum(1 for _ in dst.rglob("*") if _.is_file())

    # ground_truth/screenshots/ + ground_truth/source/assets/ —
    # referenced by the task's environment/Dockerfile via COPY
    # ground_truth/... at build time. Harbor uses `environment/` as the
    # Docker build context, so these files must live INSIDE environment/,
    # not at the task root.
    env_gt = dest / "environment" / "ground_truth"
    copy_subtree(run_dir / "screenshots",
                 env_gt / "screenshots", "environment/ground_truth/screenshots")
    # Only assets/ is needed for the image (HTML files are the Oracle's
    # answer key, copied into solution/source/ above).
    if (run_dir / "source" / "assets").is_dir():
        copy_subtree(run_dir / "source" / "assets",
                     env_gt / "source" / "assets", "environment/ground_truth/source/assets")

    return counts


# ─── per-variant packaging ────────────────────────────────────────────


def package_one(
    run_dir: Path,
    design: dict[str, Any],
    variant: str,
    *,
    task_id: str | None = None,
    template_dir: Path = DEFAULT_TEMPLATE_DIR,
    tasks_dir: Path = DEFAULT_TASKS_DIR,
    force: bool = False,
) -> Path:
    """Package one variant of one design. Returns the destination dir."""
    if variant not in VARIANTS:
        raise PackageError(f"unknown variant {variant!r}; expected one of {VARIANTS}")

    tid = task_id or run_dir.name  # e.g. "task_1"
    dest = tasks_dir / f"{tid}-{variant}"

    if dest.exists():
        if not force:
            raise PackageError(
                f"destination exists: {dest.relative_to(REPO_ROOT)} — pass --force to overwrite"
            )
        shutil.rmtree(dest)
    dest.mkdir(parents=True)

    subs = build_substitutions(design, tid, variant, run_dir=run_dir)

    n_template = _copy_template_tree(template_dir, dest, subs)
    gt_counts = _copy_task_artifacts(run_dir, dest, design)

    gt_summary = ", ".join(f"{k}:{v}" for k, v in gt_counts.items() if v > 0)
    try:
        shown = dest.relative_to(REPO_ROOT)
    except ValueError:
        shown = dest
    print(
        f"  [{variant:7s}]  {shown}  "
        f"(template:{n_template} files, ground_truth: {gt_summary})"
    )
    return dest


def package_run(
    run_dir: Path,
    *,
    variants: tuple[str, ...] = VARIANTS,
    task_id: str | None = None,
    template_dir: Path = DEFAULT_TEMPLATE_DIR,
    tasks_dir: Path = DEFAULT_TASKS_DIR,
    force: bool = False,
) -> list[Path]:
    """Validate `run_dir` once, then stamp every variant."""
    design = validate_run_dir(run_dir)
    tid = task_id or run_dir.name
    print(f"packaging {tid} ({len(design['pages'])} pages, variants={','.join(variants)})")
    dests = [
        package_one(
            run_dir, design, v,
            task_id=tid,
            template_dir=template_dir,
            tasks_dir=tasks_dir,
            force=force,
        )
        for v in variants
    ]
    return dests


# ─── CLI ──────────────────────────────────────────────────────────────


def _discover_run_dirs(root: Path) -> list[Path]:
    """Used by --all: every immediate subdir of `root` that has design.json."""
    return sorted(
        p for p in root.iterdir()
        if p.is_dir() and (p / "design.json").exists()
    )


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__.split("\n")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument(
        "target",
        help="path to a single recipe run dir (e.g. recipe/runs/task_1) "
             "OR a parent dir with --all (e.g. recipe/runs)",
    )
    ap.add_argument(
        "--all", action="store_true",
        help="treat `target` as a parent dir and package every subdir with a design.json",
    )
    ap.add_argument(
        "--variants", default=",".join(VARIANTS),
        help=f"comma-separated subset of {VARIANTS} (default: both)",
    )
    ap.add_argument(
        "--tasks-dir", default=None,
        help=f"output dir (default: {DEFAULT_TASKS_DIR.relative_to(REPO_ROOT)})",
    )
    ap.add_argument(
        "--template", default=None,
        help=f"template dir (default: {DEFAULT_TEMPLATE_DIR.relative_to(REPO_ROOT)})",
    )
    ap.add_argument(
        "--force", action="store_true",
        help="overwrite existing task dirs",
    )
    args = ap.parse_args()

    target = Path(args.target).resolve()
    tasks_dir = Path(args.tasks_dir).resolve() if args.tasks_dir else DEFAULT_TASKS_DIR
    template_dir = Path(args.template).resolve() if args.template else DEFAULT_TEMPLATE_DIR

    variants = tuple(v.strip() for v in args.variants.split(",") if v.strip())
    unknown = [v for v in variants if v not in VARIANTS]
    if unknown:
        print(f"error: unknown variants {unknown}; expected subset of {VARIANTS}", file=sys.stderr)
        return 2

    if not template_dir.is_dir():
        print(f"error: template dir not found: {template_dir}", file=sys.stderr)
        return 2

    if args.all:
        runs = _discover_run_dirs(target)
        if not runs:
            print(f"error: no recipe runs found under {target}", file=sys.stderr)
            return 2
    else:
        runs = [target]

    failed = 0
    for run_dir in runs:
        try:
            package_run(
                run_dir,
                variants=variants,
                template_dir=template_dir,
                tasks_dir=tasks_dir,
                force=args.force,
            )
        except PackageError as e:
            failed += 1
            print(f"FAIL  {run_dir.name}: {e}", file=sys.stderr)

    if failed:
        print(f"\n{failed}/{len(runs)} runs failed", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
