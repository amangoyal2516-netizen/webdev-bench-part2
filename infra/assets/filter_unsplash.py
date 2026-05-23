#!/usr/bin/env python3
"""
Build a flat, tag-rich manifest of the ~500 most web-useful photos from
the Unsplash Lite catalog.

Reads  : infra/assets/_source/unsplash-lite/photos.csv*
Writes : infra/assets/_picks.json

Selection strategy:
- Drop photos with no ai_description, width < MIN_WIDTH, or aspect_ratio
  < MIN_ASPECT (portrait-only is rarely useful as a hero or background).
- Multi-label tag each surviving photo against the TAGS regex map; drop
  photos that carry no tags.
- For each tag in QUOTAS, walk eligible photos (most-popular first) and
  pick until that tag has at least `quota` representatives in the pool.
  Photos satisfying multiple quotas count once but contribute to every
  tag they carry — diversity falls out of the per-tag floor.
- If quotas under-fill POOL_CAP (sparse tags can't supply enough photos),
  top up with the most-popular remaining tagged photos. Overshoots are
  trimmed back to POOL_CAP, dropping the least-popular first.

The tag set is deliberately broad and overlapping — most photos hit
multiple tags. The recipe queries the manifest by `tags ⊇ {…}` to find
candidates for any given design (e.g., a SaaS hero might ask for
`{workspace, tech, bright}`).
"""

import csv
import json
import pathlib
import re
import sys
from collections import Counter

HERE = pathlib.Path(__file__).parent
SOURCE_DIR = HERE / "_source" / "unsplash-lite"
PICKS_PATH = HERE / "_picks.json"

MIN_WIDTH = 1600
MIN_ASPECT = 1.0

# Multi-label tag rules. A photo can carry multiple tags. Tag values are
# lowercase. Used both for filtering at pick time and for searchability by
# the recipe at task-generation time.
TAGS = {
    "people":       [r"\bperson\b", r"\bman\b", r"\bwoman\b", r"\bboy\b", r"\bgirl\b", r"\bpeople\b", r"\bhuman\b"],
    "portrait":     [r"\bportrait\b", r"\bheadshot\b", r"\bface\b", r"\bselfie\b"],
    "lifestyle":    [r"holding", r"wearing", r"sitting", r"standing", r"walking", r"smiling"],
    "people-group": [r"\bcrowd\b", r"\baudience\b", r"\bmeeting\b", r"\bgroup\b", r"\bteam\b"],
    "workspace":    [r"\bdesk\b", r"\blaptop\b", r"\bcomputer\b", r"\boffice\b", r"\bkeyboard\b", r"\bnotebook\b", r"\bmacbook\b", r"\bmonitor\b", r"working", r"\bwriting\b", r"workstation", r"\bstudy\b"],
    "tech":         [r"\blaptop\b", r"\bphone\b", r"smartphone", r"\bscreen\b", r"\bdevice\b", r"\bcomputer\b", r"keyboard", r"\biphone\b", r"\bipad\b", r"\btablet\b", r"\bmacbook\b", r"\bmonitor\b", r"headphones?", r"earbuds?", r"\bcamera\b"],
    "food":         [r"\bfood\b", r"\bmeal\b", r"\bdish\b", r"\bcoffee\b", r"\btea\b", r"\bcake\b", r"\bbread\b", r"\bfruit\b", r"vegetable", r"\bplate\b", r"\bbowl\b", r"\bdrink\b", r"\bjuice\b", r"breakfast", r"lunch", r"dinner", r"salad", r"\bpizza\b", r"\bpasta\b", r"\bwine\b", r"\bcup\b", r"\bmug\b", r"sandwich", r"dessert", r"snack"],
    "kitchen":      [r"\bkitchen\b", r"cooking", r"\bchef\b", r"\bbaker", r"\boven\b", r"\bstove\b", r"\bpan\b", r"\bpot\b", r"cutting board", r"\bknife\b", r"preparing", r"chopping"],
    "interior":     [r"\broom\b", r"\bbedroom\b", r"\bliving room\b", r"\bsofa\b", r"\bcouch\b", r"\bchair\b", r"\btable\b", r"\binterior\b"],
    "architecture": [r"\bbuilding\b", r"architecture", r"\bbridge\b", r"\bcity\b", r"\bskyline\b", r"\bstreet\b", r"urban", r"\bfacade\b"],
    "nature":       [r"\bnature\b", r"\bforest\b", r"\btree\b", r"\bmountain\b", r"\bocean\b", r"\bsea\b", r"\bbeach\b", r"\bsky\b", r"\bfield\b", r"\bgrass\b", r"\bflower\b", r"\bleaf\b", r"\blake\b"],
    "travel":       [r"\bbeach\b", r"\bmountain\b", r"\bisland\b", r"\bdesert\b", r"\broad\b", r"travel"],
    "product":      [r"flat lay", r"\bobjects?\b", r"\bbottle\b", r"\bshoe", r"sneaker", r"\bbag\b", r"\bwatch\b", r"\bcamera\b", r"\bbook\b", r"\bphone\b", r"sunglasses", r"\bvase\b", r"\blamp\b", r"\bperfume\b"],
    "transport":    [r"\bcar\b", r"\bbike\b", r"\bbicycle\b", r"\btrain\b", r"\bplane\b", r"\bboat\b", r"\bship\b", r"\bmotorcycle\b"],
    "cinematic":    [r"cinematic", r"dramatic", r"moody", r"silhouette", r"\bnight\b", r"\bneon\b", r"backlit", r"\bstorm\b", r"\bfog\b"],
    "abstract":     [r"\bpattern\b", r"\btexture\b", r"\babstract\b", r"\bbackground\b", r"\bgradient\b", r"geometric", r"\bwall\b"],
    "bright":       [r"\bwhite\b", r"\bbright\b", r"\blight\b", r"minimal", r"\bclean\b"],
    "dark":         [r"\bdark\b", r"\bblack\b", r"\bshadow\b", r"\bnight\b"],
}

