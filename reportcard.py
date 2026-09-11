"""
Report Card: the day's grade of record, frozen at the scheduled morning run.

The live dashboard answers "is this ticket healthy NOW?" — every refetch and
re-run overwrites its grades, which is right for driving fixes and wrong for
accountability: a rep who failed at 09:30 could fix the tag at 11:00, get
re-scored, and the morning's Fail had never existed as far as the leaderboard
knew. The scoreboard was retroactively editable.

This module freezes the record instead of freezing the work:

* `capture(date)` copies the board — grade, AND the assignee/state/title as of
  that run, because a snapshot that joins live ticket data at read time lets a
  reassignment move a Fail onto someone else's frozen scoreboard — into
  insert-once rows. Only the SCHEDULED pipeline captures (the scheduler is the
  one trigger nobody can game: fixed time, once per date, before anyone has
  seen the results). A date whose scheduled run never completed is a visible
  hole, deliberately: the support team is 24/7, so "first manual run of the
  day" is not a fair notary.
* Nothing ever UPDATEs snapshot rows. The one sanctioned override is a human
  review: the single `ticket_reviews` stream is overlaid at read time, so a
  lead's verdict (named, dated, note required) changes the record everywhere
  at once — frozen and live — without touching the frozen base grade.
* The delta between frozen and current grades is the remediation story:
  Fail→Pass is REMEDIATED (praise it), Fail→Fail OUTSTANDING, Pass→Fail
  REGRESSED (r11 aging shows up here). Rewardable metrics are computed from
  exactly this delta, so the number leads are rewarded on is the one number
  nobody can game by editing tickets after the fact.
"""

import csv
import io
import logging
from datetime import date as date_cls, datetime, timedelta, timezone

import db
from leaderboard import EFFECTIVE_GRADE_SQL, LATEST_REVIEW_SQL

logger = logging.getLogger(__name__)

# Verdict columns frozen per ticket. r6/r9 are dead and stay out.
_CHECK_COLS = ("r1", "r2", "r3", "r4", "r5", "r7", "r8", "r10", "r11",
               "a1", "a2", "a3", "a4", "a5")

# Effective grade over SNAPSHOT rows: the same review-over-machine precedence
# as the live EFFECTIVE_GRADE_SQL, with the frozen overall as the base.
_FROZEN_GRADE = ("COALESCE(CASE WHEN rev.decision IN ('Pass','Fail')"
                 " THEN rev.decision END, st.overall_result)")


def capture(date_str: str, run_id: int | None = None,
            created_by: str = "scheduler") -> dict:
    """Freeze one day's board, once. A second call is a no-op by design.

    Copies every in-scope ticket of the day with its grades and its attribution
    (assignee, state, title, account) as of this moment. Insert-only: the
    UNIQUE date key plus the existing-snapshot guard means a re-run, however
    triggered, can never rewrite what the morning found.
    """
    with db.get_conn() as conn:
        if conn.execute("SELECT 1 FROM day_snapshots WHERE snapshot_date = ?",
                        (date_str,)).fetchone():
            return {"date": date_str, "captured": False,
                    "reason": "snapshot already exists"}

        rows = _board_rows(conn, date_str)
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "INSERT INTO day_snapshots (snapshot_date, run_id, created_at,"
            " ticket_count, created_by) VALUES (?, ?, ?, ?, ?)",
            (date_str, run_id, now, len(rows), created_by))
        cols = ["snapshot_date", "ticket_id", "number", "title", "link",
                "state", "assignee_name", "account_name", "source",
                *_CHECK_COLS, "overall_result", "ai_notes"]
        conn.executemany(
            f"INSERT INTO snapshot_tickets ({', '.join(cols)})"
            f" VALUES ({', '.join('?' for _ in cols)})",
            [(date_str, r["id"], r["number"], r["title"], r["link"],
              r["state"], r["assignee_name"], r["account_name"], r["source"],
              *(r[c] for c in _CHECK_COLS), r["overall_result"], r["ai_notes"])
             for r in rows])
    logger.info("Report Card snapshot captured for %s: %d tickets",
                date_str, len(rows))
    return {"date": date_str, "captured": True, "tickets": len(rows)}


