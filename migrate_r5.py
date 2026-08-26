"""
One-time migration: re-score R5 using updated logic and recompute overall_result
for all existing tickets. No Gemini API calls — pure DB reads and writes.
"""
import sys, os
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
    # ── load all QC'd tickets with their current checks ───────────────────────
    with db.get_conn() as conn:
        rows = conn.execute("""
            SELECT t.id, t.state, t.assignee_id,
                   t.custom_fields, t.external_issues, t.body_html,
                   rc.r1, rc.r2, rc.r3, rc.r4, rc.r5, rc.r6, rc.r7,
                   ac.a1, ac.a3, ac.a4, ac.a5,
                   ac.overall_result
            FROM tickets t
            JOIN rule_checks rc ON t.id = rc.ticket_id
            JOIN ai_checks   ac ON t.id = ac.ticket_id
        """).fetchall()

        # bulk-load all messages keyed by ticket_id
        msg_rows = conn.execute(
            "SELECT ticket_id, message_html FROM messages ORDER BY timestamp"
        ).fetchall()

    msgs_by_ticket = defaultdict(list)
    for m in msg_rows:
        msgs_by_ticket[m["ticket_id"]].append({"message_html": m["message_html"]})

    total = len(rows)
    print(f"Tickets to process: {total}")

    r5_changes      = defaultdict(int)  # old→new counts
    overall_changes = defaultdict(int)
    r5_updated      = 0
    overall_updated = 0
    now             = datetime.now(timezone.utc).isoformat()

    r5_updates      = []   # (new_r5, ticket_id)
    overall_updates = []   # (new_overall, ticket_id)

    for row in rows:
        t          = dict(row)
        ticket_id  = t["id"]
        messages   = msgs_by_ticket[ticket_id]

        # re-score R5 with new logic
        import json as _json
        cf  = _json.loads(t["custom_fields"]  or "{}")
        ext = _json.loads(t["external_issues"] or "[]")
        issue = {
            "state":        t["state"],
            "assignee":     t["assignee_id"],
            "custom_fields": cf,
            "body_html":    t["body_html"],
        }
        new_r5 = scorer.r5(issue, messages, ext)
        old_r5 = t["r5"]

        r_checks = {k: t[k] for k in ["r1","r2","r3","r4","r6","r7"]}
        r_checks["r5"] = new_r5
        a_checks = {k: t[k] for k in ["a1","a3","a4","a5"]}

        new_overall = _compute_overall(r_checks, a_checks)
        old_overall = t["overall_result"]

        if new_r5 != old_r5:
            r5_changes[f"{old_r5} → {new_r5}"] += 1
            r5_updates.append((new_r5, ticket_id))
            r5_updated += 1

        if new_overall != old_overall:
            overall_changes[f"{old_overall} → {new_overall}"] += 1
            overall_updates.append((new_overall, ticket_id))
            overall_updated += 1

    # ── write changes in a single transaction ─────────────────────────────────
    with db.get_conn() as conn:
        if r5_updates:
            conn.executemany(
                "UPDATE rule_checks SET r5 = ?, checked_at = ? WHERE ticket_id = ?",
                [(r5, now, tid) for r5, tid in r5_updates],
            )
        if overall_updates:
            conn.executemany(
                "UPDATE ai_checks SET overall_result = ? WHERE ticket_id = ?",
                overall_updates,
            )

    # ── report ────────────────────────────────────────────────────────────────
    print(f"\nR5 updated:      {r5_updated:>4} tickets")
    for change, cnt in sorted(r5_changes.items()):
        print(f"  {change:35s} ×{cnt}")

    print(f"\nOverall updated: {overall_updated:>4} tickets")
    for change, cnt in sorted(overall_changes.items()):
        print(f"  {change:35s} ×{cnt}")

    print("\nDone.")

if __name__ == "__main__":
    run()
