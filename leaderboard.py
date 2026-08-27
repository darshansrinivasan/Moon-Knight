"""
Team and individual QC scoring over a date range.

Teams are `review_coverages` — the coverage groups admins already maintain for
sign-off. A coverage's assignees are its members and its reviewer is its lead,
so there is no second roster to keep in sync.

The metric is defined once, here, and every surface reads it from this module:

    in_scope   tickets whose state is not in rules.excluded_states()
    graded     in-scope tickets whose EFFECTIVE grade is Pass/Fail/Needs Review
    pass_rate  100 * pass / graded, or None when graded == 0

`pass_rate` is None — never 0 — for a team with nothing graded. Scoring an empty
team as 0% would rank it below a team that did the work and failed some of it,
which inverts the thing the leaderboard is for.

The effective grade is the latest human sign-off if one exists, else the AI
verdict, matching the dashboard. Reading ai_checks directly is what made Slack
and the dashboard disagree; do not reintroduce it here.
"""

import logging

import db

logger = logging.getLogger(__name__)

UNTEAMED = "No team"

# Latest sign-off per ticket, and the grade that actually applies. Constant SQL;
# no caller input is interpolated. Public because every surface that reports a
# grade must use these — reading ai_checks directly is what previously made two
# pages disagree. The underscored aliases below are kept for existing callers.
LATEST_REVIEW_SQL = """
    SELECT r.ticket_id, r.decision
    FROM ticket_reviews r
    JOIN (SELECT ticket_id, MAX(id) AS max_id
          FROM ticket_reviews GROUP BY ticket_id) x ON x.max_id = r.id
"""
EFFECTIVE_GRADE_SQL = (
    "COALESCE(CASE WHEN rev.decision IN ('Pass','Fail') THEN rev.decision END,"
    " ac.overall_result)"
)

# Kept so existing in-module references keep working; new code should use the
# public names above.
_LATEST_REVIEW = LATEST_REVIEW_SQL
_EFFECTIVE_GRADE = EFFECTIVE_GRADE_SQL

RULE_KEYS = ("r1", "r2", "r3", "r4", "r5", "r7", "r8")


def _pass_rate(pass_count: int, graded: int) -> float | None:
    """None when nothing was graded — see the module docstring."""
    if not graded:
        return None
    return round(pass_count / graded * 100, 1)


def _rank_key(row: dict) -> tuple:
    """Best first. Ungraded rows sort last; equal rates break on volume.

    More graded tickets at the same pass rate ranks higher: a 100% from three
    tickets should not outrank a 100% from ninety.
    """
    rate = row["pass_rate"]
    return (0 if rate is None else 1, rate or 0, row["graded"], )


def _team_membership() -> tuple[dict, dict, list]:
    """(assignee -> [team names], team name -> lead, warnings).

    An assignee may appear in more than one coverage. That is counted in every
    team they belong to but only once in the totals, and reported as a warning
    so the double-count is visible rather than silent.
    """
    with db.get_conn() as conn:
        rows = conn.execute("""
            SELECT c.name, c.reviewer_email, c.reviewer_name, a.assignee_name
            FROM review_coverages c
            LEFT JOIN review_coverage_assignees a ON a.coverage_id = c.id
            ORDER BY c.name
        """).fetchall()

    by_assignee: dict = {}
    leads: dict = {}
    for r in rows:
        leads[r["name"]] = {
            "lead_email": r["reviewer_email"],
            "lead_name": r["reviewer_name"] or r["reviewer_email"],
        }
        if r["assignee_name"]:
            by_assignee.setdefault(r["assignee_name"], []).append(r["name"])

    warnings = [
        f"{name} is in more than one team ({', '.join(teams)}); "
        "counted in each, once in the totals."
        for name, teams in sorted(by_assignee.items()) if len(teams) > 1
    ]
    return by_assignee, leads, warnings


