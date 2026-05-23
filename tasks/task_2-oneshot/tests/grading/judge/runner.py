"""Track B orchestrator — runs the judge across all 6 criteria and all
pages, returns per-criterion scores.

Per-criterion score = `(mean(raw_verdict) - 1) / 4` where each raw
verdict is on the 1-5 scale. The normalization maps a perfect-match
pack mean of 5 to score 1.0 and a no-match pack mean of 1 to score
0.0. Overall Track B aggregation (weighted_mean × gate) happens in
`grading/aggregator.py`.

Scopes the runner handles (each pack declares its scope in its JSON):

  - `per_page`               — render agent at desktop, ask every question
                               once per page. Mean across pages.
  - `per_page_per_viewport`  — render agent at desktop + tablet + mobile,
                               ask every question once per (page, viewport).
                               Mean within viewport → mean across viewports
                               → mean across pages. Catches cases where the
                               agent's @media adaptations diverge from the
                               reference's (collapsed nav, palette swap,
                               typography rescale, etc.).
  - `per_component`          — `component_presence`. Loads design.json,
                               substitutes each `design.pages[i].components[j]`
                               description into the `{component}` placeholder
                               in every question, asks the judge per
                               (page, component). Mean within component →
                               mean across components → mean across pages.
                               Unique cache key per (template_id, comp_idx).
  - `per_image`              — `image_content_fidelity`. v1 simplified:
                               aliases to `per_page` at desktop. The pack's
                               questions read at page-level ("is the same
                               photograph visible somewhere", "no extras",
                               etc.); full per-image bbox-pairing dispatch
                               is a future enhancement.
  - `per_page_motion_strip`  — `animation_fidelity`. The judge sees two
                               5-panel motion strips (reference + agent),
                               each showing the page's load-triggered
                               entrance animation at t=0/25/50/75/100 %.
                               Reference strip lives at
                               `<gt>/screenshots/desktop/<page>/motion-strip.png`;
                               agent strip is rendered fresh by importing
                               `grading.criteria.animation_fidelity`'s
                               strip generator.

Scope dispatch is data-driven off the pack's `scope` field — to flip a
criterion's viewport coverage, edit its question_pack JSON; no code change.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

QUESTION_PACKS_DIR = Path(__file__).resolve().parent / "question_packs"

VIEWPORT_SIZES = {
    "desktop": (1440, 900),
    "tablet": (768, 1024),
    "mobile": (375, 812),
}

# Criteria the runner actively scores via the judge
ACTIVE_CRITERIA: tuple[str, ...] = (
    "layout_structure",
    "component_presence",
    "color_palette",
    "typography",
    "image_content_fidelity",
    "visible_text_fidelity",
    "animation_fidelity",
)

# Criteria still stubbed to 1.0 — currently empty.
# Keep the constant around so future scopes (e.g. animation_fidelity) can
# land here without touching the dispatcher.
STUBBED_CRITERIA: tuple[str, ...] = ()


def load_question_pack(criterion: str) -> dict[str, Any]:
    return json.loads((QUESTION_PACKS_DIR / f"{criterion}.json").read_text())


def render_agent_page(agent_dir: Path, page_name: str, viewport: str) -> Path:
    """Render agent's <page>.html at `viewport`, return a temp PNG path."""
    from playwright.sync_api import sync_playwright

    w, h = VIEWPORT_SIZES[viewport]
    html = agent_dir / f"{page_name}.html"

    out = Path(tempfile.mkstemp(prefix=f"agent_{page_name}_{viewport}_", suffix=".png")[1])
    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(viewport={"width": w, "height": h})
        page = ctx.new_page()
        page.goto(f"file://{html.resolve()}")
        page.wait_for_load_state("networkidle")
        page.screenshot(path=str(out), full_page=True)
        browser.close()
    return out


def reference_screenshot_path(gt_dir: Path, page_name: str, viewport: str) -> Path | None:
    """Find the reference screenshot for a (page, viewport). Tries the
    chunked layout first, then the legacy flat layout."""
    new_layout = gt_dir / "screenshots" / viewport / page_name / "full.png"
    if new_layout.exists():
        return new_layout
    legacy = gt_dir / "screenshots" / viewport / f"{page_name}.png"
    if legacy.exists():
        return legacy
    return None


