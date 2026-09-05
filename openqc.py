"""
QC over the open backlog: every ticket whose status is not terminal, whatever
day it was fetched on.

The day pipeline answers "how did Tuesday go" and cannot answer "is anything
open being mishandled right now" — a ticket fetched three weeks ago and still
open belongs to a date nobody is going to re-run. This module is that second
question. It is deliberately small in scope:

    open        state not in TERMINAL_STATES ('closed', 'archived'), plus
                whatever Admin already excludes from QC everywhere
                (rules.excluded_state_clause) — an out-of-scope status does not
                come back into scope by being open.
    all time    the default range. Open tickets are few; the value of the tab
                is precisely the ones that fell out of the daily window.
    refresh     scoring the fetch-time snapshot of an open ticket grades stale
                evidence, so a run refetches the backlog first — by id, since a
                date window cannot re-find an old ticket. A 404 on GET by id is
                a positive deletion signal (unlike absence from a window) and
                soft-deletes the ticket, except when it carries a human review.

Runs are filed in qc_runs under the label 'open' rather than a date, and every
grade is written under the ticket's own fetch_date — the run crosses dates and
must not tear a grade away from the day the rest of the app files it under.
Filters (dates, states) ride in config_json, so a run record stays
reconstructible from its own row.
"""

import asyncio
import json
import logging
from datetime import datetime, timezone

import db
import qc_runner
import resync_overall
import scorer
from drilldown import _require_iso_date, safe_link
from leaderboard import EFFECTIVE_GRADE_SQL, LATEST_REVIEW_SQL

logger = logging.getLogger(__name__)

# What the run is filed under in qc_runs.date. Not a date on purpose: stability
# comparisons key on this label, so consecutive backlog runs compare with each
# other and never with a day.
RUN_LABEL = "open"

# "Open" per the product definition: everything except these. Fixed, not a
# setting — the tab's promise is "not Closed, not Archived", and a config knob
# would let that promise drift silently.
TERMINAL_STATES = ("closed", "archived")

_STATE = "LOWER(COALESCE(t.state, ''))"


def open_scope(start: str | None = None, end: str | None = None,
               states: list[str] | None = None) -> tuple[str, list]:
    """SQL predicate + params selecting the open backlog (alias `t`).

    `states` narrows within the open set; a terminal state in it selects
    nothing rather than widening the scope — the NOT IN stays unconditional.
    Dates are inclusive and validated here because they are compared as
    strings: a malformed one would silently match the wrong rows.
    """
    marks = ",".join("?" * len(TERMINAL_STATES))
    conds = [f"{_STATE} NOT IN ({marks})"]
    params: list = [*TERMINAL_STATES]

    if start:
        conds.append("t.fetch_date >= ?")
        params.append(_require_iso_date(start, "start"))
    if end:
        conds.append("t.fetch_date <= ?")
        params.append(_require_iso_date(end, "end"))

    wanted = sorted({str(s).strip().lower() for s in (states or []) if str(s).strip()})
    if wanted:
        conds.append(f"{_STATE} IN ({','.join('?' * len(wanted))})")
        params += wanted

    return " AND ".join(conds), params


def _where(start, end, states) -> tuple[str, list]:
    """The full listing predicate: open scope, soft-delete, Admin exclusions."""
    import rules as qc_rules

    scope, params = open_scope(start, end, states)
    clauses = ["t.deleted_at IS NULL", scope]
    excl, extra = qc_rules.excluded_state_clause("t")
    if excl:
        clauses.append(excl)
        params += extra
    return " AND ".join(clauses), params


