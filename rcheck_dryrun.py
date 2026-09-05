"""What would the R-checks say if the rules were X? Answered without saving X.

The A-check rubric already has `dryrun`: grade a sample with unsaved text, show
what moves, write nothing. This is the same instrument for the deterministic
checks, and it is cheaper — R-checks are local Python, so a month costs CPU and
no AI call. It is also the instrument SPEC_v5 D10 asks for: Phase 2 replaces
five hardcoded state sets with an editable table, and the only way to know the
seed is a no-op is to re-run every stored ticket through it and count zero.

**Nothing is written.** No `rule_checks` row, no `ai_checks` row, no rules
document, no run record. A dry-run is a question.

## What "moved" means here, and what it does not

A rules save does **not** retroactively change stored R-verdicts. `score_all`
runs once, in the fetch loop, and nothing recomputes it (SPEC_v5 D1). So this
module reports two different, both-true numbers, and they must not be confused:

  `overall`   the *counterfactual*: the verdicts these tickets would have got
              had the rules been the draft. This is what tells you whether a
              rule edit is doing what you meant. It does not predict a change
              to today's dashboard.

  `applied`   what saving actually rewrites today: `resync_overall` recomputes
              `overall_result` from the STORED verdicts under the new
              enabled-check mask. Exact, no reconstruction involved. If an
              admin clicks save and watches the dashboard, this is the number
              they will see move.

Reporting only the first would be honest-looking and wrong in the way D13
warns about: "41 tickets move" followed by a save that changes nothing visible.

## Three columns, because two of them cannot be told apart otherwise

Per check and per ticket this computes:

  `stored`  what the database holds — the fact a reviewer sees today
  `saved`   the check re-run under the SAVED rules — the control
  `draft`   the check re-run under the DRAFT rules

Movement is `saved` vs `draft`: same clock, same ticket data, same code, one
variable. `stored` vs `saved` is reported separately as **drift**, never as
movement, because it has nothing to do with the edit. Drift is not rare — QC
exists to make agents fill in `functionalities`, so a ticket stored as `r1 =
Fail` in August is very often `Pass` on the ticket now. Diffing the draft
against `stored` alone would have reported hundreds of those as the work of a
rule change that touched nothing.

## The two corrections (SPEC_v5 D13)

**R4's clock is pinned to when the ticket was scored.** `scorer.r4` measures the
last customer message against `datetime.now`, and there is no seam to inject a
clock through. Rather than patch `datetime` — process-global, and a concurrent
fetch loop would score against it — the messages handed to r4 are aged forward
by `now - scored_at`, which leaves r4's arithmetic byte-identical while making
it measure the ticket as it stood when it was scored. This is not cosmetic: with
a live clock every ticket older than the SLA fails at any SLA, so raising
`r4_sla_hours` from 24 to 48 would report zero movement, which is the exact
opposite of the truth. The clock comes from `rule_checks.checked_at` (the
instant r4 actually ran), falling back to `ai_checks.checked_at` and then
`tickets.fetched_at`. A ticket with none of the three reports R4 as `Unknown`
rather than guessing with `now`.

**R5's Slack branch is never fetched.** `scorer._fetch_slack_thread` is a live
API call; over 30 days it would be slow, rate-limited, and — with the
deployment guard — possibly refused outright. It is stubbed out for the duration
of a dry-run, but a stub that returns "" makes r5 fall through to `Fail`, and
reporting a guessed Fail is worse than reporting nothing. Every use of the
fetched thread inside `scorer.r5` is `if <found> return "Pass"`, so the branch
can only ever turn a Fail into a Pass: a Pass reached without it is certain, and
a Fail reached with it consulted is unknown. Those become the third verdict
`Unknown`, counted in `checks["r5"]["unknown"]` and shown in the transitions as
`Fail → Unknown`, so the state is visible instead of passing for "unchanged". An
`Unknown` on an enabled check makes the overall `Unknown` too — `_compute_overall`
would otherwise read it as "not a Fail" and hand back a confident Pass.

## How the draft is scoped

`scorer` and `rules` read configuration from module state, not from parameters,
so a draft cannot be passed down the call chain — and `qc_runner._compute_overall`
reads the enabled-check mask through `rules.enabled_rule_keys`, so it too has to
run under the draft or the mask half of the diff is silently wrong. Swapping
`rules._cache` for the duration would work and is not safe: this runs in a
thread of a live server whose fetch loop *writes* the verdicts it computes, and
it would compute them from an admin's unsaved draft.

So both seams are shimmed once, at import, with functions that consult a
thread-local and delegate to the real implementation on every thread that is not
inside `_under_rules`. Importing this module is inert for everyone else.

Duplicating any of `scorer`'s logic was the one thing not on the table: a
dry-run that drifts from the real run is worse than no dry-run.
"""

