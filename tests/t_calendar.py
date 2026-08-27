"""Calendar cells must come from tickets, not fetch_log.

Keying the calendar off fetch_log meant a day whose fetch partly failed had
tickets stored but no calendar entry, so it was invisible in the UI.
"""
import db

db.init_db()
DATE = "2026-08-26"
OTHER = "2026-08-27"
T0 = "2026-08-26T08:00:00+00:00"

fails = []


def check(name, got, want):
    ok = got == want
    print(f"  {'OK ' if ok else 'FAIL'} {name}: got {got!r}, want {want!r}")
    if not ok:
        fails.append(name)


def add(tid, date, r3="Pass", overall=None):
    with db.get_conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO tickets (id,number,fetch_date,title,state,fetched_at)"
            " VALUES (?,?,?,'T','closed',?)",
            (tid, abs(hash(tid)) % 10000, date, T0),
        )
        c.execute(
            "INSERT OR REPLACE INTO rule_checks (ticket_id,fetch_date,r3) VALUES (?,?,?)",
            (tid, date, r3),
        )
        if overall:
            c.execute(
                "INSERT OR REPLACE INTO ai_checks (ticket_id,fetch_date,a1,a2,a3,a4,a5,"
                "ai_notes,overall_result,checked_at) VALUES (?,?,'Pass','Neutral','Good',"
                "'Pass','Pass','n',?,?)",
                (tid, date, overall, T0),
            )


print("=== a day with tickets but NO fetch_log row is still visible ===")
add("a1", DATE, overall="Pass")
add("a2", DATE, r3="Fail", overall="Fail")
month = {r["fetch_date"]: r for r in db.get_calendar_month(2026, 8)}
check("date present in month", DATE in month, True)
check("ticket_count", month[DATE]["ticket_count"], 2)
check("rule_fails", month[DATE]["rule_fails"], 1)
check("ai_fails", month[DATE]["ai_fails"], 1)
check("ai_done_count", month[DATE]["ai_done_count"], 2)
check("logged flag is 0", month[DATE]["logged"], 0)
print("   (previously this day was absent from the calendar entirely)")

print()
print("=== the logged flag flips once fetch_log exists ===")
with db.get_conn() as c:
    c.execute(
        "INSERT OR REPLACE INTO fetch_log (fetch_date,ticket_count,fetched_at)"
        " VALUES (?,2,?)", (DATE, T0),
    )
month = {r["fetch_date"]: r for r in db.get_calendar_month(2026, 8)}
check("logged flag is 1", month[DATE]["logged"], 1)

print()
print("=== the day endpoint agrees with the month endpoint ===")
day = db.get_calendar_day(DATE)
for key in ("ticket_count", "rule_fails", "ai_fails", "needs_review",
            "ai_done_count", "logged"):
    check(f"day.{key} == month.{key}", day[key], month[DATE][key])

print()
print("=== a date with no tickets returns None, not a zero row ===")
check("unknown date", db.get_calendar_day("2026-01-01"), None)

print()
print("=== other dates are not mixed in ===")
add("b1", OTHER, overall="Pass")
day = db.get_calendar_day(DATE)
check("still 2 tickets for the target date", day["ticket_count"], 2)
check("other date has its own count", db.get_calendar_day(OTHER)["ticket_count"], 1)

print()
print("=== needs_review is counted separately from fails ===")
add("a3", DATE, overall="Needs Review")
day = db.get_calendar_day(DATE)
check("needs_review", day["needs_review"], 1)
check("ai_fails unchanged", day["ai_fails"], 1)

print()
if fails:
    print(f"FAILURES ({len(fails)}): {fails}")
    raise SystemExit(1)
print("ALL CALENDAR ASSERTIONS PASSED")