def list_open(start: str | None = None, end: str | None = None,
              states: list[str] | None = None) -> dict:
    """The open backlog for the tab: tickets, state counts, grade summary.

    State counts are computed over the date range but NOT the state filter, so
    the status pills keep their numbers while being toggled — a pill that
    zeroes everything else the moment it is selected cannot be un-selected by
    reading it. Grades are the effective grade, same as every other surface.
    """
    where, params = _where(start, end, states)
    counts_where, counts_params = _where(start, end, None)

    with db.get_conn() as conn:
        rows = conn.execute(f"""
            SELECT t.id AS ticket_id, t.number, t.title, t.link, t.state,
                   t.fetch_date, t.assignee_name,
                   {EFFECTIVE_GRADE_SQL} AS overall_result,
                   COALESCE(NULLIF(TRIM(rev.reviewer_name), ''),
                            rev.reviewer_email) AS signed_off_by,
                   rev.note AS review_note,
                   ac.checked_at
            FROM tickets t
            LEFT JOIN ai_checks ac ON ac.ticket_id = t.id
            LEFT JOIN ({LATEST_REVIEW_SQL}) rev ON rev.ticket_id = t.id
            WHERE {where}
            ORDER BY t.fetch_date DESC, t.number DESC
        """, params).fetchall()

        state_rows = conn.execute(f"""
            SELECT {_STATE} AS state, COUNT(*) AS n
            FROM tickets t
            WHERE {counts_where}
            GROUP BY {_STATE} ORDER BY {_STATE}
        """, counts_params).fetchall()

    tickets = []
    summary = {"total": 0, "pass": 0, "fail": 0, "review": 0, "pending": 0}
    for r in rows:
        grade = r["overall_result"]
        summary["total"] += 1
        if grade == "Pass":
            summary["pass"] += 1
        elif grade == "Fail":
            summary["fail"] += 1
        elif grade == "Needs Review":
            summary["review"] += 1
        else:
            summary["pending"] += 1
        tickets.append({
            "ticket_id": r["ticket_id"],
            "number": r["number"],
            "title": r["title"],
            "link": safe_link(r["link"]),
            "state": r["state"],
            "fetch_date": r["fetch_date"],
            "assignee_name": r["assignee_name"] or "Unassigned",
            "overall_result": grade,
            # Only meaningful when the latest sign-off IS the grade shown; a
            # review of Pass/Fail always is, per EFFECTIVE_GRADE_SQL.
            "signed_off_by": r["signed_off_by"],
            "review_note": r["review_note"] or None,
            "checked_at": r["checked_at"],
        })

    return {
        "range": {"start": start, "end": end},
        "states": states or None,
        "state_counts": [dict(r) for r in state_rows],
        "summary": summary,
        "tickets": tickets,
    }


def preview(start: str | None = None, end: str | None = None,
            states: list[str] | None = None) -> dict:
    """What a run would do — same eligibility function the run uses, so the
    number shown can never disagree with what happens.

    `eligible` counts fingerprint changes against the DB as it stands; a run
    that refreshes first may grade more, and the reason says so rather than
    letting the preview read as a promise.
    """
    scope, params = open_scope(start, end, states)
    in_scope = qc_runner._load_in_scope_where(scope, params)
    eligible = [t for t in in_scope if qc_runner._needs_scoring(t)]

    if not in_scope:
        reason = "No open tickets in scope."
    elif not eligible:
        reason = ("Every open ticket is already scored and unchanged — "
                  "a run with refresh may still find changes in Pylon.")
    else:
        reason = (f"{len(eligible)} of {len(in_scope)} open tickets changed or "
                  "were never scored; a refresh may add more.")

    return {
        "eligible": len(eligible),
        "in_scope": len(in_scope),
        "reason": reason,
        "spend": db.qc_spend_for_date(RUN_LABEL),
    }


def report_tickets(start: str | None = None, end: str | None = None,
                   states: list[str] | None = None) -> list[dict]:
    """Open tickets in the row shape `db.get_day_tickets` returns.

    The Slack report builder was written against that shape (grades, R-checks,
    ai_notes, account name), so the open-backlog report feeds it the same rows
    rather than teaching it a second one. Grades here are raw ai_checks values;
    the caller applies review.apply_effective_grades, exactly as the day
    report does.
    """
    where, params = _where(start, end, states)
    with db.get_conn() as conn:
        rows = conn.execute(f"""
            SELECT
                t.*,
                a.name  AS account_name,
                a.type  AS account_type,
                a.domain AS account_domain,
                rc.r1, rc.r2, rc.r3, rc.r4, rc.r5, rc.r6, rc.r7, rc.r8, rc.r9,
                ac.a1, ac.a2, ac.a3, ac.a4, ac.a5,
                ac.ai_notes, ac.overall_result, ac.checked_at AS ai_checked_at
            FROM tickets t
            LEFT JOIN accounts    a  ON t.account_id = a.id
            LEFT JOIN rule_checks rc ON t.id = rc.ticket_id
            LEFT JOIN ai_checks   ac ON t.id = ac.ticket_id
            WHERE {where}
            ORDER BY t.number
        """, params).fetchall()
    return [dict(r) for r in rows]


