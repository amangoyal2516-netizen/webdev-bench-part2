# Sensitivity tests — `animation_fidelity` (part 2)

_Fixture: `part2/tasks/task_1-oneshot` (5 pages: chat, help, home, report-issue, topics). Regenerate with `python part2/grading/sensitivity.py --track-b --write-md part2/grading/SENSITIVITY.md`._

Ground truth was **locally rebaselined** — part 2's `recipe/02-capture/capture.py` was re-run against the fixture's `solution/source/` so the reference motion strips come from the same Playwright the grader uses. Oracle SSIM is not bounded by cross-machine Chromium drift.

## Corruption catalogue

`animation_fidelity` is a single-criterion grader; the matrix collapses to one column per track.

| corruption | what it does | predicted effect on the grader |
|---|---|---|
| `strip_animation`   | Removes the `animation: ...` declaration from `.wdvb-anim-*` class rules | Grader's `find_agent_widget` finds no element with `animationName !== 'none'` → **score 0** |
| `static_at_final`   | Sets `animation: none` on the class so the element renders at its default (final) state | All 5 motion-strip panels are byte-identical → "no animation played" detector → **score 0** |
| `reverse_keyframes` | Swaps `from` and `to` blocks in `@keyframes wdvb-anim-…` | Motion direction reverses — mid-strip panels diverge from the reference, SSIM falls but not to 0 (start/end frames still close) |
| `slow_10x`          | Multiplies every `animation: … <N>ms` by 10 | At reference duration offsets the animation has barely started → mid-frames look static → SSIM drops |
| `fast_10x`          | Divides duration by 10 | Animation settles before panel 1 → all panels match the final state, missing mid-motion → SSIM drops |
| `wrong_widget`      | Moves the `wdvb-anim-*` class onto the page's `<header>` | Bbox IoU vs the reference widget fails (< 0.05) → **score 0** |

## Results

| track | oracle | strip_animation | static_at_final | reverse_keyframes | slow_10x | fast_10x | wrong_widget |
|---|---|---|---|---|---|---|---|
| **Track A** (SSIM) | 0.934 | 0.000 (-0.93) | 0.000 (-0.93) | 0.476 (-0.46) | 0.074 (-0.86) | 0.110 (-0.82) | 0.000 (-0.93) |
| **Track B** (judge) | 0.821 | 0.714 (-0.11) | 0.714 (-0.11) | 0.464 (-0.36) | 0.143 (-0.68) | 0.786 (-0.04) | 0.607 (-0.21) |

_Each cell is `score (Δ vs oracle)`. Track A scope: every page in the fixture. Track B scope: a single page (default `home`) to bound API cost._

## Track A vs Track B — agreement matrix

| corruption | Track A Δ | Track B Δ | agree? |
|---|---|---|---|
| `strip_animation` | -0.93 | -0.11 | ✓ both register |
| `static_at_final` | -0.93 | -0.11 | ✓ both register |
| `reverse_keyframes` | -0.46 | -0.36 | ✓ both register |
| `slow_10x` | -0.86 | -0.68 | ✓ both register |
| `fast_10x` | -0.82 | -0.04 | ⚠ only Track A |
| `wrong_widget` | -0.93 | -0.21 | ✓ both register |

## Per-page Track A scores

| page | oracle | strip_animation | static_at_final | reverse_keyframes | slow_10x | fast_10x | wrong_widget |
|---|---|---|---|---|---|---|---|
| `chat` | 0.911 | 0.000 | 0.000 | 0.000 | 0.066 | 0.102 | 0.000 |
| `help` | 0.977 | 0.000 | 0.000 | 0.907 | 0.114 | 0.116 | 0.000 |
| `home` | 0.971 | 0.000 | 0.000 | 0.619 | 0.079 | 0.120 | 0.000 |
| `report-issue` | 0.923 | 0.000 | 0.000 | 0.000 | 0.000 | 0.103 | 0.000 |
| `topics` | 0.887 | 0.000 | 0.000 | 0.852 | 0.108 | 0.109 | 0.000 |

## Known interactions & caveats

- **`strip_animation` and `static_at_final` collapse to the same observable behaviour on Track A** (no widget found / panels identical, both → 0.0). They differ in DOM state but produce identical strips. Track B's `anim_q7` (panel distinguishability) catches both, but the surrounding 6 questions still rate the fallback strip generously, so the Track B drop is more modest than Track A's.
- **Track A applies a multiplicative `duration_factor`** in [0, 1] based on the agent's `animation-duration` vs the reference's `duration_ms`. Within ±25 % of the reference: factor 1.0. Beyond that the factor decays linearly on the ratio scale, so a 10 × duration mismatch caps the per-page score at ~0.125. This closes the pre-fix blind spot where `fast_10x` (animation settled before panel 1) scored near oracle because all panels looked like the reference's settled-state.
- **`wrong_widget` zeros Track A by construction** (IoU gate), but Track B can still rate it partially positive (Δ ≈ −0.2) if the animation it sees looks valid in isolation. This asymmetry is intentional: Track A enforces "the animation is *here*", Track B asks "is *an* animation present and well-shaped".
- The reference strip captures motion at `t = 0 / 25 / 50 / 75 / 100 %` of `duration_ms`. The Track A duration_factor compensates for the panel-SSIM metric's blindness to badly-timed animations; without it, an animation that ran 10× too fast or 10× too slow could still score highly on panel mean because mid-frames either matched the reference's start panel or its end panel.

## Recent fixes

- **Track A — duration factor** (`grading/criteria/animation_fidelity.py::duration_factor`). Multiplies the panel-SSIM mean by a [0, 1] factor reflecting how well the agent's `animation-duration` matches the reference. Lifted `fast_10x` Δ from −0.04 to −0.82.
- **Track B — `anim_q7`** (`grading/judge/question_packs/animation_fidelity.json`). New question: "Are the 5 panels in the agent's strip visually distinguishable from each other?". Detects fallback strips and identical-panel renders. Lifted `strip_animation` / `static_at_final` Δ from +0.00 to −0.11.

## What this does NOT prove

- That a higher animation_fidelity score implies a *better* animation in absolute terms — only that it more faithfully replicates the reference.
- That the per-frame panel offsets generalise — non-load-triggered or staggered animations would need a different sampling rule.
