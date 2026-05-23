# DESIGN.md — Part 2 (animations) design deltas

This is the **animations** branch of webdev-bench. The base pipeline —
asset pool, author + per-page builder LLMs, dual-track grading, Track A
deterministic sub-graders, Track B 1-5 anchored-scale LLM judge,
`framework_compliance` gate, per-task Dockerfile + bake-in — is all
inherited unchanged from Part 1.

See Part 1's `DESIGN.md` for that base design rationale.
This file documents only what's new or changed in Part 2.

---

## 1. What's new: per-page entrance animations

Each generated page now carries **exactly one** load-triggered entrance
animation on a small above-the-fold widget. The author chooses from a
closed enum:

| field | values |
|---|---|
| `type` | `slide-up` · `slide-down` · `slide-left` · `slide-right` · `scale-up` |
| `trigger` | `load` (only) |
| `duration_ms` | 1000–2000 |
| `easing` | `ease-in-out` (default) |
| `element` | author prose — "Large circular breath timer in the centre…" |

The builder emits the animation as a `wdvb-anim-<type>-<page>` class +
matching `@keyframes` block in the page's inline `<style>`. The class
name is namespaced so each page's animation is independently
controllable (and so the recipe-time `_make_motion_strip` can find it
deterministically).

Schema field landed in `recipe/01-generate/schemas/design-doc.schema.json`;
author + per-page builder prompts updated accordingly.

### Why these specific constraints

The whole schema is a closed enum on purpose, and every value in it
was chosen so that **the animation is unambiguously evident from 5
still frames**. The reference artefact (the next section) is a PNG
strip, not a live render — anything that doesn't read clearly in a
5-frame sample is out of scope.

- **One animation per page.** Two competing motions would compete for
  the agent's attention and force the motion strip to show two
  things at once. With one widget, the strip is unambiguous, the red
  marker (next section) has a single target, and the
  `animation_fidelity` grader has a single bbox to score against.
  More than one animation per page also tends to mean visual noise
  the agent can't tell apart in stills.

- **Only translation and scale-up — no fade, no rotation, no colour
  shifts.** The five allowed types — `slide-{up,down,left,right}` and
  `scale-up` — are the **kinematic** animations that have a clear
  positional or dimensional delta between t=0 and t=1. A still-frame
  sampler can see them. The rejected alternatives all break in
  stills:
  - *Fade-in* — opacity at frames 2 / 3 is identical to a static
    element with reduced alpha. The agent can't tell "fading" from
    "translucent and static" by looking at panels.
  - *Rotation* — a 5 ° vs 355 ° rotation looks the same in a still;
    direction is lost.
  - *Colour transitions* — only the start and end colours read
    cleanly; the agent has no way to know whether the curve was
    linear or eased.
  Translation gives the agent **unmistakable position deltas** across
  the 5 panels; scale-up gives **unmistakable size deltas**. Both
  read at a glance.

- **Translation magnitude ≈ 120 px, scale 0.6 → 1.0.** Big enough to
  fall outside ambient page-noise (e.g. anti-aliasing on thin lines)
  but small enough to stay inside the band crop. A 5 px slide would
  look like a static element across all 5 panels.

- **Duration 1000–2000 ms (deliberately long).** At a typical UI
  default of 200-300 ms, the 5-panel sampler lands frames at
  t = 0 / 50 / 100 / 150 / 200 ms — each step is too tight, and the
  difference between adjacent panels is often below SSIM noise.
  At 1500 ms, the panels land at 0 / 375 / 750 / 1125 / 1500 ms —
  each is a visually distinct intermediate, the human reviewer can
  see the motion at a glance, the SSIM grader gets meaningful
  panel-to-panel differences, and the LLM judge has enough signal
  per panel to verdict on. Long duration also means *the animation
  itself is leisurely enough to read* — agents replicating it can
  pick a duration anywhere in the range and still match the
  reference's overall pace.

- **`trigger: load` only.** Scroll-, hover-, click- triggered
  animations can't be captured by simply loading the page and
  sampling — they require interaction the recipe sampler doesn't
  model. Restricting to `load` keeps capture deterministic: load
  the page, wait `duration_ms + buffer`, retrigger the class,
  sample 5 frames. No interaction harness needed.

The common thread: **every choice optimises for "is this animation
self-evident from 5 PNG panels?"** Anything that isn't gets dropped.

## 2. Reference artefact: 5-panel motion strip with red marker

Captured per `(viewport, page)` alongside the existing `full.png` +
viewport-height slices that Part 1 already shipped:

