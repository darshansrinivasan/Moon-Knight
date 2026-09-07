"""Week-over-week support operations dashboard.

Builds the `D` payload in docs/SPEC_weekly.md from the QC ticket store.
No Gemini, no Pylon, no writes.
"""

from __future__ import annotations

import json
import logging
import math
import statistics
from collections import defaultdict
from datetime import date, datetime, timedelta

import db
import leaderboard
import rules as qc_rules
import scorer

logger = logging.getLogger(__name__)

WEEK_CURR = "Current Week"
WEEK_PREV = "Previous Week"

STATUS_ORDER = (
    "Closed",
    "On customer",
    "Waiting on Engg",
    "Investigating",
    "On you",
    "Waiting on CSM",
    "On hold",
    "Waiting on Legal",
    "Waiting on Product",
)

STATUS_MAP = {
    "closed": "Closed",
    "waiting_on_customer": "On customer",
    "waiting_on_engg": "Waiting on Engg",
    "waiting_on_engineering": "Waiting on Engg",
    "investigating": "Investigating",
    "waiting_on_you": "On you",
    "new": "On you",
    "waiting_on_csm": "Waiting on CSM",
    "on_hold": "On hold",
    "waiting_on_legal": "Waiting on Legal",
    "waiting_on_product": "Waiting on Product",
}

PRIORITY_ORDER = ("Urgent", "High", "Medium", "Low", "Unknown")
PRIORITY_MAP = {
    "urgent": "Urgent",
    "high": "High",
    "medium": "Medium",
    "med": "Medium",
    "low": "Low",
}

ENG_WAIT = {"waiting_on_engg", "waiting_on_engineering"}
TERMINAL = {"closed", "archived"}
WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")

CAT_LIMIT = 12
CUST_LIMIT = 10
ESC_CAT_LIMIT = 8
MAX_PERIOD_DAYS = 31


def _tz():
    return leaderboard._schedule_tz()


def _now(now: datetime | None) -> datetime:
    if now is None:
        return datetime.now(_tz())
    if now.tzinfo is None:
        return now.replace(tzinfo=_tz())
    return now.astimezone(_tz())


def monday_of(day: date) -> date:
    return day - timedelta(days=day.weekday())


def resolve_week_start(week: str | None, *, now: datetime | None = None) -> date:
    """Monday of the requested current week, clamped to this week."""
    today = _now(now).date()
    this_monday = monday_of(today)
    if not week:
        return this_monday
    try:
        requested = date.fromisoformat(week)
    except ValueError:
        raise ValueError("week must be YYYY-MM-DD") from None
    monday = monday_of(requested)
    if monday > this_monday:
        return this_monday
    return monday


def resolve_period(
    week: str | None = None,
    start: str | None = None,
    end: str | None = None,
    *,
    now: datetime | None = None,
) -> tuple[date, date, date, date]:
    """Current [start, end] and the equal-length previous period.

    `start`+`end` win when both are set. Otherwise `week` (or today) selects
    a Monday–Sunday. End is clamped to today; a range longer than
    MAX_PERIOD_DAYS is rejected.
    """
    today = _now(now).date()
    if start or end:
        if not start or not end:
            raise ValueError("start and end are required together")
        try:
            curr_start = date.fromisoformat(start)
            curr_end = date.fromisoformat(end)
        except ValueError:
            raise ValueError("start and end must be YYYY-MM-DD") from None
        if curr_start > curr_end:
            raise ValueError("start must not be after end")
        if curr_start > today:
            raise ValueError("start cannot be in the future")
    else:
        monday = resolve_week_start(week, now=now)
        curr_start = monday
        curr_end = monday + timedelta(days=6)

    span = (curr_end - curr_start).days + 1
    if span < 1:
        raise ValueError("period must include at least one day")
    if span > MAX_PERIOD_DAYS:
        raise ValueError(f"period cannot exceed {MAX_PERIOD_DAYS} days")

    prev_end = curr_start - timedelta(days=1)
    prev_start = prev_end - timedelta(days=span - 1)
    return curr_start, curr_end, prev_start, prev_end


def _range_label(start: date, end: date) -> str:
    if start == end:
        try:
            return start.strftime("%b %-d, %Y")
        except ValueError:
            return start.strftime("%b %d, %Y").replace(" 0", " ")
    if (end - start).days == 6 and start.weekday() == 0:
        return _week_label(start)
    try:
        left = start.strftime("%b %-d")
        if start.month == end.month and start.year == end.year:
            right = end.strftime("%-d, %Y")
        elif start.year == end.year:
            right = end.strftime("%b %-d, %Y")
        else:
            right = end.strftime("%b %-d, %Y")
            left = start.strftime("%b %-d, %Y")
    except ValueError:
        left = start.strftime("%b %d").replace(" 0", " ")
        if start.month == end.month and start.year == end.year:
            right = f"{end.day}, {end.year}"
        else:
            right = end.strftime("%b %d, %Y").replace(" 0", " ")
    return f"{left}-{right}"


def _day_labels(start: date, n: int) -> list[str]:
    if n == 7 and start.weekday() == 0:
        return list(WEEKDAYS)
    labels = []
    for i in range(n):
        day = start + timedelta(days=i)
        try:
            labels.append(day.strftime("%b %-d"))
        except ValueError:
            labels.append(day.strftime("%b %d").replace(" 0", " "))
    return labels


