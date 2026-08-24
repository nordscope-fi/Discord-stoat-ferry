#!/usr/bin/env bash
# assert-doc-refs.sh: paths cited in the instruction layer must resolve.
#
# LOCAL ONLY. Its inputs (CLAUDE.md, .claude/rules/*.md, docs/architecture/) are
# gitignored, so a CI runner receives none of them: a clean clone has no CLAUDE.md
# and no rules directory at all. Called from /df-ship, never from a workflow.
#
# Usage:  scripts/assert-doc-refs.sh
# Exit 0  every cited path resolves and the ADR index matches the directory
# Exit 1  at least one citation or index row is wrong
# Exit 2  could not run (not a git repository)
#
# A citation drifts the moment you insert a line above it. All five that one
# branch added had drifted by ship time, broken by that same branch, and nothing
# caught it.

set -uo pipefail

# Anchor to the repo root. `git rev-parse --git-dir` only proves we are somewhere
# inside a repository, not at the top of one, and every path below is relative.
# Run from a subdirectory without this, the script finds no CLAUDE.md, no rules
# and no ADR directory, checks nothing, and prints "clean" with exit 0. Measured
# from src/ before the fix: a false pass indistinguishable from a real one, in
# the very script written to stop checks from doing that.
ROOT=$(git rev-parse --show-toplevel 2>/dev/null) || {
  echo "assert-doc-refs: not a git repository" >&2; exit 2; }
cd "$ROOT" || { echo "assert-doc-refs: cannot enter $ROOT" >&2; exit 2; }

FAILURES=0
DOCS="CLAUDE.md"
for f in .claude/rules/*.md; do [ -e "$f" ] && DOCS="$DOCS $f"; done

# Second belt: even at the root, if nothing readable was found there is nothing
# to check and "clean" would be a lie. Same shape as the ADR row-count guard
# below, applied to the inputs rather than to one sub-check.
FOUND=0
for doc in $DOCS; do [ -e "$doc" ] && FOUND=$((FOUND + 1)); done
if [ "$FOUND" -eq 0 ]; then
  echo "assert-doc-refs: found none of its input documents under $ROOT." >&2
  echo "  Expected CLAUDE.md and/or .claude/rules/*.md. Checking nothing is not passing." >&2
  exit 2
fi

# Global excludes disabled so a developer's own ~/.gitignore cannot hide drift
# that CI-equivalent conditions would show. No global file is set on this machine
# today; this is protection against a future one.
is_excluded() { git -c core.excludesfile=/dev/null check-ignore -q "$1" 2>/dev/null; }

# Process substitution rather than a pipe: a piped `while` runs in a subshell and
# every FAILURES increment would be discarded, so the guard would always pass.
for doc in $DOCS; do
  [ -e "$doc" ] || continue
  while IFS= read -r t; do
    case "$t" in
      ''|/*|@*|~*|*' '*) continue ;;                        # absolute, alias, home, prose
      *'*'*|*'<'*|*'>'*|*'{'*|*'}'*|*'|'*) continue ;;      # glob or placeholder
    esac
    case "$t" in *...*) continue ;; esac                     # ellipsis placeholder
    case "$t" in */*) : ;; *) continue ;; esac               # require a slash
    clean="${t%%#*}"                                         # strip #fragment
    clean=$(printf '%s' "$clean" | sed -E 's/:[0-9]+$//')    # strip :line citation
    # The change manifest is transient: created when a build starts, deleted by
    # /df-ship after merge. CLAUDE.md cites the convention, not a file that
    # survives a ship, so a missing manifest is not drift. Without this, the
    # guard fails locally after every ship until the next build recreates it.
    [ "$clean" = ".claude/change-manifest.md" ] && continue
    seg="${clean%%/*}"
    [ -e "$seg" ] || continue          # first segment must be a real top-level entry
    is_excluded "$clean" && continue
    if [ ! -e "$clean" ]; then
      echo "  MISSING: $clean  (cited in $doc)" >&2
      FAILURES=$((FAILURES + 1))
    fi
  done < <(grep -oE '`[^`]+`' "$doc" 2>/dev/null | tr -d '`')
done

# ADR index parity, checked in BOTH directions. One direction alone would have
# missed the ADR-018 collision that critique round 3 found: a second session had
# taken the number, so a new file would have overwritten an Accepted record while
# the index still showed one row.
ADR_DIR="docs/architecture/adr"
INDEX="$ADR_DIR/README.md"
if [ -d "$ADR_DIR" ] && [ -f "$INDEX" ]; then
  for f in "$ADR_DIR"/[0-9]*.md; do
    [ -e "$f" ] || continue
    n=$(basename "$f" | cut -c1-3)
    grep -q "\[$n\](" "$INDEX" || {
      echo "  ADR $n has no row in $INDEX" >&2; FAILURES=$((FAILURES + 1)); }
  done
  # The row->file direction depends on the index's table format. If that format
  # ever changes, this extraction finds nothing and the direction silently checks
  # nothing, which is indistinguishable from passing. Count what was parsed and
  # fail loudly when the index has rows to find and none were found. The
  # file->row direction above does not need this: it matches `[NNN](` anywhere,
  # so it survives a format change.
  ROWS=0
  while IFS= read -r n; do
    ROWS=$((ROWS + 1))
    ls "$ADR_DIR/$n"-*.md >/dev/null 2>&1 || {
      echo "  $INDEX row $n points at no file" >&2; FAILURES=$((FAILURES + 1)); }
  done < <(grep -oE '^\| \[[0-9]{3}\]' "$INDEX" | grep -oE '[0-9]{3}')

  ADR_COUNT=$(ls "$ADR_DIR"/[0-9]*.md 2>/dev/null | wc -l | tr -d ' ')
  if [ "$ROWS" -eq 0 ] && [ "$ADR_COUNT" -gt 0 ]; then
    echo "  parsed 0 index rows from $INDEX while $ADR_COUNT ADR files exist." >&2
    echo "  The row format changed and this check stopped checking anything." >&2
    FAILURES=$((FAILURES + 1))
  fi
fi

[ "$FAILURES" -eq 0 ] || { echo "assert-doc-refs: $FAILURES problem(s)" >&2; exit 1; }
echo "assert-doc-refs: clean."
