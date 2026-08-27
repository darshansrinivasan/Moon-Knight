"""Tickets removed at source get soft-deleted — but only from a COMPLETE fetch.

Pylon gives no tombstone for a deleted ticket, so "deleted" can only be inferred
from absence. That makes an incomplete fetch indistinguishable from a mass
deletion, and being wrong destroys grades and human sign-offs that refetching
cannot restore. The negative test here is the important one.
"""
import db
import leaderboard as lb
import pylon
import qc_runner as q
import review
import rules as qc_rules
import vault

db.init_db()
DATE = "2026-08-26"
T0 = "2026-08-26T08:00:00+00:00"

fails = []


def check(name, got, want):
    ok = got == want
    print(f"  {'OK ' if ok else 'FAIL'} {name}: got {got!r}, want {want!r}")
    if not ok:
        fails.append(name)


def ticket(tid, number, assignee="Alice", grade="Pass"):
    with db.get_conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO tickets (id,number,fetch_date,title,state,"
            "assignee_name,custom_fields,fetched_at,deleted_at)"
            " VALUES (?,?,?,'T','closed',?,'{}',?,NULL)",
            (tid, number, DATE, assignee, T0),
        )
        c.execute(
            "INSERT OR REPLACE INTO rule_checks (ticket_id,fetch_date,r3)"
            " VALUES (?,?,'Pass')", (tid, DATE),
        )
        c.execute(
            "INSERT OR REPLACE INTO ai_checks (ticket_id,fetch_date,a1,a2,a3,a4,"
            "a5,ai_notes,overall_result,checked_at)"
            " VALUES (?,?,'Pass','Neutral','Good','Pass','Pass','n',?,?)",
            (tid, DATE, grade, T0),
        )


def deleted_at(tid):
    with db.get_conn() as c:
        row = c.execute("SELECT deleted_at FROM tickets WHERE id = ?", (tid,)).fetchone()
    return row["deleted_at"] if row else "MISSING"


vault.set_raw_setting("qc_rules_json", '{"excluded_states": []}', "t")
qc_rules.invalidate()

for i, tid in enumerate(["a", "b", "c"], start=1):
    ticket(tid, 100 + i)

print("=== a complete fetch missing one ticket marks only that one ===")
res = db.mark_deleted_tickets(DATE, ["a", "b"])
check("deleted count", res["deleted"], 1)
check("c is marked", deleted_at("c") is not None, True)
check("a untouched", deleted_at("a"), None)
check("b untouched", deleted_at("b"), None)

print()
print("=== an INCOMPLETE fetch marks NOTHING — the regression that matters ===")
for tid in ["a", "b", "c"]:
    with db.get_conn() as c:
        c.execute("UPDATE tickets SET deleted_at = NULL WHERE id = ?", (tid,))

day = pylon.FetchedDay(issues=[], messages_by_id={}, accounts_by_id={},
                       issues_complete=False)
check("may_infer_deletions is False", day.may_infer_deletions(), False)
# The caller must not even ask; assert the guard rather than the sweep.
if day.may_infer_deletions():
    db.mark_deleted_tickets(DATE, [])
for tid in ["a", "b", "c"]:
    check(f"{tid} survives an incomplete fetch", deleted_at(tid), None)
print("   (a naive sweep here would have wiped the whole day)")

print()
print("=== a complete fetch reports itself so ===")
ok_day = pylon.FetchedDay(issues=[{"id": "a"}], messages_by_id={},
                          accounts_by_id={}, issues_complete=True)
check("may_infer_deletions is True", ok_day.may_infer_deletions(), True)

print()
print("=== a ticket with a human review is NEVER marked ===")
with db.get_conn() as c:
    c.execute(
        "INSERT INTO ticket_reviews (ticket_id,decision,kept_ai,reviewer_email,"
        "reviewer_name,note,reviewed_at) VALUES ('b','Pass',0,'r@x.com','R','',?)",
        (T0,),
    )
res = db.mark_deleted_tickets(DATE, ["a"])          # b and c both absent
check("only c marked", res["deleted"], 1)
check("b kept for its review", deleted_at("b"), None)
check("kept_reviewed reported", res["kept_reviewed"], 1)
check("kept id reported", res["kept_reviewed_ids"], ["b"])

print()
print("=== a soft-deleted ticket disappears from every read path ===")
check("get_day_tickets", [t["id"] for t in db.get_day_tickets(DATE)], ["a", "b"])
cal = db.get_calendar_day(DATE)
check("calendar day count", cal["ticket_count"], 2)
month = {r["fetch_date"]: r for r in db.get_calendar_month(2026, 8)}
check("calendar month count", month[DATE]["ticket_count"], 2)
check("ticket_stats total", db.ticket_stats(DATE)["total_tickets"], 2)
scope = q._load_in_scope(DATE)
check("_load_in_scope", sorted(t["id"] for t in scope), ["a", "b"])
board = lb.build()
check("leaderboard in_scope", board["totals"]["in_scope"], 2)

print()
print("=== assignee list drops an assignee whose only ticket was removed ===")
ticket("d", 104, assignee="Zoe")
check("Zoe listed while live", "Zoe" in review.list_assignee_names(), True)
db.mark_deleted_tickets(DATE, ["a", "b"])          # d now absent
check("Zoe gone once removed", "Zoe" in review.list_assignee_names(), False)

print()
print("=== a ticket that reappears is restored ===")
res = db.mark_deleted_tickets(DATE, ["a", "b", "c"])
check("c restored", deleted_at("c"), None)
check("restored counted", res["restored"], 1)
check("c back in the day view",
      "c" in [t["id"] for t in db.get_day_tickets(DATE)], True)

print()
print("=== a non-data page reports not-ok rather than an empty page ===")
page = pylon.IssuePage([], None, False, ok=False)
check("ok is False", page.ok, False)
check("issues empty", page.issues, [])
print("   (this used to be indistinguishable from 'no tickets today')")

print()
if fails:
    print(f"FAILURES ({len(fails)}): {fails}")
    raise SystemExit(1)
print("ALL CLEANUP ASSERTIONS PASSED")
