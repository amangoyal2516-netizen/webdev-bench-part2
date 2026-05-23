# Replicate the website shown in the reference screenshots

You will see reference screenshots of a multi-page website. Each page also carries one **load-triggered entrance animation** that you must reproduce. Your job is to replicate both the static design AND the entrance animation as faithfully as possible in **html-css**. Functionality is out of scope — only what the user's eye sees is graded.

## What the site is

A community social feed for senior crafters to share handmade projects, swap patterns, and follow makers — chronological timeline with large readable type and senior-friendly affordances

## Files you can see (read-only)

For each `<viewport>` in `{desktop, tablet, mobile}` and each `<page>` listed below, you have:

- `/workspace/reference/<viewport>/<page>/full.png` — full-page settled (animation-finished) screenshot. **This is your primary reference for the static design.**
- `/workspace/reference/<viewport>/<page>/001.png`, `002.png`, … — viewport-height slices of the same render, top → bottom in scroll order. Read these in order for high-resolution detail.
- `/workspace/reference/<viewport>/<page>/motion-strip.png` — a 5-panel horizontal strip showing the page's load-triggered entrance animation at t=0 %, 25 %, 50 %, 75 %, 100 % of its duration. Each panel is the full viewport width centred on the widget's row, so you can see both **which element moves** and **the surrounding context** (where on the page it lives). **A thin red rectangle outline is drawn on every panel around the animated widget's *settled* (post-animation) position.** The red box stays fixed across all 5 panels even while the widget itself moves through the animation states — use it to identify the exact DOM element to apply the entrance animation to (not any neighbouring header, hero CTA, or section sitting in the same band). Read the panels left-to-right to infer the direction, magnitude, and type of motion.

> **Match every asset by position, not by category.** For each spot where the reference shows an asset — image, icon, avatar, or font — use the **same exact asset file** the reference uses at that position. Not just any plausible asset of the same kind. The graders compare asset choices against the reference per-position:
>
> - **Images**: picking a different food photo for the hero, even if it looks similar, is a miss (`image_content_fidelity` uses pHash by bbox).
> - **Icons**: picking a different SVG (e.g., `bell.svg` when the reference uses `bell-ringing.svg`) is a miss.
> - **Avatars**: picking a different avatar variant at each user-list slot is a miss.
> - **Fonts**: using a different font family than the reference's (e.g., Roboto when the reference uses Inter) is a miss even if it looks similar — `typography` reads computed `font-family` exactly.
>
> Read the reference screenshots carefully to identify which exact asset is at each position, then use that exact file from `assets/`.

Other files:
- `/workspace/output/assets/` — image, font, and icon files the design uses. **Pre-populated for you.** Use only these — don't fetch external assets from the internet, and don't add to, remove, or overwrite files in this directory.
- `/workspace/instruction.md` — this file.

## Files you write

- `/workspace/output/<name>.html` — one HTML file per canonical page name listed below.
- `/workspace/output/styles.css` — your stylesheet.
- Reference vendored assets as `./assets/<filename>` (your HTML and the `assets/` directory both live under `/workspace/output/`, so a relative `./assets/<file>` resolves correctly).

## Canonical page filenames

You must produce **exactly** these HTML files — filenames matter, the grader pairs your pages to references by name.

- `feed.html` — Main chronological timeline of craft posts from people you follow
- `post.html` — Detailed view of a single craft post with materials, steps, and community comments
- `profile.html` — A crafter's profile page showing their portfolio and activity timeline
- `share.html` — Form for sharing a new craft project to the community feed
- `patterns.html` — Browseable library of community-shared patterns and printable instructions
- `groups.html` — Directory of crafting circles and interest groups within the community

## Framework constraint

You are allowed to use: **html-css**.

For `html-css`: produce static HTML files + a single CSS stylesheet. No JS frameworks (React, Vue, Solid, Svelte, Preact, Lit, Alpine, htmx). No build step. No `package.json`, `tsconfig.json`, `vite.config.*`, `next.config.*`, or `node_modules/`. The `framework_compliance` gate caps your final reward at 30% if you violate this.

## Animation requirement

Every page has **one load-triggered entrance animation** on a small above-the-fold widget — a directional slide (≈120 px traversal) or a scale-up (scale 0.6 → 1). It fires once on page load and is visible on first paint without scrolling. The `motion-strip.png` for each page shows what the entrance looks like; reproduce it as a pure-CSS `@keyframes` rule on the same widget. **No JS animation libraries** (gsap, framer-motion, motion, popmotion, animejs, lottie-web) — the `framework_compliance` gate forbids them.

The motion-strip's 5 panels are sampled at evenly-spaced time offsets (t=0 %, 25 %, 50 %, 75 %, 100 % of the animation's duration). Direction, magnitude, easing, and type are all readable from the panels — but duration is not (5 stills don't reveal absolute time), so the exact authored `animation-duration` per page is given here:

- `feed`: 1300 ms
- `post`: 1500 ms
- `profile`: 1600 ms
- `share`: 1200 ms
- `patterns`: 1400 ms
- `groups`: 1400 ms


## Fonts to declare

The reference uses these `@font-face` rules. Use them as-is in your CSS — the family name is what the `typography` grader reads via `getComputedStyle()`, and it doesn't always match the filename:

```css
@font-face { font-family: 'Lora'; src: url('./assets/fonts/lora/lora-400.woff2') format('woff2'); font-weight: 400; font-style: normal; }
@font-face { font-family: 'Lora'; src: url('./assets/fonts/lora/lora-700.woff2') format('woff2'); font-weight: 700; font-style: normal; }
@font-face { font-family: 'Plus Jakarta Sans'; src: url('./assets/fonts/plus-jakarta-sans/plus-jakarta-sans-400.woff2') format('woff2'); font-weight: 400; font-style: normal; }
@font-face { font-family: 'Plus Jakarta Sans'; src: url('./assets/fonts/plus-jakarta-sans/plus-jakarta-sans-700.woff2') format('woff2'); font-weight: 700; font-style: normal; }
```


## How you'll be graded

Your output is compared to the reference on seven dimensions, each in [0, 1]:

1. **`layout_structure`** — SSIM between agent's and reference's full-page screenshots at desktop / tablet / mobile, downsampled to a common width. Weight 2.5.
2. **`component_presence`** — whether the expected components render. Weight 2.0.
3. **`color_palette`** — k-means in LAB color space + Earth-Mover's-Distance vs reference. Weight 1.5.
4. **`typography`** — font-family + font-size per text node, area-weighted. Weight 1.5.
5. **`image_content_fidelity`** — perceptual hash of each `<img>` region matched against the reference. Weight 1.5.
6. **`visible_text_fidelity`** — Sørensen-Dice over tokenized DOM textContent. Weight 1.0.
7. **`animation_fidelity`** — your page's motion strip is rendered the same way as the reference and compared panel-by-panel via SSIM, early-weighted across the 5 panels. Scores 0 if your widget doesn't actually animate (all 5 panels identical). Weight 1.0.

Final reward = `weighted_mean(7 sub-scores) × framework_compliance_gate`.

A second, parallel **Track B** score is produced by an MLLM judge running atomic questions on a 1-5 anchored scale against the same screenshots — same weights, same gate, reported side-by-side. You don't need to do anything different for Track B; it scores the same artifacts.

Now study the references and start writing HTML + CSS in `/workspace/output/`.