import threading
from contextlib import contextmanager
from datetime import datetime, timezone

import db
import dryrun
import evidence
import prompts
import qc_runner
import rules as qc_rules
import scorer

# Bounded because the cost is real even without an AI call: every ticket costs
# several BeautifulSoup passes over its whole thread, twice.
MAX_LIMIT     = 2000
DEFAULT_LIMIT = 500

# Counts cover everything scanned; only movers are listed, and only this many.
# A month of movement is a number, not two thousand rows in a JSON payload.
MAX_SAMPLE = 50

# The third verdict. Not a grade `scorer` can produce, and deliberately not one
# of Pass/Fail/N/A, so nothing downstream can mistake it for a decision.
UNKNOWN = "Unknown"
UNKNOWN_SLACK = ("would consult the linked oncall Slack thread, which a dry-run "
                 "never fetches")
UNKNOWN_CLOCK = ("no recorded scoring time to pin the SLA clock to, so the age "
                 "of the last message cannot be reconstructed")

# The checks re-run here: everything `scorer.score_all` computes except r9,
# which is hardcoded to N/A and cannot move. t_rdryrun keeps this in step.
RECHECKED_KEYS = ("r1", "r2", "r3", "r4", "r5", "r7", "r8")

NOTHING_TO_COMPARE = (
    "No stored R-check verdicts in range. Fetch a date first, then dry-run "
    "against it — there is nothing to compare a draft to."
)


# ── the draft scope ───────────────────────────────────────────────────────────

_local = threading.local()


def _refusing_reader(session: dict):
    """A stand-in for the live Slack read, which records that it was wanted.

    Returning "" is what makes r5 fall through; the recorded flag is what stops
    that fall-through being reported as a verdict. Passed into `scorer.r5` as
    its `fetch_thread`, so nothing global changes — an earlier version rebound
    `scorer._fetch_slack_thread` at import, which meant importing this module
    quietly altered how the real scorer behaved for every caller.
    """
    def read(url: str) -> str:
        session["slack_consulted"] = True
        return ""
    return read


@contextmanager
def _under_rules(doc: dict):
    """Answer rules reads from `doc`, on this thread, for the block.

    Delegates to `rules.scoped`, which owns the thread-local. Keeping it there
    rather than here means `rules.py` documents its own seam, and importing this
    module has no effect on anything else.
    """
    session = {"slack_consulted": False}
    previous = getattr(_local, "session", None)
    _local.session = session
    try:
        with qc_rules.scoped(doc):
            yield session
    finally:
        _local.session = previous


# ── stored row → the shapes `scorer` expects ─────────────────────────────────

def clamp(limit) -> int:
    try:
        n = int(limit)
    except (TypeError, ValueError):
        return DEFAULT_LIMIT
    return max(1, min(MAX_LIMIT, n))


def _pinned_at(row: dict) -> datetime | None:
    """When this ticket was scored, best available.

    `rule_checks.checked_at` first because it is literally the instant `r4` ran;
    the AI grade and the fetch timestamp are later approximations of the same
    moment (app.py writes all three from one `now`, but older rows predate that).
    """
    for key in ("rc_checked_at", "ai_checked_at", "fetched_at"):
        when = scorer._parse_ts(row.get(key))
        if when is not None:
            return when
    return None


