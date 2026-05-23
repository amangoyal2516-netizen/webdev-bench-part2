#!/usr/bin/env bash
# Oracle solution — copies the ground-truth HTML/CSS straight to the
# agent's output directory. Should score exactly 1.0 on every Track A
# sub-grader; failure here is a grader bug, not an agent issue
# (per steps.md Phase 1 Step 3).
#
# Harbor 0.7.x mounts the task's `solution/` at /solution/ inside the
# agent container at runtime. The packager (recipe/03-package/) copies
# `recipe/runs/<task_id>/source/` into `solution/source/` during
# packaging, so the Oracle answer key lives at /solution/source/.
set -euo pipefail

SRC=/solution/source
OUT=/workspace/output

if [[ ! -d "$SRC" ]]; then
    echo "error: oracle answer key missing at $SRC" >&2
    echo "(the packager should have copied recipe/runs/<task>/source/ here)" >&2
    exit 2
fi

mkdir -p "$OUT"
cp -r "$SRC"/. "$OUT/"

n=$(find "$OUT" -type f | wc -l)
echo "oracle: copied $n files from $SRC → $OUT"
