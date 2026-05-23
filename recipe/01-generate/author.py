"""Author runner — parallel per-design with seeded variety.

For each of N requested design docs, the harness:

  1. Picks a `(family, structure, domain, audience)` seed combination from
     `seeds.json`. Families are sampled without replacement (when N ≤ pool
     size) so no two designs share a family.
  2. Calls Claude once for that design, with the seeds injected into the
     user message as a biasing hint. The prompt tells the model that if
     the combination is contradictory, it should use its own creativity
     rather than force a literal mash-up.
  3. Validates the response against the schema + extra tests; on failure,
     re-prompts with the error list (per-design correction loop, capped at
     --max-iterations).

All N designs run in parallel (one thread per design by default; override
with --workers). Per-design results print as they complete; the final
summary preserves seed order.

Usage:
    python recipe/01-generate/author.py --count 5
    python recipe/01-generate/author.py --count 10 --save
    python recipe/01-generate/author.py --count 8 --workers 4 --max-iterations 5

Reads ANTHROPIC_API_KEY from the environment.
Requires: pip install anthropic jsonschema
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import anthropic
import jsonschema

ROOT = Path(__file__).resolve().parent
PROMPT_PATH = ROOT / "prompts" / "author.md"
SCHEMA_PATH = ROOT / "schemas" / "design-doc.schema.json"
SEEDS_PATH = ROOT / "seeds.json"
REPO_ROOT = ROOT.parent.parent  # webdev-bench/

DEFAULT_MODEL = "claude-opus-4-7"
DEFAULT_MAX_TOKENS = 8192       # one design fits comfortably
DEFAULT_MAX_ITERATIONS = 3
DEFAULT_COUNT = 5

# Defensive fence-stripper for the rare case the model wraps its JSON.
_JSON_BLOCK = re.compile(r"```(?:json)?\s*(\{.*\})\s*```", re.DOTALL)


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def load_system_prompt() -> str:
    return PROMPT_PATH.read_text()


def load_schema() -> dict[str, Any]:
    return json.loads(SCHEMA_PATH.read_text())


def load_seeds() -> dict[str, list[str]]:
    return json.loads(SEEDS_PATH.read_text())


def extract_json(text: str) -> dict[str, Any]:
    """Parse the model's response as JSON, falling back to fence extraction."""
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    m = _JSON_BLOCK.search(text)
    if m:
        return json.loads(m.group(1))
    raise ValueError(
        f"response was not valid JSON and contained no ```json fence. "
        f"first 200 chars: {text[:200]!r}"
    )


# ---------------------------------------------------------------------------
# Tests (single design doc)
# ---------------------------------------------------------------------------


def _schema_errors(doc: dict[str, Any], schema: dict[str, Any]) -> list[str]:
    V = jsonschema.Draft202012Validator(schema)
    return [
        f"schema: {'/'.join(str(p) for p in e.absolute_path) or '<root>'}: {e.message}"
        for e in V.iter_errors(doc)
    ]


def _unique_page_names(doc: dict[str, Any]) -> list[str]:
    names = [p.get("name", "") for p in doc.get("pages", []) if isinstance(p, dict)]
    dupes = sorted({n for n in names if names.count(n) > 1 and n})
    if dupes:
        return [f"pages: duplicate page name(s): {dupes}"]
    return []


