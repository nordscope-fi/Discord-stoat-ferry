#!/bin/bash
# Bounded runner for the #956 destination probe. Same controls as run-955.sh.
set -euo pipefail
SCRATCH="${1:?usage: run-956.sh <scratch-dir>}"
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$HERE/../.." && pwd)"
mkdir -p "$SCRATCH/tmp"
rm -rf "$SCRATCH/v956"
mkdir -p "$SCRATCH/v956"
# sandbox-exec matches canonical paths, so a symlinked or /tmp-style argument
# must be resolved before it is rendered into the profile.
SCRATCH="$(cd "$SCRATCH" && pwd -P)"
PROFILE="$SCRATCH/sandbox-956-rendered.sb"
ESCAPED="$(printf '%s' "$SCRATCH" | sed -e 's/[\\&|]/\\&/g')"
sed "s|@SCRATCH@|$ESCAPED|" "$HERE/sandbox-955.sb" > "$PROFILE"
ulimit -t 60 -u 64 -f 100000
exec env -i \
  PATH=/usr/bin:/bin \
  HOME="$SCRATCH" \
  TMPDIR="$SCRATCH/tmp" \
  PROBE_SCRATCH="$SCRATCH" \
  PYTHONDONTWRITEBYTECODE=1 \
  PYTHONPATH="$REPO_ROOT/src" \
  /usr/bin/sandbox-exec -f "$PROFILE" \
  "$REPO_ROOT/.venv/bin/python" "$HERE/probe_956.py"
