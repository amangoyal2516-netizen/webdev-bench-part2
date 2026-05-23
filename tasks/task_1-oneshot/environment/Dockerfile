# Self-contained per-task agent + verifier container. No `FROM
# webdev-bench/base-verifier` — instead the base layers are inlined here
# so Modal can build this from scratch using only public Docker Hub images
# (python:3.11-slim). Trade-off: ~5 min first build per task on Modal vs.
# pulling a pre-pushed image; Modal's layer cache deduplicates the apt /
# pip / playwright layers across every task (identical content = same
# layer hash), so subsequent task builds are ~30 s.
#
# Harbor 0.7.x runs `tests/test.sh` inside this SAME container after the
# agent finishes, so the image carries everything both sides need:
#  - Agent: bash + python.
#  - Verifier: Pillow + numpy + scipy + sklearn + scikit-image + imagehash +
#    lxml + opencv (Track A graders), anthropic SDK + Playwright (Track B
#    judge and screenshot capture in test.sh).

FROM python:3.11-slim

# IMPORTANT: keep the apt / pip / playwright RUN blocks byte-identical
# across every task's Dockerfile — Modal's layer cache works by content
# hash, so identical commands share the cached layer. Only the per-task
# COPYs at the bottom should differ between tasks.
RUN apt-get update && apt-get install -y --no-install-recommends \
        bash coreutils curl ca-certificates git \
        libxml2-dev libxslt-dev libjpeg-dev zlib1g-dev \
        libnss3 libatk-bridge2.0-0 libdrm2 libxkbcommon0 libxcomposite1 \
        libxdamage1 libxfixes3 libxrandr2 libgbm1 libpango-1.0-0 libcairo2 \
        libasound2 libatspi2.0-0 libglib2.0-0 libgl1 \
        fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

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
        anthropic==0.40.0 \
        openai==1.55.0 \
        httpx==0.27.0 \
        pydantic==2.9.0

# `chromium` only — runtime libs are already installed in the apt block
# above. `--with-deps` would re-trigger apt for fonts that don't exist
# in Debian Trixie and fail the build.
RUN playwright install chromium

# Stage the reference screenshots + vendored assets where instruction.md
# tells the agent they are. Harbor builds with the task root as context,
# so `ground_truth/...` paths resolve. (Decision #7's "agent never sees
# ground truth" is enforced at the prompt level — the agent's
# instruction.md does not mention /workspace/ground_truth/ — not by
# absent files on disk.)
#
# Part 2: each (viewport, page) ships full.png + 001.png/002.png/… slices
# + motion-strip.png. The motion-strip is a 5-panel horizontal PNG that
# shows the page's load-triggered entrance animation at five time
# offsets. All land under /workspace/reference/<viewport>/<page>/.
COPY ground_truth/screenshots/. /workspace/reference/
COPY ground_truth/source/assets/. /workspace/output/assets/

WORKDIR /workspace
