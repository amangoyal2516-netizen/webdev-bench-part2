#!/usr/bin/env python3
"""
Vendor a small Google Fonts pool as .woff2 files.

Reads  : FAMILIES dict below (canonical font selection).
Writes : infra/assets/font-pool/<family-slug>/<family-slug>-<weight>.woff2
         infra/assets/font-pool/manifest.json
         infra/assets/font-pool/OFL.txt          (license text)

Mechanism:
- For each family, request the Google Fonts CSS2 endpoint with a modern
  Chrome User-Agent so the API returns .woff2 URLs (without that UA, it
  returns TTF).
- Parse out (weight, .woff2 URL) pairs from the CSS.
- Download each .woff2 into the family's directory.

Stdlib only.
"""

import json
import pathlib
import re
import sys
import urllib.error
import urllib.parse
from urllib.request import Request, urlopen

HERE = pathlib.Path(__file__).parent
POOL_DIR = HERE / "font-pool"
MANIFEST_PATH = POOL_DIR / "manifest.json"
OFL_PATH = POOL_DIR / "OFL.txt"
REPO_ROOT = HERE.parent.parent

CHROME_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)

# (family, category, weights). Category is informational for the recipe's
# search ("give me a serif body font") — not used for fetching.
FAMILIES = [
    ("Inter",               "sans-serif",  [400, 500, 700]),
    ("Manrope",             "sans-serif",  [400, 600]),
    ("DM Sans",             "sans-serif",  [400, 500, 700]),
    ("Plus Jakarta Sans",   "sans-serif",  [400, 700]),
    ("Space Grotesk",       "sans-serif",  [400, 700]),
    ("Playfair Display",    "serif",       [400, 700]),
    ("Source Serif 4",      "serif",       [400, 700]),
    ("Lora",                "serif",       [400, 700]),
    ("Crimson Text",        "serif",       [400, 700]),
    ("JetBrains Mono",      "monospace",   [400, 700]),
    ("IBM Plex Mono",       "monospace",   [400, 700]),
    ("Bebas Neue",          "display",     [400]),
]


def _slug(family):
    return family.lower().replace(" ", "-")


def _fetch_css(family, weights):
    name = urllib.parse.quote_plus(family)
    weight_spec = ";".join(str(w) for w in weights)
    url = f"https://fonts.googleapis.com/css2?family={name}:wght@{weight_spec}&display=swap"
    req = Request(url, headers={"User-Agent": CHROME_UA})
    with urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8")


# Each @font-face block in css2 looks like:
#   @font-face { ... font-weight: 400; ... src: url(<URL>) format('woff2'); }
FACE_RE = re.compile(r"@font-face\s*{[^}]*?}", re.DOTALL)
WEIGHT_RE = re.compile(r"font-weight:\s*(\d+)")
SRC_RE = re.compile(r"src:\s*url\((https://fonts\.gstatic\.com/[^)]+)\)\s*format\('woff2'\)")


def _parse_css(css):
    # Google Fonts returns one @font-face per (weight, unicode-subset). We
    # only want one file per weight — the latin subset, which is the last
    # block emitted in the CSS for each weight. Dedupe by weight, keeping
    # the latest URL (= latin subset).
    by_weight = {}
    for block in FACE_RE.findall(css):
        m_w = WEIGHT_RE.search(block)
        m_s = SRC_RE.search(block)
        if not (m_w and m_s):
            continue
        by_weight[int(m_w.group(1))] = m_s.group(1)
    return sorted(by_weight.items())


def _download(url, out_path):
    if out_path.exists() and out_path.stat().st_size > 0:
        return "cached"
    req = Request(url, headers={"User-Agent": CHROME_UA})
    try:
        with urlopen(req, timeout=30) as r:
            data = r.read()
    except (urllib.error.URLError, TimeoutError) as e:
        return f"err:{e}"
    out_path.write_bytes(data)
    return "ok"


# SIL OFL 1.1 — vendored as the shared license file for the pool. Every
# family in FAMILIES is OFL-licensed.
OFL_TEXT = """\
SIL OPEN FONT LICENSE Version 1.1 - 26 February 2007

PREAMBLE
The goals of the Open Font License (OFL) are to stimulate worldwide
development of collaborative font projects, to support the font creation
efforts of academic and linguistic communities, and to provide a free and
open framework in which fonts may be shared and improved in partnership
with others.

The OFL allows the licensed fonts to be used, studied, modified and
redistributed freely as long as they are not sold by themselves. The
fonts, including any derivative works, can be bundled, embedded,
redistributed and/or sold with any software provided that any reserved
names are not used by derivative works. The fonts and derivatives,
however, cannot be released under any other type of license. The
requirement for fonts to remain under this license does not apply to any
document created using the fonts or their derivatives.

Full license text: https://scripts.sil.org/OFL
"""


def main():
    POOL_DIR.mkdir(parents=True, exist_ok=True)
    OFL_PATH.write_text(OFL_TEXT)

    manifest = []
    for family, category, weights in FAMILIES:
        slug = _slug(family)
        family_dir = POOL_DIR / slug
        family_dir.mkdir(exist_ok=True)
        print(f"  {family}  ({category}, weights={weights})", file=sys.stderr)
        try:
            css = _fetch_css(family, weights)
        except urllib.error.URLError as e:
            print(f"    CSS fetch failed: {e}", file=sys.stderr)
            continue
        pairs = _parse_css(css)
        if not pairs:
            print(f"    no @font-face matches parsed", file=sys.stderr)
            continue
        for weight, url in pairs:
            out_path = family_dir / f"{slug}-{weight}.woff2"
            status = _download(url, out_path)
            if status not in ("ok", "cached"):
                print(f"    weight {weight}: {status}", file=sys.stderr)
                continue
            manifest.append({
                "family": family,
                "slug": slug,
                "weight": weight,
                "style": "normal",
                "category": category,
                "path": str(out_path.relative_to(REPO_ROOT)),
                "source": "Google Fonts",
                "license": "SIL Open Font License 1.1",
                "license_file": str(OFL_PATH.relative_to(REPO_ROOT)),
            })

    manifest.sort(key=lambda r: (r["category"], r["family"], r["weight"]))
    MANIFEST_PATH.write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"Wrote {MANIFEST_PATH}  ({len(manifest)} font files)", file=sys.stderr)


if __name__ == "__main__":
    main()
