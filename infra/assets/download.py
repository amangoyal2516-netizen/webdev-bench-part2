#!/usr/bin/env python3
"""
Download the chosen Unsplash photos from infra/assets/_picks.json into a
flat pool under infra/assets/photo-pool/. Writes a JSON manifest beside
the photos.

Reads  : infra/assets/_picks.json
Writes : infra/assets/photo-pool/<photo_id>.jpg
         infra/assets/photo-pool/manifest.json

Stdlib only — parallel via ThreadPoolExecutor. Idempotent: existing
files are kept; the manifest is rewritten every run from on-disk truth.
"""

import json
import pathlib
import sys
import urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.request import Request, urlopen

HERE = pathlib.Path(__file__).parent
PICKS_PATH = HERE / "_picks.json"
POOL_DIR = HERE / "photo-pool"
MANIFEST_PATH = POOL_DIR / "manifest.json"
REPO_ROOT = HERE.parent.parent

CONCURRENCY = 16
TIMEOUT_SEC = 60
# Cap width at 1600 px — large enough for hero use, small enough to keep
# the pool under ~200 MB. The filter pre-screens for original_width ≥ 1600
# so every downloaded file is exactly 1600 px wide.
RESIZE_WIDTH = 1600
RESIZE_PARAMS = f"?w={RESIZE_WIDTH}&fit=max&fm=jpg&q=80"
USER_AGENT = "webdev-bench/0.1"


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
    if not PICKS_PATH.exists():
        raise SystemExit(f"{PICKS_PATH} not found. Run filter_unsplash.py first.")
    picks = json.loads(PICKS_PATH.read_text())
    POOL_DIR.mkdir(parents=True, exist_ok=True)

    jobs = []
    for entry in picks:
        url = entry.get("photo_image_url") or ""
        if not url:
            continue
        out_path = POOL_DIR / f"{entry['photo_id']}.jpg"
        jobs.append((entry, url + RESIZE_PARAMS, out_path))

    print(f"Dispatching {len(jobs)} downloads ({CONCURRENCY}-way parallel)…", file=sys.stderr)
    statuses = {}
    with ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
        futures = {pool.submit(_download, url, out): entry for entry, url, out in jobs}
        for n, fut in enumerate(as_completed(futures), 1):
            statuses[futures[fut]["photo_id"]] = fut.result()
            if n % 50 == 0:
                print(f"  {n}/{len(jobs)}", file=sys.stderr)

    manifest = []
    fail_count = 0
    for entry in picks:
        status = statuses.get(entry["photo_id"], "missing")
        if status in ("ok", "cached"):
            on_disk = POOL_DIR / f"{entry['photo_id']}.jpg"
            aspect = entry.get("aspect_ratio") or 1.0
            # All downloads resized to w=RESIZE_WIDTH; height follows aspect.
            on_disk_width = RESIZE_WIDTH
            on_disk_height = int(round(RESIZE_WIDTH / aspect)) if aspect else None
            manifest.append({
                "photo_id": entry["photo_id"],
                "path": str(on_disk.relative_to(REPO_ROOT)),
                "source": "Unsplash Lite",
                "license": "Unsplash License",
                "photographer": entry.get("photographer") or "",
                "ai_description": entry.get("ai_description") or "",
                "tags": entry.get("tags") or [],
                "width": on_disk_width,
                "height": on_disk_height,
                "aspect_ratio": aspect,
                "downloads": entry.get("downloads"),
            })
        else:
            fail_count += 1
            print(f"FAIL {entry['photo_id']}: {status}", file=sys.stderr)

    manifest.sort(key=lambda r: r["photo_id"])
    MANIFEST_PATH.write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"Wrote {MANIFEST_PATH}  ({len(manifest)} photos; {fail_count} failures)", file=sys.stderr)


if __name__ == "__main__":
    main()