def _board_rows(conn, date_str: str) -> list[dict]:
    """The day's in-scope board exactly as the live dashboard would count it."""
    import rules as qc_rules
    clause, extra = qc_rules.excluded_state_clause("t")
    scope = f" AND {clause}" if clause else ""
    check_sel = ", ".join(f"rc.{c}" for c in _CHECK_COLS if c.startswith("r"))
    a_sel = ", ".join(f"ac.{c}" for c in _CHECK_COLS if c.startswith("a"))
    rows = conn.execute(f"""
        SELECT t.id, t.number, t.title, t.link, t.state, t.assignee_name,
               t.source, a.name AS account_name,
               {check_sel}, {a_sel},
               ac.overall_result, ac.ai_notes
        FROM tickets t
        LEFT JOIN accounts    a  ON a.id = t.account_id
        LEFT JOIN rule_checks rc ON rc.ticket_id = t.id
        LEFT JOIN ai_checks   ac ON ac.ticket_id = t.id
        WHERE t.fetch_date = ? AND t.deleted_at IS NULL {scope}
        ORDER BY t.number
    """, (date_str, *extra)).fetchall()
    return [dict(r) for r in rows]


def snapshot_dates(limit: int = 400) -> list[dict]:
    """Captured dates newest-first, plus the holes between them.

    A hole — a fetched day inside the snapshot era with no snapshot — is shown,
    not hidden: an unexplained gap reads as data loss, a labelled one reads as
    "the scheduled run never completed for this date".
    """
    with db.get_conn() as conn:
        snaps = [dict(r) for r in conn.execute(
            "SELECT snapshot_date, created_at, ticket_count, created_by"
            " FROM day_snapshots ORDER BY snapshot_date DESC LIMIT ?",
            (limit,)).fetchall()]
        if not snaps:
            return []
        first = snaps[-1]["snapshot_date"]
        fetched = {r["fetch_date"] for r in conn.execute(
            "SELECT fetch_date FROM fetch_log WHERE fetch_date >= ?",
            (first,)).fetchall()}
    have = {s["snapshot_date"] for s in snaps}
    out = [{"date": s["snapshot_date"], "hole": False,
            "created_at": s["created_at"], "tickets": s["ticket_count"],
            "created_by": s.get("created_by") or "scheduler"}
           for s in snaps]
    out += [{"date": d, "hole": True} for d in fetched - have
            if d <= max(have)]
    out.sort(key=lambda x: x["date"], reverse=True)
    return out


def day(date_str: str) -> dict:
    """One frozen day with the review overlay and the remediation delta.

    Every ticket carries THREE grades: frozen (what the morning run found —
    immutable), effective (frozen with the latest human review on top — the
    official record), and current (what the live board says now). frozen vs
    current is the remediation story; current never changes the record.
    """
    with db.get_conn() as conn:
        snap = conn.execute(
            "SELECT * FROM day_snapshots WHERE snapshot_date = ?",
            (date_str,)).fetchone()
        if snap is None:
            fetched = conn.execute(
                "SELECT 1 FROM fetch_log WHERE fetch_date = ?",
                (date_str,)).fetchone()
            return {"date": date_str, "snapshot": None,
                    "hole": bool(fetched)}
        rows = [dict(r) for r in conn.execute(f"""
            SELECT st.*,
                   {_FROZEN_GRADE} AS effective_result,
                   rev.decision AS review_decision,
                   rev.reviewer_name, rev.note AS review_note,
                   rev.check_overrides AS _overrides_json,
                   cur.overall_result AS current_result,
                   cur.checked_at    AS current_checked_at,
                   {EFFECTIVE_GRADE_SQL.replace('ac.', 'cur.')} AS current_effective
            FROM snapshot_tickets st
            LEFT JOIN ({LATEST_REVIEW_SQL}) rev ON rev.ticket_id = st.ticket_id
            LEFT JOIN ai_checks cur ON cur.ticket_id = st.ticket_id
            WHERE st.snapshot_date = ?
            ORDER BY st.number
        """, (date_str,)).fetchall()]

    summary = {"total": len(rows), "pass": 0, "fail": 0, "review": 0,
               "pending": 0, "remediated": 0, "outstanding": 0,
               "regressed": 0}
    for r in rows:
        r["check_overrides"] = _row_overrides(r)
        grade = r["effective_result"]
        if grade == "Pass":
            summary["pass"] += 1
        elif grade == "Fail":
            summary["fail"] += 1
        elif grade == "Needs Review":
            summary["review"] += 1
        else:
            summary["pending"] += 1
        r["delta"] = _delta(r)
        if r["delta"] in summary:
            summary[r["delta"]] += 1
    return {"date": date_str, "hole": False,
            "snapshot": {"created_at": snap["created_at"],
                         "run_id": snap["run_id"],
                         "created_by": snap["created_by"] or "scheduler"},
            "summary": summary, "tickets": rows}


