#!/usr/bin/env python3
"""
Vendor the Lucide icon pack as a flat SVG pool.

Reads  : nothing (clones Lucide from GitHub if not already cached).
Writes : infra/assets/icon-pool/<icon-name>.svg
         infra/assets/icon-pool/manifest.json
         infra/assets/icon-pool/LICENSE              (ISC text)

Mechanism:
- `git clone --depth 1 https://github.com/lucide-icons/lucide` into
  infra/assets/_source/lucide (skipped if already present).
- Copy every .svg from `icons/` into infra/assets/icon-pool/.
- Read each icon's sibling .json (`icons/<name>.json`) for its `categories`
  field and use that as the icon's tag list — gives the recipe a search
  key (e.g., icons tagged "media" → tags=["media"]).

Stdlib only (uses `git` via subprocess).
"""

import json
import pathlib
import shutil
import subprocess
import sys

HERE = pathlib.Path(__file__).parent
SOURCE_DIR = HERE / "_source" / "lucide"
POOL_DIR = HERE / "icon-pool"
MANIFEST_PATH = POOL_DIR / "manifest.json"
LICENSE_PATH = POOL_DIR / "LICENSE"
REPO_ROOT = HERE.parent.parent

CLONE_URL = "https://github.com/lucide-icons/lucide"

# Lucide has renamed several icons upstream over time. LLMs trained on older
# Lucide versions reach for the legacy names (`home`, `filter`, `edit`, ...).
# We add each legacy name as an alias SVG so the pool resolves both. Map is
# legacy_name → real_name_in_current_lucide.
ALIASES = {
    "home":             "house",
    "filter":           "list-filter",
    "edit":             "pencil",
    "more-horizontal":  "ellipsis",
    "more-vertical":    "ellipsis-vertical",
    "sort":             "arrow-up-down",
    "sort-asc":         "arrow-up-narrow-wide",
    "sort-desc":        "arrow-down-wide-narrow",
    "gear":             "cog",
    "tool":             "wrench",
    "tools":            "wrench",
    "comment":          "message-square",
    "sign-in":          "log-in",
    "sign-out":         "log-out",
    "cart":             "shopping-cart",
    "align-left":       "align-horizontal-justify-start",
    "align-right":      "align-horizontal-justify-end",
    "align-center":     "align-horizontal-justify-center",
    "align-justify":    "align-horizontal-justify-center",
}


def _ensure_clone():
    if (SOURCE_DIR / "icons").exists():
        return
    SOURCE_DIR.parent.mkdir(parents=True, exist_ok=True)
    print(f"Cloning Lucide into {SOURCE_DIR} (depth=1)…", file=sys.stderr)
    subprocess.run(
        ["git", "clone", "--depth", "1", CLONE_URL, str(SOURCE_DIR)],
        check=True,
    )


def _load_tags():
    """Map icon-name → list of category tags from Lucide's categories.json."""
    # Lucide's per-icon metadata is one JSON per icon under icons/.
    # The repo also ships `categories.json` listing icons per category at root
    # (older layout) OR under `categories/`. Try both, prefer per-icon JSON
    # since that's stable.
    tags = {}
    icons_dir = SOURCE_DIR / "icons"
    for json_path in icons_dir.glob("*.json"):
        try:
            meta = json.loads(json_path.read_text())
        except json.JSONDecodeError:
            continue
        cats = meta.get("categories") or []
        if cats:
            tags[json_path.stem] = list(cats)
    return tags


def main():
    _ensure_clone()
    POOL_DIR.mkdir(parents=True, exist_ok=True)

    # License text — ISC from the upstream LICENSE file if present.
    upstream_license = SOURCE_DIR / "LICENSE"
    if upstream_license.exists():
        shutil.copy(upstream_license, LICENSE_PATH)

    icons_dir = SOURCE_DIR / "icons"
    svgs = sorted(icons_dir.glob("*.svg"))
    print(f"Copying {len(svgs)} icons…", file=sys.stderr)

    for svg in svgs:
        shutil.copy(svg, POOL_DIR / svg.name)

    tags_by_name = _load_tags()
    real_names = {svg.stem for svg in svgs}
    manifest = []
    for svg in svgs:
        name = svg.stem
        manifest.append({
            "name": name,
            "path": str((POOL_DIR / svg.name).relative_to(REPO_ROOT)),
            "source": "Lucide",
            "license": "ISC",
            "license_file": str(LICENSE_PATH.relative_to(REPO_ROOT)) if LICENSE_PATH.exists() else None,
            "tags": tags_by_name.get(name, []),
        })

    # Add legacy-name aliases. Each is a copy of its target SVG written under
    # the legacy filename; the manifest entry records the alias so the pool
    # menu surfaces both names.
    aliases_added = 0
    for legacy, target in ALIASES.items():
        if legacy in real_names:
            continue  # Lucide actually has it — no alias needed.
        if target not in real_names:
            print(f"  skip alias {legacy}→{target}: target not in pool", file=sys.stderr)
            continue
        src_svg = POOL_DIR / f"{target}.svg"
        dst_svg = POOL_DIR / f"{legacy}.svg"
        shutil.copy(src_svg, dst_svg)
        manifest.append({
            "name": legacy,
            "path": str(dst_svg.relative_to(REPO_ROOT)),
            "source": "Lucide",
            "license": "ISC",
            "license_file": str(LICENSE_PATH.relative_to(REPO_ROOT)) if LICENSE_PATH.exists() else None,
            "tags": tags_by_name.get(target, []),
            "alias_of": target,
        })
        aliases_added += 1
    print(f"Added {aliases_added} legacy-name aliases", file=sys.stderr)

    manifest.sort(key=lambda r: r["name"])
    MANIFEST_PATH.write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"Wrote {MANIFEST_PATH}  ({len(manifest)} icons)", file=sys.stderr)


if __name__ == "__main__":
    main()
