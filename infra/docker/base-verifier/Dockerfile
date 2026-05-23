# Verifier container — runs the grader against the agent's output.
# Receives the agent's /workspace/output/ via Harbor's separate-mode copy
# (decision #13 — agent never sees ground-truth).
#
# Includes everything Track A (deterministic) and Track B (MLLM judge) need.

FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        bash coreutils curl ca-certificates git \
        # Pillow / lxml / OCR / Chromium native deps
        libxml2-dev libxslt-dev libjpeg-dev zlib1g-dev \
        libnss3 libatk-bridge2.0-0 libdrm2 libxkbcommon0 libxcomposite1 \
        libxdamage1 libxfixes3 libxrandr2 libgbm1 libpango-1.0-0 libcairo2 \
        libasound2 libatspi2.0-0 libglib2.0-0 libgl1 \
        fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

# Pinned versions — review/refresh before first build per decision #15.
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
        # NOTE: paddleocr / paddlepaddle dropped from this image — they pin
        # numpy < 2 and conflict with the rest of the stack. text-in-image
        # OCR was an optional fallback for `image_content_fidelity`; the
        # criterion degrades to pHash-only without it. Re-add when we
        # actually need OCR (would require a separate image).
        # MLLM judge SDKs (Track B, decision #16)
        anthropic==0.40.0 \
        openai==1.55.0 \
        httpx==0.27.0 \
        pydantic==2.9.0

# `chromium` only — runtime libs are already installed in the apt block
# above. `--with-deps` would re-trigger apt for fonts (ttf-ubuntu-font-family,
# ttf-unifont) that don't exist in Debian Trixie and fail the build.
RUN playwright install chromium

WORKDIR /grading
