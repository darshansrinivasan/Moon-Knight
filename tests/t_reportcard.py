"""Report Card: the snapshot is immutable, reviews are the one override,
and the rewardable metrics measure fixing — never rewriting — the record."""
import json
from datetime import date, datetime, timedelta, timezone

import db
import reportcard
import vault

db.init_db()
vault.set_raw_setting("qc_rules_json", '{"excluded_states": ["archived"]}', "t")
import rules as qc_rules
qc_rules.invalidate()

# Recent dates: the rewards window is "last N weeks from today".
TODAY = date.today()
D1 = (TODAY - timedelta(days=8)).isoformat()      # last ISO week (usually)
D2 = (TODAY - timedelta(days=1)).isoformat()      # this week / yesterday
T0 = datetime.now(timezone.utc).isoformat()

fails = []


def check(name, got, want):
    ok = got == want
    print(f"  {'OK ' if ok else 'FAIL'} {name}: got {got!r}, want {want!r}")
    if not ok:
        fails.append(name)


def ticket(tid, day, assignee, overall, state="closed", title="T"):
    with db.get_conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO tickets (id,number,fetch_date,title,state,"
            "assignee_name,custom_fields,source,customer_portal_visible,fetched_at)"
            " VALUES (?,?,?,?,?,?,'{}','email',1,?)",
            (tid, int(tid[1:]), day, title, state, assignee, T0))
        c.execute(
            "INSERT OR REPLACE INTO rule_checks (ticket_id,fetch_date,r1)"
            " VALUES (?,?,'Pass')", (tid, day))
        c.execute(
            "INSERT OR REPLACE INTO ai_checks (ticket_id,fetch_date,a1,a3,a4,a5,"
            "overall_result,ai_notes,checked_at) VALUES (?,?,"
            "'Accurate','Good','Consistent','Pass',?, 'note', ?)",
            (tid, day, overall, T0))
        c.execute("INSERT OR REPLACE INTO fetch_log (fetch_date,ticket_count,"
                  "fetched_at) VALUES (?, 1, ?)", (day, T0))


print("=== capture freezes the board: grades AND attribution ===")
ticket("t1", D1, "Ann", "Pass")
ticket("t2", D1, "Ann", "Fail")
ticket("t3", D1, "Bob", "Fail")
ticket("t9", D1, "Zed", "Fail", state="archived")   # out of scope
res = reportcard.capture(D1, run_id=1)
check("capture reports what it froze", (res["captured"], res["tickets"]), (True, 3))
with db.get_conn() as c:
    frozen_ids = {r["ticket_id"] for r in c.execute(
        "SELECT ticket_id FROM snapshot_tickets WHERE snapshot_date=?", (D1,))}
check("excluded states stay out of the record", "t9" in frozen_ids, False)

print()
print("=== the snapshot is immutable: re-scores and edits change nothing ===")
with db.get_conn() as c:
    # The manipulation this whole feature exists to stop: fix the ticket,
    # get re-scored, reassign it — then look innocent.
    c.execute("UPDATE ai_checks SET overall_result='Pass' WHERE ticket_id='t2'")
    c.execute("UPDATE tickets SET assignee_name='Bob' WHERE id='t2'")
res2 = reportcard.capture(D1, run_id=2)
check("a second capture is a no-op", res2["captured"], False)
d = reportcard.day(D1)
t2 = next(t for t in d["tickets"] if t["ticket_id"] == "t2")
check("the frozen grade did not move", t2["overall_result"], "Fail")
check("the frozen assignee did not move — reassignment cannot shift blame",
      t2["assignee_name"], "Ann")
check("but the delta shows the fix as remediation, not erasure",
      t2["delta"], "remediated")
check("summary counts the remediation",
      (d["summary"]["remediated"], d["summary"]["outstanding"]), (1, 1))

print()
print("=== a human review is the one sanctioned override, in both views ===")
import review as review_mod
with db.get_conn() as c:
    c.execute("INSERT INTO ticket_reviews (ticket_id, decision, reviewer_email,"
              " reviewer_name, note, reviewed_at) VALUES"
              " ('t3','Pass','lead@x','Lead','known cosmetic issue', ?)", (T0,))
d = reportcard.day(D1)
t3 = next(t for t in d["tickets"] if t["ticket_id"] == "t3")
check("frozen base grade still shows what the machine said",
      t3["overall_result"], "Fail")
check("the official (effective) grade is the lead's verdict",
      t3["effective_result"], "Pass")
check("the reviewer is named on the record", t3["reviewer_name"], "Lead")

print()
print("=== a hole is visible, never silently filled ===")
hole_day = (TODAY - timedelta(days=5)).isoformat()
with db.get_conn() as c:
    c.execute("INSERT OR REPLACE INTO fetch_log (fetch_date,ticket_count,"
              "fetched_at) VALUES (?, 1, ?)", (hole_day, T0))
