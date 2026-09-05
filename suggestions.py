"""
Learned suggestions: what the reviewers' overrides say about the AI and the rules.

Every time a reviewer signs a ticket off with a verdict that differs from the
AI's, `ticket_reviews` keeps a labelled example of the grading being wrong. That
signal is already in the database and nothing reads it. This module reads it
back and groups each disagreement under the check that actually produced the AI
verdict, so a pattern — "R3 failed nine tickets that reviewers all passed" —
becomes a proposal to edit one specific rule instead of a vague feeling that
scoring is off.

This module only ever *proposes*. It is strictly read-only: no INSERT, no
UPDATE, no DELETE, no schema change, here or in anything it calls. Auto-tuning a
grading rubric from its own past disagreements is a feedback loop with no human
in it, and the failure mode is silent drift in what "Pass" means with no one
able to say when it changed. An observation carries its evidence and the edit it
implies; a person accepts it, and that acceptance is a normal audited rules
change made elsewhere.

Three definitions decide what counts as evidence, and each exists because the
opposite reading is tempting:

  * `kept_ai = 1` records that the reviewer pressed "accept" — they confirmed
    the AI. That is agreement, not evidence the AI was wrong.
  * `decision = 'Revert'` clears a previous sign-off. It is the *absence* of a
    human verdict, not a verdict against the machine.
  * a sign-off that lands on the AI's own verdict is agreement too, however it
    was entered — someone clicking Pass on a ticket the AI already passed is
    not an override.

And one gate, `MIN_OVERRIDES`: two disagreements are noise, and a suggestion
built on noise costs more trust than it can ever repay.
"""

import re
from datetime import datetime, timedelta, timezone

import db
import rules as qc_rules

# Nothing is surfaced below this many overrides for the same check in the
# window. Deliberately a blunt count rather than a rate: a reviewer disagreeing
# twice is a reviewer having a day, and acting on it would edit a rule for
# everyone. Suppressed observations are counted and reported as `gated_out` so
# the gate is visible rather than looking like "no signal".
MIN_OVERRIDES = 5

# Evidence per observation. Enough to click through and judge, few enough that
# the payload stays a summary.
SAMPLE_SIZE = 5

# Used when an override cannot be pinned on any single check — the AI passed a
# ticket the reviewers failed, so no check objected and there is nothing to
# blame but the rubric itself.
OVERALL = "overall"

# The checks that can drive a verdict, mirroring qc_runner._compute_overall,
# which is the authority. Duplicated rather than imported because qc_runner
# pulls in the Vertex/genai stack and this is a read-only aggregate over SQLite;
# if that function's inputs change, change `_drivers` below with it.
R_KEYS = ("r1", "r2", "r3", "r4", "r5", "r7", "r8", "r9")

# A-checks whose "Needs Review" can produce a Needs Review overall. A2
# (sentiment) is absent on purpose: it never feeds the verdict, so it can never
# have driven an overridden one.
_A_HEDGE_KEYS = ("a1", "a3", "a4", "a5")

CHECK_LABELS = {
    "r1": "Functionality",
    "r2": "Request category",
    "r3": "Real customer account",
    "r4": "Response time",
    "r5": "Status ownership",
    "r7": "Rootly/Jira link",
    "r8": "Oncall completeness",
    # Not computed any more; a stored row can still carry a verdict from it.
    "r9": "Legacy check (R9)",
    "a1": "Category accuracy",
    "a3": "Response quality",
    "a4": "Status vs conversation",
    "a5": "Not closed prematurely",
    OVERALL: "Overall verdict",
}

# The editable rules key an observation points at, or absent when there is
# honestly nothing to pre-fill. R1, R2 and R7 take no parameters at all, and R5
# reads from eight different rosters — naming one of them would be a guess
# dressed up as a suggestion, so those say None and explain the options in
# `implies` instead.
TARGET_RULE_KEYS = {
    "r3": "r3_internal_account_ids",
    "r4": "r4_sla_hours",
    "r8": "r8_oncall_categories",
    "a1": "a_guidance",
    "a3": "a_guidance",
    "a4": "a_guidance",
    "a5": "a_guidance",
    OVERALL: "a_guidance",
}

