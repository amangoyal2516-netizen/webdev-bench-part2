"""Modal Functions wrapping the recipe pipeline stages.

`generate_one_task` is the load-bearing entry point — it runs the full
four-stage recipe (author → builder → capture → package) inside a single
Modal sandbox and writes everything to the `recipe-artifacts-part2` Volume
(see `infra/modal/volumes.py`).

Per-stage entry points (`build_html_css`, etc.) exist for re-running one
stage of an existing task without re-doing the earlier ones.

Bulk fan-out for N tasks in parallel comes in Phase B
(see docs/modal-pipeline-plan.md).
"""

from __future__ import annotations

from .app import SECRETS, app
from .images import base_recipe
from .volumes import asset_pools, recipe_artifacts

# Inside the Modal sandbox:
#   /repo/recipe/    — baked into base_recipe via add_local_dir (Phase A2)
#   /repo/tasks/_template/ — baked into base_recipe (Phase A2)
#   /cache/recipe/   — recipe-artifacts-part2 Volume (read/write)
#   /cache/assets/   — asset-pools Volume (read-only, seeded once)
_REPO = "/repo"
_RUNS_MOUNT = "/cache/recipe"
_ASSETS_MOUNT = "/cache/assets"
_RECIPE_VOLUMES = {
    _RUNS_MOUNT: recipe_artifacts,
    _ASSETS_MOUNT: asset_pools,
}
# asset_menu.POOL_ROOT (Phase A1) consults this env var to find the pools.
_RECIPE_ENV = {"WEBDEV_BENCH_POOL_ROOT": _ASSETS_MOUNT}


@app.function(
    image=base_recipe,
    secrets=[SECRETS["anthropic-key"]],
    volumes=_RECIPE_VOLUMES,
    timeout=60 * 30,
)
def generate_one_task(task_id: str | None = None) -> dict:
    """End-to-end recipe for one task — all four stages, one sandbox.

    Stages (sequential, in this single sandbox):
      1. recipe/01-generate/author.py            → design.json
      2. recipe/01-generate/builders/html-css/   → source/{*.html, styles.css, assets/}
      3. recipe/02-capture/capture.py            → screenshots/ + ground_truth/
      4. recipe/03-package/package.py            → _packaged/<task_id>-{oneshot,iter}/

    All outputs land at /cache/recipe/<task_id>/. Asset pool data is read
    from /cache/assets/ (seeded once via scripts/seed_modal_volumes.py).

    Args:
        task_id: optional, e.g. "task_3". If omitted, author auto-assigns
            the next free task_N slot. For parallel fan-out (Phase B) pass
            explicit IDs to avoid concurrent-write races on /cache/recipe/.

    Returns a small summary dict; large artifacts stay in the Volume.
    """
    import os
    import shutil
    import subprocess
    from pathlib import Path

    os.environ["WEBDEV_BENCH_POOL_ROOT"] = _ASSETS_MOUNT

    runs = Path(_RUNS_MOUNT)
    runs.mkdir(parents=True, exist_ok=True)

    def _existing_task_nums() -> set[int]:
        return {
            int(p.name[5:])
            for p in runs.iterdir()
            if p.is_dir() and p.name.startswith("task_") and p.name[5:].isdigit()
        }

    # If the caller wants a specific slot, wipe any stale state so the run
    # is reproducible. Snapshot pre-existing IDs AFTER the wipe so the
    # "new task_N dir" detection below works even when task_id is reused.
    if task_id is not None:
        target = runs / task_id
        if target.exists():
            shutil.rmtree(target)

    pre_existing = _existing_task_nums()

    def _run(*args: str) -> None:
        # No capture_output — children stream to Modal's log so progress is
        # visible during long calls (capture especially can take a minute).
        subprocess.run(["python", *args], check=True)

    # ── Stage 1 — author ─────────────────────────────────────────────────
    print("=== stage 1/4 — author ===", flush=True)
    _run(f"{_REPO}/recipe/01-generate/author.py",
         "--count", "1", "--save", "--runs-dir", str(runs))

    new_nums = _existing_task_nums() - pre_existing
    if not new_nums:
        raise RuntimeError(
            "author reported success but no new task_N dir appeared in /cache/recipe/"
        )
    auto_id = f"task_{max(new_nums)}"
    if task_id is None:
        task_id = auto_id
    elif task_id != auto_id:
        # author wrote to a different slot; move it to the requested one.
        shutil.move(str(runs / auto_id), str(runs / task_id))
    task_dir = runs / task_id
    print(f"  → {task_dir}", flush=True)

    # ── Stage 2 — builder ────────────────────────────────────────────────
    print("=== stage 2/4 — builder ===", flush=True)
    _run(f"{_REPO}/recipe/01-generate/builders/html-css/builder.py",
         "--task", task_id, "--runs-dir", str(runs))

    # ── Stage 3 — capture ────────────────────────────────────────────────
    print("=== stage 3/4 — capture ===", flush=True)
    _run(f"{_REPO}/recipe/02-capture/capture.py", str(task_dir))

    # ── Stage 4 — package ────────────────────────────────────────────────
    print("=== stage 4/4 — package ===", flush=True)
    packaged = task_dir / "_packaged"
    _run(f"{_REPO}/recipe/03-package/package.py", str(task_dir),
         "--tasks-dir", str(packaged),
         "--template", f"{_REPO}/tasks/_template",
         "--force")

    # Persist Volume writes so subsequent `modal volume get` and other
    # functions see them.
    recipe_artifacts.commit()

    variants = sorted(p.name for p in packaged.iterdir() if p.is_dir())
    print(f"=== done — {task_id} with variants={variants} ===", flush=True)
    return {
        "task_id": task_id,
        "task_dir": str(task_dir),
        "packaged_dir": str(packaged),
        "variants": variants,
    }


