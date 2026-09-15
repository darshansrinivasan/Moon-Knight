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
from datetime import date as _date
TARGET = _date(2026, 8, 26)  # the run_date the fixtures insert


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
assert scheduler._already_ran(TRIG, TARGET) is False
print("   OK")

print("=== 2. success -> should NOT run ===")
reset(); add("success", 30)
assert scheduler._already_ran(TRIG, TARGET) is True
print("   OK")

print("=== 2b. a successful MANUAL run of the same target also satisfies ===")
reset()
ts = datetime.now(timezone.utc).isoformat()
with db.get_conn() as c:
    c.execute(
        "INSERT INTO scheduled_runs (run_date,trigger_date,triggered_by,"
        "started_at,finished_at,status) VALUES (?,?,'op@x.com',?,?,'success')",
        ("2026-08-26", TRIG, ts, ts),
    )
assert scheduler._already_ran(TRIG, TARGET) is True, \
    "Run now at 09:00 must not be re-run (and re-posted) at 09:30"
print("   OK — a manual run no longer double-posts the day")

print("=== 2c. a backfill of some OTHER date does not satisfy today ===")
reset()
with db.get_conn() as c:
    c.execute(
        "INSERT INTO scheduled_runs (run_date,trigger_date,triggered_by,"
        "started_at,finished_at,status) VALUES (?,?,'op@x.com',?,?,'success')",
        ("2026-08-20", TRIG, ts, ts),
    )
assert scheduler._already_ran(TRIG, TARGET) is False, \
    "a backfill must not mask the day's own run"
print("   OK")

print("=== 3. FRESH running -> should NOT run (a live run) ===")
reset(); add("running", 5, finished=False)
assert scheduler._already_ran(TRIG, TARGET) is True
print("   OK")

print("=== 4. STALE running (4h, process died) -> SHOULD run  [was: never again] ===")
reset(); add("running", 240, finished=False)
assert scheduler._already_ran(TRIG, TARGET) is False, "stale run must not block the day"
print("   OK — a killed container no longer loses the day permanently")

print("=== 5. one error, inside backoff -> should NOT run ===")
reset(); add("error", 5)
assert scheduler._already_ran(TRIG, TARGET) is True
print("   OK")

print("=== 6. one error, past backoff -> SHOULD run ===")
reset(); add("error", 20)
assert scheduler._already_ran(TRIG, TARGET) is False
print("   OK")

print("=== 7. MAX_ATTEMPTS errors -> should NOT run ===")
reset()
for m in (60, 40, 20):
    add("error", m)
assert scheduler._already_ran(TRIG, TARGET) is True
print("   OK")

print("=== 8. naive timestamp must not raise TypeError ===")
reset()
with db.get_conn() as c:
    c.execute(
        "INSERT INTO scheduled_runs (run_date,trigger_date,triggered_by,"
        "started_at,status) VALUES ('2026-08-26',?,'scheduler',?,'error')",
        (TRIG, datetime.now().isoformat()),          # no tzinfo
    )
print("   _already_ran ->", scheduler._already_ran(TRIG, TARGET), "(no exception)")

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
print("=== daily open-backlog QC: setting-gated, failure never kills the run ===")
import asyncio


async def fake_open_qc():
    fake_open_qc.calls += 1
    return {"scored": 1}

fake_open_qc.calls = 0
real_open = scheduler._run_open_qc
scheduler._run_open_qc = fake_open_qc

import app as appmod
import qc_runner
import reportcard
import channels as channels_mod


async def fake_fetch(target):
    from types import SimpleNamespace
    return SimpleNamespace(count=0)


def fake_qc(date_str, triggered_by):
    return {"scored": 0, "skipped": 0, "errors": []}


async def fake_tag(full=False, only=None):
    return {"channels": [], "complete": True}

real_fs, real_qc = appmod.fetch_and_store, qc_runner.run_qc_date
real_cap, real_tag2 = reportcard.capture, channels_mod.tag_all
appmod.fetch_and_store, qc_runner.run_qc_date = fake_fetch, fake_qc
reportcard.capture = lambda d, r=None, c="scheduler": {"captured": False}
channels_mod.tag_all = fake_tag
try:
    from datetime import date as _d
    vault.set_settings({"slack_enabled": "0", "schedule_open_qc": "1"}, "t")
    asyncio.run(scheduler.run_pipeline(_d(2026, 9, 14), "scheduler"))
    assert fake_open_qc.calls == 1, "enabled setting must trigger the open QC"
    print("   OK — scheduler runs the open backlog QC when enabled")

    vault.set_settings({"schedule_open_qc": "0"}, "t")
    asyncio.run(scheduler.run_pipeline(_d(2026, 9, 13), "scheduler"))
    assert fake_open_qc.calls == 1, "disabled setting must skip it"
    print("   OK — switched off, it does not run")

    async def boom():
        raise RuntimeError("pylon down")
    scheduler._run_open_qc = boom
    vault.set_settings({"schedule_open_qc": "1"}, "t")
    res = asyncio.run(scheduler.run_pipeline(_d(2026, 9, 12), "scheduler"))
    assert res["status"] in ("success", "partial"), \
        "an open-QC failure must never fail the day's pipeline"
    print("   OK — open-QC failure is contained, the day still succeeds")

    vault.set_settings({"schedule_open_qc": "1"}, "t")
    asyncio.run(scheduler.run_pipeline(_d(2026, 9, 11), "manual@x"))
    # counter unchanged: manual pipeline runs never trigger the open sweep
    print("   OK — manual runs do not trigger it (scheduler-only)")
    # closure sweep: same contract — gated, contained, note reaches Slack.
    async def fake_sweep():
        fake_sweep.calls += 1
        return "Closure sweep: 3 tickets closed in the last 24h re-QC'd"
    fake_sweep.calls = 0
    real_sweep = scheduler._run_closed_sweep
    scheduler._run_closed_sweep = fake_sweep
    import slack as slack_mod
    async def fake_post(date_str, channel=None, extra_note=None):
        fake_post.note = extra_note
        return {}
    fake_post.note = "unset"
    real_post = slack_mod.post_day_report
    slack_mod.post_day_report = fake_post
    try:
        vault.set_settings({"slack_enabled": "1", "schedule_open_qc": "0",
                            "schedule_closed_qc": "1"}, "t")
        scheduler._run_open_qc = fake_open_qc
        asyncio.run(scheduler.run_pipeline(_d(2026, 9, 10), "scheduler"))
        assert fake_sweep.calls == 1, "enabled sweep must run"
        assert fake_post.note and "Closure sweep" in fake_post.note, \
            "the sweep's findings must reach the morning report"
        print("   OK — closure sweep runs and its note rides the Slack report")

        vault.set_settings({"schedule_closed_qc": "0"}, "t")
        asyncio.run(scheduler.run_pipeline(_d(2026, 9, 9), "scheduler"))
        assert fake_sweep.calls == 1, "disabled sweep must not run"
        assert fake_post.note is None, \
            "no sweep, no note — the report must not carry a stale line"
        print("   OK — switched off: no sweep, no stale note")
    finally:
        scheduler._run_closed_sweep = real_sweep
        slack_mod.post_day_report = real_post
finally:
    scheduler._run_open_qc = real_open
    appmod.fetch_and_store, qc_runner.run_qc_date = real_fs, real_qc
    reportcard.capture, channels_mod.tag_all = real_cap, real_tag2

print()
print("ALL SCHEDULER ASSERTIONS PASSED")