# ── refresh: refetch the backlog by id before grading it ─────────────────────

def _store_refreshed(fetched, date_by_id: dict[str, str]) -> dict:
    """Persist refreshed tickets, preserving each one's original fetch_date.

    Modeled on app.fetch_and_store's loop but deliberately separate: that loop
    owns day-shaped side effects (fetch_log, deletion sweeps keyed to a date's
    completeness) that must not run here, and a refresh must never move a
    ticket to a new date — fetch_date is the key every other surface files it
    under.
    """
    import rules as qc_rules

    now = datetime.now(timezone.utc).isoformat()
    rules_hash = qc_rules.rules_hash()
    stored = 0
    rescored = 0
    failures: list = []

    user_cache: dict[str, tuple[str, str]] = {}
    for msgs in fetched.messages_by_id.values():
        for m in msgs:
            u = (m.get("author") or {}).get("user")
            if u and u.get("id"):
                user_cache[u["id"]] = (
                    (m.get("author") or {}).get("name", ""),
                    u.get("email", ""),
                )

    with db.get_conn() as conn:
        for acc in fetched.accounts_by_id.values():
            conn.execute("""
                INSERT OR REPLACE INTO accounts
                    (id, name, domain, type, custom_fields, fetched_at)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (acc["id"], acc.get("name"), acc.get("domain"), acc.get("type"),
                  json.dumps(acc.get("custom_fields") or {}), now))

        for uid, (name, email) in user_cache.items():
            conn.execute(
                "INSERT OR IGNORE INTO users (id, name, email) VALUES (?, ?, ?)",
                (uid, name, email))

        for issue in fetched.issues:
            fetch_date = date_by_id.get(issue["id"])
            if not fetch_date:
                # Not a ticket we asked about — never invent a date for it.
                continue

            msgs     = fetched.messages_by_id.get(issue["id"], [])
            cf       = issue.get("custom_fields") or {}
            assignee = issue.get("assignee") or {}
            account  = issue.get("account") or {}

            assignee_name = None
            if assignee.get("id"):
                cached = user_cache.get(assignee["id"])
                if cached:
                    assignee_name = cached[0]

            priority = issue.get("priority")
            if not priority:
                priority = (cf.get("priority") or {}).get("value") or \
                           (cf.get("priority") or {}).get("interpreted_value")

            ext_issues = issue.get("external_issues") or []
            cpv = issue.get("customer_portal_visible")
            conn.execute("""
                INSERT OR REPLACE INTO tickets
                    (id, number, fetch_date, title, link, state, source, type,
                     priority, assignee_id, assignee_name, account_id,
                     custom_fields, external_issues, body_html,
                     created_at, updated_at, latest_message_time,
                     customer_portal_visible, fetched_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                issue["id"], issue.get("number"), fetch_date,
                issue.get("title"), issue.get("link"),
                issue.get("state"), issue.get("source"), issue.get("type"),
                priority, assignee.get("id"), assignee_name, account.get("id"),
                json.dumps(cf), json.dumps(ext_issues), issue.get("body_html"),
                issue.get("created_at"), issue.get("updated_at"),
                issue.get("latest_message_time"),
                1 if cpv else 0, now,
            ))
            stored += 1

            # Same rule as the day fetch: an archived ticket keeps its fresh
            # state row so the dashboard is honest, but is not scored.
            if issue.get("state") == "archived":
                continue

            for m in msgs:
                author  = m.get("author") or {}
                contact = author.get("contact") or {}
                user_   = author.get("user") or {}
                is_cust = "contact" in author
                email   = contact.get("email") or user_.get("email")
                conn.execute("""
                    INSERT OR REPLACE INTO messages
                        (id, ticket_id, message_html, timestamp, source,
                         author_name, author_email, is_customer, is_private)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (m["id"], issue["id"], m.get("message_html"),
                      m.get("timestamp"), m.get("source"),
                      author.get("name"), email,
                      1 if is_cust else 0, 1 if m.get("is_private") else 0))

            # R-checks read absence as evidence, so an incomplete refresh for
            # this ticket keeps its previous scores rather than guessing.
            if not fetched.is_complete(issue):
                logger.warning(
                    "Skipping rule rescore for #%s — incomplete refresh",
                    issue.get("number"))
                continue

            acc_data = fetched.accounts_by_id.get(account.get("id"))
            try:
                scores = scorer.score_all(issue, msgs, acc_data, ext_issues)
            except Exception:
                logger.exception("Rule scoring failed for #%s", issue.get("number"))
                failures.append(issue.get("number"))
                continue

            conn.execute("""
                INSERT OR REPLACE INTO rule_checks
                    (ticket_id, fetch_date, r1, r2, r3, r4, r5, r7, r8, r9,
                     checked_at, rules_hash)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (issue["id"], fetch_date,
                  scores["r1"], scores["r2"], scores["r3"], scores["r4"],
                  scores["r5"], scores["r7"], scores["r8"], scores["r9"],
                  now, rules_hash))
            rescored += 1

    return {"stored": stored, "rescored": rescored, "failures": failures}


