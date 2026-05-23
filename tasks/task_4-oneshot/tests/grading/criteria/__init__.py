"""Track A sub-graders.

Each module exports a `score(agent_output_dir, ground_truth_dir, pages)`
function returning::

    {"score": float ∈ [0, 1],
     "per_page": {<page>: {"score": float, ...details}},
     "detail": "<one-line mechanism summary>"}

Conventions:
    - agent_output_dir contains the agent's <page>.html + styles.css + assets/
    - ground_truth_dir contains pre-computed JSON artifacts from
      `recipe/02-capture/capture.py` (bboxes/, palette/, typography/, …)
    - `pages` is the list of canonical page names (without .html)

Identity case: if you point the agent_output_dir at the ground-truth source/
directory, the score should be ~1.0 (sanity check).
"""

from . import (
    animation_fidelity,
    color_palette,
    image_content_fidelity,
    layout_structure,
    typography,
    visible_text_fidelity,
)

__all__ = [
    "animation_fidelity",
    "color_palette",
    "image_content_fidelity",
    "layout_structure",
    "typography",
    "visible_text_fidelity",
]
