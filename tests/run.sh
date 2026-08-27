#!/bin/sh
# Run every suite against a throwaway database.
#
# These are plain scripts, not pytest: they need no dependency beyond the app's
# own requirements, and each exits non-zero on the first failed assertion.
#
#   ./tests/run.sh
#
# Each suite pins behaviour that was actually broken in production. Read the
# section headers before changing an expected value — a "failing" assertion here
# usually means a real regression, not a stale test.
set -e
cd "$(dirname "$0")/.."

PYTHON=${PYTHON:-python3}
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

# A throwaway key: these suites write encrypted settings to a throwaway database.
KEY=$("$PYTHON" -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")
FAILED=""

for suite in t_scorer t_vault t_sched t_grades t_cost t_rescore t_cleanup t_theme t_calendar t_leaderboard t_slack t_route t_lb_http t_http; do
    printf '\n═══ %s ═══\n' "$suite"
    if QC_DB_PATH="$TMP/$suite.db" QC_MASTER_KEY="$KEY" \
       PYTHONPATH="$PWD" "$PYTHON" "tests/$suite.py" 2>/dev/null; then
        :
    else
        FAILED="$FAILED $suite"
    fi
done

printf '\n'
if [ -n "$FAILED" ]; then
    echo "SUITES FAILED:$FAILED"
    exit 1
fi
echo "ALL SUITES PASSED"
