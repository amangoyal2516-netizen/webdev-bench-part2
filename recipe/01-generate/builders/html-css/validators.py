"""Post-LLM validation gates for the html-css builder.

Returns a list of human-readable error strings (empty list = success). Errors
are fed back into the correction prompt verbatim, so phrase them so the LLM
can act on them without further context.
"""

from __future__ import annotations

import pathlib
import re
from typing import Any

import lxml.html

# Reference extractors --------------------------------------------------------

_HTML_REFS = ("src", "href", "srcset", "data-src", "poster")
_CSS_URL = re.compile(r"url\(\s*['\"]?([^'\")\s]+)['\"]?\s*\)")


def _refs_in_html(tree: lxml.html.HtmlElement) -> list[str]:
    out: list[str] = []
    for el in tree.iter():
        for attr in _HTML_REFS:
            v = el.get(attr)
            if v:
                out.append(v.strip())
    return out


def _refs_in_css(css: str) -> list[str]:
    return [m.group(1).strip() for m in _CSS_URL.finditer(css)]


def _is_local(ref: str) -> bool:
    """A ref counts as local (must resolve on disk) if it's not http(s):, mailto:, etc."""
    if not ref:
        return False
    if ref.startswith(("http://", "https://", "//", "mailto:", "tel:", "data:", "#")):
        return False
    return True


# Main validator --------------------------------------------------------------


def validate(
    task_dir: pathlib.Path,
    design: dict[str, Any],
    declared_assets: dict[str, Any] | None = None,
) -> list[str]:
    """Run every gate. Returns [] when everything passes."""
    errors: list[str] = []
    source = task_dir / "source"
    assets_root = task_dir / "assets"

    if not source.is_dir():
        return [f"missing-dir: {source} does not exist"]

    # Assets live inside source/ so the HTML's `./assets/...` relative
    # paths resolve (browser + Playwright both treat the .html dir as
    # base). The on-disk asset tree therefore sits at source/assets/.
    assets_root = source / "assets"

    expected_html = {p["name"] + ".html" for p in design.get("pages", [])}
    expected_top = expected_html | {"styles.css", "assets"}
    actual_top = {p.name for p in source.iterdir()}

    for m in sorted(expected_top - actual_top):
        errors.append(f"missing-file: source/{m}")
    for x in sorted(actual_top - expected_top):
        errors.append(f"unexpected-file: source/{x} (only <page>.html, styles.css, assets/ allowed)")

    referenced_paths: set[str] = set()

    for html_name in sorted(expected_html & actual_top):
        html_path = source / html_name
        raw = html_path.read_text()
        if html_path.stat().st_size < 1500:
            errors.append(f"too-small: source/{html_name} is {html_path.stat().st_size} bytes (< 1500)")
        try:
            tree = lxml.html.fromstring(raw)
        except Exception as e:
            errors.append(f"parse-error: source/{html_name}: {type(e).__name__}: {e}")
            continue
        body = tree.find(".//body")
        if body is None or len(body) == 0:
            errors.append(f"empty-body: source/{html_name} has no body children")

        for ref in _refs_in_html(tree):
            if not _is_local(ref):
                continue
            for piece in (ref.split() if " " in ref else [ref]):
                # srcset may have "url 1x, url 2x" — split on whitespace + take url
                target = piece.split(",")[0].strip()
                if not target:
                    continue
                # Strip URL fragment (#id) and query string (?…) — they're
                # browser-side concerns, not part of the on-disk filename.
                file_part = target.split("#", 1)[0].split("?", 1)[0]
                if not file_part:
                    continue
                referenced_paths.add(target)
                resolved = (html_path.parent / file_part).resolve()
                if not resolved.is_file():
                    errors.append(f"dead-ref: source/{html_name}: {target} (resolves to {resolved}, missing)")

    # styles.css gate
    css_path = source / "styles.css"
    if css_path.is_file():
        css = css_path.read_text()
        for url in _refs_in_css(css):
            if not _is_local(url):
                continue
            file_part = url.split("#", 1)[0].split("?", 1)[0]
            if not file_part:
                continue
            referenced_paths.add(url)
            resolved = (css_path.parent / file_part).resolve()
            if not resolved.is_file():
                errors.append(f"dead-ref: source/styles.css: url({url}) (resolves to {resolved}, missing)")

    # External / CDN sniff — soft warning becomes hard error since the agent
    # container has no network.
    if any("//fonts.googleapis.com" in r or "//cdn" in r for r in referenced_paths if r):
        errors.append("external-ref: detected CDN / fonts.googleapis.com URL — only vendored assets allowed")

    # declared vs actually-used cross-check
    if declared_assets is not None:
        decl_photo_ids = set(declared_assets.get("photos") or [])
        decl_icon_names = set(declared_assets.get("icons") or [])
        decl_avatar_ids = set(declared_assets.get("avatars") or [])

        # Walk what landed under source/assets/ — only those should have been declared.
        for kind, declared in (("photos", decl_photo_ids),
                                ("icons", decl_icon_names),
                                ("avatars", decl_avatar_ids)):
            on_disk_dir = assets_root / kind
            if not on_disk_dir.is_dir():
                if declared:
                    errors.append(f"missing-asset-dir: source/assets/{kind}/ does not exist but {len(declared)} {kind} declared")
                continue
            on_disk_ids = {p.stem for p in on_disk_dir.iterdir() if p.is_file()}
            for extra in sorted(declared - on_disk_ids):
                errors.append(f"declared-unused: {kind}:{extra} declared but not on disk under source/assets/{kind}/")

    return errors