def _as_api_message(row: dict) -> dict:
    """A stored message row wearing the Pylon envelope scoring saw.

    `scorer._is_customer_msg` reads `author.contact`, which exists only on the
    live API object — app.py flattens that decision into the `is_customer`
    column on the way in. Rebuilding the envelope keeps r4 on exactly one code
    path; teaching it a second way to ask is how the dry-run and the real run
    start disagreeing.
    """
    msg = dict(row)
    msg["author"] = {"contact": {}} if row.get("is_customer") else {"user": {}}
    return msg


def _aged_to(messages: list[dict], pinned_at: datetime) -> list[dict]:
    """The same messages, aged forward so `datetime.now` reads as `pinned_at`.

    age = now - (ts + (now - pinned_at)) = pinned_at - ts, which is the age r4
    measured when it ran. The shift is uniform, so `max()` still selects the
    same message. The residual error is the wall-clock gap between this call and
    r4's own `datetime.now()` — milliseconds, against an SLA counted in hours.
    """
    delta = datetime.now(timezone.utc) - pinned_at
    out = []
    for m in messages:
        when = scorer._parse_ts(m.get("timestamp"))
        if when is None:
            out.append(m)      # r4 drops undated messages; leave it to do that
            continue
        aged = dict(m)
        aged["timestamp"] = (when + delta).isoformat()
        out.append(aged)
    return out


class _StoredTicket:
    """A stored row and its messages, re-inflated into `scorer`'s inputs.

    `evidence._Context` flattens the same row for reading; this is the other
    direction, and it hits the same impedance: `custom_fields` and
    `external_issues` arrive as JSON strings, the account is joined in as two
    flat columns, and the message author payload is already reduced to a flag.

    Raises `ValueError` when the stored JSON cannot be read. An empty dict would
    make r1, r2 and r8 report Fail on a ticket nobody has looked at — a fabricated
    verdict is exactly what this feature exists not to produce.
    """

    def __init__(self, row: dict, messages: list[dict]):
        self.row = row
        fields, fields_ok = evidence._parse_json(row.get("custom_fields"), dict)
        if not fields_ok:
            raise ValueError("stored custom_fields could not be read as JSON")
        external, external_ok = evidence._parse_json(row.get("external_issues"), list)
        if not external_ok:
            raise ValueError("stored external_issues could not be read as JSON")
        self.external = external

        assignee_id   = row.get("assignee_id")
        assignee_name = row.get("assignee_name")
        self.issue = {
            "id":            row.get("id"),
            "state":         row.get("state") or "",
            "custom_fields": fields,
            "body_html":     row.get("body_html"),
            "account":       {"id": row["account_id"]} if row.get("account_id") else {},
            "assignee":      ({"id": assignee_id, "name": assignee_name}
                              if (assignee_id or assignee_name) else None),
        }

        # r3 separates "the account is internal" from "no account row exists",
        # and the LEFT JOIN carries that distinction as two NULLs.
        if row.get("account_name") is None and row.get("account_type") is None:
            self.account = None
        else:
            self.account = {"name": row.get("account_name"),
                            "type": row.get("account_type")}

        self.messages  = [_as_api_message(m) for m in messages]
        self.pinned_at = _pinned_at(row)
        self.messages_at_scoring_time = (
            _aged_to(self.messages, self.pinned_at) if self.pinned_at else None
        )


# ── re-running the checks ────────────────────────────────────────────────────

