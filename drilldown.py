"""
Failure drill-down: the tickets behind one number on the leaderboard.

`leaderboard.build()` answers "Ann failed R3 four times". A lead cannot act on a
count — they need the four tickets and a link into Pylon. This module is that
one query, and nothing else.

Two invariants make it safe to hand a check name straight from a URL:

    allowlist   `check` is looked up in `_CHECKS`, never interpolated into SQL.
                An unknown name raises ValueError; it must never degrade into
                "no predicate", which would list every ticket in the range.
    scheme      `tickets.link` is Pylon's value, not ours. A `javascript:` URL
                survives HTML escaping, so a link is emitted only when it
                matches `^https?://` and is None otherwise.

What "failing" means is the backend's decision, not this module's. It is read
off `qc_runner._compute_overall`, which is the only place a verdict is made:

    R1–R8      the check column literally reads 'Fail'
    A3         'Poor'  (not 'Needs Improvement' — that is not a failure)
    A5         'Fail'
    A1, A4     'Fail'
    A2         NEVER a failure — see `NOT_A_FAILURE` below

Two of those are worth stating out loud, because a reader will otherwise assume
this module is wrong:

  * A1 and A4 reading 'Fail' does NOT make the overall verdict Fail.
    `_compute_overall` only escalates a3='Poor' and a5='Fail' to Fail; a1/a4
    contribute to the verdict solely through 'Needs Review'. So `check="a1"`
    lists tickets where A1 itself failed, some of which are graded Pass overall.
    That is the backend's semantics, faithfully reported, not a bug here.

  * A2 is sentiment (Positive/Neutral/Concerned/Frustrated/Urgent) and carries
    no pass/fail meaning at all. Asking for it returns the *notable* end of the
    scale so a lead can read the room, and `NOT_A_FAILURE` plus the label say so
    explicitly. Do not let a caller present these as failures.

The displayed verdict is the effective grade — the latest human sign-off if one
exists, else the AI result — imported from `leaderboard` rather than rewritten.
A second copy of that expression is what made Slack and the dashboard disagree.

Read-only. Nothing here writes.
"""

import logging
import re

import db
# Imported rather than re-declared, deliberately: one definition of the
# effective grade, one definition of which R-checks the leaderboard counts. They
# are private to `leaderboard` only because nothing outside it needed them
# before this module existed.
from leaderboard import _EFFECTIVE_GRADE, _LATEST_REVIEW, RULE_KEYS

logger = logging.getLogger(__name__)

UNASSIGNED = "Unassigned"

# A drill-down dialog is a page of evidence, not a data export. The cap is on
# the page; `count` always reports the true total and `truncated` says a page
# was cut, so nothing is hidden by it.
DEFAULT_LIMIT = 200
MAX_LIMIT = 1000

_HTTP_URL = re.compile(r"^https?://", re.IGNORECASE)
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# Labels mirror app.RULE_DESCRIPTIONS and static/index.html's CHECKS. Kept here
# because a module that validates a check name should be able to name it without
# importing the web app.
_R_LABELS = {
    "r1": "R1 — Functionality",
    "r2": "R2 — Request category",
    "r3": "R3 — Real customer account",
    "r4": "R4 — Response time",
    "r5": "R5 — Status ownership",
    "r6": "R6 — Retired",
    "r7": "R7 — Rootly/Jira link",
    "r8": "R8 — Oncall completeness",
    "r9": "R9 — Retired",
}

# check -> (label, fixed SQL predicate). The predicate is module source, never
# caller input: the R-check keys come from `leaderboard.RULE_KEYS`, so the set
# that can be drilled into is exactly the set the leaderboard counts, and the
# two cannot drift apart.
_CHECKS: dict[str, tuple[str, str]] = {
    key: (_R_LABELS.get(key, key.upper()), f"rc.{key} = 'Fail'")
    for key in RULE_KEYS
}
_CHECKS.update({
    "a1": ("A1 — Category accuracy", "ac.a1 = 'Fail'"),
    # Not "A2 failures". The label carries the caveat because the dialog title
    # is the one place a reader is guaranteed to look.
    "a2": ("A2 — Customer sentiment (notable, not a failure)",
           "ac.a2 IN ('Concerned','Frustrated','Urgent')"),
    "a3": ("A3 — Response quality", "ac.a3 = 'Poor'"),
    "a4": ("A4 — Status vs conversation", "ac.a4 = 'Fail'"),
    "a5": ("A5 — Premature closure", "ac.a5 = 'Fail'"),
})

ALLOWED_CHECKS = tuple(sorted(_CHECKS))

# Checks whose match is "notable", not "failed". Callers must not word these as
# failures; see the module docstring.
NOT_A_FAILURE = frozenset({"a2"})

# The name a ticket is filtered and displayed under. `leaderboard` groups on
# COALESCE(assignee_name, 'Unassigned'); this folds blank names in too, so that
# filtering by "Unassigned" and reading the column back agree with each other.
_ASSIGNEE_DISPLAY = "COALESCE(NULLIF(TRIM(t.assignee_name), ''), 'Unassigned')"


def _resolve_check(check: str) -> tuple[str, str, str]:
    """(key, label, predicate) for an allowed check name.

    Raises ValueError for anything else. Failing loudly is the point: a silent
    fallback here would answer a bad request with every ticket in the range.
    """
    key = str(check or "").strip().lower()
    if key not in _CHECKS:
        raise ValueError(
            f"Unknown check {check!r}; expected one of {', '.join(ALLOWED_CHECKS)}"
        )
    label, predicate = _CHECKS[key]
    return key, label, predicate