def _mark_missing(missing_ids: set[str]) -> dict:
    """Soft-delete tickets Pylon 404'd by id.

    Unlike the day sweep this needs no completeness proof — a 404 on the id is
    the tombstone Pylon otherwise lacks. The one rule kept from the sweep: a
    ticket carrying a human review is never marked, because a sign-off is a
    record of someone's decision and refetching cannot bring it back.
    """
    if not missing_ids:
        return {"deleted": 0, "kept_reviewed": 0}

    now = datetime.now(timezone.utc).isoformat()
    marks = ",".join("?" * len(missing_ids))
    ids = sorted(missing_ids)
    with db.get_conn() as conn:
        rows = conn.execute(f"""
            SELECT t.id, EXISTS(SELECT 1 FROM ticket_reviews r
                                WHERE r.ticket_id = t.id) AS reviewed
            FROM tickets t WHERE t.id IN ({marks})
        """, ids).fetchall()
        to_mark = [r["id"] for r in rows if not r["reviewed"]]
        kept    = [r["id"] for r in rows if r["reviewed"]]
        if to_mark:
            conn.executemany(
                "UPDATE tickets SET deleted_at = ? WHERE id = ?",
                [(now, tid) for tid in to_mark])
    if kept:
        logger.info("Kept %d missing ticket(s) that carry a human review: %s",
                    len(kept), ", ".join(kept[:10]))
    return {"deleted": len(to_mark), "kept_reviewed": len(kept)}


async def refresh_open(start: str | None = None, end: str | None = None,
                       states: list[str] | None = None) -> dict:
    """Refetch the open backlog from Pylon by id; rescore rules; sync grades.

    Returns counts, never raises for a partially failed refresh — a ticket
    that could not be refreshed simply keeps its snapshot and is graded (or
    skipped) on that, exactly as an incomplete day fetch behaves.
    """
    import pylon

    where, params = _where(start, end, states)
    with db.get_conn() as conn:
        rows = conn.execute(
            f"SELECT t.id, t.fetch_date FROM tickets t WHERE {where}",
            params).fetchall()
    date_by_id = {r["id"]: r["fetch_date"] for r in rows}
    if not date_by_id:
        return {"requested": 0, "stored": 0, "rescored": 0,
                "deleted": 0, "kept_reviewed": 0, "failed": 0}

    fetched = await pylon.fetch_tickets_by_id(sorted(date_by_id))
    result = await asyncio.to_thread(_store_refreshed, fetched, date_by_id)
    missing = await asyncio.to_thread(_mark_missing, fetched.missing_ids)

    # R-scores may have changed under already-graded tickets; each affected
    # date resyncs its overall verdicts, same as after a day fetch.
    affected = sorted({date_by_id[i["id"]] for i in fetched.issues
                       if i["id"] in date_by_id}
                      | {date_by_id[t] for t in fetched.missing_ids
                         if t in date_by_id})
    for d in affected:
        await asyncio.to_thread(resync_overall.run, d)

    return {
        "requested": len(date_by_id),
        "stored": result["stored"],
        "rescored": result["rescored"],
        "deleted": missing["deleted"],
        "kept_reviewed": missing["kept_reviewed"],
        "failed": len(fetched.failed_ids),
        "scoring_failures": result["failures"] or None,
    }


# ── re-fetch: discover the whole open backlog, not just the known ids ────────