def _recheck(t: _StoredTicket) -> tuple[dict, dict]:
    """Every re-runnable R-check for one ticket: `(verdicts, reasons)`.

    Must be called inside `_under_rules`, which is what makes the verdicts the
    draft's and what keeps the Slack fetch from happening.
    """
    session = _local.session
    issue, msgs, ext = t.issue, t.messages, t.external

    verdicts: dict[str, str] = {}
    reasons:  dict[str, str] = {}

    verdicts["r1"] = scorer.r1(issue["custom_fields"])
    verdicts["r2"] = scorer.r2(issue["custom_fields"])
    verdicts["r3"] = scorer.r3(issue, t.account)

    if t.messages_at_scoring_time is None:
        verdicts["r4"], reasons["r4"] = UNKNOWN, UNKNOWN_CLOCK
    else:
        verdicts["r4"] = scorer.r4(issue, t.messages_at_scoring_time)

    session["slack_consulted"] = False
    r5 = scorer.r5(issue, msgs, ext, fetch_thread=_refusing_reader(session))
    if session["slack_consulted"] and r5 == "Fail":
        # The thread was the deciding input and we refused to read it. It could
        # only have said "Pass", so the Fail is a guess, not a verdict.
        verdicts["r5"], reasons["r5"] = UNKNOWN, UNKNOWN_SLACK
    else:
        verdicts["r5"] = r5

    verdicts["r7"] = scorer.r7(issue, msgs, ext)
    verdicts["r8"] = scorer.r8(issue, msgs, ext)
    return verdicts, reasons


def _stored_r_checks(row: dict) -> dict:
    """The stored verdict for every key the overall depends on.

    Loud on a missing column: `_compute_overall` reads a missing key as "not
    Fail", so a query that stopped selecting r9 would quietly soften every
    verdict it computed — the same trap `dryrun._draft_grades` guards.
    """
    missing = [k for k in qc_runner.R_CHECK_KEYS if k not in row]
    if missing:
        raise KeyError(f"the sample query did not select {missing} — the "
                       f"verdict would be computed from an incomplete rule set")
    return {k: row.get(k) for k in qc_runner.R_CHECK_KEYS}


def _overall(row: dict, verdicts: dict, a_grades: dict) -> str:
    """The verdict these re-run checks imply. Call inside `_under_rules`.

    Goes through `qc_runner._compute_overall` rather than restating its rules,
    which is also what puts the draft's enabled-check mask in play: the function
    reads it from `rules.enabled_rule_keys`, and inside the scope that answers
    with the draft.
    """
    r_checks = _stored_r_checks(row)      # r9, and anything not re-run
    r_checks.update(verdicts)
    live = qc_rules.enabled_rule_keys(qc_runner.R_CHECK_KEYS)
    if any(r_checks.get(k) == UNKNOWN for k in live):
        # A confident Pass built on a check we declined to evaluate is the one
        # output this feature must never produce.
        return UNKNOWN
    return qc_runner._compute_overall(r_checks, a_grades)


def _applied_overall(row: dict, a_grades: dict) -> str:
    """What `resync_overall` would write on save. Call inside `_under_rules`.

    Stored verdicts, new mask — the whole retroactive effect of a rules save,
    per D1. Nothing is reconstructed here, so this number has no caveats.
    """
    return qc_runner._compute_overall(_stored_r_checks(row), a_grades)


# ── loading ──────────────────────────────────────────────────────────────────

def _date_range(start, end) -> tuple[str | None, str | None]:
    if bool(start) != bool(end):
        raise ValueError("a date range needs both start and end, or neither")
    if start and end and str(start) > str(end):
        raise ValueError(f"start {start} is after end {end}")
    return (str(start) if start else None, str(end) if end else None)