def _require_iso_date(value: str, field: str) -> str:
    """Dates are compared as strings in SQLite, so a malformed one does not
    error — it silently matches the wrong rows. Reject it at the door."""
    if not _ISO_DATE.match(str(value)):
        raise ValueError(f"{field} must be an ISO date (YYYY-MM-DD), got {value!r}")
    return value


def safe_link(link: str | None) -> str | None:
    """Pylon's URL, or None when it is not one we will put in an href.

    Escaping is not enough: `javascript:alert(1)` contains nothing an HTML
    escaper touches, so the scheme has to be checked here rather than trusted
    at the template.
    """
    text = (link or "").strip()
    return text if _HTTP_URL.match(text) else None


def _assignee_clause(assignee: str) -> tuple[str, list]:
    """Filter for one assignee. "Unassigned" (or a blank name) means no owner.

    None is handled by the caller and means every assignee — the difference
    between "nobody owns these" and "no filter" must stay explicit.
    """
    name = str(assignee).strip()
    if not name or name == UNASSIGNED:
        return "TRIM(COALESCE(t.assignee_name, '')) = ''", []
    return "t.assignee_name = ?", [name]


def _build_where(predicate: str, assignee: str | None,
                 start: str | None, end: str | None) -> tuple[str, list]:
    """WHERE clause and bound parameters shared by the count and the page."""
    import rules as qc_rules

    # Always present. A ticket withdrawn at source stops being evidence against
    # anyone, exactly as in leaderboard._load_rows.
    clauses = ["t.deleted_at IS NULL", predicate]
    params: list = []

    if start:
        clauses.append("t.fetch_date >= ?")
        params.append(_require_iso_date(start, "start"))
    if end:
        clauses.append("t.fetch_date <= ?")
        params.append(_require_iso_date(end, "end"))

    if assignee is not None:
        clause, values = _assignee_clause(assignee)
        clauses.append(clause)
        params += values

    excluded = qc_rules.excluded_states()
    if excluded:
        clauses.append(f"t.state NOT IN ({','.join('?' * len(excluded))})")
        params += excluded

    return " AND ".join(clauses), params


def _row_to_ticket(row) -> dict:
    return {
        "ticket_id": row["ticket_id"],
        "number": row["number"],
        "title": row["title"],
        "link": safe_link(row["link"]),
        "state": row["state"],
        "fetch_date": row["fetch_date"],
        "assignee_name": row["assignee_name"],
        "overall_result": row["overall_result"],
    }


def tickets_failing(check: str, assignee: str | None = None,
                    start: str | None = None, end: str | None = None,
                    limit: int = DEFAULT_LIMIT) -> dict:
    """The tickets where `check` failed, newest first.

    `check` must be one of `ALLOWED_CHECKS`; anything else raises ValueError.
    For a check in `NOT_A_FAILURE` (A2 sentiment) the matches are *notable*, not
    failures — A2 never contributes a failure to the verdict, so a caller must
    not present them as such.

    `assignee=None` means every assignee. `"Unassigned"` means the tickets with
    no owner. Both `start` and `end` are optional and inclusive; either may be
    given alone to leave that end of the range open.

    `count` is the total number of matching tickets, which may exceed
    `len(tickets)`: `limit` caps the page, and `truncated` is True when it cut
    one. The count is never capped, so a truncated page is always visible as
    such rather than silently short.

    Soft-deleted tickets and states in `rules.excluded_states()` are excluded,
    matching the leaderboard the numbers came from. `overall_result` is the
    effective grade — latest human sign-off, else the AI verdict. A `link` that
    is not http(s) comes back as None.
    """
    key, label, predicate = _resolve_check(check)
    page = max(1, min(int(limit), MAX_LIMIT))
    where, params = _build_where(predicate, assignee, start, end)

    # ai_checks and rule_checks are keyed one row per ticket, and the latest
    # review is collapsed to one row per ticket, so these joins cannot multiply
    # a ticket and COUNT(*) counts tickets.
    from_clause = f"""
        FROM tickets t
        LEFT JOIN ai_checks   ac ON ac.ticket_id = t.id
        LEFT JOIN rule_checks rc ON rc.ticket_id = t.id
        LEFT JOIN ({_LATEST_REVIEW}) rev ON rev.ticket_id = t.id
        WHERE {where}
    """

    with db.get_conn() as conn:
        count = conn.execute(
            f"SELECT COUNT(*) AS n {from_clause}", params
        ).fetchone()["n"]
        rows = conn.execute(f"""
            SELECT
                t.id         AS ticket_id,
                t.number     AS number,
                t.title      AS title,
                t.link       AS link,
                t.state      AS state,
                t.fetch_date AS fetch_date,
                {_ASSIGNEE_DISPLAY} AS assignee_name,
                {_EFFECTIVE_GRADE}  AS overall_result
            {from_clause}
            ORDER BY t.fetch_date DESC, t.number DESC
            LIMIT ?
        """, params + [page]).fetchall()

    tickets = [_row_to_ticket(r) for r in rows]
    if count > len(tickets):
        logger.info("Drill-down %s truncated: showing %d of %d",
                    key, len(tickets), count)

    return {
        "check": key,
        "check_label": label,
        "assignee": assignee,
        "range": {"start": start, "end": end},
        "count": count,
        "truncated": count > len(tickets),
        "tickets": tickets,
    }
