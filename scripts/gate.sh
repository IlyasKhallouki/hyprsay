#!/usr/bin/env bash
# Everything that must be true before a commit. Run it, do not pipe it: a pipe hides the
# exit status, which is how a commit with two failing tests got made once.
set -euo pipefail
cd "$(dirname "$0")/.."
echo "== ruff (what CI runs)"; uv run ruff check .
echo "== format";            uv run ruff format --check src/hyprsay tests/hyprsay evals hud
echo "== tests";             uv run python -m pytest tests -q -p no:cacheprovider
echo "== secrets"
if git grep -nI -E '(vck|sk|ghp)_[A-Za-z0-9]{16,}|_API_KEY=[A-Za-z0-9_-]{12,}' -- . ':!scripts/gate.sh'; then
  echo "BLOCKED: key material in the tree" >&2; exit 1
fi
echo "== house style"
if git grep -nI $'—' -- src tests evals hud docs packaging README.md ARCHITECTURE.md; then
  echo "BLOCKED: em dash" >&2; exit 1
fi
echo "all gates passed"