_TUNABLE_HINT = {
    "r1": "R1 takes no parameters, so there is nothing to tune: either these "
          "tickets do not belong in scope at all (excluded_states), or reviewers "
          "are signing off tickets that genuinely fail it.",
    "r2": "R2 takes no parameters, so there is nothing to tune: either these "
          "tickets do not belong in scope at all (excluded_states), or reviewers "
          "are signing off tickets that genuinely fail it.",
    "r3": "Look for an entry in r3_internal_account_ids or "
          "r3_invalid_name_fragments that is matching real customer accounts.",
    "r4": "Look at r4_sla_hours — the SLA may be tighter than the hours the team "
          "actually works to.",
    "r5": "Look at the handoff rosters (cs_user_ids, impl_user_ids, eng_user_ids, "
          "pt_user_ids and their group lists) and at r5_group_states: someone "
          "who owns handoffs but is missing from a roster reads as an unowned "
          "ticket.",
    "r7": "R7 takes no parameters, so there is nothing to tune: either these "
          "tickets do not belong in scope at all (excluded_states), or reviewers "
          "are signing off tickets that genuinely fail it.",
    "r8": "Look at r8_oncall_categories — a category the team really does "
          "escalate through may be missing from the list.",
    "r9": "R9 is no longer computed, so this verdict came from a stored row "
          "rather than current scoring. Rescoring these tickets should clear it.",
    "a1": "Add a line to a_guidance telling the model how to read this case.",
    "a3": "Add a line to a_guidance telling the model how to read this case.",
    "a4": "Add a line to a_guidance telling the model how to read this case.",
    "a5": "Add a line to a_guidance telling the model how to read this case.",
    OVERALL: "Add a line to a_guidance describing what the rubric is missing "
             "here — no single check objected, so the pattern is not in it yet.",
}

# A Pylon link is arbitrary text from outside. HTML-escaping protects the text
# and does nothing to the scheme, so `javascript:...` survives escaping intact
# and stays clickable. Only http(s) leaves this module.
_SAFE_LINK = re.compile(r"^https?://", re.IGNORECASE)

# The latest review per ticket. MAX(id) is taken over EVERY review, not over the
# ones inside the window: picking the newest of the in-window rows would let a
# superseded override outvote the Revert that replaced it. Constant SQL — no
# caller value is interpolated anywhere in this module.
_LATEST_REVIEW = """
    SELECT r.ticket_id, r.decision, r.kept_ai, r.reviewed_at
    FROM ticket_reviews r
    JOIN (SELECT ticket_id, MAX(id) AS max_id
          FROM ticket_reviews GROUP BY ticket_id) x ON x.max_id = r.id
"""


def _window(days: int) -> tuple[str, str]:
    """The last `days` calendar days in UTC, inclusive of today.

    UTC, not the schedule timezone, because `reviewed_at` is written as a UTC
    timestamp by review.accept_ticket — measuring the window in another zone
    would shift the boundary away from the values being compared.
    """
    today = datetime.now(timezone.utc).date()
    return (today - timedelta(days=days - 1)).isoformat(), today.isoformat()


def _load_sign_offs(start: str, end: str) -> list[dict]:
    """Every in-scope ticket whose latest review is a sign-off in the window.

    Reverts are dropped here rather than later: they are the absence of a
    sign-off, so they belong in neither the numerator nor the denominator.
    """
    clauses = [
        # A ticket Pylon no longer returns stops counting as evidence, the same
        # way it stops counting toward anyone's standing.
        "t.deleted_at IS NULL",
        "rev.decision IN ('Pass','Fail')",
        # substr, not date(): reviewed_at carries a UTC offset and SQLite's
        # date() would reinterpret it, moving a review across the boundary.
        "substr(rev.reviewed_at, 1, 10) BETWEEN ? AND ?",
    ]
    params: list = [start, end]

    excluded = qc_rules.excluded_states()
    if excluded:
        clauses.append(f"t.state NOT IN ({','.join('?' * len(excluded))})")
        params += excluded

    rule_cols = ", ".join(f"rc.{k}" for k in R_KEYS)
    with db.get_conn() as conn:
        rows = conn.execute(f"""
            SELECT t.id, t.number, t.title, t.link,
                   ac.overall_result AS ai_result,
                   ac.a1, ac.a3, ac.a4, ac.a5,
                   {rule_cols},
                   rev.decision, rev.kept_ai
            FROM tickets t
            JOIN ({_LATEST_REVIEW}) rev ON rev.ticket_id = t.id
            LEFT JOIN ai_checks   ac ON ac.ticket_id = t.id
            LEFT JOIN rule_checks rc ON rc.ticket_id = t.id
            WHERE {' AND '.join(clauses)}
            ORDER BY t.number
        """, params).fetchall()

    return [dict(r) for r in rows]


def _is_override(row: dict) -> bool:
    """True when this sign-off contradicts the AI — see the module docstring."""
    if row.get("kept_ai"):
        return False
    ai = row.get("ai_result")
    if not ai:
        return False          # never graded, so there is no verdict to contradict
    return row["decision"] != ai


