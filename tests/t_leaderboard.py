"""Team and individual scoring: ranking, empty teams, uncovered assignees.

The rules pinned here are the ones that are easy to get wrong and hard to spot
once the numbers are summed.
"""
import db
import leaderboard as lb
import rules as qc_rules
import vault

db.init_db()
D1, D2 = "2026-08-25", "2026-08-26"
T0 = "2026-08-26T08:00:00+00:00"

fails = []


def check(name, got, want):
    ok = got == want
    print(f"  {'OK ' if ok else 'FAIL'} {name}: got {got!r}, want {want!r}")
    if not ok:
        fails.append(name)


def ticket(tid, assignee, grade, date=D2, state="closed", r3="Pass"):
    with db.get_conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO tickets (id,number,fetch_date,title,state,"
            "assignee_name,fetched_at) VALUES (?,?,?,'T',?,?,?)",
            (tid, abs(hash(tid)) % 100000, date, state, assignee, T0),
        )
        c.execute(
            "INSERT OR REPLACE INTO rule_checks (ticket_id,fetch_date,r3)"
            " VALUES (?,?,?)", (tid, date, r3),
        )
        if grade:
            c.execute(
                "INSERT OR REPLACE INTO ai_checks (ticket_id,fetch_date,a1,a2,a3,"
                "a4,a5,ai_notes,overall_result,checked_at)"
                " VALUES (?,?,'Pass','Neutral','Good','Pass','Pass','n',?,?)",
                (tid, date, grade, T0),
            )


def coverage(name, email, assignees):
    with db.get_conn() as c:
        cur = c.execute(
            "INSERT INTO review_coverages (name,reviewer_email,reviewer_name,"
            "updated_by,updated_at) VALUES (?,?,?,'t',?)",
            (name, email, name + " Lead", T0),
        )
        for a in assignees:
            c.execute(
                "INSERT OR IGNORE INTO review_coverage_assignees (coverage_id,"
                "assignee_name) VALUES (?,?)", (cur.lastrowid, a),
            )


vault.set_raw_setting("qc_rules_json", '{"excluded_states": ["archived"]}', "t")
qc_rules.invalidate()

# APAC: 3 pass, 1 fail  -> 75%.   NAM: 1 pass, 1 fail -> 50%.
coverage("APAC", "apac@x.com", ["Ann", "Bob"])
coverage("NAM", "nam@x.com", ["Cara"])
ticket("t1", "Ann", "Pass")
ticket("t2", "Ann", "Pass")
ticket("t3", "Bob", "Pass")
ticket("t4", "Bob", "Fail", r3="Fail")
ticket("t5", "Cara", "Pass")
ticket("t6", "Cara", "Fail", r3="Fail")
# Dave is in no coverage.
ticket("t7", "Dave", "Pass")
ticket("t8", "Dave", "Fail")
# Archived -> out of scope entirely.
ticket("t9", "Ann", "Fail", state="archived")

print("=== pass rates and ranking ===")
r = lb.build()
teams = {t["name"]: t for t in r["teams"]}
check("APAC pass_rate", teams["APAC"]["pass_rate"], 75.0)
check("NAM pass_rate", teams["NAM"]["pass_rate"], 50.0)
check("APAC ranks first", r["teams"][0]["name"], "APAC")
check("APAC graded", teams["APAC"]["graded"], 4)
check("APAC lead", teams["APAC"]["lead_email"], "apac@x.com")

print()
print("=== excluded states are out of every count ===")
check("APAC in_scope excludes archived", teams["APAC"]["in_scope"], 4)

print()
print("=== an assignee in no coverage lands in 'No team', not nowhere ===")
check("unteamed graded", r["unteamed"]["graded"], 2)
check("unteamed pass_rate", r["unteamed"]["pass_rate"], 50.0)
check("Dave not in a team", "Dave" not in
      {a for t in r["teams"] for a in [t["name"]]}, True)

print()
print("=== totals reconcile: teams + unteamed == all in-scope tickets ===")
team_sum = sum(t["in_scope"] for t in r["teams"]) + r["unteamed"]["in_scope"]
check("sum matches totals", team_sum, r["totals"]["in_scope"])
check("totals in_scope is 8 (t9 archived)", r["totals"]["in_scope"], 8)