def _week_label(monday: date) -> str:
    sunday = monday + timedelta(days=6)
    left = monday.strftime("%b %-d") if monday.day else monday.strftime("%b %d")
    # %-d is POSIX; Windows would choke, but this app runs on mac/linux.
    try:
        left = monday.strftime("%b %-d")
        right = (
            sunday.strftime("%-d, %Y")
            if monday.month == sunday.month and monday.year == sunday.year
            else sunday.strftime("%b %-d, %Y")
        )
    except ValueError:
        left = monday.strftime("%b %d").replace(" 0", " ")
        if monday.month == sunday.month and monday.year == sunday.year:
            right = f"{sunday.day}, {sunday.year}"
        else:
            right = sunday.strftime("%b %d, %Y").replace(" 0", " ")
    return f"{left}-{right}"


def _parse_ts(value) -> datetime | None:
    return scorer._parse_ts(value)


def _local_date(ts: datetime | None, tz) -> date | None:
    if ts is None:
        return None
    return ts.astimezone(tz).date()


def _cf(cf: dict, role: str):
    key = qc_rules.field(role)
    raw = cf.get(key)
    if raw is None:
        return None
    if isinstance(raw, dict):
        return scorer._cf_val(raw)
    return raw


def _cf_yes(cf: dict, role: str) -> bool:
    val = _cf(cf, role)
    if val is None:
        return False
    return str(val).strip().lower() in {"yes", "true", "1"}


def _humanize(raw) -> str:
    if raw is None or str(raw).strip() == "":
        return "Unknown"
    text = str(raw).strip()
    if " " in text or any(c.isupper() for c in text[1:]):
        return text
    return text.replace("_", " ").replace("-", " ").title()


def _canon_category(raw) -> str:
    """The synced Pylon label for a category slug, else _humanize's guess."""
    import funcheck
    value = (str(raw).strip() if raw is not None else "")
    if not value:
        return "Unknown"
    label = funcheck.canon("category", value)
    return label if label != value else _humanize(value)


def _priority(raw) -> str:
    if not raw:
        return "Unknown"
    return PRIORITY_MAP.get(str(raw).strip().lower(), "Unknown")


def _status_label(state: str | None) -> str | None:
    if not state:
        return None
    return STATUS_MAP.get(state.strip().lower())


def _is_escalated(state: str | None, cf: dict) -> bool:
    st = (state or "").strip().lower()
    if st in ENG_WAIT:
        return True
    cat = str(_cf(cf, "request_category") or "").strip().lower()
    if cat and (cat in qc_rules.oncall_categories() or cat.startswith("oncall")):
        return True
    reso = str(_cf(cf, "resolution_category") or "").strip().lower()
    if "escalat" in reso:
        return True
    if _cf_yes(cf, "rootly_exists"):
        return True
    if _cf(cf, "rootly_reference"):
        return True
    return False


def _pct(curr, prev):
    if prev in (None, 0):
        return None
    if curr is None:
        return None
    return round((curr - prev) / prev * 100, 1)


def _diff(curr, prev):
    if curr is None or prev is None:
        return None
    return curr - prev


def _mean(xs: list[float]) -> float | None:
    return round(statistics.fmean(xs), 3) if xs else None


def _median(xs: list[float]) -> float | None:
    return round(statistics.median(xs), 3) if xs else None


def _minmax(xs: list[float], fn) -> float | None:
    return round(fn(xs), 3) if xs else None


def _pctile(xs: list[float], p: float, *, min_n: int = 1) -> float | None:
    if len(xs) < min_n:
        return None
    if len(xs) == 1:
        return round(xs[0], 3)
    ordered = sorted(xs)
    k = (len(ordered) - 1) * (p / 100)
    lo = math.floor(k)
    hi = min(lo + 1, len(ordered) - 1)
    frac = k - lo
    return round(ordered[lo] + (ordered[hi] - ordered[lo]) * frac, 3)


def _secs_to_mins(v):
    return None if v is None else round(v / 60, 2)


def _secs_to_hrs(v):
    return None if v is None else round(v / 3600, 2)


def _first_support_reply(messages: list[dict]) -> datetime | None:
    dated = []
    for m in messages:
        if m.get("is_private") or m.get("is_customer"):
            continue
        if scorer._is_bot_ack(m):
            continue
        ts = _parse_ts(m.get("timestamp"))
        if ts is not None:
            dated.append(ts)
    return min(dated) if dated else None


def _load_tickets(since: date) -> tuple[list[dict], dict[str, list[dict]]]:
    bound = since.isoformat()
    with db.get_conn() as conn:
        rows = conn.execute(
                """
            SELECT t.id, t.number, t.title, t.link, t.state, t.type, t.priority,
                   t.assignee_name, t.account_id, t.custom_fields, t.created_at,
                   t.updated_at, t.fetch_date, t.deleted_at, t.csat_responses,
                   a.name AS account_name
            FROM tickets t
            LEFT JOIN accounts a ON a.id = t.account_id
            WHERE t.deleted_at IS NULL
              AND COALESCE(t.state, '') != 'archived'
              AND (
                    COALESCE(t.created_at, t.fetch_date) >= ?
                 OR COALESCE(t.updated_at, '') >= ?
                 OR COALESCE(t.fetch_date, '') >= ?
              )
            """,
            (bound, bound, bound),
        ).fetchall()
        ids = [r["id"] for r in rows]
        msgs: dict[str, list[dict]] = defaultdict(list)
        if ids:
            q = ",".join("?" * len(ids))
            for m in conn.execute(
                f"""
                SELECT ticket_id, message_html, timestamp, is_customer, is_private
                FROM messages
                WHERE ticket_id IN ({q})
                """,
                ids,
            ):
                msgs[m["ticket_id"]].append(dict(m))
    return [dict(r) for r in rows], msgs


