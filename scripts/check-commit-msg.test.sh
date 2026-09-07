#!/bin/sh
# Tests for scripts/check-commit-msg.sh.
#
#   ./scripts/check-commit-msg.test.sh
#
# The checker gates every commit three people make. A wrong regex here does not
# fail quietly — it either blocks all work or waves everything through, so the
# gate gets its own guard. Run by CI before the checker is used on real commits.
#
# Each case names the specification rule it pins where there is one, so a future
# edit that "simplifies" the regex has to argue with the spec rather than with a
# preference.
set -e
cd "$(dirname "$0")/.."

CHECK=./scripts/check-commit-msg.sh
pass=0
fail=0

t() { # t <expected-exit> <label> <message>
    printf '%s' "$3" | "$CHECK" - >/tmp/ccm_out 2>&1 && got=0 || got=$?
    if [ "$got" -eq "$1" ]; then
        pass=$((pass + 1))
        printf '  ok    %s\n' "$2"
    else
        fail=$((fail + 1))
        printf '  FAIL  %s (want exit %s, got %s)\n' "$2" "$1" "$got"
        sed 's/^/          /' /tmp/ccm_out | head -5
    fi
}

echo "=== accepted ==="
t 0 'feat, lowercase'                    'feat: add weekly support dashboard'
t 0 'fix with a scope (rule 4)'          'fix(rules): restore admin access to the rubric'
t 0 'capital type is legal (rule 15)'    'Feat: capital is not an error per the spec'
t 0 'breaking marker (rule 13)'          'feat(auth)!: operators own the grading rules'
t 0 'revert type'                        'revert: feat: add dashboard'
t 0 'body after a blank line (rule 6)'   'feat: add thing

A longer explanation.'
t 0 'BREAKING CHANGE upper (rule 12)'    'feat!: drop the v1 api

BREAKING CHANGE: the v1 endpoint is gone'
t 0 'BREAKING-CHANGE synonym (rule 16)'  'feat!: drop the v1 api

BREAKING-CHANGE: the v1 endpoint is gone'
t 0 'a trailer footer (rule 8)'          'fix: correct the thing

Co-Authored-By: Someone <a@b.com>'

echo "=== exempt, because git writes these itself ==="
t 0 'git merge subject'                  "Merge branch 'main' of github.com:x/y"
t 0 'git revert subject'                 'Revert "feat: add dashboard"'
t 0 'rebase fixup'                       'fixup! feat: add dashboard'
t 0 'rebase squash'                      'squash! feat: add dashboard'

echo "=== rejected ==="
# The shape every commit in this repo used before the convention. This is the
# case that matters most: if it ever starts passing, the convention is dead.
t 1 'no space after the colon (rule 1)'  'Feat:SupportWeeklyDashboard'
t 1 'Enhanc, the old house type'         'Enhanc:FilterUpdateChips'
t 1 'a type not on the list'             'wibble: something'
t 1 'no description (rule 5)'            'feat:'
t 1 'two spaces after the colon'         'feat:  too much air'
t 1 'no type prefix at all'              'just some words about a change'
t 1 'empty scope (rule 4)'               'feat(): add thing'
t 1 'unclosed scope'                     'feat(rules: add thing'
t 1 'body without a blank line (rule 6)' 'feat: add thing
immediately a body'
t 1 'lowercase breaking change (r12/15)' 'feat!: drop api

breaking change: v1 is gone'
t 1 'trailing full stop (house rule)'    'feat: add the thing.'
t 1 'over the 72-char cap (house rule)'  'feat: this subject line is deliberately far too long to be acceptable here ok'
t 1 'an empty message'                   ''

echo
if [ "$fail" -gt 0 ]; then
    echo "COMMIT-MESSAGE CHECKER: $fail of $((pass + fail)) cases FAILED"
    exit 1
fi
echo "COMMIT-MESSAGE CHECKER: all $pass cases passed"
