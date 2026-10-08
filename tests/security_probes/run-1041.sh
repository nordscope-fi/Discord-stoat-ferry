#!/bin/bash
# Bounded runner for the #1041 guild-icon containment probe.
# Same controls as tests/security_probes/run-955.sh: empty allowlisted env,
# scratch-only HOME/TMPDIR, no bytecode writes, CPU/process/file-size rlimits,
# OS-enforced sandbox with no network and scratch-only writes rendered from the
# canonicalized scratch path.
#
# Usage: bash run-1041.sh <scratch-dir> [repo-root]
# Argument order matches run-955.sh and run-956.sh: scratch first.
# Restored to .qwen/tmp/probe-1041/ on 2026-10-07 after /tmp was cleaned.
HERE="$(cd "$(dirname "$0")" && pwd)"
SCRATCH="${1:?usage: run-1041.sh <scratch-dir> [repo-root]}"
REPO_ROOT="${2:-$(cd "$HERE/../.." && pwd)}"
rm -rf "$SCRATCH"
mkdir -p "$SCRATCH/tmp"
# sandbox-exec matches canonical paths, so resolve before rendering the profile.
SCRATCH="$(cd "$SCRATCH" && pwd -P)"
PROFILE="$SCRATCH/sandbox-1041-rendered.sb"
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
  "$REPO_ROOT/.venv/bin/python" "$HERE/probe_1041.py"
