"""Scheduler guard behaviour: stale runs, retry bounds, alarm throttling."""
from datetime import datetime, timedelta, timezone

import db
import scheduler
import vault

db.init_db()
vault.set_settings({"schedule_enabled": "1", "schedule_time": "09:30",
                    "schedule_tz": "Asia/Kolkata",
                    "schedule_target": "yesterday"}, "test")

TRIG = "2026-08-27"


def add(status, minutes_ago, finished=True, trigger=TRIG):
    ts = (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()
    fin = ts if finished and status != "running" else None
    with db.get_conn() as c:
        c.execute(
            "INSERT INTO scheduled_runs (run_date,trigger_date,triggered_by,"
            "started_at,finished_at,status) VALUES (?,?,'scheduler',?,?,?)",
            ("2026-08-26", trigger, ts, fin, status),
        )


def reset():
    with db.get_conn() as c:
        c.execute("DELETE FROM scheduled_runs")


print("=== 1. no rows -> should run ===")
reset()
assert scheduler._already_ran(TRIG) is False
print("   OK")

print("=== 2. success -> should NOT run ===")
reset(); add("success", 30)
assert scheduler._already_ran(TRIG) is True
print("   OK")

print("=== 3. FRESH running -> should NOT run (a live run) ===")
reset(); add("running", 5, finished=False)
assert scheduler._already_ran(TRIG) is True
print("   OK")

print("=== 4. STALE running (4h, process died) -> SHOULD run  [was: never again] ===")
reset(); add("running", 240, finished=False)
assert scheduler._already_ran(TRIG) is False, "stale run must not block the day"
print("   OK — a killed container no longer loses the day permanently")

print("=== 5. one error, inside backoff -> should NOT run ===")
reset(); add("error", 5)
assert scheduler._already_ran(TRIG) is True
print("   OK")

print("=== 6. one error, past backoff -> SHOULD run ===")
reset(); add("error", 20)
assert scheduler._already_ran(TRIG) is False
print("   OK")

print("=== 7. MAX_ATTEMPTS errors -> should NOT run ===")
reset()
for m in (60, 40, 20):
    add("error", m)
assert scheduler._already_ran(TRIG) is True
print("   OK")

print("=== 8. naive timestamp must not raise TypeError ===")
reset()
with db.get_conn() as c:
    c.execute(
        "INSERT INTO scheduled_runs (run_date,trigger_date,triggered_by,"
        "started_at,status) VALUES ('2026-08-26',?,'scheduler',?,'error')",
        (TRIG, datetime.now().isoformat()),          # no tzinfo
    )
print("   _already_ran ->", scheduler._already_ran(TRIG), "(no exception)")

print("=== 9. alarm fires once per trigger date ===")
first = scheduler._claim_alarm(TRIG)
second = scheduler._claim_alarm(TRIG)
third = scheduler._claim_alarm("2026-08-28")
print(f"   first={first} second={second} next_day={third}")
assert first is True and second is False and third is True
print("   OK — 3 retries + a restart no longer spam the channel")

print("=== 10. schedule_time parsing ===")
for raw, want in [("09:30", (9, 30)), ("9:5", (9, 5)), ("", None),
                  ("25:00", None), ("09", None), ("09:30:00", None),
                  ("ab:cd", None), ("23:59", (23, 59))]:
    got = scheduler._parse_schedule_time(raw)
    status = "OK " if got == want else "BAD"
    print(f"   {status} {raw!r:10} -> {got}")
    assert got == want

print("=== 11. next_run_description reports a due run, not tomorrow ===")
reset()
d = scheduler.next_run_description()
print("   ", {k: d[k] for k in ("enabled", "due_now")})
vault.set_settings({"schedule_time": "99:99"}, "test")
print("   invalid time ->", scheduler.next_run_description().get("error"))
print()
print("ALL SCHEDULER ASSERTIONS PASSED")
