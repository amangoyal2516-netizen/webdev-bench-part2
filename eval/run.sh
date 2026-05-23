#!/usr/bin/env bash
# Wrapper around `harbor run -c <config>`.
#
#   bash eval/run.sh          → full 20-trial campaign (eval/job.yaml)
#   bash eval/run.sh --quick  → 3-trial dev loop        (eval/job-quick.yaml)
#
# All knobs (parallelism, model, environment type, env vars) live in the
# YAML — this script just picks which one to use.
#
# Schema: each YAML conforms to harbor.models.job.config:JobConfig
# (Harbor 0.7.x). Env-var templating (${VAR}) is expanded at run time,
# so ANTHROPIC_API_KEY must be set in the calling shell.

set -euo pipefail

# Run from repo root regardless of where invoked.
cd "$(dirname "$0")/.."

CONFIG="eval/job.yaml"
LABEL="full eval (20 trials)"
if [[ "${1:-}" == "--quick" ]]; then
    CONFIG="eval/job-quick.yaml"
    LABEL="quick eval (3 trials)"
fi

if [[ ! -f "$CONFIG" ]]; then
    echo "error: $CONFIG not found" >&2
    exit 2
fi

if [[ -z "${ANTHROPIC_API_KEY:-}" ]]; then
    echo "error: ANTHROPIC_API_KEY not set in environment" >&2
    echo "  (Track B in tasks/_template/tests/test.sh requires it, and" >&2
    echo "   the claude-code agent itself also needs it.)" >&2
    exit 2
fi

echo "→ $LABEL  ($CONFIG)"
exec harbor run -c "$CONFIG"
