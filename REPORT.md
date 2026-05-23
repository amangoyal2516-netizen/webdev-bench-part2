# REPORT.md — reading the Part 2 HTML report

See Part 1's `REPORT.md` for the Part 1 layout (top dashboard,
per-task tabs, per-trial scoreboards, per-page reference/agent
screenshot pairs, judge-detail spotlight rows). This file documents
only what's different in the Part 2 reports.

## 1. New on every page block: animation cells (desktop only)

Above each desktop reference/agent screenshot pair, the report now
embeds a second pair labelled **"reference (animation)"** and
**"agent (animation)"** — both animated WebPs that play the load
animation looping forever.

- **Reference WebP** is sliced from the captured 5-panel
  `motion-strip.png` (with the red marker visible). Frame durations
  add up to the authored `duration_ms` so the loop plays at the
  reference's true speed.
- **Agent WebP** is rendered at report-render time in Playwright:
  load the agent's HTML at desktop, retrigger any class-based
  animation, sample 5 frames at evenly-spaced offsets through the
  reference's `duration_ms`, crop each frame to **the same band coords
  as the reference**, encode as animated WebP.
- Same band coords on both sides on purpose: if the agent animated a
  different element in a different position, you'll see motion only on
  one side. That's the fastest visual diagnostic for "did the agent
  pick the right element".

Animation cells are **desktop only** by design — animations look very
similar across viewports, and emitting them at mobile + tablet too
would triple the report size for marginal information.

## 2. New criterion column: `animation_fidelity`

Every per-trial scoreboard has 7 criteria (was 6 in Part 1). The 7th
column is `animation_fidelity` — see `DESIGN.md` §3 for the
algorithm. A score of `0.0` means the agent either didn't animate or
animated the wrong element by IoU. Mid-range scores (0.10–0.55) are
the typical band on a Part 2 oneshot rollout.

The dashboard panel, per-task rollups, and per-criterion mean row all
include the new column.

## 3. Track A vs Track B disagreement

Part 2 trials consistently show Track B scoring ~0.10 higher than
Track A overall. This is expected: Track A's `layout_structure` is
strict SSIM and `animation_fidelity` is panel SSIM × IoU gating, both
of which penalise "close-but-not-pixel-perfect"; Track B's 1-5
anchored scale doesn't. Trials with `|Δ| > 0.1` are flagged with a
⚠ in the dashboard for spot review — not a wiring bug.

## 4. Per-criterion judge-detail row

The Track B judge runs a small atomic question pack per criterion.
The judge-detail row in the report shows each question's 1-5 verdict.
For `animation_fidelity`, the pack covers element / direction /
magnitude / trigger / duration / final-state — see
`grading/judge/question_packs/animation_fidelity.json` for exact
wording.
