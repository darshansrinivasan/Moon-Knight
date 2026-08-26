"""Backfill R-check failure reasons into existing ai_notes."""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))
os.chdir(os.path.dirname(__file__))

from dotenv import load_dotenv
load_dotenv()

import db
from qc_runner import _r_check_notes

with db.get_conn() as conn:
    rows = conn.execute("""
        SELECT ac.ticket_id, ac.ai_notes,
               rc.r1, rc.r2, rc.r3, rc.r4, rc.r5, rc.r7, rc.r8, rc.r9
        FROM ai_checks ac
        JOIN rule_checks rc ON ac.ticket_id = rc.ticket_id
    """).fetchall()

R_PREFIXES = ("R1 Fail", "R2 Fail", "R3 Fail", "R4 Fail",
              "R5 Fail", "R7 Fail", "R8 Fail", "R9 Fail")

updates = []
for row in rows:
    t        = dict(row)
    r_checks = {k: t[k] for k in ["r1","r2","r3","r4","r5","r7","r8","r9"]}
    r_note   = _r_check_notes(r_checks)
    if not r_note:
        continue

    old_note = t["ai_notes"] or ""
    if any(old_note.startswith(p) for p in R_PREFIXES):
        continue  # already backfilled

    new_note = f"{r_note} | {old_note}".strip(" |") if old_note else r_note
    updates.append((new_note, t["ticket_id"]))

with db.get_conn() as conn:
    conn.executemany("UPDATE ai_checks SET ai_notes = ? WHERE ticket_id = ?", updates)

print(f"Updated ai_notes for {len(updates)} tickets")

# verify ticket 69734
with db.get_conn() as conn:
    row = conn.execute("""
        SELECT ac.ai_notes, ac.overall_result, rc.r9
        FROM tickets t
        JOIN ai_checks ac  ON t.id = ac.ticket_id
        JOIN rule_checks rc ON t.id = rc.ticket_id
        WHERE t.number = 69734
    """).fetchone()
    if row:
        d = dict(row)
        print(f"\nTicket 69734:")
        print(f"  R9: {d['r9']}")
        print(f"  Overall: {d['overall_result']}")
        print(f"  Notes: {d['ai_notes']}")
