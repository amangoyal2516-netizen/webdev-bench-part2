# RESULTS.md — Part 2 latest run + the journey behind it

**Canonical run:** `jobs/webdev-bench-20260523-223403/`
**HTML report:** `eval/reports/webdev-bench-20260523-223403.html`
**Date:** 2026-05-23
**Setup:** 4 trials × 1 attempt = 4 rollouts on Modal, Claude Code +
Opus 4.7 agent, Track A + Track B, 34 min wall, 32-way concurrency.

## Headline

| trial          | Track A   | Track B   | gate | `animation_fidelity` A / B |
|----------------|-----------|-----------|------|----------------------------|
| task_1-oneshot | 0.667     | 0.801     | ✓    | 0.100 / 0.283              |
| task_2-oneshot | **0.743** | 0.792     | ✓    | **0.556** / 0.431          |
| task_3-oneshot | 0.679     | 0.796     | ✓    | 0.374 / 0.361              |
| task_4-oneshot | 0.675     | 0.777     | ✓    | 0.494 / 0.300              |
| **mean**       | **0.691** | **0.793** |      |                            |

All four trials passed the `framework_compliance` gate (pure-CSS
animations, no JS animation library imports). Three of four are
flagged for spot review (|Δ| Track A vs B > 0.1) — Track B is
consistently more forgiving than Track A on `layout_structure` and
`typography`; expected dual-track noise, not a bug.

---

## How the animation input evolved

Part 2's central design problem: give the agent enough information to
reproduce an animation it has never seen execute. The shape of the
reference artefact went through three iterations before landing on
what now ships.

### Attempt 1 — Video files (`.webm`). Failed: Claude can't read videos.

Initial plan was to capture the load animation as a WebM video,
alongside the screenshots, expecting the agent to play it. We had it
working at the capture step. The Anthropic Messages API turned out
**not to support a video content block** — only PNG / JPEG / GIF /
WebP images. Claude Code, which talks to that API for every
multimodal input, can only "see" still-frame thumbnails of any video
the agent reads. Dropped video entirely; `.webm` files are no longer
captured.

### Attempt 2 — 5-panel motion strip (no marker). Worked, but agent picked the wrong element.

Replaced video with a single PNG: 5 frames sampled at
t = 0 / 25 / 50 / 75 / 100 % of the animation, each cropped to a
horizontal band (full viewport width × `widget_h + 2 × 80 px`),
stitched left-to-right. PNG is fully Messages-API-compatible.

The band intentionally preserves left/right context so the agent can
see WHERE on the page the animation lives, not just the widget in
isolation. Trade-off: the same context introduces ambiguity. A band
typically contains a header + hero CTA + a small widget below — the
agent looks at the strip and tends to pick **the most visually
prominent element**, not the actually-moving one.

Concrete evidence from `task_1-oneshot` (salvage pass, strip-only,
pre-marker):

| page          | reference widget (y) | agent's animated element (y) | gap     | type matched? |
|---------------|----------------------|------------------------------|---------|---------------|
| chat          | 234                  | 121 (`.chat-intro`)          | 113 ↑  | ✓ slideDown   |
| help          | 210                  | 106 (`.anim-help-badge`)     | 104 ↑  | ✓ slideLeft   |
| home          | 578                  | 420 (`.hero-cta`)            | 158 ↑  | ✓ scaleUp     |
| report-issue  | 210                  | 93  (`.anim-step-tracker`)   | 117 ↑  | ✗ scaleUpSmall|
| topics        | 210                  | 101 (`.anim-topics-h1`)      | 109 ↑  | ✗ fadeUp      |

Agents consistently picked an element 100–160 px **above** the actual
animated widget. IoU collapsed to 0 on all 5 → task_1's
`animation_fidelity = 0.000`. task_3 scored 0.336 on the same setup
(the agent sometimes picked correctly), so the task wasn't impossible
— it was just consistently ambiguous.

### Attempt 3 — Add a red marker rectangle. Works.

Drew a thin red rectangle outline (3 px wide, `#FF0000`, no fill) on
every panel of the strip at the widget's *settled* (post-animation)
position. The rectangle stays fixed across all 5 panels even while
the widget moves through animation intermediates — gives the agent
an unambiguous "**this** DOM element is the one being animated"
pointer while still preserving the surrounding row of context.

Implementation:
- `recipe/02-capture/capture.py:_make_motion_strip` draws the
  rectangle natively for new captures.
- Existing tasks were backfilled in place via
  `scripts/redraw_motion_strip_markers.py` — 61 of 66 strips marked.
