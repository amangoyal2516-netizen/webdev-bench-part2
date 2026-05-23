"""Harbor → Modal runner.

When the eval invokes `harbor run --runner modal`, this module is the bridge
that dispatches each agent container (e.g. base-html-css-oneshot) and the
separate verifier container (base-verifier) to Modal sandboxes instead of
running them under local Docker.

STATUS: stub. Phase 0c (steps.md) is to read the Harbor runner-plugin
contract at https://harborframework.com/docs/runners/ and implement against
it. The import path is declared here now so downstream code can already
reference `infra.modal.harbor_runner.dispatch_task`.

Open questions for Phase 0c:
- Does Harbor's runner plugin run in-process in the harbor CLI, or does it
  spawn its own process? Affects how we hand off to `modal.Function.call`.
- Does Harbor stream agent stdout/stderr live, or wait for completion? Modal
  Functions support both via `.spawn()` vs `.remote()`.
- How does the separate verifier container get the agent's /workspace/output/?
  Harbor probably tars it; we mount that tarball into the verifier Function.
"""

from __future__ import annotations

from .app import app
from .images import base_verifier
from .volumes import eval_runs


@app.function(
    image=base_verifier,
    volumes={"/cache/eval": eval_runs},
    timeout=60 * 30,
)
def dispatch_task(task_dir: str, model_id: str, agent: str) -> dict:
    """Per-task entry that boots the agent image, runs the agent, runs the
    verifier, writes `reward.json` back to the `eval-runs-part2` Volume.

    Args:
        task_dir: path to a Harbor task directory (validated by `harbor task validate`).
        model_id: agent model ID, e.g. "claude-opus-4-7".
        agent:    agent framework, e.g. "claude-code".

    Returns:
        Path within `eval_runs` Volume where `reward.json` was written.
    """
    raise NotImplementedError(
        "Phase 0c — wire to Harbor's runner-plugin API before first eval."
    )
