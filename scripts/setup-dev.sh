#!/bin/sh
# One-time per-clone developer setup. Safe to re-run.
#
#   ./scripts/setup-dev.sh
#
# Sets the two things a fresh clone cannot know: where the shared git hooks live,
# and how to reconcile a branch that has moved under you. Neither is inherited
# from the repo — git deliberately does not let a repository configure its own
# hooks path or pull strategy, because that would let a clone run code you have
# not read. So it is opt-in, per person, once.
set -e
cd "$(dirname "$0")/.."

echo "Setting up Moon-Knight QC for local development"
echo

# ── shared hooks ──────────────────────────────────────────────────────────────
# .git/hooks is not versioned, so the committed .githooks directory plus this
# pointer is the only way three people can share one commit-message rule.
git config core.hooksPath .githooks
chmod +x .githooks/* scripts/*.sh 2>/dev/null || true
echo "  ✓ hooks        core.hooksPath = .githooks"
echo "                 commit messages are checked at commit time"

# ── reconcile strategy ────────────────────────────────────────────────────────
# main takes commits from three people, often minutes apart. git already refuses
# to push from a stale clone, so nobody can clobber anyone. What is NOT decided
# for you is how you catch up, and the default is to guess wrong: a merge commit
# per catch-up, or a conflict resolution that silently reverts someone's work.
# Rebase keeps main linear and keeps your commits yours.
git config pull.rebase true
git config rebase.autoStash true
git config fetch.prune true
echo "  ✓ pull         pull.rebase = true (no merge commits on main)"
echo "  ✓ rebase       rebase.autoStash = true (a dirty tree will not block it)"
echo "  ✓ fetch        fetch.prune = true (drops branches deleted on the remote)"
echo

# ── python ────────────────────────────────────────────────────────────────────
# app.py and db.py use `X | None` annotations at module scope, which 3.9
# evaluates at import time and rejects. A 3.9 `python3` therefore fails at
# `import db` — before a single assertion runs — so it is worth saying plainly.
FOUND=""
for candidate in python3.13 python3.12 python3.11 python3; do
    if command -v "$candidate" >/dev/null 2>&1; then
        ver=$("$candidate" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || echo "0.0")
        major=${ver%%.*}
        minor=${ver#*.}
        if [ "$major" -eq 3 ] && [ "$minor" -ge 10 ]; then
            FOUND="$candidate"
            break
        fi
    fi
done

if [ -n "$FOUND" ]; then
    echo "  ✓ python       $FOUND ($("$FOUND" -c 'import sys; print(sys.version.split()[0])'))"
    echo
    echo "To run the tests:"
    echo "    $FOUND -m venv .venv"
    echo "    .venv/bin/pip install -r requirements.txt"
    echo "    PYTHON=.venv/bin/python ./tests/run.sh"
else
    echo "  ✗ python       no interpreter >= 3.10 found"
    echo
    echo "    The app needs 3.10+: db.py annotates \`dict | None\` at module scope,"
    echo "    which 3.9 rejects at import. Install one (brew install python@3.11)."
fi
echo
echo "Conventions: docs/CONVENTIONS.md"
