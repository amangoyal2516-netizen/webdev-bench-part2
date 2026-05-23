"""Reward Kit @criterion wrapper for component_presence.

The actual algorithm lives in `grading/criteria/component_presence.py`:
a purely geometric self-comparison of macro-block counts per page (no
author-side convention required).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

GRADING_DIR = Path("/grading")
sys.path.insert(0, str(GRADING_DIR))

from grading.criteria import component_presence


def check() -> float:
    pages = list(json.loads((GRADING_DIR / "task_config.json").read_text())["pages"])
    result = component_presence.score(
        agent_output_dir=GRADING_DIR / "agent_output",
        ground_truth_dir=GRADING_DIR / "ground_truth",
        pages=pages,
    )
    return result["score"]


if __name__ == "__main__":
    print(json.dumps({"score": check()}, indent=2))
