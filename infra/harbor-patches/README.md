# Harbor patches (apply after `uv tool install --force 'harbor[modal]'`)

## Why these exist

Harbor 0.8.0's `harbor/agents/installed/claude_code.py` pipes the
Claude Code agent's stream-json output through `| tee` to file. This
means the full output (10+ MB on Part 2 image-heavy rollouts) flows
through stdout, which Harbor reads via Modal's gRPC stream
(`_sdk_exec`). Modal's stream router fails on large blob reads,
causing:

- `modal.exception.InternalError: Failed to read exec stdio stream`
  (the `UK470W36` / `Q8T4WDCQ` family of errors)
- Silent hangs when the same bug manifests as a stuck stream rather
  than an exception

Affected trials in `jobs/webdev-bench-20260523-204706/`: 3 of 4.

The patch redirects the agent's stdout/stderr directly to file (no
`tee`), so the exec returns ~zero stdio bytes to Harbor. The agent
output survives intact in `/logs/agent/claude-code.txt` inside the
container, and Harbor reads from that file (see
`_parse_total_cost_from_stream_json` in claude_code.py:490+).

## Apply this patch

```bash
bash infra/harbor-patches/01-no-tee-for-claude-code.sh
```

`uv tool upgrade harbor` will undo the patch — re-run the script.

## Verify the patch is active

```bash
grep -A 2 "claude --verbose" /home/aman/.local/share/uv/tools/harbor/lib/python3.12/site-packages/harbor/agents/installed/claude_code.py | grep -E "tee|> /logs"
# expected: "> /logs/agent/claude-code.txt 2>&1"  (NOT "| tee /logs/agent/claude-code.txt")
```