@app.function(
    image=base_recipe,
    secrets=[SECRETS["anthropic-key"]],
    volumes=_RECIPE_VOLUMES,
    timeout=60 * 60 * 2,
)
def generate_all_tasks(
    count: int = 10,
    start_id: int = 1,
    ids: str | None = None,
) -> list[dict]:
    """Fan out generate_one_task calls in parallel.

    Two modes:
      - default: contiguous task_<start_id>..task_<start_id+count-1>
      - `ids`:   comma-separated explicit IDs, e.g. "task_5,task_9,task_10"
                 (used for retries of non-contiguous failures)

    All sandboxes are submitted via `.spawn()` first, then we block on
    each via `.get()` — wall time is ~ max(per-call), not sum.

    Failures don't abort the batch — each child's outcome (ok or error
    string) is in the result list so the caller can see which IDs made it.
    """
    if ids:
        task_ids = [s.strip() for s in ids.split(",") if s.strip()]
    else:
        task_ids = [f"task_{start_id + i}" for i in range(count)]

    futures = [(tid, generate_one_task.spawn(task_id=tid)) for tid in task_ids]
    print(f"spawned {len(task_ids)} task generation(s): {task_ids}", flush=True)
    results: list[dict] = []
    for tid, fut in futures:
        try:
            r = fut.get()
            print(f"  ✓ {tid}: variants={r.get('variants')}", flush=True)
            results.append({"ok": True, **r})
        except Exception as e:
            print(f"  ✗ {tid}: {type(e).__name__}: {e}", flush=True)
            results.append({"ok": False, "task_id": tid, "error": f"{type(e).__name__}: {e}"})
    ok = sum(1 for r in results if r["ok"])
    print(f"\n{ok}/{len(task_ids)} task(s) generated successfully", flush=True)
    return results


@app.function(
    image=base_recipe,
    secrets=[SECRETS["anthropic-key"]],
    volumes=_RECIPE_VOLUMES,
    timeout=60 * 30,
)
def build_html_css(task_id: str) -> dict:
    """Re-run only the HTML/CSS builder on an existing task dir.

    Useful when iterating on the builder prompt or validator without
    regenerating the design + screenshots + ground_truth.
    """
    import os
    import sys
    from pathlib import Path

    os.environ["WEBDEV_BENCH_POOL_ROOT"] = _ASSETS_MOUNT
    builder_dir = Path(f"{_REPO}/recipe/01-generate/builders/html-css")
    sys.path.insert(0, str(builder_dir))
    from builder import build_one  # noqa: E402

    design_path = Path(f"{_RUNS_MOUNT}/{task_id}/design.json")
    result = build_one(design_path)
    recipe_artifacts.commit()
    return result