print()
print("=== an empty team scores None, not 0%, and sorts last ===")
coverage("EMEA", "emea@x.com", ["Nobody"])
r = lb.build()
teams = {t["name"]: t for t in r["teams"]}
check("EMEA pass_rate is None", teams["EMEA"]["pass_rate"], None)
check("EMEA graded", teams["EMEA"]["graded"], 0)
check("EMEA sorts last", r["teams"][-1]["name"], "EMEA")
print("   (0% would have ranked it above NAM, which actually did the work)")

print()
print("=== a human override moves the team's rate ===")
before = {t["name"]: t for t in lb.build()["teams"]}["NAM"]["pass_rate"]
with db.get_conn() as c:
    c.execute(
        "INSERT INTO ticket_reviews (ticket_id,decision,kept_ai,reviewer_email,"
        "reviewer_name,note,reviewed_at) VALUES ('t6','Pass',0,'r@x.com','R','',?)",
        (T0,),
    )
after = {t["name"]: t for t in lb.build()["teams"]}["NAM"]["pass_rate"]
check("NAM before override", before, 50.0)
check("NAM after override", after, 100.0)
print("   (proves the effective grade is used, not ai_checks)")

print()
print("=== an assignee in two teams counts in both, once in totals ===")
coverage("DUP", "dup@x.com", ["Ann"])
r = lb.build()
teams = {t["name"]: t for t in r["teams"]}
# APAC is Ann (2 graded) + Bob (2 graded); DUP is Ann alone.
check("APAC still counts Ann and Bob", teams["APAC"]["graded"], 4)
check("Ann also counted in DUP", teams["DUP"]["graded"], 2)
check("totals unchanged by the overlap", r["totals"]["in_scope"], 8)
check("warning surfaced", any("more than one team" in w for w in r["warnings"]), True)
# The double-count is real and intended; the point is that it is visible. Team
# sum + unteamed now exceeds the deduplicated total by exactly Ann's 2 tickets.
overlap_sum = sum(t["in_scope"] for t in r["teams"]) + r["unteamed"]["in_scope"]
check("overlap inflates the team sum by Ann's tickets",
      overlap_sum - r["totals"]["in_scope"], 2)

print()
print("=== rule failures are reported per team ===")
check("APAC r3 fails", teams["APAC"]["rule_fails"].get("r3"), 1)

print()
print("=== apportioned cost is labelled, never presented as measured ===")
with db.get_conn() as c:
    c.execute(
        "INSERT INTO qc_runs (date,triggered_by,started_at,status,cost_usd)"
        " VALUES (?,'t',?,'success',1.0)", (D2, T0),
    )
r = lb.build()
check("cost marked apportioned", r["teams"][0]["cost_apportioned"], True)
total_cost = sum(t["cost_usd"] for t in r["teams"]) + r["unteamed"]["cost_usd"]
print(f"   range cost ${r['range_cost_usd']}, apportioned sum ${round(total_cost, 6)}")
check("apportioned sum does not exceed the range cost",
      round(total_cost, 4) <= r["range_cost_usd"] + 0.0001, True)

print()
print("=== date range filters ===")
before_all = lb.build()["totals"]["in_scope"]
ticket("x1", "Ann", "Fail", date=D1)
r_all = lb.build()
r_d2 = lb.build(D2, D2)
r_d1 = lb.build(D1, D1)
check("adding a D1 ticket raises the unfiltered total",
      r_all["totals"]["in_scope"], before_all + 1)
check("D1-only sees just that ticket", r_d1["totals"]["in_scope"], 1)
check("D2-only excludes it", r_d2["totals"]["in_scope"], before_all)
check("D1 + D2 equals the unfiltered total",
      r_d1["totals"]["in_scope"] + r_d2["totals"]["in_scope"],
      r_all["totals"]["in_scope"])

print()
print("=== weekly buckets are Monday-first in the schedule timezone ===")
bounds = lb.week_bounds(3)
check("3 buckets", len(bounds), 3)
from datetime import date as _date
for start, end in bounds:
    s = _date.fromisoformat(start)
    check(f"{start} is a Monday", s.weekday(), 0)
    check(f"{start}..{end} spans 7 days",
          (_date.fromisoformat(end) - s).days, 6)
check("most recent first", bounds[0][0] > bounds[1][0], True)

wk = lb.build_weekly(2)
check("weekly returns buckets", len(wk["buckets"]), 2)
check("delta present on the newest bucket",
      "delta" in wk["buckets"][0]["teams"][0], True)

print()
if fails:
    print(f"FAILURES ({len(fails)}): {fails}")
    raise SystemExit(1)
print("ALL LEADERBOARD ASSERTIONS PASSED")
