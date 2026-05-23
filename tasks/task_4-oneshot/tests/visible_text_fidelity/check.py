"""Reward Kit @criterion wrapper for visible_text_fidelity.

The actual algorithm lives in `grading/criteria/visible_text_fidelity.py` —
this file is just the per-task adapter Reward Kit invokes.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# Harbor mounts the verifier's working content under /grading/.
GRADING_DIR = Path("/grading")
sys.path.insert(0, str(GRADING_DIR))

from grading.criteria import visible_text_fidelity


def check() -> float:
    """Reward Kit's discovery hook. Returns the criterion score ∈ [0, 1].
    `tests/_weights.toml` controls how this is aggregated."""
    pages = list(json.loads((GRADING_DIR / "task_config.json").read_text())["pages"])
    result = visible_text_fidelity.score(
        agent_output_dir=GRADING_DIR / "agent_output",
        ground_truth_dir=GRADING_DIR / "ground_truth",
        pages=pages,
    )
    return result["score"]


if __name__ == "__main__":
    # Local development: `python tests/visible_text_fidelity/check.py`
    print(json.dumps({"score": check()}, indent=2))
