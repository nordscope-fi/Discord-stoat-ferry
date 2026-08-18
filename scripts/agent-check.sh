#!/usr/bin/env bash
# Verify generated agent config matches templates, skills are bridged, hooks are in parity.
set -euo pipefail
exec node "$(dirname "$0")/agent-compat/check.mjs" "$@"