def _created_at(row: dict, tz) -> datetime | None:
    ts = _parse_ts(row.get("created_at"))
    if ts is not None:
        return ts
    day = row.get("fetch_date")
    if not day:
        return None
    try:
        return datetime.fromisoformat(day).replace(tzinfo=tz)
    except ValueError:
        return None


def _annotate(row: dict, messages: list[dict], tz, sla: float,
              now: datetime) -> dict | None:
    created = _created_at(row, tz)
    if created is None:
        return None
    try:
        cf = json.loads(row.get("custom_fields") or "{}")
    except (TypeError, json.JSONDecodeError):
        cf = {}
    if not isinstance(cf, dict):
        cf = {}

    state = (row.get("state") or "").strip().lower()
    updated = _parse_ts(row.get("updated_at"))
    reply_at = _first_support_reply(messages)
    frt = None
    if reply_at is not None and reply_at >= created:
        frt = (reply_at - created).total_seconds()

    closed = state == "closed"
    res_secs = None
    resolved_day = None
    if closed and updated is not None and updated >= created:
        res_secs = (updated - created).total_seconds()
        resolved_day = _local_date(updated, tz)

    owed = qc_rules.status_policy(state).get("r4_reply_owed", True)
    sla_secs = sla * 3600
    sla_breached = False
    if frt is not None:
        sla_breached = frt > sla_secs
    elif owed:
        clock = now if now.tzinfo else now.replace(tzinfo=created.tzinfo)
        if created.tzinfo and clock.tzinfo != created.tzinfo:
            clock = clock.astimezone(created.tzinfo)
        age = (clock - created).total_seconds()
        sla_breached = age > sla_secs

    assignee = (row.get("assignee_name") or "").strip() or "Unassigned"
    created_day = _local_date(created, tz)
    csat_events = []
    csat_raw = list(normalize_csat_items(row.get("csat_responses")))
    if not csat_raw:
        csat_raw = _csat_from_custom_fields(cf)
    for item in csat_raw:
        day = _local_date(_parse_ts(item.get("submitted_at")), tz)
        if day is None:
            continue
        csat_events.append({
            "score": item["score"],
            "day": day,
            "assignee": assignee,
            "submitted_at": item.get("submitted_at") or "",
            "ticket_id": row["id"],
        })

    return {
        "id": row["id"],
        "number": row.get("number"),
        "title": row.get("title") or "",
        "link": row.get("link") or "",
        "state": state,
        "status": _status_label(state) or (row.get("state") or "").replace("_", " ").title(),
        "type": row.get("type") or "",
        "priority": _priority(row.get("priority")),
        "assignee": assignee,
        "account": (row.get("account_name") or "").strip() or "Unknown",
        # Pylon's own label when the synced map knows the slug — the weekly
        # and monthly pages must name a category identically. _humanize stays
        # as the fallback for values the map has never seen.
        "category": _canon_category(_cf(cf, "request_category")),
        "created": created,
        "created_day": created_day,
        "resolved_day": resolved_day,
        "frt_secs": frt,
        "res_secs": res_secs,
        "is_resolved": closed,
        "is_open": state not in TERMINAL,
        "is_escalated": _is_escalated(state, cf),
        "is_reopened": False,
        "sla_breached": sla_breached,
        "csat_events": csat_events,
    }


def _empty_csat() -> dict:
    return {
        "total": 0,
        "avg": None,
        "star5": 0,
        "star4": 0,
        "low": 0,
        "positivePct": None,
        "agents": [],
    }


def _int_score(value) -> int | None:
    if value is None or value == "":
        return None
    try:
        n = int(round(float(value)))
    except (TypeError, ValueError):
        return None
    if 1 <= n <= 5:
        return n
    return None


def normalize_csat_items(raw) -> list[dict]:
    """Issue-level or survey-shaped CSAT into {score, comment, submitted_at}."""
    if not raw:
        return []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            return []
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, list):
        return []
    out = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        score = _int_score(
            item.get("score") or item.get("csat_score") or item.get("rating")
        )
        if score is None:
            typed = []
            loose = []
            for ans in item.get("answers") or []:
                if not isinstance(ans, dict):
                    continue
                qtype = str(ans.get("question_type") or "").lower()
                val = _int_score(ans.get("value") or ans.get("score"))
                if val is None:
                    continue
                if qtype in {"score", "csat", "rating", ""} or "csat" in qtype:
                    typed.append(val)
                else:
                    loose.append(val)
            score = (typed or loose or [None])[0]
        if score is None:
            continue
        comment = item.get("comment")
        if not comment:
            for ans in item.get("answers") or []:
                if not isinstance(ans, dict):
                    continue
                if str(ans.get("question_type") or "").lower() == "comment":
                    comment = ans.get("value")
                    break
        out.append({
            "score": score,
            "comment": comment or "",
            "submitted_at": item.get("submitted_at") or "",
        })
    return out


def _csat_from_custom_fields(cf: dict) -> list[dict]:
    """Some workspaces put the score on a custom field instead of surveys."""
    if not isinstance(cf, dict):
        return []
    out = []
    for key, raw in cf.items():
        slug = str(key).lower()
        if not any(tok in slug for tok in ("csat", "satisfaction", "survey_score")):
            continue
        val = scorer._cf_val(raw) if isinstance(raw, dict) else raw
        score = _int_score(val)
        if score is not None:
            out.append({"score": score, "comment": "", "submitted_at": ""})
    return out


