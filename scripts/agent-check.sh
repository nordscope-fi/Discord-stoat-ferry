#!/usr/bin/env bash
# Verify ferry blocks present, plain-english lint hooks synced, skills bridged, hooks in parity.
# --ci: check only tracked templates (CI-safe, no gitignored files or ~/.claude/).
set -euo pipefail
exec node "$(dirname "$0")/agent-compat/check.mjs" "$@"
