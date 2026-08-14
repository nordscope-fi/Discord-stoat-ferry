#!/usr/bin/env bash
# check-deferral-fields.sh: the four-field deferral requirement.
#
# A deferral is only a plan when it names why not now, what it costs now against
# later, who owns it, and what triggers it. Missing any one of those, it is
# abandonment wearing a deferral's clothes.
#
# Read on stdin by a local pre-flight guard before it allows a `gh issue create`
# for a deferral, and by the deferral sweep in CI. The local guard FAILS OPEN
# when this file is absent, so deleting it turns that gate off with no error.
#
# Usage:
#   scripts/check-deferral-fields.sh <file>     check a file
#   ... | scripts/check-deferral-fields.sh      check stdin
#
# Exit 0  all four fields present with content
# Exit 1  one or more missing or empty (names them on stderr)
#
# A missing field is abandonment wearing a deferral's clothes. An empty one is
# the same thing with extra steps, so a bare heading does not count.

set -uo pipefail

BODY=$(cat "${1:--}" 2>/dev/null) || BODY=""

if [ -z "${BODY//[[:space:]]/}" ]; then
  echo "check-deferral-fields: empty body" >&2
  exit 1
fi

# Any of: "## Why not now", "**Why not now:**", "- Why not now:", "Why not now -"
label_re() { printf '^[[:space:]]*[#>*_-]*[[:space:]]*\\**[[:space:]]*%s\\b' "$1"; }

# A field counts when its label carries text on the same line after the
# separator, or a non-empty line follows it that is not another field label.
ANY_LABEL='^[[:space:]]*[#>*_-]*[[:space:]]*\**[[:space:]]*(why not now|cost( comparison)?|owner|trigger|deadline)\b'

has_field() {
  local re="$1" inline="$2" line n rest following
  n=$(printf '%s\n' "$BODY" | grep -niE "$re" | head -1 | cut -d: -f1)
  # A body written with -b often puts all four on one line. Only the first
  # label sits at a line start there, so fall back to matching the label
  # anywhere as long as a separator and content follow it.
  if [ -z "$n" ]; then
    printf '%s\n' "$BODY" | grep -qiE "${inline}[[:space:]]*[:=-]+[[:space:]]*[^[:space:]]" && return 0
    return 1
  fi
  line=$(printf '%s\n' "$BODY" | sed -n "${n}p")
  # Text on the same line, after ':' or '-' or '=' separator.
  rest=$(printf '%s' "$line" | sed -E 's/^[^:=-]*([:=-]+)?//; s/[*_[:space:]]//g')
  [ -n "$rest" ] && return 0

  # Otherwise the first non-blank line after the label has to be a value rather
  # than the next label. Written without a `while read` in a pipeline: that runs
  # in a subshell, and a run that falls off the end of the input leaves the
  # pipeline at status 0, so a bare label as the LAST line of the body counted
  # as filled in. That is a gate passing when it should fail, which is the one
  # outcome this script exists to prevent.
  following=$(printf '%s\n' "$BODY" | tail -n "+$((n + 1))" | grep -vE '^[[:space:]]*$' | head -1)
  [ -n "$following" ] || return 1
  printf '%s' "$following" | grep -qiE "$ANY_LABEL" && return 1
  return 0
}

MISSING=()
has_field "$(label_re 'why not now')"        'why not now'          || MISSING+=("Why not now")
has_field "$(label_re 'cost( comparison)?')" 'cost( comparison)?'   || MISSING+=("Cost comparison")
has_field "$(label_re 'owner')"              'owner'                || MISSING+=("Owner")
has_field "$(label_re '(trigger|deadline)')" '(trigger|deadline)'   || MISSING+=("Trigger")

if [ ${#MISSING[@]} -eq 0 ]; then
  echo "Deferral fields: all four present."
  exit 0
fi

{
  echo "Deferral is missing ${#MISSING[@]} required field(s):"
  for f in "${MISSING[@]}"; do echo "  - $f"; done
  echo ""
  echo "Every deferral carries all four, or it is abandonment:"
  echo "  Why not now      the actual blocker, not \"too much work\""
  echo "  Cost comparison  now vs later, with estimates"
  echo "  Owner            who does it (default: whoever deferred it)"
  echo "  Trigger          a date, or a measurable condition"
  echo ""
  echo "If the item is foundational it cannot be deferred at all."
} >&2
exit 1
