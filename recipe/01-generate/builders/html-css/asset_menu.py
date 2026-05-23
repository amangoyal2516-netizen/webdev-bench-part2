"""Build a compact asset menu string for the builder prompt.

Strategy (per plan):
- photos: one line per asset, fields id / ar / tags / desc / path
- fonts:  one line per (family, weight)
- avatars: one line per asset (style + seed)
- icons:  bare name list, comma-separated (1700+ names; full JSON adds no signal
          because Lucide names are self-describing)

The resulting menu block is cached by the runner across the iteration loop
via Anthropic prompt_caching's `cache_control` field on the message part.
"""

from __future__ import annotations

import functools
import json
import os
import pathlib
from typing import Any

REPO_ROOT = pathlib.Path(__file__).resolve().parents[4]  # webdev-bench/

# Where the four asset pools live on disk.
#   - Local default: <repo>/infra/assets/
#   - On Modal: set WEBDEV_BENCH_POOL_ROOT=/cache/assets in the function
#     decorator; the asset-pools Volume is seeded with the per-pool subdirs.
POOL_ROOT = pathlib.Path(
    os.environ.get("WEBDEV_BENCH_POOL_ROOT") or (REPO_ROOT / "infra/assets")
)
POOLS = {
    "photos":  POOL_ROOT / "photo-pool/manifest.json",
    "fonts":   POOL_ROOT / "font-pool/manifest.json",
    "icons":   POOL_ROOT / "icon-pool/manifest.json",
    "avatars": POOL_ROOT / "avatar-pool/manifest.json",
}


def asset_abs_path(rec: dict[str, Any]) -> pathlib.Path:
    """Resolve a manifest record's `path` field to an absolute on-disk path.

    The manifests as currently written store paths like
    `infra/assets/photo-pool/<id>.jpg` (relative to repo root). When run on
    Modal those manifests live under POOL_ROOT (which is the parent of the
    per-pool dirs), so we strip the legacy `infra/assets/` prefix if present
    and anchor at POOL_ROOT. Manifests authored with already-pool-relative
    paths (e.g. `photo-pool/<id>.jpg`) work unchanged.
    """
    p = pathlib.Path(rec["path"])
    parts = p.parts
    if len(parts) >= 2 and parts[0] == "infra" and parts[1] == "assets":
        p = pathlib.Path(*parts[2:])
    return POOL_ROOT / p


def _load(name: str) -> list[dict[str, Any]]:
    return json.loads(POOLS[name].read_text())


def _escape(s: str) -> str:
    """Minimal escape: replace quotes/newlines so menu lines stay single-line."""
    return s.replace('"', "'").replace("\n", " ").replace("\r", " ").strip()


def _photos_block(rows: list[dict[str, Any]]) -> str:
    lines = ["# PHOTOS (path is relative to repo root)"]
    for r in rows:
        tags = ",".join(r.get("tags") or [])
        lines.append(
            f'id={r["photo_id"]} ar={r.get("aspect_ratio"):.2f} '
            f'tags={tags} desc="{_escape(r.get("ai_description") or "")}" '
            f'path={r["path"]}'
        )
    return "\n".join(lines)


def _fonts_block(rows: list[dict[str, Any]]) -> str:
    lines = ["# FONTS (one line per weight; family + slug + category)"]
    for r in rows:
        lines.append(
            f'family="{r["family"]}" slug={r["slug"]} weight={r["weight"]} '
            f'category={r["category"]} path={r["path"]}'
        )
    return "\n".join(lines)


def _avatars_block(rows: list[dict[str, Any]]) -> str:
    lines = ["# AVATARS (DiceBear; deterministic per seed)"]
    for r in rows:
        lines.append(
            f'id={r["style"]}-{r["seed"]} style={r["style"]} seed={r["seed"]} '
            f'path={r["path"]}'
        )
    return "\n".join(lines)


def _icons_block(rows: list[dict[str, Any]]) -> str:
    """Bare comma-separated name list. Resolve at vendor time via the manifest."""
    names = sorted(r["name"] for r in rows)
    return (
        "# ICONS (closed vocabulary — exhaustive; reference by name; "
        "resolves to ./assets/icons/<name>.svg)\n"
        "# Pick ONLY from the names below. Names you might expect from a "
        "common icon library may NOT be here — do not assume any name "
        "exists unless you see it literally in the list.\n"
        + ", ".join(names)
    )


@functools.lru_cache(maxsize=1)
def asset_menu() -> str:
    """Assembled menu string, cached per process."""
    return "\n\n".join([
        _photos_block(_load("photos")),
        _fonts_block(_load("fonts")),
        _avatars_block(_load("avatars")),
        _icons_block(_load("icons")),
    ])


@functools.lru_cache(maxsize=1)
def _lookups() -> dict[str, dict[str, dict[str, Any]]]:
    """Index every pool by its primary id for fast resolution by the runner."""
    photos = {r["photo_id"]: r for r in _load("photos")}
    fonts: dict[str, dict[str, Any]] = {}
    for r in _load("fonts"):
        # key = family-slug:weight, e.g. "inter:400"
        fonts[f'{r["slug"]}:{r["weight"]}'] = r
    avatars = {f'{r["style"]}-{r["seed"]}': r for r in _load("avatars")}
    icons = {r["name"]: r for r in _load("icons")}
    return {"photos": photos, "fonts": fonts, "avatars": avatars, "icons": icons}


def resolve_photo(photo_id: str) -> dict[str, Any] | None:
    return _lookups()["photos"].get(photo_id)


def resolve_font(slug: str, weight: int) -> dict[str, Any] | None:
    return _lookups()["fonts"].get(f"{slug}:{weight}")


def resolve_avatar(avatar_id: str) -> dict[str, Any] | None:
    return _lookups()["avatars"].get(avatar_id)


def resolve_icon(name: str) -> dict[str, Any] | None:
    return _lookups()["icons"].get(name)


def font_slug(family: str) -> str:
    """Reverse-lookup a family name → its slug (e.g. 'Inter' → 'inter')."""
    for r in _load("fonts"):
        if r["family"] == family:
            return r["slug"]
    return family.lower().replace(" ", "-")