def frozen_ticket(date_str: str, number: int) -> dict:
    """One ticket's frozen record — the shared review sheet's data contract.

    Same row shape day() produces, for one ticket, so the Open tab's embedded
    sheet and the Report Card's sheet can never disagree about what a frozen
    record contains. Missing states are explicit: `hole` (no snapshot for the
    date) and `ticket: None` (snapshotted day, but this ticket was fetched
    after the freeze).
    """
    with db.get_conn() as conn:
        snap = conn.execute(
            "SELECT * FROM day_snapshots WHERE snapshot_date = ?",
            (date_str,)).fetchone()
        if snap is None:
            fetched = conn.execute(
                "SELECT 1 FROM fetch_log WHERE fetch_date = ?",
                (date_str,)).fetchone()
            return {"date": date_str, "snapshot": None,
                    "hole": bool(fetched), "ticket": None}
        row = conn.execute(f"""
            SELECT st.*,
                   {_FROZEN_GRADE} AS effective_result,
                   rev.decision AS review_decision,
                   rev.reviewer_name, rev.note AS review_note,
                   rev.check_overrides AS _overrides_json,
                   cur.overall_result AS current_result,
                   cur.checked_at    AS current_checked_at,
                   {EFFECTIVE_GRADE_SQL.replace('ac.', 'cur.')} AS current_effective
            FROM snapshot_tickets st
            LEFT JOIN ({LATEST_REVIEW_SQL}) rev ON rev.ticket_id = st.ticket_id
            LEFT JOIN ai_checks cur ON cur.ticket_id = st.ticket_id
            WHERE st.snapshot_date = ? AND st.number = ?
        """, (date_str, number)).fetchone()
    t = dict(row) if row else None
    if t:
        t["check_overrides"] = _row_overrides(t)
        t["delta"] = _delta(t)
    return {"date": date_str, "hole": False,
            "snapshot": {"created_at": snap["created_at"],
                         "created_by": snap["created_by"] or "scheduler"},
            "ticket": t}


def _row_overrides(r: dict) -> dict:
    """The active review's per-check adjudications for this row, parsed.

    Applied only while a Pass/Fail review is active — a revert clears the
    check-level record along with the overall, one lifecycle for both.
    """
    import review as review_mod
    raw = r.pop("_overrides_json", None)
    if r.get("review_decision") not in ("Pass", "Fail"):
        return {}
    return review_mod.parse_check_overrides(raw)


def effective_check(r: dict, key: str):
    """One check's verdict with any lead adjudication applied."""
    return (r.get("check_overrides") or {}).get(key, r.get(key))


def _delta(r: dict) -> str:
    """frozen → current, as the remediation vocabulary the page shows."""
    frozen, current = r.get("overall_result"), r.get("current_effective")
    if frozen == "Fail" and current == "Pass":
        return "remediated"
    if frozen == "Fail" and current == "Fail":
        return "outstanding"
    if frozen == "Pass" and current == "Fail":
        return "regressed"
    return "unchanged"