def _load_rows(start: str | None, end: str | None) -> list[dict]:
    """Per-assignee aggregates over the range, with excluded states removed."""
    import rules as qc_rules

    # Always present: a ticket removed at source stops counting toward anyone's
    # standing, rather than freezing their rate at whatever it was.
    clauses = ["t.deleted_at IS NULL"]
    params: list = []
    if start and end:
        clauses.append("t.fetch_date BETWEEN ? AND ?")
        params += [start, end]

    excluded = qc_rules.excluded_states()
    if excluded:
        clauses.append(f"t.state NOT IN ({','.join('?' * len(excluded))})")
        params += excluded

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rule_sums = ",\n".join(
        f"SUM(CASE WHEN rc.{k} = 'Fail' THEN 1 ELSE 0 END) AS fail_{k}"
        for k in RULE_KEYS
    )

    with db.get_conn() as conn:
        rows = conn.execute(f"""
            SELECT
                COALESCE(t.assignee_name, 'Unassigned') AS assignee,
                COUNT(*)                                AS in_scope,
                SUM(CASE WHEN {_EFFECTIVE_GRADE} IN ('Pass','Fail','Needs Review')
                         THEN 1 ELSE 0 END)             AS graded,
                SUM(CASE WHEN {_EFFECTIVE_GRADE} = 'Pass'
                         THEN 1 ELSE 0 END)             AS pass_count,
                SUM(CASE WHEN {_EFFECTIVE_GRADE} = 'Fail'
                         THEN 1 ELSE 0 END)             AS fail_count,
                SUM(CASE WHEN {_EFFECTIVE_GRADE} = 'Needs Review'
                         THEN 1 ELSE 0 END)             AS review_count,
                {rule_sums}
            FROM tickets t
            LEFT JOIN ai_checks   ac ON ac.ticket_id = t.id
            LEFT JOIN rule_checks rc ON rc.ticket_id = t.id
            LEFT JOIN ({_LATEST_REVIEW}) rev ON rev.ticket_id = t.id
            {where}
            GROUP BY COALESCE(t.assignee_name, 'Unassigned')
        """, params).fetchall()

    return [dict(r) for r in rows]


def _blank(name: str) -> dict:
    return {
        "name": name, "in_scope": 0, "graded": 0,
        "pass": 0, "fail": 0, "review": 0,
        "rule_fails": {k: 0 for k in RULE_KEYS},
    }


def _accumulate(bucket: dict, row: dict) -> None:
    bucket["in_scope"] += row["in_scope"]
    bucket["graded"]   += row["graded"]
    bucket["pass"]     += row["pass_count"]
    bucket["fail"]     += row["fail_count"]
    bucket["review"]   += row["review_count"]
    for k in RULE_KEYS:
        bucket["rule_fails"][k] += row[f"fail_{k}"] or 0


def _finalise(bucket: dict) -> dict:
    bucket["pending"] = bucket["in_scope"] - bucket["graded"]
    bucket["pass_rate"] = _pass_rate(bucket["pass"], bucket["graded"])
    # Only non-zero rule failures are worth carrying to the UI.
    bucket["rule_fails"] = {k: v for k, v in bucket["rule_fails"].items() if v}
    return bucket


def _apportion_cost(start: str | None, end: str | None,
                    teams: list[dict], total_graded: int) -> float:
    """Split the range's scoring spend across teams by graded share.

    qc_runs records spend per DATE, not per ticket, so per-team cost cannot be
    measured — only apportioned. Callers must label it as such.
    """
    clauses = ["status IN ('success','partial')"]
    params: list = []
    if start and end:
        clauses.append("date BETWEEN ? AND ?")
        params += [start, end]

    with db.get_conn() as conn:
        row = conn.execute(
            f"SELECT COALESCE(SUM(cost_usd), 0) AS total,"
            f" MAX(COALESCE(cost_estimated, 0)) AS est"
            f" FROM qc_runs WHERE {' AND '.join(clauses)}",
            params,
        ).fetchone()

    total = float(row["total"] or 0)
    for team in teams:
        share = (team["graded"] / total_graded) if total_graded else 0
        team["cost_usd"] = round(total * share, 6)
        team["cost_apportioned"] = True
    return round(total, 6)


