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

git rev-parse --git-dir >/dev/null 2>&1 || {
  echo "assert-doc-refs: not a git repository" >&2; exit 2; }

FAILURES=0
DOCS="CLAUDE.md"
for f in .claude/rules/*.md; do [ -e "$f" ] && DOCS="$DOCS $f"; done

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
    seg="${clean%%/*}"
    [ -e "$seg" ] || continue          # first segment must be a real top-level entry
    is_excluded "$clean" && continue
    if [ ! -e "$clean" ]; then
      echo "  MISSING: $clean  (cited in $doc)" >&2
      FAILURES=$((FAILURES + 1))
    fi
  done < <(grep -oE '`[^`]+`' "$doc" 2>/dev/null | tr -d '`')
done

[ "$FAILURES" -eq 0 ] || { echo "assert-doc-refs: $FAILURES broken citation(s)" >&2; exit 1; }
echo "assert-doc-refs: clean."
