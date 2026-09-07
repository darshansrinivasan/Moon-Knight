#!/bin/sh
# Validate one commit message against Conventional Commits v1.0.0.
#   https://www.conventionalcommits.org/en/v1.0.0/
#
#   scripts/check-commit-msg.sh <file>     # a file holding the message
#   scripts/check-commit-msg.sh -          # the message on stdin
#
# Single source of truth for the rule. The commit-msg hook calls it so a bad
# message is caught before the commit exists, and CI calls it per pushed commit
# so a --no-verify bypass still shows up. One implementation on purpose: a regex
# copied into a hook and a workflow drifts, and then the gates disagree about
# what is legal.
#
# Spec rules enforced, by their number in the specification:
#   1  type, optional (scope), optional !, then a REQUIRED colon AND space
#   4  a scope is a noun in parentheses
#   5  a description immediately follows the colon and space
#   6  a body begins one blank line after the description
#  12  a BREAKING CHANGE footer is uppercase, then colon, space, description
#  14  types beyond feat/fix are allowed — the house list is TYPES below
#  15  type and scope are NOT case-sensitive; only BREAKING CHANGE must be upper
#
# Two house additions the spec does not cover, marked as such in the errors:
# a 72-character subject cap, and no trailing full stop.
#
# Not enforced: general footer token syntax (rule 9). A line like "See the note
# below:" in a body is indistinguishable from a malformed footer without parsing
# the whole message, and a validator that rejects prose is one people disable.
set -e

# Rule 14: feat and fix are mandated by the spec; the rest is this repo's list.
TYPES='feat|fix|build|chore|ci|docs|perf|refactor|revert|style|test'
MAX_SUBJECT=72

case "$1" in
    ""|-h|--help)
        echo "usage: $0 <message-file>|-" >&2
        exit 2
        ;;
    -)  MSG=$(cat) ;;
    *)  [ -f "$1" ] || { echo "no such message file: $1" >&2; exit 2; }
        MSG=$(cat "$1") ;;
esac

# Git's comment lines are not part of the message. `sed '/./,$!d'` drops leading
# blank lines so the subject is the first line that actually says something.
BODY_AND_SUBJECT=$(printf '%s\n' "$MSG" | grep -v '^#' | sed '/./,$!d')
SUBJECT=$(printf '%s\n' "$BODY_AND_SUBJECT" | head -n 1)

reject() {
    echo >&2
    echo "  ✗ Commit message rejected: $1" >&2
    echo >&2
    echo "      got:  $SUBJECT" >&2
    [ -n "${2:-}" ] && echo "      try:  $2" >&2
    echo >&2
    echo "    Conventional Commits v1.0.0 — <type>[(scope)][!]: <description>" >&2
    echo "    Types:  $(printf '%s' "$TYPES" | tr '|' ' ')" >&2
    echo >&2
    echo "      feat: add weekly support dashboard" >&2
    echo "      fix(rules): restore admin access to the rubric" >&2
    echo "      refactor(slack): collapse the two mention builders" >&2
    echo "      feat(auth)!: operators own the grading rules" >&2
    echo >&2
    echo "    Full rule: docs/CONVENTIONS.md      Spec: conventionalcommits.org" >&2
    echo "    Amend it:  git commit --amend       Override: git commit --no-verify" >&2
    echo >&2
    exit 1
}

# ── exemptions ────────────────────────────────────────────────────────────────
# Messages git writes or rewrites itself. Rejecting these would break `git merge`,
# `git revert` and every interactive rebase using fixup/squash — the hook would
# be fighting the tool rather than the author. (A hand-written revert should use
# the `revert:` type; this only exempts git's own generated subject.)
case "$SUBJECT" in
    "Merge "*|'Revert "'*|"fixup! "*|"squash! "*|"amend! "*)
        exit 0 ;;
esac

[ -n "$SUBJECT" ] || reject "the message is empty"