def _load(limit: int, start: str | None, end: str | None) -> list[dict]:
    """Stored tickets that have R-verdicts, newest first, with their messages.

    An INNER JOIN on `rule_checks`: a ticket that was never R-scored (archived
    ones are skipped in the fetch loop) has no verdict to move, so including it
    would pad the count with tickets the diff cannot say anything about.

    No excluded-state filter, deliberately. Out-of-scope tickets still carry
    stored verdicts, and Phase 2's whole subject is which states are in scope —
    hiding them would understate the very change this exists to preview.

    Recency rather than randomness, as `dryrun._sample`: the same draft run
    twice must compare the same tickets or an admin cannot tell a rules change
    from a sampling change.
    """
    where  = ["t.deleted_at IS NULL"]
    params: list = []
    if start and end:
        where.append("t.fetch_date BETWEEN ? AND ?")
        params += [start, end]

    with db.get_conn() as conn:
        rows = [dict(r) for r in conn.execute(f"""
            SELECT t.id, t.number, t.title, t.link, t.state, t.fetch_date,
                   t.assignee_id, t.assignee_name, t.account_id,
                   t.custom_fields, t.external_issues, t.body_html, t.fetched_at,
                   a.name AS account_name, a.type AS account_type,
                   rc.r1, rc.r2, rc.r3, rc.r4, rc.r5, rc.r7, rc.r8, rc.r9,
                   rc.checked_at AS rc_checked_at,
                   ac.a1, ac.a2, ac.a3, ac.a4, ac.a5,
                   ac.overall_result, ac.checked_at AS ai_checked_at
            FROM      tickets     t
            JOIN      rule_checks rc ON t.id = rc.ticket_id
            LEFT JOIN accounts    a  ON t.account_id = a.id
            LEFT JOIN ai_checks   ac ON t.id = ac.ticket_id
            WHERE {' AND '.join(where)}
            ORDER BY t.fetch_date DESC, t.number DESC
            LIMIT ?
        """, [*params, limit])]

        for row in rows:
            row["messages"] = [dict(m) for m in conn.execute(
                "SELECT author_name, is_customer, is_private, message_html, "
                "timestamp FROM messages WHERE ticket_id = ? ORDER BY timestamp",
                (row["id"],))]
    return rows


# ── reporting ────────────────────────────────────────────────────────────────

def _label(verdict) -> str:
    return str(verdict) if verdict else "not scored"


def _transition(counter: dict, before, after) -> None:
    key = f"{_label(before)} → {_label(after)}"
    counter[key] = counter.get(key, 0) + 1


def _blank_counts() -> dict:
    return {"moved": 0, "drift": 0, "unknown": 0, "transitions": {}}


