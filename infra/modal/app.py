"""Top-level Modal App for webdev-bench.

Every other module in `infra/modal/` imports `app` (and `SECRETS`) from here.
Provisioning these secrets is steps.md Phase 0a.

Smoke entry point:

    modal run infra/modal/app.py::smoke

Should print `{"ok": True, "has_anthropic_key": True}` if Phase 0a is wired.
"""

from __future__ import annotations

import os

import modal

app = modal.App("webdev-bench")

# Secrets that must exist before any function runs.
# Listed centrally so `modal run` fails fast if a required one is missing.
SECRETS: dict[str, modal.Secret] = {
    "anthropic-key": modal.Secret.from_name("anthropic-key"),
    # Uncomment when needed:
    # "openai-key":   modal.Secret.from_name("openai-key"),
    # "qwen-vl":      modal.Secret.from_name("qwen-vl"),     # Track B open-weight referee
}


@app.function(secrets=list(SECRETS.values()), timeout=60)
def smoke() -> dict:
    """Sanity check — confirms App resolves and required secrets attach.

    Run via: `modal run infra/modal/app.py::smoke`
    """
    print({
        "ok": True,
        "has_anthropic_key": bool(os.environ.get("ANTHROPIC_API_KEY")),
    })
    return {
        "ok": True,
        "has_anthropic_key": bool(os.environ.get("ANTHROPIC_API_KEY")),
    }
