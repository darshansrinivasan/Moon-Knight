"""
Recompute overall_result from the current rule_checks values.

Run after rule_checks change (a refetch rescores R-checks, or an admin edits the
rules) so the stored overall grade and the R-check failure notes stay consistent
with what the rules now say. No AI calls — pure database reads and writes.

Import-safe: this module previously called `os.chdir()` and `load_dotenv()` at
import time, which moved the working directory of the whole web process because
app.py imports it. Side effects now happen only inside `run()` / `main()`.
"""

import json
import logging
from collections import defaultdict

import db
from qc_runner import _compute_overall, _r_check_notes, _strip_r_notes

logger = logging.getLogger(__name__)

R_KEYS = ["r1", "r2", "r3", "r4", "r5", "r7", "r8", "r9"]


def run(date_str: str | None = None) -> dict:
    """Resync one day, or the whole table when `date_str` is None.

    Returns counts of what changed, so callers can log or display it rather
    than reading stdout.
    """
    where, params = ("WHERE ac.fetch_date = ?", (date_str,)) if date_str else ("", ())
    # Soft-deleted tickets keep their rows but must not be resynced — the grade
    # is frozen at whatever it was when the ticket left Pylon.
    deleted_guard = ("AND t.deleted_at IS NULL" if where
                     else "WHERE t.deleted_at IS NULL")

    with db.get_conn() as conn:
        rows = conn.execute(f"""
            SELECT ac.ticket_id,
                   ac.a1, ac.a3, ac.a4, ac.a5,
                   ac.ai_notes, ac.overall_result,
                   rc.r1, rc.r2, rc.r3, rc.r4, rc.r5, rc.r7, rc.r8, rc.r9,
                   t.custom_fields, t.state,
                   a.name AS account_name
            FROM ai_checks ac
            JOIN rule_checks rc ON ac.ticket_id = rc.ticket_id
            JOIN tickets t      ON ac.ticket_id = t.id
            LEFT JOIN accounts a ON t.account_id = a.id
            {where}
            {deleted_guard}
        """, params).fetchall()

    overall_updates: list = []
    notes_updates: list = []
    overall_changes: dict = defaultdict(int)

    for row in rows:
        t = dict(row)
        r_checks = {k: t[k] for k in R_KEYS}
        a_checks = {"a1": t["a1"], "a3": t["a3"], "a4": t["a4"], "a5": t["a5"]}
        try:
            cf = json.loads(t.get("custom_fields") or "{}")
        except json.JSONDecodeError:
            cf = {}

        new_overall = _compute_overall(r_checks, a_checks)
        if new_overall != t["overall_result"]:
            overall_changes[f"{t['overall_result']} → {new_overall}"] += 1
            overall_updates.append((new_overall, t["ticket_id"]))

        # Strip every previously prepended R-note, then add the current ones, so
        # repeated runs never stack duplicates.
        ai_only = _strip_r_notes(t["ai_notes"] or "")
        r_note = _r_check_notes(
            r_checks, cf,
            state=t.get("state") or "",
            account_name=t.get("account_name") or "",
        )
        new_note = f"{r_note} | {ai_only}".strip(" |") if r_note else ai_only
        if new_note != (t["ai_notes"] or ""):
            notes_updates.append((new_note, t["ticket_id"]))

    with db.get_conn() as conn:
        if overall_updates:
            conn.executemany(
                "UPDATE ai_checks SET overall_result = ? WHERE ticket_id = ?",
                overall_updates,
            )
        if notes_updates:
            conn.executemany(
                "UPDATE ai_checks SET ai_notes = ? WHERE ticket_id = ?",
                notes_updates,
            )

    if overall_updates or notes_updates:
        logger.info(
            "Resynced %s: %d overall, %d notes (%s)",
            date_str or "all dates", len(overall_updates), len(notes_updates),
            ", ".join(f"{k} ×{v}" for k, v in sorted(overall_changes.items())) or "-",
        )

    return {
        "examined": len(rows),
        "overall_updated": len(overall_updates),
        "notes_updated": len(notes_updates),
        "changes": dict(overall_changes),
    }


def main() -> None:
    """CLI entry point: `python resync_overall.py [YYYY-MM-DD]`."""
    import sys

    from dotenv import load_dotenv
    load_dotenv()
    logging.basicConfig(level="INFO", format="%(levelname)s: %(message)s")

    date_str = sys.argv[1] if len(sys.argv) > 1 else None
    result = run(date_str)
    print(f"Examined:        {result['examined']} tickets")
    print(f"Overall updated: {result['overall_updated']}")
    for change, count in sorted(result["changes"].items()):
        print(f"  {change:35s} ×{count}")
    print(f"Notes updated:   {result['notes_updated']}")


if __name__ == "__main__":
    main()
