#!/usr/bin/env python3
"""One-time seed of the `asset-pools` Modal Volume.

Uploads the four asset pools (photos / fonts / icons / avatars) from
`infra/assets/` into the `asset-pools` Volume, where the recipe pipeline
expects them mounted at `/cache/assets/<pool>/`.

Layout inside the Volume after seeding:

    asset-pools://
    ├── photo-pool/
    │   ├── manifest.json
    │   └── <photo_id>.jpg ...
    ├── font-pool/
    │   ├── manifest.json
    │   └── <family>/<family>-<weight>.woff2 ...
    ├── icon-pool/
    │   ├── manifest.json
    │   └── <name>.svg ...
    └── avatar-pool/
        ├── manifest.json
        └── <style>-<seed>.svg ...

This matches the layout the recipe's `asset_menu.POOL_ROOT` expects when
`WEBDEV_BENCH_POOL_ROOT=/cache/assets` is set in the Modal function
decorator (see docs/modal-pipeline-plan.md A3).

Idempotent — `modal volume put` overwrites existing files; safe to re-run
after refreshing any pool.

Usage:

    python scripts/seed_modal_volumes.py                 # all four pools
    python scripts/seed_modal_volumes.py --pool photos   # one only
    python scripts/seed_modal_volumes.py --dry-run       # print commands

Requires: `modal` CLI on PATH and `modal token new` already run.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ASSETS = REPO_ROOT / "infra" / "assets"
VOLUME = "asset-pools"

# Friendly name (CLI arg) → on-disk subdir (also the volume's top-level dir).
POOLS: dict[str, str] = {
    "photos":  "photo-pool",
    "fonts":   "font-pool",
    "icons":   "icon-pool",
    "avatars": "avatar-pool",
}


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__.split("\n")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument(
        "--pool",
        choices=list(POOLS),
        default=None,
        help="seed one pool only (default: all four)",
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="print commands without executing",
    )
    args = ap.parse_args()

    if shutil.which("modal") is None:
        print(
            "error: `modal` CLI not found on PATH.\n"
            "  install: pip install modal\n"
            "  auth:    modal token new",
            file=sys.stderr,
        )
        return 2

    # Ensure the Volume exists. `create_if_missing=True` in volumes.py only
    # fires when a Modal function mounts the Volume — the CLI `modal volume
    # put` requires the Volume to already exist.
    create_cmd = ["modal", "volume", "create", VOLUME]
    print("→", " ".join(create_cmd), "(idempotent)")
    if not args.dry_run:
        proc = subprocess.run(create_cmd, capture_output=True, text=True)
        if proc.returncode != 0 and "already exists" not in (proc.stderr + proc.stdout).lower():
            print(proc.stdout, file=sys.stderr)
            print(proc.stderr, file=sys.stderr)
            return proc.returncode

    pools = [args.pool] if args.pool else list(POOLS)
    for name in pools:
        subdir = POOLS[name]
        local = ASSETS / subdir
        if not local.is_dir():
            print(
                f"error: {local} does not exist. "
                "Run the relevant fetcher first "
                "(infra/assets/{filter_unsplash,download,fetch_fonts,fetch_icons,fetch_avatars}.py).",
                file=sys.stderr,
            )
            return 2
        # `modal volume put <vol> <local-dir> <remote-dir>` uploads the directory.
        cmd = ["modal", "volume", "put", VOLUME, str(local), f"/{subdir}"]
        print("→", " ".join(cmd))
        if not args.dry_run:
            try:
                subprocess.run(cmd, check=True)
            except subprocess.CalledProcessError as e:
                print(f"error: `modal volume put` failed ({e.returncode})", file=sys.stderr)
                return e.returncode

    print(f"\nseeded {len(pools)} pool(s) into Modal volume '{VOLUME}'")
    return 0


if __name__ == "__main__":
    sys.exit(main())
