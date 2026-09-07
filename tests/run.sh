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

# One place that runs a suite, so the stderr policy is not duplicated.
#
# stderr is hidden by default: the suites print their own OK/FAIL lines and a
# local run wants those uncluttered. CI needs the opposite — a suite that dies
# on an import or a traceback prints nothing to stdout, so hiding stderr turns a
# real crash into "SUITES FAILED: t_x" with no cause. Set QC_TEST_STDERR=1 there.
run_suite() {
    if [ -n "$QC_TEST_STDERR" ]; then
        QC_DB_PATH="$TMP/$1.db" QC_MASTER_KEY="$KEY" \
            PYTHONPATH="$PWD" "$PYTHON" "tests/$1.py"
    else
        QC_DB_PATH="$TMP/$1.db" QC_MASTER_KEY="$KEY" \
            PYTHONPATH="$PWD" "$PYTHON" "tests/$1.py" 2>/dev/null
    fi
}

for suite in t_scorer t_evidence t_vault t_sched t_grades t_cost t_rescore t_cleanup t_reap t_theme t_calendar t_leaderboard t_drilldown t_open t_funcheck t_report t_share t_suggestions t_prompts t_dryrun t_rdryrun t_logger t_rulecfg t_status t_roles t_slack t_route t_lb_http t_http t_weekly t_weekly_http; do
    printf '\n═══ %s ═══\n' "$suite"
    if run_suite "$suite"; then
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