def _drivers(row: dict) -> list[str]:
    """The checks that produced this ticket's AI verdict.

    Mirrors qc_runner._compute_overall: a Fail comes from any failing R-check,
    or A3=Poor, or A5=Fail; a Needs Review from any R-check or A-check that
    hedged. A Pass is driven by nothing — no check objected — so this returns
    [] and the caller attributes the override to OVERALL.

    A ticket can have several drivers and counts toward each of them. That is
    deliberate: when R3 and R4 both failed a ticket a reviewer passed, either
    could be the one that is wrong, and suppressing one would hide it.
    """
    # Only checks that are switched on. A disabled check keeps its stored
    # verdict, so without this the panel would propose tuning a rule that is not
    # running — advice that cannot be acted on and reads as a bug.
    import rules as qc_rules
    live = qc_rules.enabled_rule_keys(tuple(R_KEYS))

    verdict = row.get("ai_result")
    if verdict == "Fail":
        drivers = [k for k in live if row.get(k) == "Fail"]
        if row.get("a3") == "Poor":
            drivers.append("a3")
        if row.get("a5") == "Fail":
            drivers.append("a5")
        return drivers
    if verdict == "Needs Review":
        return ([k for k in live if row.get(k) == "Needs Review"]
                + [k for k in _A_HEDGE_KEYS if row.get(k) == "Needs Review"])
    return []


def _sample_entry(row: dict) -> dict:
    """One evidence ticket. `link` is omitted entirely unless it is http(s)."""
    entry = {
        "number": row.get("number"),
        "title": row.get("title") or "",
        "ticket_id": row["id"],
    }
    link = (row.get("link") or "").strip()
    if _SAFE_LINK.match(link):
        entry["link"] = link
    return entry


def _implies(check: str, ai_said: str, human_said: str, count: int) -> str:
    """Plain English: what this pattern is, and which edit it points at."""
    label = CHECK_LABELS.get(check, check.upper())
    if check == OVERALL:
        lead = (f"The AI graded {count} tickets {ai_said} and reviewers made them "
                f"{human_said}, with no single check to blame for the verdict.")
    elif human_said == "Pass":
        lead = (f"{label} drove {ai_said} on {count} tickets that reviewers then "
                f"passed, so it is stricter than the team it grades for.")
    else:
        lead = (f"{label} drove {ai_said} on {count} tickets that reviewers then "
                f"failed, so it hedged where the team was certain.")
    return f"{lead} {_TUNABLE_HINT.get(check, '')}".strip()


def _observation(check: str, ai_said: str, human_said: str,
                 tickets: list[dict]) -> dict:
    """One grouped disagreement, with its evidence and the edit it implies."""
    return {
        # An R-check driving the verdict is a rules problem; an A-check (or an
        # unattributable verdict) is a prompt problem. They land on different
        # people, so the caller must be able to tell them apart.
        "kind": "rule_suspect" if check.startswith("r") else "ai_overridden",
        "check": check,
        "check_label": CHECK_LABELS.get(check, check.upper()),
        "ai_said": ai_said,
        "human_said": human_said,
        "count": len(tickets),
        "sample": [_sample_entry(t) for t in tickets[:SAMPLE_SIZE]],
        "implies": _implies(check, ai_said, human_said, len(tickets)),
        "target_rule_key": TARGET_RULE_KEYS.get(check),
    }


def build(days: int = 30) -> dict:
    """Grouped human-vs-AI disagreements over the last `days` days.

    Read-only. `total_reviews` is the denominator: sign-offs on in-scope tickets
    whose latest review falls in the window, Reverts excluded. `total_overrides`
    is how many of those contradicted the AI. `gated_out` counts the groups
    suppressed by MIN_OVERRIDES, so a quiet page can be told apart from a page
    that is holding back thin evidence.
    """
    window = max(1, int(days))
    start, end = _window(window)
    rows = _load_sign_offs(start, end)

    # Grouped by (check, AI verdict, human verdict): "R3 turned Fail into Pass"
    # is a different proposal from "R3 turned Needs Review into Fail".
    groups: dict[tuple[str, str, str], list[dict]] = {}
    overrides = 0
    for row in rows:
        if not _is_override(row):
            continue
        overrides += 1
        for check in _drivers(row) or [OVERALL]:
            key = (check, row["ai_result"], row["decision"])
            groups.setdefault(key, []).append(row)

    observations: list[dict] = []
    gated_out = 0
    for (check, ai_said, human_said), tickets in groups.items():
        if len(tickets) < MIN_OVERRIDES:
            gated_out += 1
            continue
        observations.append(_observation(check, ai_said, human_said, tickets))

    # Strongest evidence first; the check name breaks ties so the order is
    # stable between calls on unchanged data.
    observations.sort(key=lambda o: (-o["count"], o["check"]))

    return {
        "window_days": window,
        "range": {"start": start, "end": end},
        "total_reviews": len(rows),
        "total_overrides": overrides,
        "observations": observations,
        "gated_out": gated_out,
    }
