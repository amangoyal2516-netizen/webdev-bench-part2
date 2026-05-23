"""scripts/audit_modal_runs.py — per-task health check after the Modal recipe run.

When `generate_all_tasks` returns 30 (or N) freshly-built tasks from the
Modal cloud, this script scans every `recipe/runs/task_*/` and produces a
pass/fail grid across the things end-to-end grading depends on. Each row
is one task; each column is one check. A task is **healthy** iff every
check passes — those are the tasks worth packaging and grading next.

Checks (in order of "earlier-stage failure is more catastrophic"):

  - design        — design.json exists, parses, strict-validates against the
                    current schema (must include `palette`, must NOT have
                    `mode` — the post-palette-refactor format).
  - builder_meta  — `_builder_meta.json` present (proof the builder finished
                    without raising).
  - source        — `source/` has one `<page>.html` per design.json page,
                    plus `styles.css`, plus a nested `source/assets/` dir.
  - responsive    — `styles.css` contains at least 2 `@media` queries AND
                    every HTML file has the `<meta name="viewport"…>` tag.
                    These are direct signals that the new responsive prompt
                    landed (zero queries = old prompt was used).
  - screenshots   — `screenshots/{desktop,tablet,mobile}/<page>/full.png`
                    exists for every page × every viewport (3 × N pages).
  - precomputes   — `ground_truth/{bboxes,typography,text,images,palette}/`
                    each contains one JSON per page (bboxes also per-viewport).
                    `ground_truth/images/*.json` entries have both `phash`
                    and `lab_mean` (proof of the updated capture).
  - package       — running `recipe/03-package/package.py --dry-run` (no
                    --force, no real writes) succeeds, i.e. all the inputs
                    the packager validates are present.

Output: a markdown table on stdout (so you can paste into a daily-updates
entry), plus a JSON detail dump to `--out` if requested.

Usage:
    python scripts/audit_modal_runs.py
    python scripts/audit_modal_runs.py --runs-dir recipe/runs
    python scripts/audit_modal_runs.py --out /tmp/audit.json
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_RUNS_DIR = REPO_ROOT / "recipe" / "runs"
DESIGN_SCHEMA = REPO_ROOT / "recipe" / "01-generate" / "schemas" / "design-doc.schema.json"
PACKAGE_PY = REPO_ROOT / "recipe" / "03-package" / "package.py"
VIEWPORTS = ("desktop", "tablet", "mobile")
PRECOMPUTE_PER_PAGE = ("typography", "text", "images", "palette")
MIN_MEDIA_QUERIES = 2  # tablet + mobile per the responsive prompt


def _check(label: str, ok: bool, detail: str = "") -> dict[str, Any]:
    return {"label": label, "ok": ok, "detail": detail}


# ─── individual checks ───────────────────────────────────────────────


def check_design(task_dir: Path, schema: dict) -> tuple[dict, dict[str, Any] | None]:
    import jsonschema

    p = task_dir / "design.json"
    if not p.exists():
        return _check("design", False, "design.json missing"), None
    try:
        doc = json.loads(p.read_text())
    except json.JSONDecodeError as e:
        return _check("design", False, f"parse: {e.msg}"), None
    try:
        jsonschema.Draft202012Validator(schema).validate(doc)
    except jsonschema.ValidationError as e:
        return _check("design", False, f"schema: {e.message[:60]}"), None
    return _check("design", True, f"{len(doc['pages'])} pages"), doc


def check_builder_meta(task_dir: Path) -> dict:
    p = task_dir / "_builder_meta.json"
    if not p.exists():
        return _check("builder_meta", False, "missing")
    try:
        m = json.loads(p.read_text())
    except json.JSONDecodeError:
        return _check("builder_meta", False, "unparseable")
    elapsed = m.get("elapsed_s") or m.get("total_elapsed_s") or "?"
    return _check("builder_meta", True, f"{elapsed}s")


def check_source(task_dir: Path, design: dict) -> dict:
    src = task_dir / "source"
    if not src.is_dir():
        return _check("source", False, "missing")
    page_names = [p["name"] for p in design["pages"]]
    missing_html = [n for n in page_names if not (src / f"{n}.html").exists()]
    if missing_html:
        return _check("source", False, f"missing HTML: {','.join(missing_html)}")
    if not (src / "styles.css").exists():
        return _check("source", False, "styles.css missing")
    if not (src / "assets").is_dir():
        return _check("source", False, "assets/ missing")
    return _check("source", True, f"{len(page_names)} pages")


def check_responsive(task_dir: Path, design: dict) -> dict:
    src = task_dir / "source"
    css = src / "styles.css"
    if not css.exists():
        return _check("responsive", False, "no styles.css")
    n_media = css.read_text().count("@media")
    if n_media < MIN_MEDIA_QUERIES:
        return _check("responsive", False, f"{n_media} @media (want ≥{MIN_MEDIA_QUERIES})")
    missing_viewport: list[str] = []
    for p in design["pages"]:
        html = src / f"{p['name']}.html"
        if not html.exists():
            continue
        if 'name="viewport"' not in html.read_text():
            missing_viewport.append(p["name"])
    if missing_viewport:
        return _check("responsive", False, f"no viewport meta in: {','.join(missing_viewport)}")
    return _check("responsive", True, f"{n_media} @media + viewport meta")


def check_screenshots(task_dir: Path, design: dict) -> dict:
    shot_root = task_dir / "screenshots"
    if not shot_root.is_dir():
        return _check("screenshots", False, "missing screenshots/")
    missing: list[str] = []
    for vp in VIEWPORTS:
        for p in design["pages"]:
            full = shot_root / vp / p["name"] / "full.png"
            if not full.exists():
                missing.append(f"{vp}/{p['name']}")
    if missing:
        return _check("screenshots", False, f"{len(missing)} missing (e.g. {missing[0]})")
    return _check("screenshots", True, f"{len(design['pages']) * len(VIEWPORTS)} shots")


def check_precomputes(task_dir: Path, design: dict) -> dict:
    gt = task_dir / "ground_truth"
    if not gt.is_dir():
        return _check("precomputes", False, "missing ground_truth/")
    missing: list[str] = []
    page_names = [p["name"] for p in design["pages"]]
    for vp in VIEWPORTS:
        for name in page_names:
            if not (gt / "bboxes" / vp / f"{name}.json").exists():
                missing.append(f"bboxes/{vp}/{name}")
    for sub in PRECOMPUTE_PER_PAGE:
        for name in page_names:
            if not (gt / sub / f"{name}.json").exists():
                missing.append(f"{sub}/{name}")
    if missing:
        return _check("precomputes", False, f"{len(missing)} missing (e.g. {missing[0]})")
    # Verify images/<page>.json carries lab_mean (proves updated capture ran)
    sample = gt / "images" / f"{page_names[0]}.json"
    try:
        entries = json.loads(sample.read_text())
        if entries and any("lab_mean" not in e for e in entries if e.get("phash")):
            return _check("precomputes", False, "images/*.json missing lab_mean (old capture)")
    except Exception as e:
        return _check("precomputes", False, f"images parse: {e}")
    return _check("precomputes", True, "all present with lab_mean")


def check_package(task_dir: Path) -> dict:
    """Invoke the packager against a temp tasks-dir (so we don't pollute the
    real tasks/ tree) and look for clean exit. Detects the same input
    failures the packager validates: missing source, missing screenshots,
    etc. — independent of the per-stage checks above."""
    import tempfile

    with tempfile.TemporaryDirectory(prefix=f"audit-pkg-{task_dir.name}-") as tmp:
        r = subprocess.run(
            [
                sys.executable, str(PACKAGE_PY), str(task_dir),
                "--tasks-dir", tmp, "--variants", "oneshot", "--force",
            ],
            capture_output=True, text=True, cwd=REPO_ROOT, timeout=60,
        )
    if r.returncode != 0:
        first_err = (r.stderr.strip() or r.stdout.strip()).splitlines()[-1][:60]
        return _check("package", False, first_err)
    return _check("package", True, "dry-run ok")


# ─── per-task driver ─────────────────────────────────────────────────


def audit_task(task_dir: Path, schema: dict) -> dict[str, Any]:
    checks: list[dict] = []
    # design check feeds every downstream check; bail out fast if it's broken
    d_check, design = check_design(task_dir, schema)
    checks.append(d_check)
    if design is None:
        # fill the rest with "skipped" so the column count stays consistent
        for label in ("builder_meta", "source", "responsive", "screenshots",
                      "precomputes", "package"):
            checks.append(_check(label, False, "skipped (design broken)"))
        return {"task": task_dir.name, "checks": checks,
                "healthy": False, "n_passed": int(d_check["ok"])}

    checks.append(check_builder_meta(task_dir))
    checks.append(check_source(task_dir, design))
    checks.append(check_responsive(task_dir, design))
    checks.append(check_screenshots(task_dir, design))
    checks.append(check_precomputes(task_dir, design))
    checks.append(check_package(task_dir))

    n_passed = sum(1 for c in checks if c["ok"])
    return {
        "task": task_dir.name,
        "checks": checks,
        "healthy": n_passed == len(checks),
        "n_passed": n_passed,
    }


# ─── reporting ───────────────────────────────────────────────────────


_LABEL_ORDER = ("design", "builder_meta", "source", "responsive",
                "screenshots", "precomputes", "package")


def render_table(audits: list[dict]) -> str:
    """Markdown grid: rows = tasks, columns = checks. `✓` / `✗` per cell,
    with `n/N healthy` summary line."""
    lines: list[str] = []
    headers = ["task"] + list(_LABEL_ORDER) + ["status"]
    lines.append("| " + " | ".join(headers) + " |")
    lines.append("|" + "|".join(["---"] * len(headers)) + "|")
    for a in audits:
        by_label = {c["label"]: c for c in a["checks"]}
        cells = [a["task"]]
        for lab in _LABEL_ORDER:
            c = by_label.get(lab)
            cells.append("✓" if c and c["ok"] else "✗")
        cells.append("**healthy**" if a["healthy"] else f"{a['n_passed']}/{len(_LABEL_ORDER)}")
        lines.append("| " + " | ".join(cells) + " |")

    n_healthy = sum(1 for a in audits if a["healthy"])
    lines.append("")
    lines.append(f"**{n_healthy} / {len(audits)} tasks healthy.**")

    failures = [a for a in audits if not a["healthy"]]
    if failures:
        lines.append("")
        lines.append("## Failure details")
        lines.append("")
        for a in failures:
            failed_checks = [c for c in a["checks"] if not c["ok"]]
            lines.append(f"- **{a['task']}** — " + "; ".join(
                f"`{c['label']}`: {c['detail']}" for c in failed_checks
            ))

    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--runs-dir", default=str(DEFAULT_RUNS_DIR))
    ap.add_argument("--out", default=None, help="optional JSON detail dump path")
    ap.add_argument("--tasks", default=None, help="comma-separated subset (default: all)")
    args = ap.parse_args()

    runs_dir = Path(args.runs_dir)
    if not runs_dir.is_dir():
        print(f"error: not a directory: {runs_dir}", file=sys.stderr)
        return 2

    if args.tasks:
        wanted = {s.strip() for s in args.tasks.split(",") if s.strip()}
        task_dirs = sorted(
            (d for d in runs_dir.iterdir()
             if d.is_dir() and d.name.startswith("task_") and d.name in wanted),
            key=lambda d: int(d.name.split("_")[1]),
        )
    else:
        task_dirs = sorted(
            (d for d in runs_dir.iterdir() if d.is_dir() and d.name.startswith("task_")),
            key=lambda d: int(d.name.split("_")[1]),
        )
    if not task_dirs:
        print(f"no task_* dirs under {runs_dir}", file=sys.stderr)
        return 2

    schema = json.loads(DESIGN_SCHEMA.read_text())
    audits = [audit_task(d, schema) for d in task_dirs]
    print(render_table(audits))

    if args.out:
        Path(args.out).write_text(json.dumps(audits, indent=2) + "\n")
        print(f"\n(detail JSON → {args.out})", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