d = reportcard.day(hole_day)
check("a fetched day without a snapshot reports itself as a hole",
      (d["snapshot"], d["hole"]), (None, True))

print()
print("=== leaderboard: frozen days rule; unfrozen days fall back, marked ===")
ticket("t4", D2, "Ann", "Pass")    # D2 has no snapshot yet -> live fallback
lb = reportcard.leaderboard(D1, D2)
check("range names its mix honestly",
      (lb["frozen_days"], lb["live_days"] >= 1), (1, True))
ann = next(p for p in lb["people"] if p["name"] == "Ann")
# Ann: frozen D1 = t1 Pass + t2 Fail (frozen grade, NOT the live fix) and
# live D2 = t4 Pass. The frozen Fail counting despite the live Pass is the pin.
check("frozen grades count from frozen days — the live fix does not launder D1",
      (ann["pass"], ann["fail"]), (2, 1))

print()
print("=== rewardable metrics: fixing scores, rewriting cannot ===")
ticket("t5", D2, "Cid", "Fail")     # frozen fail, never fixed, never reviewed
reportcard.capture(D2, run_id=3)
rw = reportcard.rewards(weeks=4)
ann = next(p for p in rw["people"] if p["name"] == "Ann")
check("remediation rate rewards the fix (1 frozen fail, 1 remediated)",
      (ann["frozen_fails"], ann["remediated"], ann["remediation_rate"]),
      (1, 1, 100.0))
cid = next(p for p in rw["people"] if p["name"] == "Cid")
check("an unremediated fail scores zero",
      (cid["frozen_fails"], cid["remediation_rate"]), (1, 0.0))
bob = next(p for p in rw["people"] if p["name"] == "Bob")
# The lead reviewed Bob's frozen Fail to Pass. The morning still found a fail
# (denominator keeps it) and it is now officially resolved (numerator counts
# it) — exactly what the Frozen Dashboard's "remediated since" chip says, so
# the two surfaces can never tell a lead two different stories.
check("a lead's Fail→Pass review counts as a remediation",
      (bob["frozen_fails"], bob["remediated"], bob["remediation_rate"]),
      (1, 1, 100.0))
check("week-over-week needs two weeks of history or says so",
      ann["wow_improvement"] is None or isinstance(ann["wow_improvement"], float),
      True)

print()
print("=== admin backfill: fills only the gaps, labelled, never overwrites ===")
D3 = (TODAY - timedelta(days=3)).isoformat()
ticket("t6", D3, "Ann", "Pass")
bf = reportcard.backfill(D1, TODAY.isoformat(), "admin@x")
check("backfill captured the fetched gaps and nothing else",
      D3 in bf["captured"] and hole_day in bf["captured"], True)
check("existing snapshots were skipped, not replaced",
      bf["skipped_existing"] >= 2, True)
d3 = reportcard.day(D3)
check("a backfilled record names its origin",
      d3["snapshot"]["created_by"], "admin@x")
d1 = reportcard.day(D1)
check("the scheduler-frozen day still says scheduler",
      d1["snapshot"]["created_by"], "scheduler")

print()
print("=== the CSV is the frozen record, formula-escaped ===")
with db.get_conn() as c:
    c.execute("UPDATE snapshot_tickets SET title='=HYPERLINK(\"evil\")'"
              " WHERE ticket_id='t1'")
csv_text = reportcard.day_csv(D1)
check("frozen assignee is in the export", "Ann" in csv_text, True)
check("formulas are neutralised", "'=HYPERLINK" in csv_text, True)
try:
    reportcard.day_csv("2020-01-01")   # never fetched, never frozen
    check("an unfrozen day cannot be exported", "no error", "ValueError")
except ValueError:
    check("an unfrozen day cannot be exported", "ValueError", "ValueError")

print()
print("=== frozen_ticket: the shared review sheet's data contract ===")
ft = reportcard.frozen_ticket(D1, 2)
check("a frozen ticket comes back with its record",
      (ft["ticket"]["ticket_id"], ft["ticket"]["overall_result"]),
      ("t2", "Fail"))
check("the delta rides along", ft["ticket"]["delta"], "remediated")
ft2 = reportcard.frozen_ticket(D1, 424242)
check("snapshotted day, unknown ticket -> ticket None, not an error",
      (ft2["snapshot"] is not None, ft2["ticket"]), (True, None))
ft3 = reportcard.frozen_ticket("2020-01-01", 1)
check("unfetched day -> no snapshot, not a hole",
      (ft3["snapshot"], ft3["hole"]), (None, False))

print()
if fails:
    print(f"FAILURES ({len(fails)}): {fails}")
    raise SystemExit(1)
print("ALL REPORT-CARD ASSERTIONS PASSED")
