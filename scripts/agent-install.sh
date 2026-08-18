#!/usr/bin/env bash
# Generate .codex/, .vibe/ and .agents/skills/ from config/agent-compat/ templates.
set -euo pipefail
exec node "$(dirname "$0")/agent-compat/install-local.mjs" "$@"