# ── rule 1 + 5: the prefix and the description ────────────────────────────────
# Rule 15 means the type and scope are matched case-insensitively, so `Feat: x`
# is as valid as `feat: x`. docs/CONVENTIONS.md asks for lowercase as house
# style; the spec forbids treating that as an error, so this does not.
if ! printf '%s' "$SUBJECT" | grep -Eiq "^($TYPES)(\([^()]+\))?!?: .+"; then

    # Diagnose the actual mistake instead of restating the whole rule.

    # Missing the space after the colon — the single most common miss, and the
    # shape every commit in this repo's history before the convention used.
    if printf '%s' "$SUBJECT" | grep -Eiq "^($TYPES)(\([^()]+\))?!?:[^ ]"; then
        suggestion=$(printf '%s' "$SUBJECT" | sed -E 's/^([A-Za-z]+(\([^()]+\))?!?):/\1: /')
        reject "rule 1 requires a colon AND a space after the type" "$suggestion"
    fi

    # Colon and space present but nothing after it.
    if printf '%s' "$SUBJECT" | grep -Eiq "^($TYPES)(\([^()]+\))?!?: *$"; then
        reject "rule 5 requires a description after the colon and space"
    fi

    # An empty or malformed scope.
    if printf '%s' "$SUBJECT" | grep -Eiq "^($TYPES)\(\)"; then
        reject "rule 4 requires a noun inside the scope parentheses"
    fi
    if printf '%s' "$SUBJECT" | grep -Eiq "^($TYPES)\([^)]*$"; then
        reject "the scope is missing its closing parenthesis"
    fi

    # A plausible type that is not on the list.
    if printf '%s' "$SUBJECT" | grep -Eq "^[A-Za-z]+(\([^()]+\))?!?:"; then
        given=$(printf '%s' "$SUBJECT" | sed -E 's/^([A-Za-z]+).*/\1/')
        # The old house types map onto spec types; name the replacement.
        case "$(printf '%s' "$given" | tr '[:upper:]' '[:lower:]')" in
            enhanc|enhance|enhancement)
                reject "'$given' is not a Conventional Commits type — use feat: for a new capability, or refactor:/perf:/style: for an internal change" ;;
            *)
                reject "'$given' is not one of the allowed types" ;;
        esac
    fi

    reject "missing the '<type>: ' prefix"
fi

# Rule 5: the description follows the colon and *one* space, so a second space
# would make the description itself start with whitespace.
if printf '%s' "$SUBJECT" | grep -Eiq "^($TYPES)(\([^()]+\))?!?:  "; then
    reject "rule 5: exactly one space between the colon and the description"
fi

# ── house additions ───────────────────────────────────────────────────────────
if [ "${#SUBJECT}" -gt "$MAX_SUBJECT" ]; then
    reject "house rule (not in the spec): subject is ${#SUBJECT} chars, cap is $MAX_SUBJECT"
fi

case "$SUBJECT" in
    *.) reject "house rule (not in the spec): no trailing full stop on the subject" ;;
esac

# ── rule 6: a body begins one blank line after the description ────────────────
SECOND=$(printf '%s\n' "$BODY_AND_SUBJECT" | sed -n '2p')
if [ -n "$SECOND" ]; then
    reject "rule 6: put a blank line between the subject and the body"
fi

# ── rule 12 + 15: BREAKING CHANGE must be uppercase ───────────────────────────
# Rule 16 makes BREAKING-CHANGE synonymous, so both spellings are accepted.
# Only the case is checked: a lowercase token is silently ignored by every
# changelog and release tool, so the breaking change would go unannounced.
OFFENDER=$(printf '%s\n' "$BODY_AND_SUBJECT" \
    | grep -Ei '^breaking[ -]change:' \
    | grep -Ev '^BREAKING[ -]CHANGE:' \
    | head -n 1 || true)
if [ -n "$OFFENDER" ]; then
    SUBJECT="$OFFENDER"
    reject "rule 12: the BREAKING CHANGE footer token must be uppercase" \
           "BREAKING CHANGE: $(printf '%s' "$OFFENDER" | sed -E 's/^[Bb][Rr][Ee][Aa][Kk][Ii][Nn][Gg][ -][Cc][Hh][Aa][Nn][Gg][Ee]: *//')"
fi

exit 0
