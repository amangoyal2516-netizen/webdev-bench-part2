# Recipe container — runs the design-generation pipeline (recipe/01-generate,
# /02-capture, /03-package) that authors the 10 ground-truth websites.
#
# Designed to run as Modal Apps (decision #17): one Modal function call per
# slug, fanned out so the 10 slugs generate in parallel. `infra/modal/images.py`
# builds the image via `modal.Image.from_dockerfile(...)`.
#
# Dep stack vs. base-verifier:
#   - SHARED: Playwright + Chromium, lxml/bs4, Pillow/numpy/scipy/sklearn/
#     scikit-image, imagehash, opencv (so the pre-computed ground-truth
#     artifacts match what the verifier reads — decision #15).
#   - ADDED: anthropic SDK (design generation per decision #2),
#     jsonschema (validates design.json against the schema).
#   - REMOVED: paddleocr/paddlepaddle (~600 MB; OCR is a verifier-only
#     fallback for text-in-images), openai SDK (only Track B verifier
#     uses the second judge model).

FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        bash coreutils curl ca-certificates git tree jq less \
        # Image-processing native deps for Pillow / lxml / opencv
        libxml2-dev libxslt-dev libjpeg-dev zlib1g-dev libgl1 \
        # Chromium runtime deps for Playwright
        libnss3 libatk-bridge2.0-0 libdrm2 libxkbcommon0 libxcomposite1 \
        libxdamage1 libxfixes3 libxrandr2 libgbm1 libpango-1.0-0 libcairo2 \
        libasound2 libatspi2.0-0 libglib2.0-0 fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

# Pinned versions — kept in sync with base-verifier where libraries overlap.
RUN pip install --no-cache-dir \
        playwright==1.49.0 \
        lxml==5.3.0 \
        beautifulsoup4==4.12.3 \
        Pillow==11.0.0 \
        numpy==2.1.0 \
        scipy==1.14.0 \
        scikit-learn==1.5.0 \
        scikit-image==0.24.0 \
        imagehash==4.3.1 \
        opencv-python-headless==4.10.0.84 \
        # Anthropic SDK for design generation (decision #2)
        anthropic==0.40.0 \
        # design.json validation against recipe/01-generate/schemas/
        jsonschema==4.23.0 \
        # Recipe-side data models
        pydantic==2.9.0

# `chromium` only — runtime libs are already installed in the apt block
# above. `--with-deps` would re-trigger apt for fonts (ttf-ubuntu-font-family,
# ttf-unifont) that don't exist in Debian Trixie and fail the build.
RUN playwright install chromium

WORKDIR /recipe
