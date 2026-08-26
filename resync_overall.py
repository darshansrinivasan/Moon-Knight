"""
Recompute overall_result for all QC'd tickets based on current rule_checks values.
Run whenever rule_checks change (e.g. after a refetch updates R-scores).
Also resyncs ai_notes to include current R-check failure reasons (no duplicates).
"""
import json
import sys
import os
sys.path.insert(0, os.path.dirname(__file__))
os.chdir(os.path.dirname(__file__))

from dotenv import load_dotenv
load_dotenv()

import db
from qc_runner import _compute_overall, _r_check_notes, _strip_r_notes
from collections import defaultdict


def run():
    with db.get_conn() as conn:
        rows = conn.execute("""
            SELECT ac.ticket_id,
                   ac.a1, ac.a3, ac.a4, ac.a5,
                   ac.ai_notes, ac.overall_result,
                   rc.r1, rc.r2, rc.r3, rc.r4, rc.r5, rc.r7, rc.r8, rc.r9,
                   t.custom_fields
            FROM ai_checks ac
            JOIN rule_checks rc ON ac.ticket_id = rc.ticket_id
            JOIN tickets t ON ac.ticket_id = t.id
        """).fetchall()

    overall_updates = []
    notes_updates   = []
    overall_changes = defaultdict(int)

    for row in rows:
        t        = dict(row)
        r_checks = {k: t[k] for k in ["r1","r2","r3","r4","r5","r7","r8","r9"]}
        a_checks = {"a1": t["a1"], "a3": t["a3"], "a4": t["a4"], "a5": t["a5"]}
        cf       = json.loads(t.get("custom_fields") or "{}")

        new_overall = _compute_overall(r_checks, a_checks)
        old_overall = t["overall_result"]
        if new_overall != old_overall:
            overall_changes[f"{old_overall} → {new_overall}"] += 1
            overall_updates.append((new_overall, t["ticket_id"]))

        # Strip ALL existing R-notes, then prepend fresh ones
        ai_only  = _strip_r_notes(t["ai_notes"] or "")
        r_note   = _r_check_notes(r_checks, cf)
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

    print(f"Overall resynced: {len(overall_updates)} tickets")
    for k, v in sorted(overall_changes.items()):
        print(f"  {k:35s} ×{v}")
    print(f"Notes resynced:   {len(notes_updates)} tickets")
    print("Done.")


if __name__ == "__main__":
    run()
