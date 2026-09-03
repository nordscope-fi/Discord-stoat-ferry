#!/usr/bin/env bash
# Discord Ferry — Vibe setup entrypoint.
# Mirrors codex-setup.sh for the Vibe host.
#
# Usage:
#   bash scripts/vibe-setup.sh            # bootstrap + install + static readiness
#   bash scripts/vibe-setup.sh --reviewers # also run reviewer readiness probes

set -euo pipefail

ROOT=$(cd "$(dirname "$0")/.." && pwd -P)
REVIEWERS=false

if [[ ${1:-} == "--reviewers" ]]; then
  REVIEWERS=true
fi

echo "=== Ferry Vibe setup ==="
echo "Project root: $ROOT"
echo ""

# Step 1: Install generated config from templates
echo "--- Installing project config ..."
bash "$ROOT/scripts/agent-install.sh"

# Step 2: Run static readiness checks
echo "--- Running Vibe readiness checks ..."
ARGS=(--root "$ROOT")
if $REVIEWERS; then
  ARGS+=(--reviewers)
fi

node "$ROOT/scripts/agent-compat/vibe-readiness.mjs" "${ARGS[@]}"

echo ""
echo "=== Vibe setup complete ==="
echo ""
echo "The shared reviewer runtime is installed by scripts/codex-setup.sh."
echo "If you have not run it yet, run: bash scripts/codex-setup.sh"
