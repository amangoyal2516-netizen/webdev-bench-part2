"""Modal Volumes shared across the webdev-bench apps.

Four Modal Volumes back the recipe pipeline, eval runs, judge cache, and
the read-only asset pool. Three of them carry a `-part2` suffix so this
repo's outputs stay isolated from any sibling repo that names its volumes
without the suffix; the asset-pool volume is shared (read-only).

- `judge-cache-part2`      — Track B judge responses, keyed by
                             (question_id, ref_screenshot_hash,
                              agent_screenshot_hash, agent_dom_hash,
                              judge_model). Prompt tweaks only re-fire
                              invalidated cells.
- `recipe-artifacts-part2` — recipe pipeline outputs (design.json, per-page
                             videos, pre-computed JSON artifacts for the grader).
- `eval-runs-part2`        — Harbor rollout outputs (agent HTML/CSS,
                             reward.json, logs).
- `asset-pools`            — read-only asset data the recipe builder draws
                             from (photos / fonts / icons / avatars). Mounted
                             at /cache/assets with subdirs /photo-pool,
                             /font-pool, /icon-pool, /avatar-pool. Seeded
                             once via scripts/seed_modal_volumes.py.

Volumes are created lazily — the first `modal run` materialises any that
don't already exist.
"""

from __future__ import annotations

import modal

judge_cache = modal.Volume.from_name("judge-cache-part2", create_if_missing=True)
recipe_artifacts = modal.Volume.from_name("recipe-artifacts-part2", create_if_missing=True)
eval_runs = modal.Volume.from_name("eval-runs-part2", create_if_missing=True)
asset_pools = modal.Volume.from_name("asset-pools", create_if_missing=True)

# Canonical mount points used across all functions. Recipe functions mount
# both `/cache/recipe` (read/write) and `/cache/assets` (read-only data
# pulled from the seed script).
MOUNTS = {
    "/cache/judge": judge_cache,
    "/cache/recipe": recipe_artifacts,
    "/cache/eval": eval_runs,
    "/cache/assets": asset_pools,
}