- 5 mobile-viewport strips skipped because the responsive layout
  pushed the animated widget below the mobile fold (y > 812).
  Desktop + tablet strips for those same pages were marked correctly,
  so the agent can cross-reference. Future builder-prompt fix is to
  require above-the-fold at *all three* viewports.

Result on `task_1-oneshot` after the marker landed:
`animation_fidelity` moved from **0.000 → 0.100** on Track A and
**0.000 → 0.283** on Track B. All four trials in the canonical run
produce non-zero `animation_fidelity` (0.10–0.56 on Track A). Honest
mid-range signal — the criterion is genuinely hard and we want it to
discriminate.

### But the marker alone didn't make animation fidelity great

The red marker fixed the **which element** problem. It did **not**
fix all the other things the agent has to get right. Even with every
trial now scoring non-zero on `animation_fidelity`, the mean across
4 trials is **0.38 on Track A** — clearly mid-range, not strong:

| trial | animation_fidelity (Track A) | what it tells us |
|---|---|---|
| task_1 | 0.100 | agent landed on the right element on some pages but produced the wrong animation type / direction on the rest |
| task_2 | 0.556 | best of the four — most pages have the right element + close-enough motion |
| task_3 | 0.374 | element mostly right; type / easing / magnitude diverged from the reference |
| task_4 | 0.494 | similar to task_3 |

Why the scores stay moderate even with the marker:

- **Type can still be wrong.** The marker points at the element but
  doesn't *spell out* `slide-down` vs `scale-up`. The agent has to
  read the 5 panels and infer the motion type. It often gets close
  (slide-up vs slide-down direction; scale vs slide) but not exact.
- **Easing / magnitude / start-state diverge.** Even when the type
  is right, the agent's `@keyframes` curve isn't pixel-identical to
  the reference's. The grader does panel-by-panel SSIM at common
  width, and SSIM is unforgiving of small differences in the
  in-between frames.
- **The grader is strict by design.** Panel-SSIM × IoU-gate ×
  early-weighted averaging is intentionally hard to ace. A score of
  ≥ 0.7 would require near-pixel-perfect motion reproduction.
  Mid-range (0.3–0.6) is what you get for "right element + roughly
  right behaviour" — exactly the signal we want.

The marker is necessary but not sufficient. A future iteration could
push scores up by leaking type / easing into `instruction.md` the
way `duration_ms` is leaked today — but at the cost of making the
task easier than it should be. For now we accept mid-range scores
as honest discrimination.

### Construct-validity sweep (post-canonical) revealed two grader gaps

After the canonical run shipped, a sensitivity sweep
(`grading/sensitivity.py`) ran the oracle plus six targeted
corruptions through both tracks and surfaced two construct-validity
gaps that the canonical run could not have flagged:

1. **Track A was nearly blind to `fast_10x`.** An animation set to
   10× the reference's `duration_ms` settled before panel 1, so all
   five agent panels matched the reference's settled state — panel-SSIM
   only dropped from 0.92 → 0.88 (Δ = −0.04). A 10× duration mismatch
   should be a near-zero score.
2. **Track B was blind to `strip_animation` / `static_at_final`.**
   With no animated element in the agent's HTML, the judge runner
   falls back to a duplicated full-page screenshot, and the original
   six questions rated that as a valid settled state (Δ = +0.00 on
   both corruptions).

Both gaps were closed by changes documented in `DESIGN.md` §3:

- **Track A** now multiplies the panel-mean by a `duration_factor`.
  `fast_10x` Δ improved from −0.04 to −0.82; `slow_10x` Δ improved
  from −0.33 to −0.86.
- **Track B** adds `anim_q7` ("are the five panels visually
  distinguishable?"). `strip_animation` / `static_at_final` Δ
  improved from +0.00 to −0.11. The drop is modest because the
  original six questions still rate the fallback strip generously
  on the dimensions they probe, but the previous total miss is
  gone.

**Implication for the canonical run.** The numbers in the table
above (`task_1` 0.100, `task_2` 0.556, etc.) predate both fixes.
Any rollout in `jobs/webdev-bench-20260523-223403/` whose animation
ran at the wrong duration would score lower on the fixed grader,
and any rollout where the agent skipped the animation entirely
would score slightly lower on Track B. Re-grading those rollouts
locally would shift the canonical mean down somewhat; the new
scores are stricter (closer to "right by construction") and we
accept that as the new baseline going forward. The headline
`animation_fidelity ~0.38 mean` from this run is no longer
directly comparable to scores produced after the fixes — re-run
`scripts/regrade_job.py` against this job to get post-fix numbers
on the canonical trials.

The full corruption matrix, oracle calibration, and per-page
detail live in `grading/SENSITIVITY.md`.

---
