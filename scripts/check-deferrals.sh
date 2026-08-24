#!/usr/bin/env bash
# check-deferrals.sh: the deferral red-flag sweep.
#
# This pattern lived inline in the shipping skill, which meant it ran only when
# someone invoked that skill. It now runs in CI too, so skipping the skill no
# longer skips the sweep. The skill calls this script, so there is one copy.
#
# Usage:
#   scripts/check-deferrals.sh [base-ref]     default base: origin/main
#
# Exit 0  clean, or every match is accounted for by a four-field justification
#         shipped in the same diff
# Exit 1  red-flag phrases with nothing justifying them
# Exit 2  could not run (bad base ref, not a repository)
#
# Exit 2 BLOCKS. A gate that cannot run is not a gate that passed, and the
# opposite decision is on record going wrong: a guard that failed open when its
# checker was absent did nothing and reported nothing.
#
# Why the pattern is broad: real engineering writing rarely uses the canonical
# "future work" phrase. It uses domain-flavoured variants. The regex matches the
# family of deferral constructions, not a list of literal phrases. It was tuned
# elsewhere against 60 merged pull requests, where the first version would have
# blocked 39 of 60 and this one blocks 8 of 60. Measured against 60 Ferry
# commits: 0 matches, which is why a demonstrated true positive is a shipping
# condition rather than an assumption.

set -uo pipefail
BASE="${1:-origin/main}"

# Ferry's exclusion list is short because the instruction layer is gitignored and
# never appears in a diff at all.
#
# Files that describe the pattern rather than commit one. Without these the sweep
# reports itself: the test file necessarily contains deferral phrases as fixture
# data, so every pull request touching it would be blocked. Found on this gate's
# first real run against its own branch.
#
# Known blind spot, documented rather than hidden: these are whole-file
# exclusions, not line-level. A genuine deferral written inside either file is
# invisible to this sweep. That is a narrow trade for a gate that would otherwise
# refuse every change to itself.
EXCLUDES=(
  ":(exclude)scripts/check-deferrals.sh"
  ":(exclude)tests/test_gate_scripts.py"
  ":(exclude)scripts/agent-compat/codex-hook-adapter.mjs"
  ":(exclude)scripts/agent-compat/vibe-hook-adapter.mjs"
  ":(exclude)scripts/agent-compat/qwen-stop-guard.mjs"
)

PATTERN='future (work|concern|hardening|enhancement|iteration|sprint|phase|fix|improvement|version)|v[0-9]+ scope|simpler for now|out of scope for (now|this (PR|pull request|change|slice|pass|iteration))|defer(red)? to (follow-?up|later|v[0-9]+|phase [0-9]+)|accepted trade-?off|we can (unify|fix|handle|address|do) (later|in v[0-9]+|in phase [0-9]+)|adds complexity|when we have time|punt(ed)? to|come back to (this|it|that)|in a (later|future|separate|follow-?up) (pass|PR|pull request|change|iteration|slice)|revisit (this|it|that|later)'

# Resolve this script's own directory BEFORE anything changes the working
# directory. $0 can be a relative path (`bash ../scripts/check-deferrals.sh`), and
# a relative $0 resolved after a `cd` points somewhere else entirely: measured at
# ship time, it produced `/check-deferral-fields.sh` and rejected a legitimately
# justified deferral with exit 2. The anchoring below introduced that regression,
# so the order of these two blocks is the fix, not an accident.
SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd) || {
  echo "check-deferrals: cannot resolve own directory from '$0'" >&2; exit 2; }

# Anchor to the repo root. The diff below is scoped with `-- .`, so from a
# subdirectory it would silently scan only that subtree and report clean for a
# deferral added anywhere else. Same class of blind spot assert-doc-refs.sh had.
ROOT=$(git rev-parse --show-toplevel 2>/dev/null) || {
  echo "check-deferrals: not a git repository" >&2; exit 2; }
cd "$ROOT" || { echo "check-deferrals: cannot enter $ROOT" >&2; exit 2; }

if ! git rev-parse --verify --quiet "$BASE" >/dev/null; then
  echo "check-deferrals: base ref '$BASE' not found." >&2
  echo "  In CI this means a shallow checkout. Set fetch-depth: 0." >&2
  exit 2
fi

# Added lines only. Scanning the whole diff reports text the author never wrote:
# editing one line surfaced a match three lines away that nobody had touched.
# Harmless while this only ran inside a skill, a blocked pull request now that it
# runs in CI. A deferral is something you add, not something near your edit.
ADDED=$(git diff "$BASE...HEAD" -- . "${EXCLUDES[@]}" 2>/dev/null \
        | grep '^+' | grep -v '^+++' | sed 's/^+//')

MSGS=""
for commit in $(git rev-list "$BASE..HEAD" 2>/dev/null); do
  MSGS="$MSGS
$(git log -1 --pretty=%B "$commit" 2>/dev/null)"
done

# Third source, and the one that matters. Ferry's recorded failure is follow-ups
# named in PULL REQUEST BODIES and never filed, and the branch policy sends
# multi-commit pull requests down the rebase path, where the body never enters
# git at all. Measured: 38 of the last 40 commits are rebase-merged, so a sweep
# reading only git would be blind to the exact failure it exists to catch.
PR_BODY=""
if [ "${GITHUB_EVENT_NAME:-}" = "pull_request" ]; then
  if command -v gh >/dev/null 2>&1 && PR_BODY=$(gh pr view --json body --jq .body 2>/dev/null); then
    :
  else
    PR_BODY=""
    # Announced, never silent. A silent degradation here reproduces the blind
    # spot this source was added to close, and would look identical to a clean run.
    echo "PR body: unavailable, scanned diff and commits only" >&2
  fi
fi

BODY=$(printf '%s\n%s\n%s\n' "$ADDED" "$MSGS" "$PR_BODY")
MATCHES=$(printf '%s' "$BODY" | grep -niE "$PATTERN" || true)

if [ -z "$MATCHES" ]; then
  echo "Deferral sweep: clean."
  exit 0
fi

# A justification shipped in the same diff accounts for the matches, but only if
# it actually carries the four fields. Checking for the heading alone recreated
# the exact hole this script exists to close: a two-line
# "## Deferral Justification" / "TBD" block, in a file unrelated to the flagged
# phrase, silenced every match in the diff. Pipe it into the one place the fields
# are defined instead of re-implementing the check here.
FIELDS_CHECK="$SCRIPT_DIR/check-deferral-fields.sh"
JUSTIFICATION=$(printf '%s' "$ADDED" | awk '/^## Deferral Justification/{f=1} f{print}')

if [ -n "$JUSTIFICATION" ]; then
  if [ ! -x "$FIELDS_CHECK" ]; then
    # Fail closed, loudly. Failing open here would silence every match whenever
    # the checker went missing, which is how a guard ends up doing nothing while
    # reporting nothing.
    echo "check-deferrals: a justification block is present but $FIELDS_CHECK" >&2
    echo "  is missing or not executable, so its fields cannot be checked." >&2
    exit 2
  fi
  if printf '%s' "$JUSTIFICATION" | "$FIELDS_CHECK" >/dev/null 2>&1; then
    echo "Deferral sweep: matches justified by a four-field block in the same diff."
    exit 0
  fi
fi

printf '%s\n' "$MATCHES" >&2
echo "check-deferrals: unjustified deferral language above." >&2
exit 1
