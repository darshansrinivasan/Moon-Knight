"""
Manual QC review: who may sign off a ticket, and the append-only accept record.

AI grades in ai_checks stay as the machine fact. A review is a separate row.
The effective grade is the latest review decision, else the AI overall.

Coverage: a named group of ticket assignees mapped to one reviewer (from the
Slack directory, identified by email so it matches Google sign-in). App
admins (super-admins) can review every ticket regardless of coverage.
"""

from datetime import datetime, timezone

import db

DECISIONS = {"Pass", "Fail"}


class ReviewDenied(RuntimeError):
    pass


class ReviewInvalid(RuntimeError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def list_assignee_names() -> list[str]:
    """Assignees seen on fetched tickets — the people a coverage can own."""
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT DISTINCT assignee_name FROM tickets"
            " WHERE assignee_name IS NOT NULL AND assignee_name != ''"
            " ORDER BY assignee_name COLLATE NOCASE"
        ).fetchall()
    names = [r["assignee_name"] for r in rows]
    if "Unassigned" not in names:
        names.append("Unassigned")
    return names


def list_coverages() -> list[dict]:
    with db.get_conn() as conn:
        heads = conn.execute(
            "SELECT * FROM review_coverages ORDER BY name COLLATE NOCASE"
        ).fetchall()
        members = conn.execute(
            "SELECT coverage_id, assignee_name FROM review_coverage_assignees"
            " ORDER BY assignee_name COLLATE NOCASE"
        ).fetchall()
    by_id: dict[int, list[str]] = {}
    for m in members:
        by_id.setdefault(m["coverage_id"], []).append(m["assignee_name"])
    return [
        {**dict(h), "assignees": by_id.get(h["id"], [])}
        for h in heads
    ]


def save_coverage(payload: dict, updated_by: str) -> list[dict]:
    """Create or replace a coverage. `id` present means update."""
    name = (payload.get("name") or "").strip()
    email = (payload.get("reviewer_email") or "").strip().lower()
    reviewer_name = (payload.get("reviewer_name") or "").strip()
    assignees = [
        str(a).strip() for a in (payload.get("assignees") or []) if str(a).strip()
    ]
    if not name:
        raise ReviewInvalid("Coverage needs a name (e.g. APAC, NAM, EMEA)")
    if not email or "@" not in email:
        raise ReviewInvalid("Pick a reviewer from the Slack directory (they must have an email)")
    cid = payload.get("id")

    with db.get_conn() as conn:
        if cid:
            exists = conn.execute(
                "SELECT id FROM review_coverages WHERE id = ?", (cid,)
            ).fetchone()
            if not exists:
                raise ReviewInvalid("Coverage not found")
            conn.execute(
                "UPDATE review_coverages SET name = ?, reviewer_email = ?, reviewer_name = ?,"
                " updated_by = ?, updated_at = ? WHERE id = ?",
                (name, email, reviewer_name, updated_by, _now(), cid),
            )
        else:
            cur = conn.execute(
                "INSERT INTO review_coverages"
                " (name, reviewer_email, reviewer_name, updated_by, updated_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (name, email, reviewer_name, updated_by, _now()),
            )
            cid = cur.lastrowid
        conn.execute(
            "DELETE FROM review_coverage_assignees WHERE coverage_id = ?", (cid,)
        )
        conn.executemany(
            "INSERT OR IGNORE INTO review_coverage_assignees (coverage_id, assignee_name)"
            " VALUES (?, ?)",
            [(cid, a) for a in assignees],
        )
    return list_coverages()


def delete_coverage(coverage_id: int) -> list[dict]:
    with db.get_conn() as conn:
        conn.execute(
            "DELETE FROM review_coverage_assignees WHERE coverage_id = ?", (coverage_id,)
        )
        conn.execute("DELETE FROM review_coverages WHERE id = ?", (coverage_id,))
    return list_coverages()


def assignees_for(user: dict) -> set[str]:
    """Assignees this signed-in user is allowed to review. Empty if admin (all)."""
    if user.get("role") == "admin":
        return set()
    email = (user.get("email") or "").lower()
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT a.assignee_name FROM review_coverage_assignees a"
            " JOIN review_coverages c ON c.id = a.coverage_id"
            " WHERE lower(c.reviewer_email) = ?",
            (email,),
        ).fetchall()
    return {r["assignee_name"] for r in rows}


def is_admin(user: dict) -> bool:
    return user.get("role") == "admin"


def can_review_ticket(user: dict, ticket: dict) -> bool:
    if is_admin(user):
        return True
    covered = assignees_for(user)
    name = ticket.get("assignee_name") or "Unassigned"
    return name in covered


def latest_review(ticket_id: str) -> dict | None:
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM ticket_reviews WHERE ticket_id = ? ORDER BY id DESC LIMIT 1",
            (ticket_id,),
        ).fetchone()
    return dict(row) if row else None


