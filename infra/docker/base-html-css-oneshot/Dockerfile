# Agent container for the -oneshot variant.
# The agent receives reference screenshots in /workspace/reference/ and writes
# its HTML/CSS to /workspace/output/. No rendering tools, no internet
# (Harbor's allow_internet = false handles the latter).

FROM python:3.11-slim

# Standard Unix tools the agent's shell needs.
RUN apt-get update && apt-get install -y --no-install-recommends \
        bash coreutils curl ca-certificates git tree jq less \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /workspace

# Harbor mounts the actual content at runtime; we just provide the layout.
RUN mkdir -p /workspace/output /workspace/assets /workspace/reference