def run_tests(doc: dict[str, Any], schema: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    errors.extend(_schema_errors(doc, schema))
    errors.extend(_unique_page_names(doc))
    return errors


# ---------------------------------------------------------------------------
# Seeds — random biasing keywords per design
# ---------------------------------------------------------------------------


def pick_seed_sets(seeds_pool: dict[str, list[str]], n: int) -> list[dict[str, str]]:
    """Pick N (family, structure, domain, audience, palette) combinations.

    Families are sampled without replacement when N ≤ pool size — that's
    the highest-leverage axis, so we guarantee uniqueness there. The
    other four axes are picked independently per design. Palette is a
    biasing hint for visual variety so generated designs don't all
    converge on the same warm-cream default.
    """
    families = seeds_pool["families"]
    if n <= len(families):
        chosen = random.sample(families, n)
    else:
        chosen = random.sample(families, len(families))
        chosen.extend(random.choices(families, k=n - len(families)))

    return [
        {
            "family": fam,
            "structure": random.choice(seeds_pool["structures"]),
            "domain": random.choice(seeds_pool["domains"]),
            "audience": random.choice(seeds_pool["audiences"]),
            "palette": random.choice(seeds_pool["palettes"]),
        }
        for fam in chosen
    ]


def _seed_block(seeds: dict[str, str]) -> str:
    return "\n".join(f"  - {key}: {value}" for key, value in seeds.items())


def make_initial_prompt(seeds: dict[str, str]) -> str:
    return (
        "Generate one design doc.\n\n"
        f"For this design, lean toward:\n{_seed_block(seeds)}\n\n"
        "If this combination feels contradictory or wouldn't produce a coherent real-world "
        "website, use your own creativity — pick a coherent subset of the seeds (or reinterpret "
        "them loosely) and invent something plausible around that. Don't force a literal mash-up "
        "of all four if it would yield nonsense.\n\n"
        "Return only the JSON object — no prose, no markdown fences."
    )


def make_correction_prompt(errors: list[str], seeds: dict[str, str]) -> str:
    n = len(errors)
    err_lines = "\n".join(f"  {i + 1}. {e}" for i, e in enumerate(errors[:10]))
    return (
        f"Your previous response failed {n} validation check{'s' if n != 1 else ''}:\n\n"
        f"{err_lines}\n\n"
        f"Original seeds (still apply):\n{_seed_block(seeds)}\n\n"
        "Return the corrected design doc as a single JSON object only — no prose, "
        "no markdown fences. Fix every listed error; do not change anything that was already valid."
    )


# ---------------------------------------------------------------------------
# Single-design call (iterative correction)
# ---------------------------------------------------------------------------


def call_one_design(
    client: anthropic.Anthropic,
    system: str,
    schema: dict[str, Any],
    seeds: dict[str, str],
    *,
    model: str,
    max_tokens: int,
    max_iterations: int,
) -> tuple[dict[str, Any] | None, dict[str, Any], list[dict[str, str]], list[str]]:
    """Generate one design doc with iterative correction.

    Returns (doc, meta, transcript, errors).
    """
    transcript: list[dict[str, str]] = [
        {"role": "user", "content": make_initial_prompt(seeds)}
    ]
    doc: dict[str, Any] | None = None
    errors: list[str] = []
    iterations_used = 0
    total_in = total_out = 0
    elapsed_total = 0.0

    for iteration in range(1, max_iterations + 1):
        iterations_used = iteration
        t0 = time.time()
        with client.messages.stream(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=transcript,
        ) as stream:
            for _ in stream:
                pass
            resp = stream.get_final_message()
        elapsed_total += time.time() - t0
        total_in += resp.usage.input_tokens
        total_out += resp.usage.output_tokens
        raw = resp.content[0].text
        transcript.append({"role": "assistant", "content": raw})

        try:
            doc = extract_json(raw)
            errors = run_tests(doc, schema)
        except ValueError as e:
            doc = None
            errors = [f"json_parse: {e}"]

        if not errors:
            break

        if iteration < max_iterations:
            transcript.append(
                {"role": "user", "content": make_correction_prompt(errors, seeds)}
            )

    meta = {
        "model": model,
        "seeds": seeds,
        "iterations_used": iterations_used,
        "max_iterations": max_iterations,
        "elapsed_s": round(elapsed_total, 2),
        "input_tokens": total_in,
        "output_tokens": total_out,
    }
    return doc, meta, transcript, errors


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def next_task_id(runs_dir: Path) -> int:
    if not runs_dir.is_dir():
        return 1
    existing = []
    for p in runs_dir.iterdir():
        if p.is_dir() and (m := re.fullmatch(r"task_(\d+)", p.name)):
            existing.append(int(m.group(1)))
    return (max(existing) + 1) if existing else 1


def save_design(
    runs_dir: Path,
    task_num: int,
    doc: dict[str, Any] | None,
    meta: dict[str, Any],
    transcript: list[dict[str, str]],
    errors: list[str],
) -> Path:
    task_dir = runs_dir / f"task_{task_num}"
    task_dir.mkdir(parents=True, exist_ok=True)
    if doc is not None:
        (task_dir / "design.json").write_text(json.dumps(doc, indent=2) + "\n")
    (task_dir / "_author_meta.json").write_text(
        json.dumps({**meta, "errors": errors}, indent=2) + "\n"
    )
    (task_dir / "_author_transcript.json").write_text(
        json.dumps(transcript, indent=2) + "\n"
    )
    return task_dir


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__.split("\n")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--count", type=int, default=DEFAULT_COUNT, help=f"how many designs to generate in parallel (default {DEFAULT_COUNT})")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--max-tokens", type=int, default=DEFAULT_MAX_TOKENS, help=f"max output tokens per design call (default {DEFAULT_MAX_TOKENS})")
    ap.add_argument("--max-iterations", type=int, default=DEFAULT_MAX_ITERATIONS, help=f"max correction rounds per design (default {DEFAULT_MAX_ITERATIONS})")
    ap.add_argument("--workers", type=int, default=None, help="parallel workers (default: --count, i.e. one thread per design)")
    ap.add_argument("--save", action="store_true", help="save each valid design to recipe/runs/task_<N>/")
    ap.add_argument("--runs-dir", default=str(REPO_ROOT / "recipe" / "runs"))
    ap.add_argument("--print-json", action="store_true", help="print every valid design.json to stdout")
    ap.add_argument("--seed", type=int, default=None, help="optional random seed for reproducible seed-picks")
    args = ap.parse_args()

    if args.count < 1 or args.max_iterations < 1:
        print("error: --count and --max-iterations must be ≥ 1", file=sys.stderr)
        return 2
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("error: ANTHROPIC_API_KEY not set in environment", file=sys.stderr)
        return 2

    if args.seed is not None:
        random.seed(args.seed)

    system = load_system_prompt()
    schema = load_schema()
    seeds_pool = load_seeds()
    client = anthropic.Anthropic()

    seed_sets = pick_seed_sets(seeds_pool, args.count)
    workers = args.workers or args.count

    print(
        f"Generating {args.count} design doc(s) in parallel "
        f"(workers={workers}, model={args.model}, max_iter={args.max_iterations})…"
    )
    print("\n=== seed combinations ===")
    for i, seeds in enumerate(seed_sets, 1):
        seed_str = ", ".join(f"{k}={v}" for k, v in seeds.items())
        print(f"  [{i:>2}] {seed_str}")
    print()

    # Submit all in parallel
    results: list[tuple[int, dict[str, str], dict[str, Any] | None, dict[str, Any], list[dict[str, str]], list[str]]] = []
    t_wall = time.time()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(
                call_one_design,
                client, system, schema, seeds,
                model=args.model, max_tokens=args.max_tokens, max_iterations=args.max_iterations,
            ): (idx, seeds)
            for idx, seeds in enumerate(seed_sets, 1)
        }
        for future in as_completed(futures):
            idx, seeds = futures[future]
            try:
                doc, meta, transcript, errors = future.result()
            except Exception as e:
                print(f"  [{idx:>2}] CALL FAILED: {type(e).__name__}: {e}", file=sys.stderr)
                results.append((idx, seeds, None, {"error": str(e), "elapsed_s": 0, "input_tokens": 0, "output_tokens": 0}, [], [f"call_failed: {e}"]))
                continue

            n_pages = len(doc.get("pages", [])) if doc else 0
            desc = ((doc.get("description") if doc else "") or "")[:70]
            iters = meta.get("iterations_used", 0)
            if errors:
                status = f"INVALID after {iters}i ({len(errors)} err)"
            elif iters == 1:
                status = "ok"
            else:
                status = f"ok (recovered in {iters}i)"

            print(
                f"  [{idx:>2}] {status:>26} | {n_pages}p | {meta['elapsed_s']:>5}s | "
                f"{meta['input_tokens']:>5}in/{meta['output_tokens']:>5}out tok | "
                f"{desc!r}"
            )
            for err in errors[:3]:
                print(f"        → {err}")
            results.append((idx, seeds, doc, meta, transcript, errors))

    wall_elapsed = round(time.time() - t_wall, 2)

    # Sort back to seed-set order for stable downstream output
    results.sort(key=lambda r: r[0])

    # Save valid designs sequentially
    runs_dir = Path(args.runs_dir)
    saved_names: list[str] = []
    if args.save:
        task_num = next_task_id(runs_dir)
        for _, _, doc, meta, transcript, errors in results:
            if doc is None or errors:
                continue
            save_design(runs_dir, task_num, doc, meta, transcript, errors)
            saved_names.append(f"task_{task_num}")
            task_num += 1
        print(f"\nsaved {len(saved_names)} → {', '.join(saved_names) if saved_names else '(none)'}")

    # Aggregate summary
    n_ok = sum(1 for _, _, doc, _, _, errors in results if doc and not errors)
    total_in = sum(m.get("input_tokens", 0) for _, _, _, m, _, _ in results)
    total_out = sum(m.get("output_tokens", 0) for _, _, _, m, _, _ in results)
    total_call_s = round(sum(m.get("elapsed_s", 0) for _, _, _, m, _, _ in results), 2)
    print(
        f"\n=== {n_ok}/{args.count} valid; tokens {total_in}in/{total_out}out; "
        f"wall={wall_elapsed}s (sum of call times: {total_call_s}s) ==="
    )

    if args.count > 1:
        print("\n=== generated descriptions (eyeball for variety) ===")
        for idx, seeds, doc, _, _, errors in results:
            family = seeds.get("family", "?")
            if doc and not errors:
                desc = (doc.get("description") or "")[:80]
                print(f"  [{idx:>2}] {family:<28} → {desc}")
            else:
                first_err = (errors or [""])[0][:60]
                print(f"  [{idx:>2}] {family:<28} → FAILED: {first_err}")

    if args.print_json:
        for _, _, doc, _, _, errors in results:
            if doc and not errors:
                print(json.dumps(doc, indent=2))

    return 0 if n_ok == args.count else 1


if __name__ == "__main__":
    sys.exit(main())
