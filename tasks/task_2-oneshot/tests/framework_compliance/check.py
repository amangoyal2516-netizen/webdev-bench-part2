"""Framework-compliance gate — thin wrapper around grading.gates.framework_compliance.

NOT a weighted Track A criterion. Returns 1.0 (compliant) or 0.3
(violation). grading/aggregator.py multiplies the weighted_mean of
the seven sub-graders by this value to produce the final reward.

If running this check.py standalone (local dev), it prints the gate's
score + the list of violations.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

GRADING_DIR = Path("/grading")
sys.path.insert(0, str(GRADING_DIR))

from grading.gates import framework_compliance


def check() -> float:
    """Returns 1.0 (pass) or 0.3 (fail). Violations are logged to stderr."""
    task_config = json.loads((GRADING_DIR / "task_config.json").read_text())
    result = framework_compliance.score(GRADING_DIR / "agent_output", task_config)
    if result["violations"]:
        print(
            "framework_compliance VIOLATIONS:\n  " + "\n  ".join(result["violations"]),
            file=sys.stderr,
        )
    return result["score"]


if __name__ == "__main__":
    task_config = json.loads((GRADING_DIR / "task_config.json").read_text())
    print(json.dumps(framework_compliance.score(GRADING_DIR / "agent_output", task_config), indent=2))
