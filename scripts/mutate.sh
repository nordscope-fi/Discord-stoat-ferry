#!/usr/bin/env bash
# mutate.sh: run a mutation sweep over Ferry's safety-critical modules.
#
# Usage:  uv sync --extra mutation && bash scripts/mutate.sh
# Exit 0  the run completed and no mutant survived
# Exit 1  survivors found, triage them
# Exit 2  could not run, or the harness failed its own self-test
#
# WHY THE SELF-TEST IS HERE AND NOT IN pytest
#
# The self-test proves this harness can observe a failure at all. Ferry has five
# recorded lessons about harnesses that could not, and every one of them looked
# exactly like a clean codebase at the time.
#
# It lives inside this script rather than in tests/ for two reasons. cosmic-ray
# sits behind the `mutation` extra, which CI does not install, so a pytest
# version would have to be skipped in CI, and a silently skipped self-test is the
# same failure wearing different clothes. And here it cannot be run around: you
# cannot get a report out of this script without the self-test having passed
# first.
#
# A cosmic-ray crash is deliberately NOT caught. If the engine failed, the run
# failed. A harness that swallows its engine's failure is the broken-detector
# case this exists to prevent.

set -uo pipefail

ROOT=$(git rev-parse --show-toplevel 2>/dev/null) || {
  echo "mutate: not a git repository" >&2; exit 2; }
cd "$ROOT"

SESSION="mutation-session.sqlite"
TARGET="src/discord_ferry/core/atomicio.py"
SUITE="tests/test_atomicio.py"

command -v uv >/dev/null 2>&1 || { echo "mutate: uv not on PATH" >&2; exit 2; }
[ -f cosmic-ray.toml ] || { echo "mutate: cosmic-ray.toml missing" >&2; exit 2; }
if ! uv run --extra mutation python -c "import cosmic_ray" >/dev/null 2>&1; then
  echo "mutate: cosmic-ray not installed. Run: uv sync --extra mutation" >&2
  exit 2
fi

# ---------------------------------------------------------------------------
# Self-test: two mutants, one the suite must kill and one it cannot see.
#
# The PAIR is the point. A detector stuck on one answer passes exactly one of
# these, so running only the killable case would prove nothing.
# ---------------------------------------------------------------------------
BACKUP=$(mktemp)
cp "$TARGET" "$BACKUP"
# Idempotent on purpose. It is called explicitly on the failure paths and again
# by the trap, and a second call must be a no-op rather than a `cp` from a file
# that is already gone. Measured: the target ends up correct either way, but a
# restore that errors on its own second call is noise in exactly the situation
# where the operator needs a clear signal.
restore() {
  [ -f "$BACKUP" ] || return 0
  cp "$BACKUP" "$TARGET"
  rm -f "$BACKUP"
}
trap restore EXIT INT TERM

selftest_fail() {
  trap - EXIT INT TERM
  echo "mutate: SELF-TEST FAILED, $1" >&2
  echo "  The harness cannot reliably tell a killed mutant from a surviving one," >&2
  echo "  so any sweep it produced would be meaningless. Not running the sweep." >&2
  exit 2
}

echo "Self-test: can this harness see a failure?"

# Mutant A: observable, and not an arbitrary one. Swapping Path.replace for
# Path.rename reintroduces issue #172 exactly: rename refuses an existing
# destination on Win32 and replaces it on POSIX, which killed the second
# checkpoint save of every Windows migration. tests/test_atomicio.py exercises
# the swap under simulated Win32 semantics, so this mutation must fail the suite.
# If it does not, the harness is not exercising the code it thinks it is.
python3 - "$TARGET" <<'PY' || exit 2
import sys
p = sys.argv[1]
s = open(p).read()
old, new = "tmp_path.replace(path)", "tmp_path.rename(path)"
if old not in s:
    sys.exit(f"self-test target not found in {p}: {old!r}. The file changed; update mutate.sh.")
open(p, "w").write(s.replace(old, new, 1))
PY
if uv run --extra mutation pytest "$SUITE" -q >/dev/null 2>&1; then
  restore; trap - EXIT
  selftest_fail "an observable mutation did NOT fail the suite"
fi
cp "$BACKUP" "$TARGET"
echo "  observable mutation   -> suite FAILED, as required"

# Mutant B: unobservable. Change a comment. Nothing can legitimately catch this,
# so a suite that fails here is failing for an unrelated reason and every
# SURVIVED verdict from this run would be suspect.
python3 - "$TARGET" <<'PY' || exit 2
import sys
p = sys.argv[1]
s = open(p).read()
if "#" not in s:
    sys.exit(f"no comment found in {p} to use as an unobservable mutation.")
i = s.index("#")
open(p, "w").write(s[:i] + "#  harness self-test, reverted immediately" + s[i + 1:])
PY
if ! uv run --extra mutation pytest "$SUITE" -q >/dev/null 2>&1; then
  restore; trap - EXIT
  selftest_fail "an unobservable mutation DID fail the suite"
fi
cp "$BACKUP" "$TARGET"
echo "  unobservable mutation -> suite PASSED, as required"

restore
trap - EXIT

if ! git diff --quiet -- "$TARGET"; then
  echo "mutate: $TARGET was not restored cleanly after the self-test." >&2
  echo "  Restore it before continuing: git diff -- $TARGET" >&2
  exit 2
fi
echo "  target restored clean"
echo

# ---------------------------------------------------------------------------
# The sweep
# ---------------------------------------------------------------------------
echo "Running the sweep. This takes minutes, not hours: 766 lines against a"
echo "100-test covering suite that runs in under a second."
rm -f "$SESSION"
uv run --extra mutation cosmic-ray init cosmic-ray.toml "$SESSION" || exit 2
uv run --extra mutation cosmic-ray exec cosmic-ray.toml "$SESSION" || exit 2
echo

# cosmic-ray mutates tracked files in place during the sweep, so an interrupted
# run can leave one behind. Observed: killing an earlier sweep left
# src/discord_ferry/cli.py with a comparison flipped, and nothing said so. The
# tree looks normal until a later `git add -A` commits the mutant.
#
# This cannot prevent it (SIGKILL runs no cleanup by definition) but it turns a
# silent corruption into a named one on every path that does reach here.
if ! git diff --quiet -- src/; then
  echo "mutate: WARNING, src/ is dirty after the sweep." >&2
  echo "  cosmic-ray mutates in place; a mutant may have been left behind." >&2
  echo "  Inspect before committing anything:  git diff -- src/" >&2
  git diff --stat -- src/ >&2
fi

uv run --extra mutation python scripts/mutation_report.py "$SESSION"
