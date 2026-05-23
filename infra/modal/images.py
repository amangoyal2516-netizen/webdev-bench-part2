"""Modal Image definitions, one per Dockerfile in infra/docker/.

Building images from the same Dockerfiles that local Docker uses keeps the
agent / verifier environment identical between a developer's laptop and the
Modal sandbox.

If we ever need image features Modal supports natively (e.g., GPU base images
for Qwen2.5-VL), prefer extending the image here rather than diverging the
Dockerfile.
"""

from __future__ import annotations

from pathlib import Path

import modal

_DOCKER = Path(__file__).resolve().parent.parent / "docker"
_REPO_ROOT = _DOCKER.parent.parent  # webdev-bench/

# Glob patterns excluded from `add_local_dir` so per-task outputs and
# Python bytecode never balloon image layers. Pool data does NOT live in
# the image — it lives in the `asset-pools` Modal Volume (Phase A3 in
# docs/modal-pipeline-plan.md).
_SRC_IGNORE: list[str] = [
    "**/__pycache__",
    "**/__pycache__/**",
    "**/*.pyc",
    "**/.pytest_cache",
    "**/.pytest_cache/**",
]
_RECIPE_IGNORE = _SRC_IGNORE + [
    "runs",
    "runs/**",
]


def _from_dockerfile(name: str) -> modal.Image:
    dockerfile = _DOCKER / name / "Dockerfile"
    if not dockerfile.exists():
        raise FileNotFoundError(f"Dockerfile not found: {dockerfile}")
    return modal.Image.from_dockerfile(str(dockerfile))


# -------- Active ----------
# Ships the oneshot variant only — the iter base image + render helper
# were removed.

base_html_css_oneshot = _from_dockerfile("base-html-css-oneshot")

# Recipe pipeline: fanned out per design on Modal so the 10 designs generate
# in parallel. Source code is baked in via add_local_dir so the sandbox can
# `import` the recipe stages without an extra runtime mount. Asset pool data
# lives in a separate Volume — too large to bake into image layers.
base_recipe = (
    _from_dockerfile("base-recipe")
    .add_local_dir(
        str(_REPO_ROOT / "recipe"),
        "/repo/recipe",
        copy=True,
        ignore=_RECIPE_IGNORE,
    )
    .add_local_dir(
        str(_REPO_ROOT / "tasks" / "_template"),
        "/repo/tasks/_template",
        copy=True,
        ignore=_SRC_IGNORE,
    )
    .add_local_file(
        str(_REPO_ROOT / "infra" / "assets" / "manifest.json"),
        "/repo/infra/assets/manifest.json",
        copy=True,
    )
)

# Verifier needs the grading code (Track A criteria + Track B judge runner).
# It also needs the recipe builders' validators module, since some grading
# criteria reuse the same DOM-extraction helpers — bake in the full recipe
# tree (same ignore rules as base_recipe).
base_verifier = (
    _from_dockerfile("base-verifier")
    .add_local_dir(
        str(_REPO_ROOT / "grading"),
        "/repo/grading",
        copy=True,
        ignore=_SRC_IGNORE,
    )
    .add_local_dir(
        str(_REPO_ROOT / "recipe"),
        "/repo/recipe",
        copy=True,
        ignore=_RECIPE_IGNORE,
    )
)

# -------- Future framework stubs -------------
# Uncomment to add React / Solid base layers. The Dockerfiles exist under
# infra/docker/ but the Image objects are intentionally not materialised
# here, so a `modal deploy` does not pull those base layers.
#
# base_react = _from_dockerfile("base-react")
# base_solid = _from_dockerfile("base-solid")