def _range_dates(start: str, end: str) -> tuple[list[str], list[str]]:
    """(snapshot dates, fetched-but-unsnapshotted dates) inside a range."""
    with db.get_conn() as conn:
        have = [r["snapshot_date"] for r in conn.execute(
            "SELECT snapshot_date FROM day_snapshots"
            " WHERE snapshot_date BETWEEN ? AND ?", (start, end)).fetchall()]
        fetched = [r["fetch_date"] for r in conn.execute(
            "SELECT fetch_date FROM fetch_log"
            " WHERE fetch_date BETWEEN ? AND ?", (start, end)).fetchall()]
    return sorted(have), sorted(set(fetched) - set(have))


def leaderboard(start: str, end: str) -> dict:
    """Per-person frozen scoreboard over a range, ticket-weighted.

    Honest mixed history rather than an empty board: dates inside the range
    with no snapshot (pre-feature, or scheduler holes) fall back to LIVE
    grades and the payload says exactly how many days did so — a marked
    approximation beats a silent one, and beats a blank page.
    """
    frozen_days, live_days = _range_dates(start, end)
    people: dict[str, dict] = {}

    def bump(name, grade):
        p = people.setdefault(name or "Unassigned",
                              {"pass": 0, "fail": 0, "review": 0, "graded": 0})
        if grade == "Pass":
            p["pass"] += 1
        elif grade == "Fail":
            p["fail"] += 1
        elif grade == "Needs Review":
            p["review"] += 1
        if grade in ("Pass", "Fail", "Needs Review"):
            p["graded"] += 1

    with db.get_conn() as conn:
        if frozen_days:
            marks = ",".join("?" for _ in frozen_days)
            for r in conn.execute(f"""
                SELECT st.assignee_name AS name,
                       {_FROZEN_GRADE} AS grade
                FROM snapshot_tickets st
                LEFT JOIN ({LATEST_REVIEW_SQL}) rev
                       ON rev.ticket_id = st.ticket_id
                WHERE st.snapshot_date IN ({marks})
            """, frozen_days).fetchall():
                bump(r["name"], r["grade"])
        if live_days:
            import rules as qc_rules
            clause, extra = qc_rules.excluded_state_clause("t")
            scope = f" AND {clause}" if clause else ""
            marks = ",".join("?" for _ in live_days)
            for r in conn.execute(f"""
                SELECT t.assignee_name AS name,
                       {EFFECTIVE_GRADE_SQL} AS grade
                FROM tickets t
                LEFT JOIN ai_checks ac ON ac.ticket_id = t.id
                LEFT JOIN ({LATEST_REVIEW_SQL}) rev ON rev.ticket_id = t.id
                WHERE t.fetch_date IN ({marks}) AND t.deleted_at IS NULL {scope}
            """, (*live_days, *extra)).fetchall():
                bump(r["name"], r["grade"])

    board = []
    for name, p in people.items():
        rate = round(p["pass"] / p["graded"] * 100, 1) if p["graded"] else None
        board.append({"name": name, **p, "pass_rate": rate})
    board.sort(key=lambda x: (-(x["pass_rate"] or -1), -x["graded"]))
    return {"start": start, "end": end, "people": board,
            "frozen_days": len(frozen_days), "live_days": len(live_days)}


def analytics(start: str, end: str) -> dict:
    """Per-person frozen verdict split — the Report Card's analytics lens."""
    return leaderboard(start, end)


def backfill(start: str, end: str, created_by: str) -> dict:
    """Capture every fetched-but-unsnapshotted date in the range, once each.

    The admin's cold-start tool: on day one nothing is frozen, and a Report
    Card with no history teaches nobody anything. Each captured day freezes the
    grades AS THEY STAND NOW — for old dates that already includes post-hoc
    fixes, which is why created_by is stored and shown: a backfilled record is
    honest history-from-today, never passed off as the morning notary. Existing
    snapshots are skipped, never replaced.
    """
    have, missing = _range_dates(start, end)
    done = [capture(d, created_by=created_by) for d in missing]
    captured = [d["date"] for d in done if d.get("captured")]
    return {"start": start, "end": end,
            "captured": captured, "count": len(captured),
            # Days already frozen inside the range: reported so the admin sees
            # "kept" rather than wondering why the count is short.
            "skipped_existing": len(have)}


