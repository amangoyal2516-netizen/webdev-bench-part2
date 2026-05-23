#!/usr/bin/env python3
"""
Generate a deterministic DiceBear avatar pool.

Reads  : STYLES + SEEDS below.
Writes : infra/assets/avatar-pool/<style>-<seed>.svg
         infra/assets/avatar-pool/manifest.json

Mechanism:
- For each (style, seed) pair, hit DiceBear's keyless public endpoint
  https://api.dicebear.com/9.x/<style>/svg?seed=<seed>.
- DiceBear is deterministic: same seed ⇒ same SVG bytes, so the pool
  is fully reproducible.

Stdlib only — parallel via ThreadPoolExecutor.
"""

import json
import pathlib
import sys
import urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.request import Request, urlopen

HERE = pathlib.Path(__file__).parent
POOL_DIR = HERE / "avatar-pool"
MANIFEST_PATH = POOL_DIR / "manifest.json"
REPO_ROOT = HERE.parent.parent

# Generic human-ish avatar styles that fit most chat / profile slots.
STYLES = ["avataaars", "notionists", "lorelei", "shapes"]
SEED_COUNT = 20
SEEDS = [f"seed-{i:02d}" for i in range(SEED_COUNT)]

CONCURRENCY = 8
TIMEOUT_SEC = 30
USER_AGENT = "webdev-bench/0.1"


def _url(style, seed):
    return f"https://api.dicebear.com/9.x/{style}/svg?seed={seed}"


def _download(url, out_path):
    if out_path.exists() and out_path.stat().st_size > 0:
        return "cached"
    req = Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urlopen(req, timeout=TIMEOUT_SEC) as r:
            if r.status != 200:
                return f"http_{r.status}"
            data = r.read()
    except (urllib.error.URLError, TimeoutError) as e:
        return f"err:{e}"
    out_path.write_bytes(data)
    return "ok"


def main():
    POOL_DIR.mkdir(parents=True, exist_ok=True)
    jobs = []
    for style in STYLES:
        for seed in SEEDS:
            out_path = POOL_DIR / f"{style}-{seed}.svg"
            jobs.append((style, seed, _url(style, seed), out_path))

    print(f"Fetching {len(jobs)} avatars…", file=sys.stderr)
    statuses = {}
    with ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
        futures = {pool.submit(_download, url, out): (style, seed, out)
                   for style, seed, url, out in jobs}
        for fut in as_completed(futures):
            statuses[futures[fut]] = fut.result()

    manifest = []
    fail = 0
    for style, seed, _, out in jobs:
        status = statuses[(style, seed, out)]
        if status in ("ok", "cached"):
            manifest.append({
                "style": style,
                "seed": seed,
                "path": str(out.relative_to(REPO_ROOT)),
                "source": "DiceBear",
                "license": "MIT",
                "license_url": "https://github.com/dicebear/dicebear/blob/main/LICENSE",
            })
        else:
            fail += 1
            print(f"FAIL {style}/{seed}: {status}", file=sys.stderr)

    manifest.sort(key=lambda r: (r["style"], r["seed"]))
    MANIFEST_PATH.write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"Wrote {MANIFEST_PATH}  ({len(manifest)} avatars; {fail} failures)", file=sys.stderr)


if __name__ == "__main__":
    main()