def _ist_fetch_date(created_at: str | None) -> str:
    """The IST calendar day a ticket was created — its natural fetch_date.

    The day pipeline keys tickets by the IST day they were created in, so a
    discovered ticket is filed exactly where a day-fetch of that date would
    have put it. An unparsable or missing created_at files under today rather
    than dropping the ticket: a slightly misfiled ticket is recoverable, an
    unfetched one is invisible.
    """
    from pylon import _IST

    try:
        stamp = datetime.fromisoformat(str(created_at).replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        return stamp.astimezone(_IST).date().isoformat()
    except (TypeError, ValueError):
        logger.warning("Unparsable created_at %r — filing under today", created_at)
        return datetime.now(_IST).date().isoformat()


async def refetch_open() -> dict:
    """Pull the entire open backlog from Pylon, including never-fetched tickets.

    `refresh_open` can only refresh ids the database already knows, so a ticket
    created on a day nobody fetched stayed invisible forever — 155 tickets
    locally against 240+ in Pylon. This is the discovery half: a state-filtered
    SEARCH (no date window) finds everything currently open, upserts it, and
    files new tickets under their creation day.

    When the search proved complete, known-open tickets it did NOT return are
    refreshed by id: they either changed state (recorded as such, leaving the
    open set honestly) or 404 (soft-deleted, reviews always kept). fetch_log is
    deliberately untouched — it records what the DAY pipeline fetched, and a
    backlog sweep claiming a day was fetched would lie to the calendar.
    """
    import pylon

    fetched = await pylon.fetch_open_issues(TERMINAL_STATES)

    def existing_dates() -> dict:
        with db.get_conn() as conn:
            return {r["id"]: r["fetch_date"] for r in
                    conn.execute("SELECT id, fetch_date FROM tickets")}

    known = await asyncio.to_thread(existing_dates)
    date_by_id = {}
    new_ids = []
    for issue in fetched.issues:
        kept = known.get(issue["id"])
        if not kept:
            new_ids.append(issue["id"])
        date_by_id[issue["id"]] = kept or _ist_fetch_date(issue.get("created_at"))

    result = await asyncio.to_thread(_store_refreshed, fetched, date_by_id)

    # Known-open tickets the search did not return: no longer open, or gone.
    # Only a complete search may say so — a dropped page is neither.
    stale = {"stored": 0, "deleted": 0, "kept_reviewed": 0, "checked": 0}
    if fetched.issues_complete:
        where, params = _where(None, None, None)
        returned = set(date_by_id)

        def leftover_ids() -> dict:
            with db.get_conn() as conn:
                return {r["id"]: r["fetch_date"] for r in conn.execute(
                    f"SELECT t.id, t.fetch_date FROM tickets t WHERE {where}",
                    params) if r["id"] not in returned}

        leftovers = await asyncio.to_thread(leftover_ids)
        if leftovers:
            refreshed = await pylon.fetch_tickets_by_id(sorted(leftovers))
            upd = await asyncio.to_thread(_store_refreshed, refreshed, leftovers)
            missing = await asyncio.to_thread(_mark_missing, refreshed.missing_ids)
            stale = {"stored": upd["stored"], "checked": len(leftovers),
                     **missing}
            date_by_id.update(leftovers)

    affected = sorted(set(date_by_id.values()))
    for d in affected:
        await asyncio.to_thread(resync_overall.run, d)

    return {
        "found_open": len(fetched.issues),
        "new": len(new_ids),
        "stored": result["stored"],
        "rescored": result["rescored"],
        "search_complete": fetched.issues_complete,
        "no_longer_open_checked": stale["checked"],
        "deleted": stale["deleted"],
        "kept_reviewed": stale["kept_reviewed"],
        "scoring_failures": result["failures"] or None,
    }


def run(triggered_by: str, start: str | None = None, end: str | None = None,
        states: list[str] | None = None) -> dict:
    """AI-score the open backlog's eligible tickets. Filed under RUN_LABEL."""
    scope, params = open_scope(start, end, states)
    extra = {
        "open_backlog": True,
        "filters": {"start": start, "end": end,
                    "states": sorted(states) if states else None},
    }
    return qc_runner._execute_run(RUN_LABEL, triggered_by, scope, params,
                                  config_extra=extra)