# ── 1:1 coaching view ─────────────────────────────────────────────────────────
# The lead's per-person story over a range: which checks keep failing, how the
# days went, what got fixed vs what's still standing — frozen basis (with the
# marked live fallback), adjudications applied, so the numbers are the same
# ones the record shows and a lead's own corrections are respected.

_PATTERN_CHECKS = ("r1", "r2", "r3", "r4", "r5", "r7", "r8", "r10", "r11",
                   "a1", "a3", "a4", "a5")
_ADVISORY = {"r10", "r11"}
_FAIL_VALUES = {"Fail", "Poor", "Inaccurate", "Inconsistent"}


def _person_rows(person: str, start: str, end: str) -> tuple[list[dict], int, int]:
    """(rows, frozen_days, live_days) for one assignee over a range.

    Frozen rows carry the FROZEN attribution (a reassignment cannot move a
    fail into or out of someone's 1:1); live-fallback rows exist only for
    dates without a snapshot and are marked `basis: live`.
    """
    frozen_days, live_days = _range_dates(start, end)
    rows: list[dict] = []
    with db.get_conn() as conn:
        if frozen_days:
            marks = ",".join("?" for _ in frozen_days)
            for r in conn.execute(f"""
                SELECT st.*, st.snapshot_date AS day,
                       {_FROZEN_GRADE} AS effective_result,
                       rev.decision AS review_decision,
                       rev.reviewer_name, rev.note AS review_note,
                       rev.check_overrides AS _overrides_json,
                       {EFFECTIVE_GRADE_SQL.replace('ac.', 'cur.')} AS current_effective
                FROM snapshot_tickets st
                LEFT JOIN ({LATEST_REVIEW_SQL}) rev ON rev.ticket_id = st.ticket_id
                LEFT JOIN ai_checks cur ON cur.ticket_id = st.ticket_id
                WHERE st.snapshot_date IN ({marks}) AND st.assignee_name = ?
            """, (*frozen_days, person)).fetchall():
                row = dict(r)
                row["basis"] = "frozen"
                row["check_overrides"] = _row_overrides(row)
                row["delta"] = _delta(row)
                rows.append(row)
        if live_days:
            import rules as qc_rules
            clause, extra = qc_rules.excluded_state_clause("t")
            scope = f" AND {clause}" if clause else ""
            marks = ",".join("?" for _ in live_days)
            check_cols = ", ".join(
                f"rc.{c}" for c in _PATTERN_CHECKS if c.startswith("r"))
            a_cols = ", ".join(
                f"ac.{c}" for c in _PATTERN_CHECKS if c.startswith("a"))
            for r in conn.execute(f"""
                SELECT t.id AS ticket_id, t.number, t.title, t.link, t.state,
                       t.assignee_name, t.fetch_date AS day,
                       {check_cols}, {a_cols}, ac.overall_result, ac.ai_notes,
                       {EFFECTIVE_GRADE_SQL} AS effective_result,
                       rev.decision AS review_decision,
                       rev.reviewer_name, rev.note AS review_note,
                       rev.check_overrides AS _overrides_json
                FROM tickets t
                LEFT JOIN rule_checks rc ON rc.ticket_id = t.id
                LEFT JOIN ai_checks   ac ON ac.ticket_id = t.id
                LEFT JOIN ({LATEST_REVIEW_SQL}) rev ON rev.ticket_id = t.id
                WHERE t.fetch_date IN ({marks}) AND t.deleted_at IS NULL
                  AND t.assignee_name = ? {scope}
            """, (*live_days, person, *extra)).fetchall():
                row = dict(r)
                row["basis"] = "live"
                row["check_overrides"] = _row_overrides(row)
                row["delta"] = "unchanged"
                rows.append(row)
    return rows, len(frozen_days), len(live_days)


def _check_fails(row: dict) -> list[str]:
    """The checks this row fails, AFTER lead adjudications."""
    return [k for k in _PATTERN_CHECKS
            if effective_check(row, k) in _FAIL_VALUES]


