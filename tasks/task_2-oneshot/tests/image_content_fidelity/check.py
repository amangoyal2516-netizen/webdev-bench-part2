"""Reward Kit @criterion wrapper for image_content_fidelity.

The actual algorithm lives in `grading/criteria/image_content_fidelity.py`.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

GRADING_DIR = Path("/grading")
sys.path.insert(0, str(GRADING_DIR))

from grading.criteria import image_content_fidelity


def check() -> float:
    pages = list(json.loads((GRADING_DIR / "task_config.json").read_text())["pages"])
    result = image_content_fidelity.score(
        agent_output_dir=GRADING_DIR / "agent_output",
        ground_truth_dir=GRADING_DIR / "ground_truth",
        pages=pages,
    )
    return result["score"]


if __name__ == "__main__":
    print(json.dumps({"score": check()}, indent=2))
