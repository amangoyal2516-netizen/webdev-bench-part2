"""Reward Kit @criterion wrapper for layout_structure.

The actual algorithm lives in `grading/criteria/layout_structure.py`.
Multi-viewport: scores at desktop + tablet + mobile and averages.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

GRADING_DIR = Path("/grading")
sys.path.insert(0, str(GRADING_DIR))

from grading.criteria import layout_structure


def check() -> float:
    pages = list(json.loads((GRADING_DIR / "task_config.json").read_text())["pages"])
    result = layout_structure.score(
        agent_output_dir=GRADING_DIR / "agent_output",
        ground_truth_dir=GRADING_DIR / "ground_truth",
        pages=pages,
    )
    return result["score"]


if __name__ == "__main__":
    print(json.dumps({"score": check()}, indent=2))
