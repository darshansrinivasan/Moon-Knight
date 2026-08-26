"""
Migration: compute R8 and R9 for all existing tickets and update rule_checks.
Also recomputes overall_result for ai_checked tickets affected by the new rules.
No API calls — pure DB reads and writes.
"""
import sys, os, json
sys.path.insert(0, os.path.dirname(__file__))
os.chdir(os.path.dirname(__file__))

from dotenv import load_dotenv
load_dotenv()

import db
import scorer
from qc_runner import _compute_overall
from datetime import datetime, timezone
from collections import defaultdict


def run():
    # Add columns if missing (init_db handles this too, but run standalone)
    with db.get_conn() as conn:
        for col in ("r8", "r9"):
            try:
                conn.execute(f"ALTER TABLE rule_checks ADD COLUMN {col} TEXT")
            except Exception:
                pass

    # Load all tickets that have rule_checks
    with db.get_conn() as conn:
        rows = conn.execute("""
            SELECT t.id, t.state, t.assignee_id,
                   t.custom_fields, t.external_issues, t.body_html,
                   rc.r1, rc.r2, rc.r3, rc.r4, rc.r5, rc.r7,
                   rc.r8, rc.r9
            FROM tickets t
            JOIN rule_checks rc ON t.id = rc.ticket_id
        """).fetchall()

        msg_rows = conn.execute(
            "SELECT ticket_id, message_html FROM messages ORDER BY timestamp"
        ).fetchall()

        # also load ai_checks for overall recompute
        ai_rows = conn.execute(
            "SELECT ticket_id, a1, a3, a4, a5, overall_result FROM ai_checks"
        ).fetchall()

    msgs_by_ticket = defaultdict(list)
    for m in msg_rows:
        msgs_by_ticket[m["ticket_id"]].append({"message_html": m["message_html"]})

    ai_by_ticket = {r["ticket_id"]: dict(r) for r in ai_rows}

    total = len(rows)
    print(f"Tickets to process: {total}")

    r8_updates      = []
    r9_updates      = []
    overall_updates = []
    r8_changes      = defaultdict(int)
    r9_changes      = defaultdict(int)
    overall_changes = defaultdict(int)
    now             = datetime.now(timezone.utc).isoformat()

    for row in rows:
        t         = dict(row)
        ticket_id = t["id"]
        messages  = msgs_by_ticket[ticket_id]

        cf  = json.loads(t["custom_fields"]  or "{}")
        ext = json.loads(t["external_issues"] or "[]")
        issue = {
            "state":         t["state"],
            "assignee":      t["assignee_id"],
            "custom_fields": cf,
            "body_html":     t["body_html"],
        }

        new_r8 = scorer.r8(issue, messages, ext)
        new_r9 = scorer.r9(issue, messages, ext)
        old_r8 = t.get("r8")
        old_r9 = t.get("r9")

        if new_r8 != old_r8:
            r8_changes[f"{old_r8} → {new_r8}"] += 1
            r8_updates.append((new_r8, now, ticket_id))

        if new_r9 != old_r9:
            r9_changes[f"{old_r9} → {new_r9}"] += 1
            r9_updates.append((new_r9, now, ticket_id))

        # recompute overall_result for ai_checked tickets
        if ticket_id in ai_by_ticket:
            ai = ai_by_ticket[ticket_id]
            r_checks = {
                "r1": t["r1"], "r2": t["r2"], "r3": t["r3"],
                "r4": t["r4"], "r5": t["r5"], "r7": t["r7"],
                "r8": new_r8,  "r9": new_r9,
            }
            a_checks = {"a1": ai["a1"], "a3": ai["a3"], "a4": ai["a4"], "a5": ai["a5"]}
            new_overall = _compute_overall(r_checks, a_checks)
            old_overall = ai["overall_result"]
            if new_overall != old_overall:
                overall_changes[f"{old_overall} → {new_overall}"] += 1
                overall_updates.append((new_overall, ticket_id))

    with db.get_conn() as conn:
        if r8_updates:
            conn.executemany(
                "UPDATE rule_checks SET r8 = ?, checked_at = ? WHERE ticket_id = ?",
                r8_updates,
            )
        if r9_updates:
            conn.executemany(
                "UPDATE rule_checks SET r9 = ?, checked_at = ? WHERE ticket_id = ?",
                r9_updates,
            )
        if overall_updates:
            conn.executemany(
                "UPDATE ai_checks SET overall_result = ? WHERE ticket_id = ?",
                overall_updates,
            )

    print(f"\nR8 updated: {len(r8_updates):>4} tickets")
    for k, v in sorted(r8_changes.items()):
        print(f"  {k:35s} ×{v}")

    print(f"\nR9 updated: {len(r9_updates):>4} tickets")
    for k, v in sorted(r9_changes.items()):
        print(f"  {k:35s} ×{v}")

    print(f"\nOverall updated: {len(overall_updates):>4} tickets")
    for k, v in sorted(overall_changes.items()):
        print(f"  {k:35s} ×{v}")

    print("\nDone.")


if __name__ == "__main__":
    run()
