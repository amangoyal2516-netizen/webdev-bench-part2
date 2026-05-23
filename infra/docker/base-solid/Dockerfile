# Part 3 (multi-framework) base image — Solid + Tailwind agent variants.
# Stub: implement once Part 1 is solid and Part 3 begins (steps.md Phase 7).

FROM node:20-bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        bash coreutils curl ca-certificates git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /workspace
RUN mkdir -p /workspace/output /workspace/assets /workspace/reference

# TODO Part 3:
# - Solid + Vite preinstall
# - Tailwind toolchain
# - Playwright + Chromium for the -iter variant