```
screenshots/<viewport>/<page>/
    full.png           (full-page settled state, Part 1)
    001.png, 002.png … (viewport-height slices, Part 1)
    motion-strip.png   ← new in Part 2
```

### Why a PNG strip and not a video

The first attempt was to ship a `.webm` video of the load animation
alongside the screenshots. **The Anthropic Messages API doesn't accept
a video content block** — only PNG / JPEG / GIF / WebP. Claude Code,
which talks to that API for every multimodal input, literally can't
see videos. We dropped video and switched to a single PNG that
stitches 5 sampled frames of the animation horizontally.

### How the strip is built

- 5 frames sampled at t = 0 / 25 / 50 / 75 / 100 % of `duration_ms`.
- Each frame is a **horizontal band crop** — full viewport width × `widget_h + 2 × 80 px padding`, centred on the widget's row. The band preserves left/right context so the agent can locate WHERE on the page the animation lives, not just the widget in isolation.
- Frames are stitched left-to-right into a single PNG.

### Why the **red marker** (and why we needed it after the strip already shipped)

The strip alone wasn't enough. The band preserves L/R context on
purpose, but that same context also gives the agent **multiple
plausible candidate elements** to animate (header, hero CTA, intro
section, a small widget below). In the first end-to-end run, the
agent consistently picked the **most visually prominent** element in
the band — usually the header or hero CTA — placing the animation
100-160 px ABOVE the actually-moving widget. IoU collapsed to 0 and
`animation_fidelity` scored 0 on every page of one task. See
`RESULTS.md` for the per-page evidence.

The fix: **a thin red rectangle outline (3 px, `#FF0000`, no fill)
is drawn on every panel at the widget's *settled* (post-animation)
position**. The rectangle stays fixed across all 5 panels even while
the widget animates through intermediate states. This gives the agent
an unambiguous "**this** DOM element is the one being animated"
pointer while still preserving the surrounding row of context. After
the marker landed, all four canonical-run trials produced non-zero
`animation_fidelity`.

### Why `duration_ms` is **leaked verbatim in `instruction.md`**

The strip conveys direction / magnitude / easing / type, but
**absolute time is not recoverable from 5 still frames**. An agent
looking at the strip can tell *what* moves and *how* it moves, but
not *for how long*. So the per-page `duration_ms` is exposed
verbatim in the agent's `instruction.md` — **the single piece of
animation information the strip itself can't carry**, leaked
deliberately so the agent isn't punished for guessing wrong on the
one thing it has no way to infer.

## 3. New criterion: `animation_fidelity` (weight 1.0)

Track A:
- Render agent's HTML in Playwright at desktop. Find the agent's
  animated element by **highest IoU vs the reference widget bbox**
  (min IoU 0.05).
- Re-emit a 5-panel motion strip cropped to a band centred on the
  agent's widget.
- SSIM-compare panel-by-panel against the reference strip with
  early-weighted averaging `[0.30, 0.25, 0.20, 0.15, 0.10]` (early
  frames contain the higher-information transition).
- Score 0 when no agent element matches by IoU, or when all 5 agent
  panels are byte-identical (agent didn't animate at all).

Track B:
- Same 1-5 anchored scale as the other six criteria.
- Question pack at `grading/judge/question_packs/animation_fidelity.json`
  covers element / direction / magnitude / trigger / duration /
  final-state. Scope: `per_page_motion_strip`.

The 7-criterion weighted mean is now:

| criterion | weight |
|---|---|
| `layout_structure` | 2.5 |
| `component_presence` | 2.0 |
| `color_palette` | 1.5 |
| `typography` | 1.5 |
| `image_content_fidelity` | 1.5 |
| `visible_text_fidelity` | 1.0 |
| `animation_fidelity` | 1.0 |

`framework_compliance` remains the multiplicative gate at 0.3× on
violation.

## 4. `framework_compliance` gate addition

The Part 2 gate **also forbids JS animation libraries**: `gsap`,
`framer-motion`, `motion`, `popmotion`, `animejs`, `lottie-web`.
Animations must be pure CSS. Detection is the same heuristic the gate
uses for the rest of the forbidden-framework list — `package.json` /
script src / inline JS identifier scan.

## 5. Variants: `-iter` dropped, `-oneshot` only

Part 1 shipped two variants per design: `-oneshot` (the agent gets the
references and writes HTML in one pass) and `-iter` (which adds a
per-page `render` helper the agent can call to visually verify
intermediate output).

Part 2 drops `-iter`. The `render` helper is a static screenshot — it
doesn't reveal motion — so the A/B comparison between oneshot and iter
becomes apples-to-oranges for the animation-replication question. Each
design now packages exactly one task: `task_N-oneshot`.
