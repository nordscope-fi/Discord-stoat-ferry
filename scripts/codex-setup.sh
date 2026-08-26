#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/.." && pwd -P)
LIVE=false
if [[ ${1:-} == "--live" ]]; then
  LIVE=true
  shift
fi
if (( $# )); then
  echo "usage: scripts/codex-setup.sh [--live]" >&2
  exit 2
fi

node "$ROOT/scripts/agent-compat/codex-bootstrap.mjs" --root "$ROOT"
"$ROOT/scripts/agent-install.sh"
node "$ROOT/scripts/agent-compat/codex-readiness.mjs" --root "$ROOT"
if $LIVE; then
  node "$ROOT/scripts/agent-compat/codex-readiness.mjs" --root "$ROOT" \
    --live --worktree
fi