def csat_json_for_store(issue: dict, existing_json: str | None = None) -> str:
    """Merge issue-level CSAT into what the column already holds.

    Merge, never replace: store_csat_responses folds survey responses (which
    can carry comments the issue payload lacks) into the same column, and a
    plain refetch of the day used to overwrite that merged list with the
    thinner issue-level one — silently losing the survey comments until the
    next weekly view happened to re-fetch them.
    """
    incoming = normalize_csat_items(issue.get("csat_responses"))
    if not incoming:
        return existing_json or "[]"
    merged = {json.dumps(x, sort_keys=True): x
              for x in normalize_csat_items(existing_json)}
    for item in incoming:
        merged[json.dumps(item, sort_keys=True)] = item
    return json.dumps(list(merged.values()))


def store_csat_responses(rows: list[dict]) -> int:
    """Persist survey rows onto csat_events and matching tickets.

    Responses without an issue_id still count — CSAT is a survey, not a
    ticket field. Returns events written.
    """
    now = datetime.now().isoformat()
    written = 0
    by_ticket: dict[str, list[dict]] = defaultdict(list)
    with db.get_conn() as conn:
        for i, row in enumerate(rows or []):
            items = normalize_csat_items(row)
            if not items:
                continue
            issue = row.get("issue") if isinstance(row.get("issue"), dict) else {}
            issue_id = row.get("issue_id") or issue.get("id")
            number = row.get("issue_number") or issue.get("number")
            ticket = None
            if issue_id:
                ticket = conn.execute(
                    "SELECT id, number, assignee_name, csat_responses FROM tickets WHERE id = ?",
                    (str(issue_id),),
                ).fetchone()
            if ticket is None and number is not None:
                ticket = conn.execute(
                    "SELECT id, number, assignee_name, csat_responses FROM tickets WHERE number = ?",
                    (number,),
                ).fetchone()
            assignee = (ticket["assignee_name"] if ticket else None) or "Unassigned"
            tid = ticket["id"] if ticket else issue_id
            for j, item in enumerate(items):
                eid = str(row.get("id") or f"{tid or 'orphan'}-{item.get('submitted_at')}-{j}-{i}")
                conn.execute(
                    """
                    INSERT OR REPLACE INTO csat_events
                        (id, issue_id, ticket_number, assignee, score, comment,
                         submitted_at, fetched_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        eid, tid, ticket["number"] if ticket else number,
                        assignee, item["score"], item.get("comment") or "",
                        item.get("submitted_at") or "", now,
                    ),
                )
                written += 1
                if tid:
                    by_ticket[tid].append(item)
        for tid, items in by_ticket.items():
            existing = conn.execute(
                "SELECT csat_responses FROM tickets WHERE id = ?",
                (tid,),
            ).fetchone()
            if existing is None:
                continue
            merged = {json.dumps(x, sort_keys=True): x
                      for x in normalize_csat_items(existing["csat_responses"])}
            for item in items:
                merged[json.dumps(item, sort_keys=True)] = item
            conn.execute(
                "UPDATE tickets SET csat_responses = ? WHERE id = ?",
                (json.dumps(list(merged.values())), tid),
            )
    return written


def _csat_events_from_store(tz) -> list[dict]:
    """Every stored CSAT response, dated only by submitted_at.

    Ticket created/updated is ignored — a July ticket whose survey was
    filled this week belongs in this week. Rows without submitted_at are
    dropped rather than bucketed by created_at.
    """
    events = []
    seen = set()
    try:
        with db.get_conn() as conn:
            survey_rows = conn.execute(
                """
                SELECT e.id, e.issue_id, e.score, e.submitted_at, e.assignee,
                       t.assignee_name AS ticket_assignee
                FROM csat_events e
                LEFT JOIN tickets t ON t.id = e.issue_id
                """
            ).fetchall()
            ticket_rows = conn.execute(
                """
                SELECT id, assignee_name, csat_responses
                FROM tickets
                WHERE deleted_at IS NULL
                  AND COALESCE(state, '') != 'archived'
                  AND csat_responses IS NOT NULL
                  AND csat_responses != ''
                  AND csat_responses != '[]'
                """
            ).fetchall()
    except Exception:
        return []
    for row in survey_rows:
        day = _local_date(_parse_ts(row["submitted_at"]), tz)
        if day is None:
            continue
        seen.add((str(row["issue_id"] or ""), row["submitted_at"] or "", row["score"]))
        events.append({
            "score": row["score"],
            "day": day,
            "assignee": (row["ticket_assignee"] or row["assignee"] or "Unassigned").strip()
                        or "Unassigned",
        })
    for row in ticket_rows:
        assignee = (row["assignee_name"] or "Unassigned").strip() or "Unassigned"
        for item in normalize_csat_items(row["csat_responses"]):
            submitted = item.get("submitted_at") or ""
            day = _local_date(_parse_ts(submitted), tz)
            if day is None:
                continue
            key = (str(row["id"]), submitted, item["score"])
            if key in seen:
                continue
            seen.add(key)
            events.append({
                "score": item["score"],
                "day": day,
                "assignee": assignee,
            })
    return events


def _csat_award(avg: float | None, n: int) -> str | None:
    if avg is None or n <= 0:
        return None
    if avg >= 4.8 and n >= 2:
        return "Champion"
    if avg >= 4.5:
        return "Top Performer"
    if avg >= 4.0:
        return "Good"
    if avg >= 3.5:
        return "Needs Improvement"
    return "Needs Attention"


def _csat_pack(events: list[dict]) -> dict:
    if not events:
        return _empty_csat()
    scores = [e["score"] for e in events]
    star5 = sum(1 for s in scores if s == 5)
    star4 = sum(1 for s in scores if s == 4)
    low = sum(1 for s in scores if s <= 3)
    avg = round(statistics.fmean(scores), 2)
    pos = star5 + star4
    by_agent: dict[str, list[int]] = defaultdict(list)
    for e in events:
        by_agent[e["assignee"]].append(e["score"])
    agents = []
    for name, xs in sorted(by_agent.items(), key=lambda kv: (-statistics.fmean(kv[1]), kv[0])):
        a5 = sum(1 for s in xs if s == 5)
        a4 = sum(1 for s in xs if s == 4)
        alow = sum(1 for s in xs if s <= 3)
        aavg = round(statistics.fmean(xs), 2)
        agents.append({
            "name": name,
            "total": len(xs),
            "scores": xs,
            "star5": a5,
            "star4": a4,
            "low": alow,
            "positivePct": round(100 * (a5 + a4) / len(xs), 1),
            "avg": aavg,
            "award": _csat_award(aavg, len(xs)),
        })
    return {
        "total": len(scores),
        "avg": avg,
        "star5": star5,
        "star4": star4,
        "low": low,
        "positivePct": round(100 * pos / len(scores), 1),
        "agents": agents,
    }


def _top_breakdown(rows: list[dict], key: str, limit: int, *,
                   curr_start: date, curr_end: date,
                   prev_start: date, prev_end: date,
                   extra_labels: tuple = ()) -> dict:
    curr_n: dict[str, int] = defaultdict(int)
    prev_n: dict[str, int] = defaultdict(int)
    for r in rows:
        label = r[key]
        if r["created_day"] and curr_start <= r["created_day"] <= curr_end:
            curr_n[label] += 1
        if r["created_day"] and prev_start <= r["created_day"] <= prev_end:
            prev_n[label] += 1

    if extra_labels:
        labels = list(extra_labels)
        return {
            "labels": labels,
            "prev": [prev_n.get(l, 0) for l in labels],
            "curr": [curr_n.get(l, 0) for l in labels],
        }

    ranked = sorted(curr_n.items(), key=lambda kv: (-kv[1], kv[0]))
    top = [k for k, _ in ranked[:limit]]
    if len(ranked) > limit:
        top.append("Other")
    # keep prev-only labels from vanishing when they still have prev volume
    for k, n in sorted(prev_n.items(), key=lambda kv: (-kv[1], kv[0])):
        if k not in top and n and len(top) < limit:
            top.append(k)

    def bucket(counter):
        out = []
        other = 0
        top_set = set(top) - {"Other"}
        for lab in top:
            if lab == "Other":
                continue
            out.append(counter.get(lab, 0))
        if "Other" in top:
            other = sum(v for k, v in counter.items() if k not in top_set)
            out.append(other)
        return out

    return {"labels": top, "prev": bucket(prev_n), "curr": bucket(curr_n)}


def _stat_block(prefix: str, xs: list[float]) -> dict:
    return {
        f"{prefix}_avg": _mean(xs),
        f"{prefix}_med": _median(xs),
        f"{prefix}_p90": _pctile(xs, 90),
        f"{prefix}_min": _minmax(xs, min),
        f"{prefix}_max": _minmax(xs, max),
    }


def _daily(rows: list[dict], start: date, end: date | None = None) -> dict:
    if end is None:
        end = start + timedelta(days=6)
    n = (end - start).days + 1
    created = [0] * n
    resolved = [0] * n
    esc = [0] * n
    frt_buckets: list[list[float]] = [[] for _ in range(n)]
    res_buckets: list[list[float]] = [[] for _ in range(n)]

    for r in rows:
        if r["created_day"] and start <= r["created_day"] <= end:
            i = (r["created_day"] - start).days
            created[i] += 1
            if r["is_escalated"]:
                esc[i] += 1
            if r["frt_secs"] is not None:
                frt_buckets[i].append(r["frt_secs"] / 60)
        if r["resolved_day"] and start <= r["resolved_day"] <= end:
            i = (r["resolved_day"] - start).days
            resolved[i] += 1
            if r["res_secs"] is not None:
                res_buckets[i].append(r["res_secs"] / 3600)

    return {
        "created": created,
        "resolved": resolved,
        "esc": esc,
        "frt_mins": [_mean(b) for b in frt_buckets],
        "res_hrs": [_mean(b) for b in res_buckets],
    }


def _insights(metrics: dict, categories: dict, customers: dict) -> list[dict]:
    out = []
    cv, pv = metrics["cv_total"], metrics["pv_total"]
    if cv or pv:
        direction = "up" if (cv or 0) >= (pv or 0) else "down"
        pct = metrics.get("total_pct")
        pct_bit = f" ({pct:+.1f}%)" if pct is not None else ""
        kind = "warn" if direction == "up" else "pos"
        out.append({
            "icon": "📈", "kind": kind, "label": "Volume",
            "title": f"Volume: {pv} → {cv}",
            "body": (
                f"{cv} tickets created this week vs {pv} the week before"
                f"{pct_bit}, {direction}."
            ),
        })

    if categories["labels"]:
        i = max(range(len(categories["labels"])),
                key=lambda j: categories["curr"][j])
        if categories["curr"][i]:
            out.append({
                "icon": "🏷️", "kind": "warn", "label": "Top category",
                "title": "Highest-volume category",
                "body": (
                    f"{categories['labels'][i]} led this week with "
                    f"{categories['curr'][i]} tickets "
                    f"(was {categories['prev'][i]})."
                ),
            })

    if customers["labels"]:
        i = max(range(len(customers["labels"])),
                key=lambda j: customers["curr"][j])
        if customers["curr"][i]:
            out.append({
                "icon": "🏢", "kind": "info", "label": "Top customer",
                "title": "Highest-volume customer",
                "body": (
                    f"{customers['labels'][i]} accounted for "
                    f"{customers['curr'][i]} tickets this week."
                ),
            })

    if metrics["cv_total"] or metrics["pv_total"]:
        esc_kind = "pos" if (metrics["cv_esc"] or 0) <= (metrics["pv_esc"] or 0) else "warn"
        out.append({
            "icon": "🚨", "kind": esc_kind, "label": "Escalations",
            "title": f"Escalations: {metrics['pv_esc']} → {metrics['cv_esc']}",
            "body": (
                f"{metrics['cv_esc']} escalations this week "
                f"({metrics['cv_esc_rate']}% of created) vs "
                f"{metrics['pv_esc']} ({metrics['pv_esc_rate']}%) last week."
            ),
        })

    frt_c, frt_p = metrics.get("cv_frt_avg"), metrics.get("pv_frt_avg")
    if frt_c is not None or frt_p is not None:
        def mins(v):
            return "—" if v is None else f"{round(v / 60, 1)} min"
        pct = metrics.get("frt_pct")
        pct_bit = f" ({pct:+.1f}%)" if pct is not None else ""
        frt_kind = "pos" if (metrics.get("frt_pct") or 0) <= 0 else "warn"
        out.append({
            "icon": "⚡", "kind": frt_kind, "label": "FRT",
            "title": f"FRT: {mins(frt_p)} → {mins(frt_c)}",
            "body": (
                f"Average FRT {mins(frt_c)} this week vs {mins(frt_p)} "
                f"last week{pct_bit}."
            ),
        })

    if metrics["cv_sla_breaches"] or metrics["pv_sla_breaches"]:
        sla_kind = "pos" if not metrics["cv_sla_breaches"] else "neg"
        out.append({
            "icon": "✅" if sla_kind == "pos" else "⚠️",
            "kind": sla_kind, "label": "SLA",
            "title": "SLA breaches",
            "body": (
                f"{metrics['cv_sla_breaches']} first-response SLA breaches "
                f"this week vs {metrics['pv_sla_breaches']} last week."
            ),
        })
    return out


def build(week_start: str | None = None, *, start: str | None = None,
          end: str | None = None, now: datetime | None = None) -> dict:
    """Return the Support Weekly Dashboard payload for a current period."""
    tz = _tz()
    current = _now(now)
    curr_monday, curr_sunday, prev_monday, prev_sunday = resolve_period(
        week_start, start, end, now=current)
    sla = qc_rules.sla_hours()

    raw_rows, msgs = _load_tickets(prev_monday)
    tickets = []
    for row in raw_rows:
        annotated = _annotate(row, msgs.get(row["id"], []), tz, sla, current)
        if annotated is None:
            continue
        tickets.append(annotated)

    def in_created(r, start, end):
        return r["created_day"] is not None and start <= r["created_day"] <= end

    def in_resolved(r, start, end):
        return r["resolved_day"] is not None and start <= r["resolved_day"] <= end

    curr_created = [r for r in tickets if in_created(r, curr_monday, curr_sunday)]
    prev_created = [r for r in tickets if in_created(r, prev_monday, prev_sunday)]
    curr_resolved = [r for r in tickets if in_resolved(r, curr_monday, curr_sunday)]
    prev_resolved = [r for r in tickets if in_resolved(r, prev_monday, prev_sunday)]

    def totals(created, resolved):
        n = len(created)
        open_n = sum(1 for r in created if r["is_open"])
        esc_n = sum(1 for r in created if r["is_escalated"])
        sla_n = sum(1 for r in created if r["sla_breached"])
        frt = [r["frt_secs"] for r in created if r["frt_secs"] is not None]
        res = [r["res_secs"] for r in resolved if r["res_secs"] is not None]
        return {
            "total": n,
            "open": open_n,
            "resolved": len(resolved),
            "esc": esc_n,
            "esc_rate": round(esc_n / n * 100, 1) if n else 0.0,
            "reopen": 0,
            "reopen_rate": 0.0,
            "sla": sla_n,
            "frt": frt,
            "res": res,
        }

    cv, pv = totals(curr_created, curr_resolved), totals(prev_created, prev_resolved)

    def status_counts(created):
        counts = {name: 0 for name in STATUS_ORDER}
        for r in created:
            label = _status_label(r["state"])
            if label in counts:
                counts[label] += 1
        return counts

    metrics = {
        "pv_total": pv["total"], "cv_total": cv["total"],
        "total_diff": cv["total"] - pv["total"],
        "total_pct": _pct(cv["total"], pv["total"]),
        "pv_open": pv["open"], "cv_open": cv["open"],
        "open_diff": cv["open"] - pv["open"],
        "open_pct": _pct(cv["open"], pv["open"]),
        "pv_resolved": pv["resolved"], "cv_resolved": cv["resolved"],
        "pv_esc": pv["esc"], "cv_esc": cv["esc"],
        "esc_diff": cv["esc"] - pv["esc"],
        "esc_pct": _pct(cv["esc"], pv["esc"]),
        "pv_esc_rate": pv["esc_rate"], "cv_esc_rate": cv["esc_rate"],
        "pv_reopen": 0, "cv_reopen": 0,
        "pv_reopen_rate": 0.0, "cv_reopen_rate": 0.0,
        "pv_sla_breaches": pv["sla"], "cv_sla_breaches": cv["sla"],
        "pv_status": status_counts(prev_created),
        "cv_status": status_counts(curr_created),
    }
    metrics.update({f"pv_{k}": v for k, v in _stat_block("frt", pv["frt"]).items()})
    metrics.update({f"cv_{k}": v for k, v in _stat_block("frt", cv["frt"]).items()})
    metrics.update({f"pv_{k}": v for k, v in _stat_block("res", pv["res"]).items()})
    metrics.update({f"cv_{k}": v for k, v in _stat_block("res", cv["res"]).items()})
    metrics["frt_pct"] = _pct(metrics["cv_frt_avg"], metrics["pv_frt_avg"])
    metrics["res_pct"] = _pct(metrics["cv_res_avg"], metrics["pv_res_avg"])

    d_curr = _daily(tickets, curr_monday, curr_sunday)
    d_prev = _daily(tickets, prev_monday, prev_sunday)
    day_n = (curr_sunday - curr_monday).days + 1
    curr_labels = _day_labels(curr_monday, day_n)
    prev_labels = _day_labels(prev_monday, day_n)

    created_either = curr_created + prev_created
    priorities = _top_breakdown(
        created_either, "priority", 5,
        curr_start=curr_monday, curr_end=curr_sunday,
        prev_start=prev_monday, prev_end=prev_sunday,
        extra_labels=PRIORITY_ORDER,
    )
    categories = _top_breakdown(
        created_either, "category", CAT_LIMIT,
        curr_start=curr_monday, curr_end=curr_sunday,
        prev_start=prev_monday, prev_end=prev_sunday,
    )
    customers = _top_breakdown(
        created_either, "account", CUST_LIMIT,
        curr_start=curr_monday, curr_end=curr_sunday,
        prev_start=prev_monday, prev_end=prev_sunday,
    )
    esc_rows = [r for r in created_either if r["is_escalated"]]
    esc_categories = _top_breakdown(
        esc_rows, "category", ESC_CAT_LIMIT,
        curr_start=curr_monday, curr_end=curr_sunday,
        prev_start=prev_monday, prev_end=prev_sunday,
    )

    names = sorted(
        {r["assignee"] for r in created_either} | {r["assignee"] for r in curr_resolved + prev_resolved},
        key=lambda n: (
            -sum(1 for r in curr_created if r["assignee"] == n),
            n,
        ),
    )

    def agent_slice(name, created, resolved):
        mine_c = [r for r in created if r["assignee"] == name]
        mine_r = [r for r in resolved if r["assignee"] == name]
        frt = [r["frt_secs"] for r in mine_c if r["frt_secs"] is not None]
        res = [r["res_secs"] for r in mine_r if r["res_secs"] is not None]
        return {
            "assigned": len(mine_c),
            "resolved": len(mine_r),
            "frt": frt,
            "res": res,
            "esc": sum(1 for r in mine_c if r["is_escalated"]),
            "sla": sum(1 for r in mine_c if r["sla_breached"]),
            "backlog": sum(1 for r in mine_c if r["is_open"]),
        }

    agents = {
        "names": names,
        "pv_assigned": [], "cv_assigned": [],
        "pv_resolved": [], "cv_resolved": [],
        "pv_frt": [], "cv_frt": [],
        "pv_res": [], "cv_res": [],
        "pv_csat_avg": [],
        "cv_csat_avg": [],
    }
    def csat_in(start, end):
        return [
            e for e in _csat_events_from_store(tz)
            if e["day"] and start <= e["day"] <= end
        ]

    csat_curr = _csat_pack(csat_in(curr_monday, curr_sunday))
    csat_prev = _csat_pack(csat_in(prev_monday, prev_sunday))
    csat_curr_by = {a["name"]: a for a in csat_curr["agents"]}
    csat_prev_by = {a["name"]: a for a in csat_prev["agents"]}

    agent_table = []
    for name in names:
        a_c = agent_slice(name, curr_created, curr_resolved)
        a_p = agent_slice(name, prev_created, prev_resolved)
        cca, pca = csat_curr_by.get(name), csat_prev_by.get(name)
        agents["pv_assigned"].append(a_p["assigned"])
        agents["cv_assigned"].append(a_c["assigned"])
        agents["pv_resolved"].append(a_p["resolved"])
        agents["cv_resolved"].append(a_c["resolved"])
        agents["pv_frt"].append(_secs_to_mins(_mean(a_p["frt"])))
        agents["cv_frt"].append(_secs_to_mins(_mean(a_c["frt"])))
        agents["pv_res"].append(_secs_to_hrs(_mean(a_p["res"])))
        agents["cv_res"].append(_secs_to_hrs(_mean(a_c["res"])))
        agents["pv_csat_avg"].append(pca["avg"] if pca else None)
        agents["cv_csat_avg"].append(cca["avg"] if cca else None)
        agent_table.append({
            "agent": name,
            "pv_assigned": a_p["assigned"], "cv_assigned": a_c["assigned"],
            "assigned_diff": a_c["assigned"] - a_p["assigned"],
            "assigned_pct": _pct(a_c["assigned"], a_p["assigned"]),
            "pv_resolved": a_p["resolved"], "cv_resolved": a_c["resolved"],
            "resolved_diff": a_c["resolved"] - a_p["resolved"],
            "pv_frt_avg": _mean(a_p["frt"]), "cv_frt_avg": _mean(a_c["frt"]),
            "frt_diff": _diff(_mean(a_c["frt"]), _mean(a_p["frt"])),
            "pv_res_avg": _mean(a_p["res"]), "cv_res_avg": _mean(a_c["res"]),
            "res_diff": _diff(_mean(a_c["res"]), _mean(a_p["res"])),
            "pv_frt_p75": _pctile(a_p["frt"], 75, min_n=2),
            "cv_frt_p75": _pctile(a_c["frt"], 75, min_n=2),
            "pv_frt_p90": _pctile(a_p["frt"], 90, min_n=2),
            "cv_frt_p90": _pctile(a_c["frt"], 90, min_n=2),
            "pv_escalations": a_p["esc"], "cv_escalations": a_c["esc"],
            "pv_sla_breaches": a_p["sla"], "cv_sla_breaches": a_c["sla"],
            "pv_reopened": 0, "cv_reopened": 0,
            "cv_backlog": a_c["backlog"],
            "pv_csat_avg": pca["avg"] if pca else None,
            "pv_csat_pos_pct": pca["positivePct"] if pca else None,
            "pv_csat_total": pca["total"] if pca else 0,
            "pv_csat_award": pca["award"] if pca else None,
            "cv_csat_avg": cca["avg"] if cca else None,
            "cv_csat_pos_pct": cca["positivePct"] if cca else None,
            "cv_csat_total": cca["total"] if cca else 0,
            "cv_csat_award": cca["award"] if cca else None,
        })

    def row_out(r, week, labels, period_start):
        created_str = r["created"].astimezone(tz).strftime("%Y-%m-%d %H:%M")
        resolved_str = ""
        if r["resolved_day"] and r["is_resolved"]:
            resolved_str = r["resolved_day"].isoformat()
        day = r["created_day"]
        created_day = ""
        if day is not None:
            idx = (day - period_start).days
            if 0 <= idx < len(labels):
                created_day = labels[idx]
        return {
            "issue": r["number"] if r["number"] is not None else r["id"],
            "account": r["account"],
            "priority": r["priority"],
            "category": r["category"],
            "issue_type": r["type"],
            "assignee": r["assignee"],
            "status": r["status"],
            "created_str": created_str,
            "resolved_str": resolved_str,
            "created_day": created_day,
            "created_date": day.isoformat() if day else "",
            "frt_secs": r["frt_secs"],
            "res_secs": r["res_secs"],
            "is_resolved": r["is_resolved"],
            "is_open": r["is_open"],
            "is_escalated": r["is_escalated"],
            "is_reopened": False,
            "sla_breached": r["sla_breached"],
            "pylon_link": r["link"],
            "week": week,
        }

    all_rows = (
        [row_out(r, WEEK_PREV, prev_labels, prev_monday) for r in prev_created]
        + [row_out(r, WEEK_CURR, curr_labels, curr_monday) for r in curr_created]
    )
    all_rows.sort(key=lambda x: (x["week"], str(x["issue"])))

    generated = current.strftime("%Y-%m-%d %H:%M:%S %Z") or current.strftime(
        "%Y-%m-%d %H:%M:%S"
    )

    # Nested the way assets/app.js reads it: scalars on D.metrics, daily
    # series on D.dailyData. Flattening those two collided on cv_resolved
    # and cv_esc — a KPI number and a 7-day array cannot share a key.
    payload = {
        "generatedAt": generated,
        "prevWeekLabel": _range_label(prev_monday, prev_sunday),
        "currWeekLabel": _range_label(curr_monday, curr_sunday),
        "metrics": metrics,
        "dailyData": {
            "currDays": curr_labels,
            "prevDays": prev_labels,
            "cv_created": d_curr["created"],
            "pv_created": d_prev["created"],
            "cv_resolved": d_curr["resolved"],
            "pv_resolved": d_prev["resolved"],
            "cv_esc": d_curr["esc"],
            "pv_esc": d_prev["esc"],
            "cv_frt_mins": d_curr["frt_mins"],
            "pv_frt_mins": d_prev["frt_mins"],
            "cv_res_hrs": d_curr["res_hrs"],
            "pv_res_hrs": d_prev["res_hrs"],
        },
        "priorities": priorities,
        "categories": categories,
        "customers": customers,
        "escCategories": esc_categories,
        "agents": agents,
        "agentTable": agent_table,
        "csatPrev": csat_prev,
        "csatCurr": csat_curr,
        "allRows": all_rows,
        "insights": _insights(metrics, categories, customers),
        "week_start": curr_monday.isoformat(),
        "period_start": curr_monday.isoformat(),
        "period_end": curr_sunday.isoformat(),
        "timezone": str(tz),
        "coverage": {
            "csat": True,
            "reopen": False,
            "resolved_proxy": "updated_at on closed tickets",
            "sla_hours": sla,
        },
    }
    return payload