def one_on_one(person: str, start: str, end: str) -> dict:
    """Everything a lead needs on one screen for a 1:1, deterministically."""
    rows, frozen_days, live_days = _person_rows(person, start, end)

    span = (date_cls.fromisoformat(end) - date_cls.fromisoformat(start)).days + 1
    prev_end = (date_cls.fromisoformat(start) - timedelta(days=1)).isoformat()
    prev_start = (date_cls.fromisoformat(start)
                  - timedelta(days=span)).isoformat()
    prev_rows, _, _ = _person_rows(person, prev_start, prev_end)

    def graded(rs):
        return [r for r in rs if r.get("effective_result")
                in ("Pass", "Fail", "Needs Review")]

    cur_g, prev_g = graded(rows), graded(prev_rows)

    def rate(rs):
        n = len(rs)
        return round(sum(1 for r in rs
                         if r["effective_result"] == "Pass") / n * 100, 1) if n else None

    # Per-check patterns, current vs previous, worst first, with examples.
    def fail_counts(rs):
        out = {k: 0 for k in _PATTERN_CHECKS}
        for r in rs:
            for k in _check_fails(r):
                out[k] += 1
        return out

    cur_fails, prev_fails = fail_counts(rows), fail_counts(prev_rows)
    patterns = []
    for k in _PATTERN_CHECKS:
        if not cur_fails[k] and not prev_fails[k]:
            continue
        examples = [{
            "number": r["number"], "title": r["title"], "date": r["day"],
            "delta": r["delta"], "basis": r["basis"],
            # The frozen advice note is the WHY — a coaching brief without it
            # can only restate counts.
            "note": (r.get("ai_notes") or "")[:280],
        } for r in sorted(rows, key=lambda x: x["day"], reverse=True)
          if k in _check_fails(r)][:3]
        patterns.append({
            "key": k, "advisory": k in _ADVISORY,
            "curr": cur_fails[k], "prev": prev_fails[k],
            "delta": cur_fails[k] - prev_fails[k],
            "examples": examples,
        })
    patterns.sort(key=lambda p: (-p["curr"], p["key"]))

    # Daily timeline + day-of-week clustering (both, as asked).
    daily: dict = {}
    dow = {i: {"graded": 0, "fails": 0} for i in range(7)}
    for r in rows:
        day = r["day"]
        cell = daily.setdefault(day, {"date": day, "graded": 0, "fails": 0,
                                      "checks": {}})
        is_graded = r.get("effective_result") in ("Pass", "Fail", "Needs Review")
        failing = _check_fails(r)
        if is_graded:
            cell["graded"] += 1
        if r.get("effective_result") == "Fail":
            cell["fails"] += 1
        for k in failing:
            cell["checks"][k] = cell["checks"].get(k, 0) + 1
        wd = date_cls.fromisoformat(day).weekday()
        if is_graded:
            dow[wd]["graded"] += 1
            if r.get("effective_result") == "Fail":
                dow[wd]["fails"] += 1

    # Remediation split over the frozen rows: fixed vs still standing.
    remediated = sum(1 for r in rows if r["delta"] == "remediated")
    outstanding = sum(1 for r in rows if r["delta"] == "outstanding")

    return {
        "person": person,
        "start": start, "end": end,
        "prev_start": prev_start, "prev_end": prev_end,
        "basis": {"frozen_days": frozen_days, "live_days": live_days},
        "summary": {
            "graded": len(cur_g),
            "fails": sum(1 for r in cur_g if r["effective_result"] == "Fail"),
            "pass_rate": rate(cur_g),
            "prev_graded": len(prev_g),
            "prev_fails": sum(1 for r in prev_g
                              if r["effective_result"] == "Fail"),
            "prev_pass_rate": rate(prev_g),
            "remediated": remediated,
            "outstanding": outstanding,
            "r10_misses": cur_fails.get("r10", 0),
            "r11_silences": cur_fails.get("r11", 0),
        },
        "patterns": patterns,
        "daily": sorted(daily.values(), key=lambda x: x["date"]),
        "day_of_week": [
            {"day": name, "graded": dow[i]["graded"], "fails": dow[i]["fails"]}
            for i, name in enumerate(
                ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"))],
    }


# ── rewardable metrics ────────────────────────────────────────────────────────
# Numbers a reward can safely hang on, computed only from the frozen record and
# the live delta — so they measure what people DID about what the morning
# found, not who managed to rewrite the morning. Ticket-weighted throughout:
# an average of personal percentages lets a 3-ticket day outvote a 40-ticket
# one.

def rewards(weeks: int = 4) -> dict:
    """Per-person rewardable metrics over the last `weeks` ISO weeks.

    remediation_rate  of tickets the machine froze as Fail, % now OFFICIALLY
                      Pass — via a fix that re-scored, or a lead's reviewed
                      verdict. Same comparison as the Frozen Dashboard's
                      "remediated since" chip, so the two can never disagree.
                      The denominator is the RAW frozen grade on purpose: a
                      review resolves a fail, it must not erase that the
                      morning found one.
    wow_improvement   this week's frozen pass rate minus last week's,
                      in percentage points. Needs two weeks of snapshots;
                      None (shown as "insufficient history") until then.
    frozen_pass_rate  context: the official pass rate over the window.
    """
    today = date_cls.today()
    start = (today - timedelta(weeks=weeks)).isoformat()
    end = today.isoformat()

    with db.get_conn() as conn:
        rows = [dict(r) for r in conn.execute(f"""
            SELECT st.snapshot_date, st.assignee_name AS name,
                   st.overall_result AS frozen_raw,
                   {_FROZEN_GRADE} AS frozen_grade,
                   {EFFECTIVE_GRADE_SQL.replace('ac.', 'cur.')} AS current_grade
            FROM snapshot_tickets st
            LEFT JOIN ({LATEST_REVIEW_SQL}) rev ON rev.ticket_id = st.ticket_id
            LEFT JOIN ai_checks cur ON cur.ticket_id = st.ticket_id
            WHERE st.snapshot_date BETWEEN ? AND ?
        """, (start, end)).fetchall()]

    def week_of(d: str) -> str:
        y, w, _ = date_cls.fromisoformat(d).isocalendar()
        return f"{y}-W{w:02d}"

    this_week = week_of(end)
    last_week = week_of((today - timedelta(days=7)).isoformat())

    people: dict[str, dict] = {}
    for r in rows:
        p = people.setdefault(r["name"] or "Unassigned", {
            "graded": 0, "passed": 0, "frozen_fails": 0, "remediated": 0,
            "weeks": {}})
        g = r["frozen_grade"]
        if g in ("Pass", "Fail", "Needs Review"):
            p["graded"] += 1
            wk = p["weeks"].setdefault(week_of(r["snapshot_date"]),
                                       {"graded": 0, "passed": 0})
            wk["graded"] += 1
            if g == "Pass":
                p["passed"] += 1
                wk["passed"] += 1
        # Remediation keys on the RAW frozen grade — the machine's morning
        # verdict — matching the dashboard's delta chip. The official pass
        # rate above stays on the reviewed grade; the two answer different
        # questions and each stays consistent with its own surface.
        if r["frozen_raw"] == "Fail":
            p["frozen_fails"] += 1
            if r["current_grade"] == "Pass":
                p["remediated"] += 1

    out = []
    for name, p in people.items():
        rate = lambda wk: (round(wk["passed"] / wk["graded"] * 100, 1)
                           if wk and wk["graded"] else None)
        cur_rate, prev_rate = rate(p["weeks"].get(this_week)), rate(p["weeks"].get(last_week))
        out.append({
            "name": name,
            "graded": p["graded"],
            "frozen_pass_rate": (round(p["passed"] / p["graded"] * 100, 1)
                                 if p["graded"] else None),
            "frozen_fails": p["frozen_fails"],
            "remediated": p["remediated"],
            "remediation_rate": (round(p["remediated"] / p["frozen_fails"] * 100, 1)
                                 if p["frozen_fails"] else None),
            "wow_improvement": (round(cur_rate - prev_rate, 1)
                                if cur_rate is not None and prev_rate is not None
                                else None),
        })
    out.sort(key=lambda x: (-(x["remediation_rate"] or -1), -x["graded"]))
    return {"start": start, "end": end, "weeks": weeks, "people": out}


def one_on_one_brief(person: str, start: str, end: str,
                     triggered_by: str) -> dict:
    """A short Gemini-written coaching brief over the 1:1 data. Real money —
    only ever on an explicit button press, cost returned and filed in qc_runs
    under '1on1:<start>' exactly like the report chat's spend."""
    import json as json_mod

    from qc_runner import PLAIN_TEXT, RunStats, _call_gemini

    data = one_on_one(person, start, end)
    if not data["summary"]["graded"] and not data["patterns"]:
        raise ValueError("No graded tickets in this period — nothing to summarize")

    payload = {k: data[k] for k in ("summary", "patterns", "daily",
                                    "day_of_week")}
    prompt = (
        f"You are preparing a support team lead for a 1:1 with {person}.\n"
        f"Period {start}..{end}, compared to {data['prev_start']}..{data['prev_end']}.\n"
        "Data (QC check failures; 'advisory' items are habits, not ticket "
        "failures; 'remediated' means fixed after being caught):\n"
        f"{json_mod.dumps(payload)}\n\n"
        "Write a coaching brief in plain text, under 250 words, three "
        "sections: Strengths (call out real positives — improvements, "
        "remediation), Patterns (the 2-3 recurring miss types, with counts "
        "and trend vs previous period), Talking points (specific, kind, "
        "actionable — reference ticket numbers from the examples). Never "
        "invent numbers or tickets not present in the data."
    )
    stats = RunStats()
    text = _call_gemini(
        prompt, stats,
        system=("You are an experienced, kind support team lead preparing "
                "for a 1:1. You write short, specific, human coaching briefs "
                "in plain prose — no JSON, no markdown syntax (no ** or #), "
                "no invented numbers."),
        # Thinking tokens bill against this cap on 2.5 models; 2048 truncated
        # the brief mid-sentence.
        schema=PLAIN_TEXT, max_output=8192)
    now = _utc_now_iso()
    with db.get_conn() as conn:
        conn.execute("""
            INSERT INTO qc_runs (date, triggered_by, started_at, finished_at,
                                 status, total, scored, skipped, model_used,
                                 prompt_tokens, output_tokens, cost_usd,
                                 cached_tokens, thought_tokens, cost_estimated,
                                 config_json)
            VALUES (?, ?, ?, ?, 'success', ?, 1, 0, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (f"1on1:{start}", triggered_by, now, now,
              data["summary"]["graded"], stats.model_summary(),
              stats.prompt_tokens, stats.output_tokens,
              round(stats.cost_usd(), 6), stats.cached_tokens,
              stats.thought_tokens, 1 if stats.cost_is_estimated() else 0,
              json_mod.dumps({"one_on_one": True, "person": person,
                              "start": start, "end": end})))
    return {"brief": text.strip(), "cost_usd": round(stats.cost_usd(), 6),
            "model": stats.model_summary(),
            "cost_estimated": stats.cost_is_estimated()}


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── CSV export ────────────────────────────────────────────────────────────────

def day_csv(date_str: str) -> str:
    """The frozen day as raw rows — the data the evaluation used, verbatim.

    Grades, attribution and notes; conversation text stays out (the link column
    is the way in). Formula-escaped like every other export: titles and notes
    are customer/model text and Excel executes leading = + - @.
    """
    d = day(date_str)
    if not d.get("snapshot"):
        raise ValueError(f"No snapshot exists for {date_str}")

    def safe(v):
        s = "" if v is None else str(v)
        return "'" + s if s[:1] in ("=", "+", "-", "@") else s

    cols = ["number", "title", "assignee_name", "state", "account_name",
            "source", *_CHECK_COLS, "overall_result", "effective_result",
            "review_decision", "reviewer_name", "review_note",
            "current_effective", "delta", "ai_notes", "link"]
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\r\n")
    w.writerow(cols)
    for r in d["tickets"]:
        w.writerow([safe(r.get(c)) for c in cols])
    return buf.getvalue()
