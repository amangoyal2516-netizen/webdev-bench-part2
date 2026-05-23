#!/usr/bin/env bash
# Replace `| tee /logs/agent/claude-code.txt` with
# `> /logs/agent/claude-code.txt 2>&1` in Harbor's claude_code agent
# command. See infra/harbor-patches/README.md for rationale.
set -euo pipefail

target="/home/aman/.local/share/uv/tools/harbor/lib/python3.12/site-packages/harbor/agents/installed/claude_code.py"
if [ ! -f "$target" ]; then
    echo "error: Harbor's claude_code.py not found at $target" >&2
    echo "  (Did you reinstall harbor? Adjust the path or re-run uv tool install.)" >&2
    exit 1
fi

# Idempotent: if already patched, exit clean.
if grep -q '> /logs/agent/claude-code.txt 2>&1' "$target"; then
    echo "already patched"
    exit 0
fi

if ! grep -q '2>&1 </dev/null | tee ' "$target"; then
    echo "error: original pattern not found in $target — Harbor version may have changed" >&2
    exit 1
fi

python3 - <<PY
from pathlib import Path
p = Path("$target")
text = p.read_text()
old = '''                f"--print -- {escaped_instruction} 2>&1 </dev/null | tee "
                f"/logs/agent/claude-code.txt"'''
new = '''                f"--print -- {escaped_instruction} </dev/null "
                f"> /logs/agent/claude-code.txt 2>&1"'''
if old not in text:
    raise SystemExit("error: expected pre-patch text not found verbatim")
p.write_text(text.replace(old, new))
print("patched", p)
PY

python3 -c "import ast; ast.parse(open('$target').read())" && echo "syntax OK"