def _schedule_tz():
    """The timezone weeks are measured in — the one the schedule already uses."""
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    import vault
    name = vault.get_setting("schedule_tz") or "Asia/Kolkata"
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        logger.warning("Unknown timezone %r for weekly buckets, using UTC", name)
        return ZoneInfo("UTC")


def week_bounds(weeks: int) -> list[tuple[str, str]]:
    """The last `weeks` Monday–Sunday ranges, most recent first.

    Computed in the schedule timezone rather than with SQLite's strftime('%W'),
    which is UTC-based and would silently shift the boundary for an IST team —
    putting Monday-morning tickets in the previous week.
    """
    from datetime import datetime, timedelta

    today = datetime.now(_schedule_tz()).date()
    this_monday = today - timedelta(days=today.weekday())
    out = []
    for i in range(weeks):
        monday = this_monday - timedelta(weeks=i)
        out.append((monday.isoformat(), (monday + timedelta(days=6)).isoformat()))
    return out


def build_weekly(weeks: int = 8) -> dict:
    """Per-week standings plus each team's movement against the week before.

    A week with nothing graded reports pass_rate None and a null delta, so the
    UI can show a gap rather than a fabricated zero.
    """
    buckets = [
        {"start": start, "end": end, **build(start, end)}
        for start, end in week_bounds(weeks)
    ]

    # Deltas compare each week with the one before it (buckets[i + 1]).
    for i, bucket in enumerate(buckets):
        previous = buckets[i + 1] if i + 1 < len(buckets) else None
        prev_teams = {t["name"]: t for t in previous["teams"]} if previous else {}
        for team in bucket["teams"]:
            before = prev_teams.get(team["name"], {}).get("pass_rate")
            now = team["pass_rate"]
            team["previous_pass_rate"] = before
            team["delta"] = (round(now - before, 1)
                             if now is not None and before is not None else None)

    return {
        "weeks": weeks,
        "timezone": str(_schedule_tz()),
        "buckets": buckets,
    }


def build(start: str | None = None, end: str | None = None) -> dict:
    """Team and individual standings for a date range (inclusive), best first."""
    membership, leads, warnings = _team_membership()
    rows = _load_rows(start, end)

    teams: dict = {name: _blank(name) for name in leads}
    unteamed = _blank(UNTEAMED)
    people: list = []

    for row in rows:
        assignee = row["assignee"]
        team_names = membership.get(assignee, [])
        for name in team_names:
            _accumulate(teams[name], row)
        if not team_names:
            _accumulate(unteamed, row)

        people.append(_finalise({
            "name": assignee,
            # An assignee in several teams shows all of them.
            "team": ", ".join(team_names) if team_names else UNTEAMED,
            "in_scope": row["in_scope"], "graded": row["graded"],
            "pass": row["pass_count"], "fail": row["fail_count"],
            "review": row["review_count"],
            "rule_fails": {k: row[f"fail_{k}"] or 0 for k in RULE_KEYS},
        }))

    team_list = [_finalise(t) for t in teams.values()]
    for team in team_list:
        team.update(leads[team["name"]])
    unteamed = _finalise(unteamed)

    # Totals reconcile against the raw rows, counting each assignee once even
    # when they belong to several teams.
    totals = _blank("All")
    for row in rows:
        _accumulate(totals, row)
    totals = _finalise(totals)

    total_graded = sum(t["graded"] for t in team_list) + unteamed["graded"]
    range_cost = _apportion_cost(start, end, team_list + [unteamed], total_graded)

    team_list.sort(key=_rank_key, reverse=True)
    people.sort(key=_rank_key, reverse=True)

    return {
        "range": {"start": start, "end": end},
        "teams": team_list,
        "people": people,
        "unteamed": unteamed,
        "totals": totals,
        "range_cost_usd": range_cost,
        "warnings": warnings,
    }
