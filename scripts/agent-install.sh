#!/usr/bin/env bash
# Generate .codex/, .vibe/ and .agents/skills/ from config/agent-compat/ templates,
# then merge plain-english lint hooks in (ADR-024).
set -euo pipefail
exec node "$(dirname "$0")/agent-compat/install-local.mjs" "$@"
