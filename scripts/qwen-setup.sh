#!/usr/bin/env bash
# Discord Ferry — Qwen setup entrypoint.
# Mirrors vibe-setup.sh for the Qwen Code host.
#
# Usage:
#   bash scripts/qwen-setup.sh            # install + static readiness
#   bash scripts/qwen-setup.sh --reviewers # also run reviewer readiness probes

set -euo pipefail

ROOT=$(cd "$(dirname "$0")/.." && pwd -P)
REVIEWERS=false

if [[ ${1:-} == "--reviewers" ]]; then
  REVIEWERS=true
fi

echo "=== Ferry Qwen setup ==="
echo "Project root: $ROOT"
echo ""

# Step 1: Install generated config from templates
echo "--- Installing project config ..."
bash "$ROOT/scripts/agent-install.sh"

# Step 2: Run static readiness checks
echo "--- Running Qwen readiness checks ..."
ARGS=(--root "$ROOT")
if $REVIEWERS; then
  ARGS+=(--reviewers)
fi

node "$ROOT/scripts/agent-compat/qwen-readiness.mjs" "${ARGS[@]}"

echo ""
echo "=== Qwen setup complete ==="
echo ""
echo "The shared reviewer runtime is installed by scripts/codex-setup.sh."
echo "If you have not run it yet, run: bash scripts/codex-setup.sh"