# Minimum representation per tag in the final pool. The pick() loop walks
# each quota in order, ensuring at least N photos carrying that tag end
# up in the pool — popular photos that satisfy multiple quotas count once
# but pay for all the tags they carry. `bright` / `dark` are aesthetic
# modifiers and not quota'd (they ride along for searchability).
QUOTAS = {
    # Rare tags first so they don't get crowded out.
    "kitchen":      20,
    "tech":         30,
    "people-group": 20,
    "product":      40,
    "abstract":     25,
    "transport":    25,
    "cinematic":    30,
    "interior":     40,
    "food":         50,
    "workspace":    40,
    "portrait":     40,
    "travel":       40,
    "architecture": 60,
    "lifestyle":    60,
    "nature":       80,
}
POOL_CAP = 500


def _iter_photos():
    if not SOURCE_DIR.exists():
        raise SystemExit(
            f"Unsplash Lite not found at {SOURCE_DIR}.\n"
            f"Get it: curl -sL -o /tmp/lite.zip "
            f"https://unsplash-datasets.s3.amazonaws.com/lite/latest/unsplash-research-dataset-lite-latest.zip"
            f" && unzip /tmp/lite.zip -d {SOURCE_DIR}"
        )
    paths = sorted(list(SOURCE_DIR.glob("photos.csv*")) + list(SOURCE_DIR.glob("photos.tsv*")))
    if not paths:
        raise SystemExit(f"No photos.csv* / photos.tsv* found under {SOURCE_DIR}")
    for path in paths:
        with path.open(newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh, delimiter="\t"):
                yield row


def _as_int(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


def _as_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _tag_photo(text):
    tags = []
    for name, patterns in TAGS.items():
        if any(re.search(p, text) for p in patterns):
            tags.append(name)
    return tags


def pick():
    eligible = []
    for row in _iter_photos():
        desc = (row.get("ai_description") or "").strip().lower()
        if not desc:
            continue
        width = _as_int(row.get("photo_width"))
        if width < MIN_WIDTH:
            continue
        aspect = _as_float(row.get("photo_aspect_ratio"))
        if aspect < MIN_ASPECT:
            continue
        tags = _tag_photo(desc)
        if not tags:
            continue
        eligible.append({
            "photo_id": row.get("photo_id"),
            "photo_image_url": row.get("photo_image_url") or "",
            "ai_description": desc,
            "photographer": row.get("photographer_username") or "",
            "width": width,
            "height": _as_int(row.get("photo_height")),
            "aspect_ratio": round(aspect, 3),
            "downloads": _as_int(row.get("stats_downloads")),
            "tags": tags,
        })

    # Most-popular first — popularity is the proxy for "general web-usable".
    eligible.sort(key=lambda r: r["downloads"], reverse=True)

    selected = {}  # photo_id → entry; dict preserves insertion order
    for tag, quota in QUOTAS.items():
        have = sum(1 for e in selected.values() if tag in e["tags"])
        if have >= quota:
            continue
        need = quota - have
        for entry in eligible:
            if entry["photo_id"] in selected:
                continue
            if tag not in entry["tags"]:
                continue
            selected[entry["photo_id"]] = entry
            need -= 1
            if need <= 0:
                break

    # If quotas under-fill the cap (sparse tags can't supply enough photos),
    # top up with the most-popular remaining photos. They'll be already
    # tag-classified, so the recipe still has tags to search on.
    if len(selected) < POOL_CAP:
        for entry in eligible:
            if entry["photo_id"] not in selected:
                selected[entry["photo_id"]] = entry
                if len(selected) >= POOL_CAP:
                    break

    # If we overshot, drop the least-popular entries until at cap.
    picked = sorted(selected.values(), key=lambda r: -r["downloads"])[:POOL_CAP]

    tag_counts = Counter()
    for e in picked:
        tag_counts.update(e["tags"])
    return picked, tag_counts


def main():
    picks, tag_counts = pick()
    PICKS_PATH.write_text(json.dumps(picks, indent=2) + "\n")
    print(f"Wrote {PICKS_PATH}  ({len(picks)} photos)", file=sys.stderr)
    print("Tag distribution (count / quota):", file=sys.stderr)
    for tag in sorted(tag_counts, key=lambda t: -tag_counts[t]):
        q = QUOTAS.get(tag)
        q_str = f"{q}" if q is not None else "—"
        print(f"  {tag:14s} {tag_counts[tag]:4d} / {q_str}", file=sys.stderr)


if __name__ == "__main__":
    main()