def _ask_pack_per_page(
    client,
    pack: dict[str, Any],
    ref_path: Path,
    agent_path: Path,
    page_name: str,
    extra: dict[str, Any] | None = None,
    questions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Ask every question in `pack` once, using cache-aware batching.

    For each question:
      - If cached, return the cached verdict immediately.
      - Otherwise queue it for a single batched API call.

    If anything needs to be asked, fire ONE API call carrying every
    cache-missed question. The per-question cache is preserved (each
    verdict gets its own cache file), so subsequent runs hit the cache
    one-question-at-a-time. Net effect on fresh runs: 1 API call per
    pack-on-an-image-pair instead of N — same image tokens billed once
    instead of N times.

    `questions` overrides `pack["questions"]` — used by per-component
    scoring where the runner pre-substitutes `{component}` into the text
    and generates per-component unique q_ids.
    """
    extra = extra or {}
    qs = questions if questions is not None else pack["questions"]

    # Hash both images once; reuse the hashes for every per-question
    # cache lookup. Avoids N file reads on a hot pack.
    ref_hash, agent_hash = client.image_hashes(ref_path, agent_path)

    cached: dict[str, dict[str, Any]] = {}
    to_ask: list[tuple[str, str]] = []
    for q in qs:
        hit = client.cached_verdict_for_hashes(q["id"], ref_hash, agent_hash)
        if hit is not None:
            cached[q["id"]] = hit
        else:
            to_ask.append((q["id"], q["text"]))

    fresh: dict[str, dict[str, Any]] = {}
    if to_ask:
        batched = client.ask_batched(to_ask, ref_path, agent_path)
        for (qid, _text), r in zip(to_ask, batched):
            fresh[qid] = r

    verdicts: list[dict[str, Any]] = []
    for q in qs:
        r = cached.get(q["id"]) or fresh.get(q["id"]) or {"verdict": 0, "cached": False}
        verdicts.append({
            "page": page_name,
            "q_id": q["id"],
            "verdict": r["verdict"],
            "cached": r.get("cached", False),
            **extra,
        })

    # 5-point scale → normalize mean from [1, 5] to [0, 1] for aggregation.
    # Raw verdicts stay 1-5 in `verdicts` for inspection.
    if verdicts:
        raw_mean = sum(v["verdict"] for v in verdicts) / len(verdicts)
        score = (raw_mean - 1) / 4
    else:
        score = 0.0
    return {"score": score, "verdicts": verdicts}


def _score_per_page(
    client,
    criterion: str,
    agent_dir: Path,
    gt_dir: Path,
    pages: list[str],
    viewport: str = "desktop",
) -> dict[str, Any]:
    pack = load_question_pack(criterion)
    per_page: dict[str, dict[str, Any]] = {}
    for page_name in pages:
        ref_path = reference_screenshot_path(gt_dir, page_name, viewport)
        if ref_path is None:
            per_page[page_name] = {"score": 0.0, "detail": f"missing reference screenshot for {viewport}/{page_name}"}
            continue
        agent_path = render_agent_page(agent_dir, page_name, viewport)
        per_page[page_name] = _ask_pack_per_page(client, pack, ref_path, agent_path, page_name)

    mean = (sum(p["score"] for p in per_page.values()) / len(per_page)) if per_page else 0.0
    return {"score": mean, "per_page": per_page, "scope": "per_page"}


def _score_multi_viewport(
    client,
    criterion: str,
    agent_dir: Path,
    gt_dir: Path,
    pages: list[str],
    viewports: list[str],
    scope_label: str,
) -> dict[str, Any]:
    """Shared inner loop for any pack scope that asks the same questions
    at multiple viewports. Mean within viewport → mean across viewports →
    mean across pages."""
    pack = load_question_pack(criterion)
    per_page: dict[str, dict[str, Any]] = {}

    for page_name in pages:
        vp_scores: dict[str, dict[str, Any]] = {}
        for vp in viewports:
            ref_path = reference_screenshot_path(gt_dir, page_name, vp)
            if ref_path is None:
                vp_scores[vp] = {"score": 0.0, "detail": f"missing ref {vp}/{page_name}"}
                continue
            agent_path = render_agent_page(agent_dir, page_name, vp)
            vp_scores[vp] = _ask_pack_per_page(
                client, pack, ref_path, agent_path, page_name,
                extra={"viewport": vp},
            )
        page_mean = (sum(v["score"] for v in vp_scores.values()) / len(vp_scores)) if vp_scores else 0.0
        per_page[page_name] = {"score": page_mean, "viewports": vp_scores}

    mean = (sum(p["score"] for p in per_page.values()) / len(per_page)) if per_page else 0.0
    return {"score": mean, "per_page": per_page, "scope": scope_label}


def _score_per_page_per_viewport(
    client, criterion: str, agent_dir: Path, gt_dir: Path, pages: list[str],
) -> dict[str, Any]:
    return _score_multi_viewport(
        client, criterion, agent_dir, gt_dir, pages,
        viewports=["desktop", "tablet", "mobile"],
        scope_label="per_page_per_viewport",
    )


def _load_design(gt_dir: Path) -> dict[str, Any] | None:
    """Load `design.json` from gt_dir, returning None if missing.

    The per-component scope needs the design's `pages[].components[]`
    list to enumerate what to ask the judge about.
    """
    p = gt_dir / "design.json"
    return json.loads(p.read_text()) if p.exists() else None


def _score_per_component(
    client, criterion: str, agent_dir: Path, gt_dir: Path, pages: list[str],
) -> dict[str, Any]:
    """Ask the pack per (page, component) at desktop.

    For each page, enumerate `design.json.pages[<page>].components[]` —
    each entry is a prose component description. Substitute the description
    into every question's `{component}` placeholder. Unique cache key per
    (template_id, component_idx) so the cache doesn't collide between
    different components reusing the same template.

    Aggregation: mean over questions → mean over components → mean over pages.
    """
    pack = load_question_pack(criterion)
    design = _load_design(gt_dir)
    if design is None:
        return {
            "score": 0.0, "scope": "per_component",
            "detail": "design.json missing — cannot enumerate components",
        }
    pages_by_name = {p["name"]: p.get("components", []) for p in design.get("pages", [])}

    per_page: dict[str, dict[str, Any]] = {}
    for page_name in pages:
        components = pages_by_name.get(page_name, [])
        ref_path = reference_screenshot_path(gt_dir, page_name, "desktop")
        if ref_path is None:
            per_page[page_name] = {"score": 0.0, "detail": f"missing reference desktop/{page_name}"}
            continue
        if not components:
            per_page[page_name] = {"score": 0.0, "detail": f"no components declared in design.json for {page_name}"}
            continue
        agent_path = render_agent_page(agent_dir, page_name, "desktop")

        comp_results: list[dict[str, Any]] = []
        for ci, comp_desc in enumerate(components):
            # Pre-substitute `{component}` and assign unique q_ids per
            # (template, component_idx) so the cache doesn't collide
            # between different components asking the same template.
            comp_questions = [
                {
                    "id": f"{q['id']}__c{ci}",
                    "text": q["text"].format(component=comp_desc),
                }
                for q in pack["questions"]
            ]
            ask_result = _ask_pack_per_page(
                client, pack, ref_path, agent_path, page_name,
                extra={"component_idx": ci},
                questions=comp_questions,
            )
            comp_results.append({
                "component_idx": ci,
                "component": (comp_desc[:80] + "…") if len(comp_desc) > 80 else comp_desc,
                "score": ask_result["score"],
                "verdicts": ask_result["verdicts"],
            })

        page_mean = sum(c["score"] for c in comp_results) / len(comp_results) if comp_results else 0.0
        per_page[page_name] = {"score": page_mean, "components": comp_results}

    mean = sum(p["score"] for p in per_page.values()) / len(per_page) if per_page else 0.0
    return {"score": mean, "per_page": per_page, "scope": "per_component"}


def _score_per_image(
    client, criterion: str, agent_dir: Path, gt_dir: Path, pages: list[str],
) -> dict[str, Any]:
    """v1 simplification: alias to `per_page` at desktop.

    The image_content_fidelity pack's questions read at page-level
    ("Is the same photograph visible somewhere on the agent's page?",
    "Are there NO extra hallucinated images present?", etc.), so the
    judge sees both rendered pages and answers about images collectively.
    Full per-image bbox-pairing dispatch (using ground_truth/images/*.json)
    is a future enhancement once the pairing heuristics are settled.
    """
    result = _score_per_page(client, criterion, agent_dir, gt_dir, pages, viewport="desktop")
    result["scope"] = "per_image"
    result["note"] = "v1 simplification: dispatched as per_page (desktop). See runner.py for status."
    return result


def _score_per_page_motion_strip(
    client, criterion: str, agent_dir: Path, gt_dir: Path, pages: list[str],
) -> dict[str, Any]:
    """Ask the pack once per page, with the judge seeing the reference
    and agent motion-strip PNGs side by side. For each page:

      1. Reference strip:  <gt>/screenshots/desktop/<page>/motion-strip.png
      2. Agent strip:      rendered fresh by re-running the same algorithm
                           against the agent's HTML, saved to a temp PNG.
      3. If the agent's strip can't be generated (no animated element
         matches the reference widget bbox), the question pack still
         runs against a fallback agent strip = the agent's full-page
         screenshot duplicated 5 wide — the judge will see "no motion"
         and score accordingly.
    """
    # Lazy-import the strip generator. Path manipulation because the
    # grading/ tree may not be on sys.path when invoked under Reward
    # Kit; this mirrors `tests/<criterion>/check.py`'s convention.
    try:
        from grading.criteria.animation_fidelity import (
            _load_widget_meta,
            make_agent_motion_strip,
            DESKTOP_VIEWPORT,
        )
    except ImportError:
        import sys as _sys
        from pathlib import Path as _Path
        _repo = _Path(__file__).resolve().parent.parent.parent
        if str(_repo) not in _sys.path:
            _sys.path.insert(0, str(_repo))
        from grading.criteria.animation_fidelity import (  # type: ignore
            _load_widget_meta,
            make_agent_motion_strip,
            DESKTOP_VIEWPORT,
        )

    from playwright.sync_api import sync_playwright  # noqa: PLC0415

    pack = load_question_pack(criterion)
    per_page: dict[str, dict[str, Any]] = {}

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        try:
            for page_name in pages:
                # Reference strip path.
                ref_path = (
                    gt_dir / "screenshots" / "desktop" / page_name / "motion-strip.png"
                )
                if not ref_path.is_file():
                    per_page[page_name] = {
                        "score": 0.0,
                        "detail": f"reference motion-strip missing: {ref_path}",
                    }
                    continue
                widget_meta = _load_widget_meta(gt_dir, page_name)
                if widget_meta is None:
                    # No animation expected on this page; neutral 1.0
                    # (mirrors animation_fidelity Track A behaviour).
                    per_page[page_name] = {
                        "score": 1.0,
                        "detail": "no widget metadata in ground truth — no animation expected",
                    }
                    continue
                agent_html = agent_dir / f"{page_name}.html"
                if not agent_html.is_file():
                    per_page[page_name] = {
                        "score": 0.0,
                        "detail": f"agent HTML missing: {page_name}.html",
                    }
                    continue

                # Render agent's strip via Playwright. Save to a temp PNG.
                ctx = browser.new_context(viewport={
                    "width": DESKTOP_VIEWPORT[0], "height": DESKTOP_VIEWPORT[1],
                })
                page = ctx.new_page()
                html_url = f"file://{agent_html.resolve()}"
                strip, info = make_agent_motion_strip(
                    page, html_url, widget_meta, viewport=DESKTOP_VIEWPORT,
                )
                ctx.close()

                if strip is None:
                    # Agent has no matching animated element. Build a
                    # fallback strip from a single full-viewport screenshot
                    # duplicated 5 wide so the judge can still score
                    # against "no motion".
                    from PIL import Image  # noqa: PLC0415
                    ctx = browser.new_context(viewport={
                        "width": DESKTOP_VIEWPORT[0], "height": DESKTOP_VIEWPORT[1],
                    })
                    page = ctx.new_page()
                    page.goto(html_url, wait_until="domcontentloaded")
                    page.wait_for_timeout(1500)
                    shot = page.screenshot(full_page=False)
                    ctx.close()
                    from io import BytesIO  # noqa: PLC0415
                    base = Image.open(BytesIO(shot))
                    strip = Image.new(
                        "RGB", (base.width * 5, base.height), color=(255, 255, 255),
                    )
                    for i in range(5):
                        strip.paste(base, (i * base.width, 0))

                agent_strip_path = Path(
                    tempfile.mkstemp(
                        prefix=f"agent_strip_{page_name}_", suffix=".png",
                    )[1]
                )
                strip.save(agent_strip_path)

                try:
                    ask_result = _ask_pack_per_page(
                        client, pack, ref_path, agent_strip_path, page_name,
                    )
                finally:
                    try:
                        agent_strip_path.unlink()
                    except FileNotFoundError:
                        pass

                ask_result["found_widget"] = info.get("found")
                ask_result["iou"] = info.get("iou")
                per_page[page_name] = ask_result
        finally:
            browser.close()

    mean = (sum(p["score"] for p in per_page.values()) / len(per_page)) if per_page else 0.0
    return {"score": mean, "per_page": per_page, "scope": "per_page_motion_strip"}


def score_judge(
    agent_output_dir: Path | str,
    ground_truth_dir: Path | str,
    pages: list[str],
    *,
    model: str = "claude-opus-4-7",
    cache_dir: Path | None = None,
) -> dict[str, dict[str, Any]]:
    """Run Track B across all 7 criteria. Returns `{criterion: result}`.

    Scope dispatch is data-driven off the pack's `scope` field — adding a
    new scope means extending the handlers map below.
    """
    # Absolute import works whether invoked as `python -m grading.judge.runner`
    # or as a script (`python grading/judge/runner.py`).
    try:
        from .client import JudgeClient
    except ImportError:
        import sys as _sys
        from pathlib import Path as _Path
        _repo = _Path(__file__).resolve().parent.parent.parent
        if str(_repo) not in _sys.path:
            _sys.path.insert(0, str(_repo))
        from grading.judge.client import JudgeClient

    agent_dir = Path(agent_output_dir)
    gt_dir = Path(ground_truth_dir)
    client = JudgeClient(model=model, cache_dir=cache_dir)

    # Scope dispatch — data-driven off each pack's `scope` field. Add a new
    # scope by extending this map; no code change needed to flip a criterion
    # between scopes — just edit its question_pack JSON.
    handlers = {
        "per_page": lambda c: _score_per_page(client, c, agent_dir, gt_dir, pages, viewport="desktop"),
        "per_page_per_viewport": lambda c: _score_per_page_per_viewport(client, c, agent_dir, gt_dir, pages),
        "per_component": lambda c: _score_per_component(client, c, agent_dir, gt_dir, pages),
        "per_image": lambda c: _score_per_image(client, c, agent_dir, gt_dir, pages),
        "per_page_motion_strip": lambda c: _score_per_page_motion_strip(client, c, agent_dir, gt_dir, pages),
    }

    per_criterion: dict[str, dict[str, Any]] = {}
    for crit in ACTIVE_CRITERIA:
        pack = load_question_pack(crit)
        scope = pack.get("scope", "per_page")
        handler = handlers.get(scope)
        if handler is None:
            per_criterion[crit] = {
                "score": 0.0,
                "scope": scope,
                "detail": f"{crit}: unsupported scope '{scope}' in question pack",
            }
            continue
        # Per-criterion try/except: a hard judge-API failure (e.g. retries
        # exhausted on `529 Overloaded`) on one criterion shouldn't nuke the
        # rest. The failed criterion records `score: None` so the aggregator
        # can decide whether to fall back gracefully.
        try:
            per_criterion[crit] = handler(crit)
        except Exception as e:
            import sys as _sys
            print(
                f"judge runner: criterion '{crit}' failed: {type(e).__name__}: {e}",
                file=_sys.stderr,
            )
            per_criterion[crit] = {
                "score": None,
                "scope": scope,
                "error": f"{type(e).__name__}: {e}",
            }

    # v1 stubs — scopes the runner doesn't have a handler for yet
    # (per_component, per_image).
    for crit in STUBBED_CRITERIA:
        per_criterion[crit] = {
            "score": 1.0,
            "scope": "stub",
            "detail": f"{crit}: v1 stub — see grading/judge/runner.py for status",
        }

    return per_criterion


def _cli() -> int:
    import argparse
    import sys

    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--agent-output", required=True)
    ap.add_argument("--ground-truth", required=True)
    ap.add_argument("--pages", default=None, help="comma-separated (default: every *.html in agent-output)")
    ap.add_argument("--model", default="claude-opus-4-7")
    ap.add_argument("--cache-dir", default=None)
    args = ap.parse_args()

    pages = (
        [p.strip() for p in args.pages.split(",") if p.strip()]
        if args.pages
        else sorted(p.stem for p in Path(args.agent_output).glob("*.html"))
    )
    if not pages:
        print("error: no pages discovered", file=sys.stderr)
        return 2

    import os
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("error: ANTHROPIC_API_KEY not set", file=sys.stderr)
        return 2

    result = score_judge(
        args.agent_output, args.ground_truth, pages,
        model=args.model,
        cache_dir=Path(args.cache_dir) if args.cache_dir else None,
    )
    summary = {c: {"score": r["score"], "scope": r.get("scope")} for c, r in result.items()}
    print(json.dumps({"summary": summary, "detail": result}, indent=2))
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(_cli())
