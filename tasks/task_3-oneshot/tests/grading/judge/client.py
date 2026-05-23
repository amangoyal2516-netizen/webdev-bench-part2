"""Anthropic-vision wrapper with a local JSON-file cache.

One `JudgeClient.ask()` call = one 1-5 judge query: reference image +
agent image + a single question → digit in {1, 2, 3, 4, 5}.

We use a 5-point scale (anchors in `prompts/system.md`). The raw 1-5
verdict is stored as-is in the cache and in `grading.json`; the
per-criterion aggregation normalises to [0, 1] via `(mean - 1) / 4`
(see `runner.py`).

Cache key: `sha256(question_id | ref_screenshot_hash | agent_screenshot_hash | judge_model | SCALE_VERSION)`
The `SCALE_VERSION` discriminator is part of the cache key so verdicts
from different scale revisions never collide. `infra/modal/judge_app.py::cache_key`
mirrors the same payload shape.

Strict-grading rules live in `prompts/system.md`.

The judge model is configured by `task_config.json.judge_model` (default
`claude-opus-4-7`). No `temperature` parameter is passed — the active
Opus model doesn't accept it.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import random
import sys
import time
from pathlib import Path
from typing import Any, Callable

DEFAULT_JUDGE_MODEL = "claude-opus-4-7"
DEFAULT_CACHE_DIR = Path(__file__).resolve().parent / "_cache"
DEFAULT_SYSTEM_PROMPT = Path(__file__).resolve().parent / "prompts" / "system.md"

# Retry budget for transient Anthropic API errors (overload, rate limit,
# connection, timeout). These compound with the SDK's own retries (default
# 2), so total effective attempts = SDK_RETRIES × MAX_RETRIES.
MAX_RETRIES = 5
BASE_DELAY_S = 2.0
MAX_DELAY_S = 60.0

# Tiny output cap — the judge returns a single digit 1-5 per question.
JUDGE_MAX_TOKENS = 16

# Cache-key version discriminator. Verdicts from a different scale revision
# hash to different cache keys and never collide with current lookups.
SCALE_VERSION = "5pt"

# Min/max valid verdict on the 5-point scale. Used by the parser as the
# domain for digit extraction.
SCALE_MIN, SCALE_MAX = 1, 5

# Anthropic vision API rejects images with any dimension > 8000 px.
# Full-page screenshots stitched by Playwright at mobile widths routinely
# stretch to 10–25k px tall (long scrolling pages). We downscale to
# MAX_IMAGE_DIM on the longest side, preserving aspect ratio, before
# base64 encoding. 7500 keeps a safety margin under the hard cap.
MAX_IMAGE_DIM = 7500


def _hash_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:16]


def cache_key(
    question_id: str,
    ref_image_path: Path,
    agent_image_path: Path,
    model: str,
) -> str:
    """Stable cache key for one (question, rollout, judge) triple.
    Mirrors `infra/modal/judge_app.py::cache_key`."""
    ref_hash = _hash_bytes(ref_image_path.read_bytes())
    agent_hash = _hash_bytes(agent_image_path.read_bytes())
    return _cache_key_for_hashes(question_id, ref_hash, agent_hash, model)


def _cache_key_for_hashes(
    question_id: str, ref_hash: str, agent_hash: str, model: str,
) -> str:
    """Pre-computed-hashes variant — lets callers hash both images once
    and reuse the hashes for every question in a pack (8× fewer file
    reads when checking the cache for a multi-question pack).

    The `SCALE_VERSION` field is part of the cache key so verdicts on
    different scales never collide (see module docstring)."""
    payload = "|".join([question_id, ref_hash, agent_hash, model, SCALE_VERSION])
    return hashlib.sha256(payload.encode()).hexdigest()


def _parse_verdict(raw: str) -> int:
    """Pull a 1-5 digit out of the model's response. Defaults to SCALE_MIN
    (= 1) on ambiguity, mirroring the system prompt's 'be strict when
    uncertain' rule — under-report rather than over-report agreement.

    The lookarounds exclude word chars (letters, digits, underscores) so a
    response like "Q1: 4" parses as 4 — the "1" inside the label is bound
    to "Q" on the left and never matches."""
    import re
    m = re.search(r"(?<!\w)([1-5])(?!\w)", raw.strip())
    if m:
        return int(m.group(1))
    return SCALE_MIN


def _is_retriable(exc: Exception) -> bool:
    """Should this Anthropic exception be retried with backoff?

    Retry on transient infrastructure errors (overload, rate limit, timeout,
    connection). Don't retry on caller-side errors (auth, bad request) —
    those won't fix themselves.
    """
    import anthropic
    return isinstance(exc, (
        anthropic.RateLimitError,
        anthropic.InternalServerError,
        anthropic.APIConnectionError,
        anthropic.APITimeoutError,
    ))


def _with_retry(call: Callable[[], Any], *, label: str = "judge API call") -> Any:
    """Exponential backoff with jitter for transient Anthropic API errors.

    Wraps `call` (a zero-arg callable). Retries up to MAX_RETRIES times on
    transient errors (overload 529, rate limit 429, 5xx, timeouts, connection
    failures). Non-retriable errors propagate immediately. The SDK already
    does 2 retries internally on the same kinds of errors; this is the
    outer layer that survives a fully exhausted SDK retry.
    """
    last_exc: Exception | None = None
    for attempt in range(MAX_RETRIES + 1):
        try:
            return call()
        except Exception as e:
            last_exc = e
            if not _is_retriable(e) or attempt == MAX_RETRIES:
                raise
            delay = min(MAX_DELAY_S, BASE_DELAY_S * (2 ** attempt))
            delay += random.uniform(0, delay * 0.3)  # jitter
            print(
                f"  {label}: {type(e).__name__} on attempt {attempt + 1}/{MAX_RETRIES + 1}; "
                f"sleeping {delay:.1f}s before retry",
                file=sys.stderr,
            )
            time.sleep(delay)
    if last_exc is not None:
        raise last_exc
    raise RuntimeError("unreachable")  # for type-checker


def _parse_verdicts(raw: str, n: int) -> list[int]:
    """Parse N 1-5 verdicts from a batched response.

    Matches standalone 1-5 digits — i.e. digits NOT adjacent to other
    word characters (letters, digits, or underscores). This is robust to:
      - the clean format we ask for (just digits, one per line)
      - the model adding labels like "Q1: 4\\nQ2: 5" — the "1" / "2"
        inside the Q-labels are bound to the letter and don't match;
        only the answer digit after the colon-space is captured.

    If fewer than N parseable digits are returned, the missing ones
    default to SCALE_MIN — the strict-grading default from
    prompts/system.md.
    """
    import re
    matches = re.findall(r"(?<!\w)([1-5])(?!\w)", raw)
    verdicts = [int(m) for m in matches[:n]]
    while len(verdicts) < n:
        verdicts.append(SCALE_MIN)
    return verdicts


def _encode_image(image_path: Path) -> str:
    """Read an image; if either dimension > MAX_IMAGE_DIM, downscale
    proportionally; return base64-encoded PNG bytes (as ASCII string).

    Anthropic vision API rejects images with any dim > 8000 px. Mobile
    full-page screenshots routinely stretch to 10–25k px tall.
    """
    raw = image_path.read_bytes()
    # Cheap-path: small images skip the Pillow round-trip.
    from PIL import Image
    img = Image.open(io.BytesIO(raw))
    w, h = img.size
    if max(w, h) <= MAX_IMAGE_DIM:
        return base64.b64encode(raw).decode("ascii")
    scale = MAX_IMAGE_DIM / max(w, h)
    new_size = (max(1, int(w * scale)), max(1, int(h * scale)))
    img = img.convert("RGB") if img.mode not in ("RGB", "RGBA") else img
    img = img.resize(new_size, Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return base64.b64encode(buf.getvalue()).decode("ascii")


class JudgeClient:
    """Single-judge client with disk cache. Thread-safe at the cache-file
    level (each verdict is its own file) but the Anthropic SDK call is
    serial per-instance."""

    def __init__(
        self,
        model: str = DEFAULT_JUDGE_MODEL,
        cache_dir: Path | None = None,
        system_prompt_path: Path | None = None,
    ):
        import anthropic

        self.model = model
        self.cache_dir = Path(cache_dir) if cache_dir else DEFAULT_CACHE_DIR
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        sp_path = Path(system_prompt_path) if system_prompt_path else DEFAULT_SYSTEM_PROMPT
        self.system_prompt = sp_path.read_text()
        self.anthropic = anthropic.Anthropic()

    def _cache_path(self, key: str) -> Path:
        return self.cache_dir / f"{key}.json"

    def image_hashes(self, ref_image_path: Path, agent_image_path: Path) -> tuple[str, str]:
        """Hash both images once. Callers reuse these for every question in
        a pack to avoid re-reading the PNGs N times during cache lookup."""
        return (
            _hash_bytes(ref_image_path.read_bytes()),
            _hash_bytes(agent_image_path.read_bytes()),
        )

    def cached_verdict_for_hashes(
        self, question_id: str, ref_hash: str, agent_hash: str,
    ) -> dict[str, Any] | None:
        """Check the per-question cache using pre-computed image hashes.
        Returns the cached result (with cached=True) or None if missing."""
        key = _cache_key_for_hashes(question_id, ref_hash, agent_hash, self.model)
        cf = self._cache_path(key)
        if not cf.exists():
            return None
        d = json.loads(cf.read_text())
        return {**d, "cached": True}

    def ask_batched(
        self,
        question_specs: list[tuple[str, str]],
        ref_image_path: Path,
        agent_image_path: Path,
    ) -> list[dict[str, Any]]:
        """Ask N independent binary questions about the same image pair in
        ONE API call. Returns a list of result dicts (same shape as `ask()`)
        in the same order as `question_specs`.

        Per-question caching is the runner's responsibility — this method
        always fires a fresh API call for ALL provided questions. Each
        verdict is written to its own cache file using the standard
        per-question cache_key scheme, so future runs hit the cache
        one-question-at-a-time even though writes were batched.

        Saves ~6× image-token cost vs N separate calls (the two images
        get sent once instead of N times) and N-1 round-trip latencies.
        """
        if not question_specs:
            return []

        ref_b64 = _encode_image(ref_image_path)
        agent_b64 = _encode_image(agent_image_path)
        n = len(question_specs)
        question_block = "\n".join(
            f"Q{i + 1}: {text}" for i, (_qid, text) in enumerate(question_specs)
        )

        t0 = time.time()
        resp = _with_retry(
            lambda: self.anthropic.messages.create(
                model=self.model,
                max_tokens=max(JUDGE_MAX_TOKENS, n * 8),
                system=self.system_prompt,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "Reference (ground truth):"},
                            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": ref_b64}},
                            {"type": "text", "text": "Agent's attempt:"},
                            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": agent_b64}},
                            {
                                "type": "text",
                                "text": (
                                    f"You will be asked {n} independent questions about the "
                                    f"images above. Each question is independent of every other "
                                    f"— do not let your answer to one question affect another. "
                                    f"Apply the 1-5 scale anchors and strict-grading rules from "
                                    f"the system prompt to each question individually.\n\n"
                                    f"Output exactly {n} digit(s), one per line, in the order "
                                    f"asked. Each digit is in {{1, 2, 3, 4, 5}}. No commentary, "
                                    f"no JSON, no labels, no prefixes — just the digits.\n\n{question_block}"
                                ),
                            },
                        ],
                    }
                ],
            ),
            label=f"ask_batched({n} q)",
        )
        elapsed = time.time() - t0
        raw = resp.content[0].text if resp.content else ""
        verdicts = _parse_verdicts(raw, n)

        ref_hash, agent_hash = self.image_hashes(ref_image_path, agent_image_path)
        results: list[dict[str, Any]] = []
        for (qid, _text), v in zip(question_specs, verdicts):
            key = _cache_key_for_hashes(qid, ref_hash, agent_hash, self.model)
            result = {
                "verdict": v,
                "raw": raw,
                "elapsed_s": round(elapsed, 2),
                "model": self.model,
                "key": key,
                "cached": False,
                "batched": True,
                "batch_size": n,
            }
            to_cache = {k: val for k, val in result.items() if k != "cached"}
            self._cache_path(key).write_text(json.dumps(to_cache, indent=2) + "\n")
            results.append(result)
        return results

    def ask(
        self,
        question_id: str,
        ref_image_path: Path,
        agent_image_path: Path,
        question_text: str,
    ) -> dict[str, Any]:
        """Ask one 1-5 scale question. Returns:

            {
              "verdict": int in [1, 5],
              "raw": "...",
              "cached": bool,
              "elapsed_s": float,
              "model": str,
              "key": str
            }

        The verdict is stored raw on the 1-5 scale. Aggregation to a
        per-criterion [0, 1] score happens in `runner.py` via
        `(mean - 1) / 4`.
        """
        key = cache_key(question_id, ref_image_path, agent_image_path, self.model)
        cache_file = self._cache_path(key)
        if cache_file.exists():
            cached = json.loads(cache_file.read_text())
            return {**cached, "cached": True}

        # Build the user message — text + two images + question. Each
        # image is downscaled if either dim > MAX_IMAGE_DIM (see helper).
        ref_b64 = _encode_image(ref_image_path)
        agent_b64 = _encode_image(agent_image_path)

        t0 = time.time()
        resp = _with_retry(
            lambda: self.anthropic.messages.create(
                model=self.model,
                max_tokens=JUDGE_MAX_TOKENS,
                system=self.system_prompt,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "Reference (ground truth):"},
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": "image/png",
                                    "data": ref_b64,
                                },
                            },
                            {"type": "text", "text": "Agent's attempt:"},
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": "image/png",
                                    "data": agent_b64,
                                },
                            },
                            {
                                "type": "text",
                                "text": (
                                    f"Question: {question_text}\n\n"
                                    "Answer with exactly one digit from 1 to 5 using the "
                                    "scale anchors from the system prompt."
                                ),
                            },
                        ],
                    }
                ],
            ),
            label=f"ask({question_id})",
        )
        elapsed = time.time() - t0
        raw = resp.content[0].text if resp.content else ""
        verdict = _parse_verdict(raw)

        result = {
            "verdict": verdict,
            "raw": raw,
            "elapsed_s": round(elapsed, 2),
            "model": self.model,
            "key": key,
            "cached": False,
        }
        # Write cache (without the `cached` flag — that gets set on read).
        to_cache = {k: v for k, v in result.items() if k != "cached"}
        cache_file.write_text(json.dumps(to_cache, indent=2) + "\n")
        return result
