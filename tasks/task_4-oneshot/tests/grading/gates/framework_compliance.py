"""framework_compliance — multiplicative gate (NOT a weighted criterion).

Composition: `reward = weighted_mean(7 sub-scores) × framework_gate`,
where `framework_gate ∈ {1.0, 0.3}`. Gate trips to 0.3 if the agent's
output violates the framework constraint declared in
`task_config.json.allowed_frameworks`.

For `html-css`: no JS framework imports, no build configs, no
`node_modules/`. Detects:

- Forbidden file extensions (`.jsx`, `.tsx`, `.vue`, `.svelte`).
- Forbidden build / config filenames (`package.json`, `vite.config.*`,
  `next.config.*`, `tsconfig.json`, …).
- `node_modules/` anywhere in the tree.
- JS framework references — three syntactic forms, NOT loose string match
  (because several forbidden names — `next`, `vue`, `lit`, `astro`,
  `remix` — are also common English words and would otherwise false-trip
  on body copy):
    * ES module imports: `import X from 'react'`, `import 'react/foo'`,
      `import('react')`.
    * CommonJS: `require('react')`.
    * HTML script src: `<script src="https://unpkg.com/react@18/…">`,
      `<script src="https://cdn.jsdelivr.net/npm/vue@3/…">`, etc.

Returns 1.0 (compliant) or 0.3 (violation). The list of detected
violations is included in the return dict for surfacing in `reward.json`.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any


_HTML_CSS_FORBIDDEN_EXTS: set[str] = {".jsx", ".tsx", ".vue", ".svelte"}

_HTML_CSS_FORBIDDEN_FILENAMES: set[str] = {
    "package.json", "package-lock.json", "yarn.lock", "pnpm-lock.yaml",
    "tsconfig.json", "vite.config.js", "vite.config.ts",
    "next.config.js", "next.config.mjs", "astro.config.mjs",
    "rollup.config.js", "webpack.config.js",
}

_HTML_CSS_FORBIDDEN_IMPORTS: frozenset[str] = frozenset({
    "react", "react-dom", "vue", "solid-js", "svelte", "preact",
    "lit", "lit-element", "lit-html", "alpinejs", "htmx.org",
    "next", "astro", "remix",
    # Part 2 — JS animation libraries. The "html-css" framework requires
    # animations to be implemented in pure CSS (@keyframes + transitions +
    # animation-timeline: view()). These libraries would let the agent
    # produce motion without doing the CSS work and would let `motion`-
    # style hover/scroll behaviour cheat the animation_fidelity grader.
    # `motion` shares its package name with an English word but the gate's
    # detection is syntactic (import/require/script-src only), so body
    # prose can't false-trip it — same logic as `next`/`vue`/`lit`.
    "gsap", "framer-motion", "motion", "popmotion", "animejs", "lottie-web",
})

# Pattern A: `import X from 'mod'` and `import 'mod'` (with optional
# default/namespace/braced bindings before `from`).
_IMPORT_RE = re.compile(
    r"""\bimport\b
        (?:\s+(?:[\w*${},\s]+?)\s+from)?     # optional bindings + from
        \s*['"]([^'"\s]+)['"]""",
    re.VERBOSE,
)

# Pattern B: dynamic `import('mod')`.
_DYNAMIC_IMPORT_RE = re.compile(r"""\bimport\s*\(\s*['"]([^'"\s]+)['"]""")

# Pattern C: CommonJS `require('mod')`.
_REQUIRE_RE = re.compile(r"""\brequire\s*\(\s*['"]([^'"\s]+)['"]""")

# Pattern D: HTML `<script src="…">` (any quote style).
_SCRIPT_SRC_RE = re.compile(
    r"""<script\b[^>]*?\bsrc\s*=\s*['"]([^'"\s]+)['"]""",
    re.IGNORECASE,
)

PASS_SCORE = 1.0
FAIL_SCORE = 0.3


def _match_forbidden(spec: str) -> str | None:
    """Given a module specifier (bare ID or URL), return the matched
    forbidden package name, or None.

    - Bare IDs like `react`, `react/jsx-runtime`, `@scope/pkg/sub` → the
      package name is the first segment (or `@scope/pkg` for scoped).
    - URLs are scanned by path component, with `@version` suffixes
      stripped, so `https://unpkg.com/react@18/umd/...` matches `react`.
    - Relative paths (`./foo`, `../bar`, `/abs`) and `data:` / `blob:` /
      `file:` URLs never match.
    """
    s = spec.strip()
    if not s:
        return None
    if s.startswith(("./", "../", "/")):
        return None
    if s.startswith(("data:", "blob:", "file:", "javascript:")):
        return None

    if "://" in s:
        # URL form — split path, strip @version, look for forbidden segments.
        path = s.split("?", 1)[0].split("#", 1)[0]
        # Drop the scheme://host. portion; only path components matter.
        try:
            _, after_scheme = path.split("://", 1)
        except ValueError:
            return None
        parts = after_scheme.split("/")[1:]  # drop the host
        for part in parts:
            name = part.split("@", 1)[0] if not part.startswith("@") else part
            if name in _HTML_CSS_FORBIDDEN_IMPORTS:
                return name
        return None

    # Bare module ID.
    if s.startswith("@"):
        segs = s.split("/", 2)
        pkg = "/".join(segs[:2]) if len(segs) >= 2 else s
    else:
        pkg = s.split("/", 1)[0]
    return pkg if pkg in _HTML_CSS_FORBIDDEN_IMPORTS else None


def _scan_text_for_imports(text: str, suffix: str) -> list[tuple[str, str]]:
    """Return a list of (matched_name, specifier) tuples for any
    forbidden framework reference found in `text`. `suffix` controls
    which patterns apply (`.html` adds the script-src scan)."""
    hits: list[tuple[str, str]] = []
    patterns = [_IMPORT_RE, _DYNAMIC_IMPORT_RE, _REQUIRE_RE]
    if suffix == ".html":
        patterns.append(_SCRIPT_SRC_RE)
    for pat in patterns:
        for spec in pat.findall(text):
            name = _match_forbidden(spec)
            if name:
                hits.append((name, spec))
    return hits


def _scan_html_css(agent_dir: Path) -> list[str]:
    """Return human-readable violations; empty list = compliant."""
    violations: list[str] = []
    for f in agent_dir.rglob("*"):
        if not f.is_file():
            continue
        rel = f.relative_to(agent_dir)
        if "node_modules" in f.parts:
            violations.append(f"node_modules present: {rel}")
            continue
        if f.suffix.lower() in _HTML_CSS_FORBIDDEN_EXTS:
            violations.append(f"forbidden file extension: {rel}")
            continue
        if f.name in _HTML_CSS_FORBIDDEN_FILENAMES:
            violations.append(f"forbidden build/config file: {rel}")
            continue
        suffix = f.suffix.lower()
        if suffix in (".js", ".mjs", ".html"):
            try:
                text = f.read_text(errors="replace")
            except OSError:
                continue
            for name, spec in _scan_text_for_imports(text, suffix):
                violations.append(f"forbidden framework reference in {rel}: {name} (via {spec!r})")
    return violations


def score(agent_output_dir: Path | str, task_config: dict[str, Any]) -> dict[str, Any]:
    """Returns {'score': PASS_SCORE | FAIL_SCORE, 'violations': [...], 'allowed': [...]}.

    Allowed frameworks come from `task_config["allowed_frameworks"]` —
    `["html-css"]` for this benchmark.
    """
    agent_dir = Path(agent_output_dir)
    allowed = set(task_config.get("allowed_frameworks", ["html-css"]))

    violations: list[str] = []
    if allowed == {"html-css"}:
        violations = _scan_html_css(agent_dir)

    return {
        "score": FAIL_SCORE if violations else PASS_SCORE,
        "violations": violations,
        "allowed": sorted(allowed),
    }
