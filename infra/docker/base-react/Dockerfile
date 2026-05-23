# Part 3 (multi-framework) base image — React + CSS or React + Tailwind agent variants.
# Stub: implement once Part 1 is solid and Part 3 begins (steps.md Phase 7).

FROM node:20-bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        bash coreutils curl ca-certificates git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /workspace
RUN mkdir -p /workspace/output /workspace/assets /workspace/reference

# TODO Part 3:
# - Vite or Next bundler preinstall
# - Playwright + Chromium for the -iter variant
# - Tailwind toolchain for the -tailwind sub-variant