def latest_reviews(ticket_ids: list[str]) -> dict[str, dict]:
    if not ticket_ids:
        return {}
    placeholders = ",".join("?" * len(ticket_ids))
    with db.get_conn() as conn:
        rows = conn.execute(
            f"SELECT r.* FROM ticket_reviews r"
            f" JOIN (SELECT ticket_id, MAX(id) AS max_id FROM ticket_reviews"
            f"       WHERE ticket_id IN ({placeholders})"
            f"       GROUP BY ticket_id) x ON x.max_id = r.id",
            ticket_ids,
        ).fetchall()
    return {r["ticket_id"]: dict(r) for r in rows}


def apply_effective_grades(tickets: list[dict]) -> list[dict]:
    """Overlay the latest human sign-off onto each ticket's `overall_result`.

    The effective grade of a ticket is the latest review decision, else the AI
    grade; `ai_result` always keeps the machine's own verdict. Every surface
    that reports a grade must go through here, or the same ticket reads Pass on
    the dashboard and Fail in Slack. Mutates and returns `tickets`.

    This carries no authorization: it says what the grade *is*, not who may
    change it. Use `annotate_tickets` for anything a signed-in user sees.
    """
    reviews = latest_reviews([t["id"] for t in tickets])
    for t in tickets:
        ai = t.get("overall_result")
        rev = _active_review(reviews.get(t["id"]))
        t["ai_result"] = ai
        t["overall_result"] = rev["decision"] if rev else ai
        t["review"] = {
            "decision": rev["decision"],
            "kept_ai": bool(rev["kept_ai"]),
            "reviewer_email": rev["reviewer_email"],
            "reviewer_name": rev["reviewer_name"],
            "reviewed_at": rev["reviewed_at"],
            "note": rev.get("note") or "",
        } if rev else None
    return tickets


def annotate_tickets(tickets: list[dict], user: dict) -> list[dict]:
    """Effective grades plus whether *this* user may sign each ticket off."""
    apply_effective_grades(tickets)
    covered = None if is_admin(user) else assignees_for(user)
    for t in tickets:
        assignee = t.get("assignee_name") or "Unassigned"
        t["can_review"] = True if covered is None else assignee in covered
    return tickets


def _active_review(rev: dict | None) -> dict | None:
    """Latest row wins, but Revert clears the human grade back to AI."""
    if not rev or rev.get("decision") not in DECISIONS:
        return None
    return rev


def _ticket_row(ticket_id: str) -> dict | None:
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT t.id, t.assignee_name, t.number, ac.overall_result AS ai_result"
            " FROM tickets t LEFT JOIN ai_checks ac ON ac.ticket_id = t.id"
            " WHERE t.id = ?",
            (ticket_id,),
        ).fetchone()
    return dict(row) if row else None


def accept_ticket(ticket_id: str, user: dict, decision: str, note: str = "") -> dict:
    """
    Sign off one ticket.

    `decision`:
      - `Pass` / `Fail` — override (or confirm) the overall grade
      - `accept` — keep the AI overall; rejected if the AI grade is not Pass or Fail
      - `revert` — clear the latest sign-off; effective grade becomes the AI overall
    """
    ticket = _ticket_row(ticket_id)
    if not ticket:
        raise ReviewInvalid("Ticket not found")
    if not can_review_ticket(user, ticket):
        raise ReviewDenied(
            "You can only review tickets assigned to people in your coverage. "
            "Admins can review every ticket."
        )

    kept_ai = 0
    decision = (decision or "").strip()
    if decision == "revert":
        prev = _active_review(latest_review(ticket_id))
        if not prev:
            raise ReviewInvalid("This ticket is not signed off")
        decision = "Revert"
    else:
        ai = ticket.get("ai_result")
        if not ai:
            raise ReviewInvalid("Run QC on this ticket before accepting it")
        if decision == "accept":
            if ai not in DECISIONS:
                raise ReviewInvalid(
                    f"AI graded this {ai}. Choose Pass or Fail to accept it."
                )
            decision = ai
            kept_ai = 1
        elif decision not in DECISIONS:
            raise ReviewInvalid("Decision must be Pass, Fail, accept, or revert")

    record = {
        "ticket_id": ticket_id,
        "decision": decision,
        "kept_ai": kept_ai,
        "reviewer_email": user["email"],
        "reviewer_name": user.get("name") or user["email"],
        "note": (note or "").strip()[:500],
        "reviewed_at": _now(),
    }
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO ticket_reviews"
            " (ticket_id, decision, kept_ai, reviewer_email, reviewer_name, note, reviewed_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (record["ticket_id"], record["decision"], record["kept_ai"],
             record["reviewer_email"], record["reviewer_name"],
             record["note"], record["reviewed_at"]),
        )
    return record
