"""Track B — LLM-as-judge sub-graders.

Each of the six Track A criteria has a matching question pack in
`question_packs/<name>.json`. The runner orchestrates judge calls per
the pack's declared scope (per_page, per_page_per_viewport, per_component,
per_image), averages the 1-5 verdicts to get the criterion's score,
and the aggregator weighted-means the six into `score_judge` with the
same weights and gate as Track A.

Verdicts are cached locally under `_cache/` keyed by (question_id,
ref_screenshot_hash, agent_screenshot_hash, judge_model) so prompt-tweak
re-runs only re-fire invalidated cells. The Modal cache layer in
`infra/modal/judge_app.py` uses the same key scheme — drop-in when we
move to Modal.
"""

from . import client, runner

__all__ = ["client", "runner"]
