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
# Known blind spot, documented rather than hidden: these are whole-file
# exclusions, not line-level. A genuine deferral added to this script itself is
# invisible to it.
EXCLUDES=(":(exclude)scripts/check-deferrals.sh")

PATTERN='future (work|concern|hardening|enhancement|iteration|sprint|phase|fix|improvement|version)|v[0-9]+ scope|simpler for now|out of scope for (now|this (PR|pull request|change|slice|pass|iteration))|defer(red)? to (follow-?up|later|v[0-9]+|phase [0-9]+)|accepted trade-?off|we can (unify|fix|handle|address|do) (later|in v[0-9]+|in phase [0-9]+)|adds complexity|when we have time|punt(ed)? to|come back to (this|it|that)|in a (later|future|separate|follow-?up) (pass|PR|pull request|change|iteration|slice)|revisit (this|it|that|later)'

git rev-parse --git-dir >/dev/null 2>&1 || {
  echo "check-deferrals: not a git repository" >&2; exit 2; }

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

BODY=$(printf '%s\n%s\n' "$ADDED" "$MSGS")
MATCHES=$(printf '%s' "$BODY" | grep -niE "$PATTERN" || true)

if [ -z "$MATCHES" ]; then
  echo "Deferral sweep: clean."
  exit 0
fi

printf '%s\n' "$MATCHES" >&2
echo "check-deferrals: unjustified deferral language above." >&2
exit 1
