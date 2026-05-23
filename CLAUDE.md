# CLAUDE.md

Orientation for Claude Code sessions in this repository.

## What this repo is

A Harbor recipe for generating RL environments that test coding-agent design-replication ability. The pipeline generates websites (HTML/CSS + assets + reference screenshots), packages them as Harbor tasks, and grades agent rollouts against a dual-track rubric.

See `DESIGN.md` for the full pipeline walkthrough — what each stage does, why it was built that way, and the key trade-offs.

## Where to look first

- `DESIGN.md` — pipeline walkthrough + design rationale
- `recipe/` — ground-truth generation (author LLM + per-page builder LLM)
- `grading/criteria/` — Track A deterministic sub-graders
- `grading/judge/` — Track B LLM-as-judge runner + question packs + cache
- `tasks/` — packaged Harbor tasks (currently 4 oneshot tasks; `python scripts/generate_tasks.py --count N` to add more)
- `eval/` — Harbor job configs + the HTML report renderer
- `infra/modal/` — Modal Apps for the recipe pipeline + Track B judge

## Hard constraints

- **Design only, not functionality.** Routing, accessibility tags, JS behavior, form handling — none of these grade. Only what the user's eye sees.
- **Dual-track grading.** Every rollout produces two scores side-by-side:
  - **Track A — Objective.** Weighted mean of **seven** deterministic sub-graders × `framework_compliance` gate. No image embeddings (DINOv2 / SigLIP / CLIP rejected). Every Track A sub-grader is a deterministic algorithm describable in two sentences. Part 2 adds `animation_fidelity` (panel-SSIM × IoU gating) as the seventh.
  - **Track B — LLM-as-judge.** Same seven criteria, same weights, same gate. Each criterion has an atomic question pack; verdicts are on a 1-5 anchored scale, normalised to [0, 1] via `(mean - 1) / 4` before the weighted mean.
  - The two scores are **never aggregated** into one number; they are reported side-by-side. Disagreements (|Δ| > 0.1) flag rollouts for spot review.
- **Strict information barrier.** Agent sees only reference screenshots, vendored assets, and `instruction.md` — never ground-truth source, pre-computed grader artifacts, or judge question packs. Harbor's separate verifier mode enforces this.
- **Per-task vendored assets, single-mount layout.** `instruction.md` tells the agent to reference assets via `./assets/<file>` — pre-populated at `/workspace/output/assets/`. The same files live under each packaged task's `environment/ground_truth/source/assets/`. The agent shouldn't add, remove, or overwrite anything in `assets/`. Network is open at the container level; the *use only vendored* rule is convention, not container enforcement (Track A's `framework_compliance` gate catches external `<img src="...">` references).
- **Modal is the compute backend.** Harbor dispatches eval containers to Modal; the recipe pipeline and Track B judge run as Modal Apps under `infra/modal/`. Don't run the full eval locally — it's a Modal job. Per-criterion unit tests are still fine on a laptop.

## Unusual choices to be aware of

- **Single `-oneshot` variant per task.** Part 2 dropped the `-iter`
  variant (and its `render` helper) that Part 1 documented — the
  animation-replication task didn't benefit from a per-page visual
  verification loop, and a single variant simplifies the eval surface.
- **`framework_compliance` is a multiplicative gate**, not a weighted sub-score. Violating it caps the reward at 0.3× of what other graders would give.
- **Pre-computed ground-truth artifacts** at recipe time; the grader reads JSON + screenshots, never re-renders ground-truth at grade time.
- **`layout_structure` is SSIM** on full-page screenshots, not Hungarian-IoU on DOM bboxes. See DESIGN.md §5.1.

## Conventions

- Tasks follow Harbor's exact layout (see `tasks/_template/`).
- Sub-graders live in `grading/criteria/<name>.py`; each task's `tests/<criterion>/check.py` imports from there.
- Never commit `.claude/settings.local.json` or local IDE config (see `.gitignore`).

## Commands you'll use

Harbor 0.7.x dropped `harbor task validate` — use an Oracle run as the functional validation. Modal env requires the `harbor[modal]` extra (`uv tool install --force 'harbor[modal]'`) and the bake-in task Dockerfile so Modal can build it without a private registry.

```bash
# Oracle smoke on Modal (should score ~1.0 on Track A — bug if not).
# Doubles as functional task validation.
harbor run -p tasks/<task_id>-<variant>/ -a oracle -e modal

# Same Oracle smoke with Track B (LLM-as-judge) enabled — validates the
# full dual-track grading path.
harbor run -p tasks/<task_id>-<variant>/ -a oracle -e modal \
    --ve RUN_TRACK_B=1 --ve ANTHROPIC_API_KEY="$ANTHROPIC_API_KEY"

# Single rollout with Claude Code on Modal
harbor run -p tasks/<task_id>-<variant>/ -m claude-opus-4-7 -a claude-code -e modal \
    --ae ANTHROPIC_API_KEY="$ANTHROPIC_API_KEY" \
    --ve RUN_TRACK_B=1 --ve ANTHROPIC_API_KEY="$ANTHROPIC_API_KEY"

# Full canonical eval (all packaged tasks on Modal at 32-way parallel)
python scripts/run_eval.py

# Quick smoke (3 tasks on Modal)
python scripts/run_eval.py --quick

# Generate N new designs end-to-end on Modal (recipe pipeline)
python scripts/generate_tasks.py --count 10

# Re-grade an existing job locally (cheap when the judge cache is warm —
# only changed questions / images re-fire against the Anthropic API)
python scripts/regrade_job.py jobs/<job-name>/

# Re-grade on Modal (parallel across trials)
modal run -m infra.modal.judge_app::regrade --job-dir jobs/<job-name>/

# Render a job's results as a self-contained visual HTML report
python eval/reports/render_report.py jobs/<job-name>/
```

## Part 2 deviations

This is the Part 2 (animations) branch. All deltas vs Part 1 are
documented in `DESIGN.md` (what changed and why), `REPORT.md` (how
the report differs), and `RESULTS.md` (the videos → motion-strip →
red-marker journey behind the design). Highlights to keep in mind
while coding:

- Per-page motion strip + red widget marker is the load-bearing
  animation reference for the agent.
- 7th criterion `animation_fidelity` (weight 1.0) is live on both
  Track A and Track B.
- Only `-oneshot` is packaged; `-iter` is dropped.
- Modal volumes use a `-part2` suffix; asset pool is shared.
- `framework_compliance` gate also rejects JS animation libraries.

## When in doubt

- Part 2 deltas in design → `DESIGN.md`
- Reading the HTML report → `REPORT.md`
- Latest run + the journey → `RESULTS.md`
- Harbor framework concepts → https://harborframework.com/docs/
