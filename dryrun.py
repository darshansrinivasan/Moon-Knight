"""Grade a small sample with unsaved rubric text and report what would move.

Editing a grading rubric is editing the product's judgement, and the only honest
way to know what an edit does is to run it. The alternative — save, re-run a
date, read the diff, revert if it was wrong — costs a full day of AI calls and
overwrites real grades in the meantime.

Three properties make this safe to hand to an admin:

  * **Nothing is written.** No `ai_checks` row, no run record, no rules change.
    The draft text reaches the model through an override parameter and is never
    persisted; a dry-run cannot alter a grade a reviewer is looking at.
  * **The sample is graded tickets only.** A ticket with no stored grade has
    nothing to compare against, so including it would pad the sample and the
    bill while telling the admin nothing.
  * **The spend is bounded and stated.** At most `MAX_LIMIT` tickets per call,
    and the reported cost is the real token usage from the call, labelled as an
    estimate of what the same edit would cost across a whole run.

The draft is merged *over* the saved rules rather than replacing them, so a
caller that sends only the guidance box still grades against the saved rubric
for everything else. Sending a partial document must not silently reset the
sections it omits.
"""

import db
import prompts
import qc_runner
import rules as qc_rules

MAX_LIMIT = 10
DEFAULT_LIMIT = 5

# The grades compared side by side. `overall` is included because it is what a
# reviewer actually acts on, and a rubric edit that moves no A-check but flips a
# verdict is the most important case to see.
COMPARED = (*prompts.A_CHECK_KEYS, "overall")


def clamp(limit) -> int:
    try:
        n = int(limit)
    except (TypeError, ValueError):
        return DEFAULT_LIMIT
    return max(1, min(MAX_LIMIT, n))


def draft_rules(draft: dict | None) -> dict:
    """Saved rules with the draft laid over the keys it actually supplies."""
    merged = qc_rules.current()
    for key, value in (draft or {}).items():
        if key in merged:
            merged[key] = value
    return merged


def _sample(limit: int, date: str | None = None) -> list[dict]:
    """The most recently graded tickets, newest first.

    Recency rather than randomness: a dry-run run twice on the same draft should
    compare the same tickets, or the admin cannot tell a rubric change from a
    sampling change.
    """
    where = "WHERE ac.overall_result IS NOT NULL AND t.deleted_at IS NULL"
    params: list = []
    if date:
        where += " AND t.fetch_date = ?"
        params.append(date)

    with db.get_conn() as conn:
        rows = conn.execute(f"""
            SELECT t.id, t.number, t.title, t.link, t.state, t.assignee_name,
                   t.custom_fields, t.source, t.customer_portal_visible,
                   t.fetch_date,
                   a.name AS account_name, a.type AS account_type,
                   rc.r1, rc.r2, rc.r3, rc.r4, rc.r5, rc.r7, rc.r8,
                   ac.a1, ac.a2, ac.a3, ac.a4, ac.a5, ac.overall_result
            FROM tickets t
            LEFT JOIN accounts    a  ON t.account_id = a.id
            LEFT JOIN rule_checks rc ON t.id = rc.ticket_id
            JOIN      ai_checks   ac ON t.id = ac.ticket_id
            {where}
            ORDER BY t.fetch_date DESC, t.number DESC
            LIMIT ?
        """, [*params, limit]).fetchall()

        tickets = [dict(r) for r in rows]
        for t in tickets:
            msgs = conn.execute(
                "SELECT author_name, is_customer, is_private, message_html "
                "FROM messages WHERE ticket_id = ? ORDER BY timestamp",
                (t["id"],),
            ).fetchall()
            t["messages"] = [dict(m) for m in msgs]

    return tickets


def _stored_grades(t: dict) -> dict:
    grades = {k: t.get(k) for k in prompts.A_CHECK_KEYS}
    grades["overall"] = t.get("overall_result")
    return grades


def _draft_grades(t: dict, result: dict) -> dict:
    """The A-grades the model just returned, plus the verdict they imply.

    The overall verdict is recomputed through the same function a real run uses,
    against the ticket's existing R-checks, so the comparison shows the verdict
    the admin would actually get — not the A-grades alone.
    """
    grades = {k: result.get(k) for k in prompts.A_CHECK_KEYS}
    r_checks = {k: t.get(k) for k in qc_runner.R_CHECK_KEYS}
    grades["overall"] = qc_runner._compute_overall(r_checks, grades)
    return grades


def _changed(current: dict, draft: dict) -> list:
    """Keys whose grade moved. A missing draft grade is not a change."""
    return [k for k in COMPARED
            if draft.get(k) is not None and current.get(k) != draft.get(k)]


def run(draft: dict | None = None, limit=DEFAULT_LIMIT,
        date: str | None = None) -> dict:
    """Grade a sample with the draft rubric. Writes nothing."""
    limit = clamp(limit)
    overrides = draft_rules(draft)
    tickets = _sample(limit, date)

    if not tickets:
        return {"sampled": 0, "results": [], "cost_usd": 0.0,
                "cost_estimated": True, "prompt_changed": False,
                "note": "No graded tickets are available to compare against. "
                        "Run QC on a date first, then dry-run against it."}

    stats = qc_runner.RunStats()
    results = qc_runner._score_batch(tickets, stats, overrides)

    out = []
    for t, result in zip(tickets, results):
        current = _stored_grades(t)
        if not result:
            # A ticket the model failed on is reported, not dropped. Silently
            # shrinking the sample would read as "nothing would change here".
            out.append({
                "id": t["id"], "number": t["number"], "title": t["title"],
                "link": t.get("link"), "current": current, "draft": {},
                "changed": [], "error": "The model returned no grade for this "
                                        "ticket, so there is nothing to compare.",
            })
            continue
        drafted = _draft_grades(t, result)
        out.append({
            "id": t["id"], "number": t["number"], "title": t["title"],
            "link": t.get("link"), "current": current, "draft": drafted,
            "changed": _changed(current, drafted),
            "ai_notes": result.get("ai_notes"),
        })

    return {
        "sampled":        len(out),
        "results":        out,
        "cost_usd":       stats.cost_usd(),
        "cost_estimated": stats.cost_is_estimated(),
        "prompt_changed": prompts.fingerprint(overrides) != prompts.fingerprint(),
        "models":         stats.models,
    }
