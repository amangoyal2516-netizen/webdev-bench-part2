# webdev-bench (Part 2 — animations)

A Harbor recipe for generating RL environments that test coding agents
at **design replication** of a website. Each task presents an agent
with full-page screenshots of a multi-page website plus a per-page
**motion strip** showing a load-triggered entrance animation; the
agent must reproduce both the static design AND the entrance
animation in HTML + CSS. Functionality is out of scope — outputs are
graded on what the user's eye sees.

This is the **Part 2** branch of webdev-bench. The Part 1 base setup
— recipe pipeline, asset pool, Track A deterministic sub-graders,
Track B LLM-judge, `framework_compliance` gate — is inherited
unchanged. What's new in Part 2:

- Per-page entrance animation baked into ground truth.
- A reference **motion strip** PNG (5 frames + red widget marker)
  per (viewport, page).
- A 7th grader, `animation_fidelity`, on both tracks.
- `-iter` variant dropped (a static render helper doesn't reveal motion).
For why each of these is the way it is, see `DESIGN.md` (Part 2
deltas only — refer to Part 1's `DESIGN.md` for the base
design). For the run-by-run story behind the design choices — what
we tried first (videos) and why we settled on the marker — see
`RESULTS.md`.

**Latest run:** `eval/reports/webdev-bench-20260523-223403.html` —
self-contained HTML report with per-trial scoreboards, animated
WebPs showing reference vs agent motion side-by-side, and per-page
reference/agent screenshot pairs.

## Documentation in this repo

| File | Purpose |
|---|---|
| `README.md` | This file — overview + run commands |
| `DESIGN.md` | Part 2 design deltas vs Part 1: animations, motion strip, `animation_fidelity` |
| `REPORT.md` | How to read the Part 2 HTML report (animation cells, 7th criterion column) |
| `RESULTS.md` | Latest canonical run + the videos → strip → red marker journey |
| `CLAUDE.md` | Orientation for AI assistants in this repo |

## Prerequisites

```bash
# Clone (as a sibling of webdev-bench or as a submodule of it)
git clone <url> webdev-bench-animations
cd webdev-bench-animations

# Install Harbor + Modal extra
uv tool install harbor
uv tool install harbor-rewardkit
uv tool install --force 'harbor[modal]'

# Install + authenticate Modal CLI (one-time; opens a browser)
pip install modal
modal token new

# Create a Modal secret holding your Anthropic API key
modal secret create anthropic-key ANTHROPIC_API_KEY=sk-ant-...

# Local Anthropic key (used by report renderer + local-side regrade)
export ANTHROPIC_API_KEY=sk-ant-...

# One-time asset-pool seed on Modal — SHARED with Part 1, so skip if
# you've already seeded for Part 1 on the same workspace.
python scripts/seed_modal_volumes.py
```

Part 2 uses dedicated Modal volumes (`recipe-artifacts-part2`,
`eval-runs-part2`, `judge-cache-part2`) so Part 1 outputs on
`recipe-artifacts` / `eval-runs` / `judge-cache` stay untouched.
`asset-pools` is shared.

## Pipeline commands

End-to-end: **generate tasks → run eval → view report**. Re-grade is
cheap when iterating on graders.

### 1. Generate tasks

```bash
python scripts/generate_tasks.py --count 10
```

Fans out across Modal sandboxes (one sandbox per task), pulls finished
tasks into `tasks/task_N-oneshot/`.

### 2. Run eval

```bash
# Full canonical run (all packaged tasks on Modal at 32-way parallel)
python scripts/run_eval.py

# Quick smoke (3 tasks)
python scripts/run_eval.py --quick

# Subset
python scripts/run_eval.py --tasks task_3,task_8

# Track A only (no Track B API spend)
python scripts/run_eval.py --no-track-b
```

Auto-renders the HTML report at `eval/reports/<job-name>.html` when
done. Outputs land at `jobs/<job-name>/`.

### 3. Re-grade an existing job (no agent re-run)

```bash
# Local
python scripts/regrade_job.py jobs/<job-name>/ --backup --concurrent 4

# Modal (parallel across trials)
modal run -m infra.modal.judge_app::regrade --job-dir jobs/<job-name>/
```

### 4. Manually render a report

```bash
python eval/reports/render_report.py jobs/<job-name>/
```

## Key principles (inherited from Part 1)

- **Design only, not functionality.** Routing / a11y / JS behavior /
  form handling do not grade. Only what the user's eye sees.
- **Dual-track grading.** Track A is fully deterministic (no
  embeddings, closed-form sub-graders); Track B is LLM-as-judge with
  atomic questions on a 1-5 anchored scale. Both scores reported
  side-by-side, never fused — disagreement is itself a signal.
- **Strict information barrier.** Agent sees only screenshots
  (full + slices + motion-strip), vendored assets, and
  `instruction.md`. Ground-truth source, pre-computed artifacts, and
  judge question packs are isolated in a separate verifier container.
- **Per-task vendored assets.** The agent doesn't search for images
  on the open web; the same vendored files are mounted to both agent
  and verifier.
- **Pre-computed reference data.** Bboxes, palette, typography, image
  pHashes, text, widget bbox/duration — all computed at recipe time.
  The grader reads JSON; no ground-truth re-extraction at run time.