def run(draft: dict | None = None, limit=DEFAULT_LIMIT,
        start: str | None = None, end: str | None = None) -> dict:
    """Re-run the R-checks under `draft` and report what moves. Writes nothing.

    `draft` is layered over the saved rules by `dryrun.draft_rules`, so a
    partial document never resets the keys it omits. It is validated as a whole
    before anything is scored: a draft that could not be saved must not be
    previewed either, or the admin is shown the effect of a document the save
    endpoint will reject.

    Raises `ValueError` on a draft that does not validate or an incoherent date
    range, and `KeyError` if the query stops selecting a check the verdict
    depends on. See the module docstring for what `overall` and `applied` each
    mean — they answer different questions and only one of them predicts a
    change to today's dashboard.
    """
    merged = dryrun.draft_rules(draft)
    errors = qc_rules.validate(merged)
    if errors:
        raise ValueError("these draft rules could not be saved, so previewing "
                         "them would be misleading: " + "; ".join(errors[:4]))

    limit = clamp(limit)
    start, end = _date_range(start, end)

    # One snapshot of the saved document for the whole comparison: a rules save
    # landing mid-run would otherwise put the control and the draft on different
    # baselines and report the difference as movement.
    saved = qc_rules.current()
    rules_changed = qc_rules.rules_hash(merged) != qc_rules.rules_hash(saved)
    with _under_rules(merged):
        draft_enabled = set(qc_rules.enabled_rule_keys(RECHECKED_KEYS))

    rows = _load(limit, start, end)
    checks = {k: {"enabled": k in draft_enabled, **_blank_counts()}
              for k in RECHECKED_KEYS}
    overall = {**_blank_counts(), "comparable": 0}
    applied = {"moved": 0, "transitions": {}, "comparable": 0}
    sample:  list[dict] = []
    failed:  list[dict] = []
    moved_tickets = 0

    for row in rows:
        try:
            ticket = _StoredTicket(row, row["messages"])
        except ValueError as e:
            # Reported, never dropped: a ticket silently missing from the scan
            # reads as "nothing would change here".
            failed.append({"id": row["id"], "number": row.get("number"),
                           "link": row.get("link"), "reason": str(e)})
            continue

        a_grades = {k: row.get(k) for k in prompts.A_CHECK_KEYS}

        with _under_rules(saved):
            saved_verdicts, saved_reasons = _recheck(ticket)
            saved_overall = _overall(row, saved_verdicts, a_grades)
        if rules_changed:
            with _under_rules(merged):
                draft_verdicts, reasons = _recheck(ticket)
                draft_overall  = _overall(row, draft_verdicts, a_grades)
                applied_draft  = _applied_overall(row, a_grades)
        else:
            # Identical documents cannot produce different verdicts, and saying
            # so exactly beats re-deriving it: an empty draft reports zero
            # movement rather than whatever the reconstruction happens to do.
            draft_verdicts, reasons = saved_verdicts, saved_reasons
            draft_overall = saved_overall
            with _under_rules(merged):
                applied_draft = _applied_overall(row, a_grades)

        row_changes = {}
        for key in RECHECKED_KEYS:
            stored_v, saved_v, draft_v = row.get(key), saved_verdicts[key], draft_verdicts[key]
            if draft_v == UNKNOWN:
                # Counted and named. An Unknown is identical on both sides, so
                # it never shows up as a transition — without this counter a
                # check the dry-run declined to evaluate would read as one that
                # nothing moved.
                checks[key]["unknown"] += 1
                checks[key]["unknown_reason"] = reasons.get(key)
            if stored_v != saved_v:
                checks[key]["drift"] += 1
            if saved_v == draft_v:
                continue
            checks[key]["moved"] += 1
            _transition(checks[key]["transitions"], saved_v, draft_v)
            row_changes[key] = {"stored": stored_v, "saved": saved_v,
                                "draft": draft_v, "reason": reasons.get(key)}

        stored_overall = row.get("overall_result")
        if stored_overall:
            overall["comparable"] += 1
            applied["comparable"] += 1
            if stored_overall != saved_overall:
                overall["drift"] += 1
        if draft_overall == UNKNOWN:
            overall["unknown"] += 1
        overall_moved = saved_overall != draft_overall
        if overall_moved:
            overall["moved"] += 1
            _transition(overall["transitions"], saved_overall, draft_overall)
        applied_moved = bool(stored_overall) and applied_draft != stored_overall
        if applied_moved:
            applied["moved"] += 1
            _transition(applied["transitions"], stored_overall, applied_draft)

        if not (row_changes or overall_moved or applied_moved):
            continue
        moved_tickets += 1
        if len(sample) < MAX_SAMPLE:
            sample.append({
                "id": row["id"], "number": row.get("number"),
                "title": row.get("title"), "link": row.get("link"),
                "state": row.get("state"), "fetch_date": row.get("fetch_date"),
                "checks": row_changes,
                "overall": {"stored": stored_overall, "saved": saved_overall,
                            "draft": draft_overall},
                "applied_overall": ({"stored": stored_overall, "draft": applied_draft}
                                    if applied_moved else None),
            })

    return {
        "scanned":       len(rows) - len(failed),
        "limit":         limit,
        "range":         {"start": start, "end": end},
        "rules_changed": rules_changed,
        "checks":        checks,
        "overall":       overall,
        "applied":       applied,
        "moved_tickets": moved_tickets,
        "sample":        sample,
        "sample_truncated": moved_tickets > len(sample),
        "errors":        failed,
        # Stated for the same reason `dryrun` states its bill: an admin should
        # know what a button costs before pressing it. Here the answer is
        # nothing but CPU, and the honest version of that is the zeros.
        "cost_usd":      0.0,
        "ai_calls":      0,
        "slack_calls":   0,
        "note":          NOTHING_TO_COMPARE if not rows else "",
    }
