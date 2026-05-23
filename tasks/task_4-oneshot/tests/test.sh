#!/usr/bin/env bash
# Verifier entry point. Harbor 0.7.x runs this inside the agent container
# after the agent finishes. Base image is webdev-bench/base-verifier:latest
# (bake-in) which already has every grading dep (Pillow, numpy, scipy,
# sklearn, scikit-image, imagehash, lxml, opencv, anthropic, playwright).
#
# Mounts:
#   /workspace/output/     ← agent's output (HTML + CSS + assets/)
#                            preserved per-trial via task.toml `artifacts =`
#   /workspace/reference/  ← reference screenshots (env Dockerfile staged)
#   /tests/                ← this task's tests/ — contains:
#                            - ground_truth/{bboxes,palette,typography,images,text,design.json}
#                            - grading/aggregator.py + criteria/ + gates/ + judge/
#                            - _weights.toml (per-criterion weights, shared A & B)
#   /logs/verifier/        ← Harbor reads `reward.json` (key→float dict) here
#
# Env vars (set via `harbor run --ve KEY=VAL ...`):
#   RUN_TRACK_B=1         opt-in to fire Track B (LLM-as-judge) per rollout
#   ANTHROPIC_API_KEY=…   required when RUN_TRACK_B=1; ignored otherwise
#
# Note: we deliberately do NOT write artifacts to /logs/artifacts here.
# Harbor's _collect_artifacts runs BEFORE this script (see
# harbor/trial/single_step.py:35), so anything written here would be
# dead-on-arrival. Per-trial artifact preservation is configured in
# task.toml's `artifacts = [...]` field instead.
set -euo pipefail

mkdir -p /logs/verifier

# Make /tests/ground_truth/screenshots resolve to /workspace/reference
# (where the env Dockerfile staged the reference PNGs). The Track B
# judge runner reads `<gt>/screenshots/<viewport>/<page>/full.png` —
# this keeps the Dockerfile COPY as the single source of truth and
# avoids duplicating the images under tests/. Harmless when Track B
# isn't running.
if [ ! -e /tests/ground_truth/screenshots ]; then
    ln -s /workspace/reference /tests/ground_truth/screenshots
fi

# Run the aggregator. Track B fires when RUN_TRACK_B=1; default off.
TRACK_B_FLAG=()
if [ "${RUN_TRACK_B:-0}" = "1" ]; then
    TRACK_B_FLAG=(--run-track-b)
fi

python3 /tests/grading/aggregator.py \
    --agent-output /workspace/output \
    --ground-truth /tests/ground_truth \
    --weights /tests/_weights.toml \
    --output /logs/verifier/grading.json \
    "${TRACK_B_FLAG[@]}"

# Project the rich aggregator output into a flat key→float dict that
# Harbor's pydantic reward schema accepts (it rejects null values). Keep
# grading.json next to it for downstream analysis.
python3 - <<'PY'
import json
from pathlib import Path

g = json.loads(Path("/logs/verifier/grading.json").read_text())

rewards: dict[str, float] = {}

# Headline scores — only include keys whose value is a real number.
for key in ("score_objective", "score_judge", "raw_score_objective", "raw_score_judge", "gate"):
    v = g.get(key)
    if isinstance(v, (int, float)):
        rewards[key] = float(v)

# Per-criterion sub-scores, one Harbor key per criterion (objective + judge).
for crit, scores in (g.get("per_criterion") or {}).items():
    for track in ("objective", "judge"):
        v = (scores or {}).get(track)
        if isinstance(v, (int, float)):
            rewards[f"{crit}__{track}"] = float(v)

Path("/logs/verifier/reward.json").write_text(json.dumps(rewards, indent=2))
PY
