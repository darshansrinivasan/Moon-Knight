"""Runs orphaned by a restart must not sit at 'running' forever.

Production had five qc_runs stuck at 'running' with scored=NULL after a deploy
restarted the container mid-run. A run can only be alive inside the process that
started it — one replica, --workers 1, in-process scheduler — so every 'running'
row present at startup belongs to a dead process. That is exact, not a guess,
which is why startup reaps unconditionally and the timeout is only the second
line of defence.
"""
from datetime import datetime, timedelta, timezone

import db

db.init_db()
fails = []


def check(name, got, want):
    ok = got == want
    print(f"  {'OK ' if ok else 'FAIL'} {name}: got {got!r}, want {want!r}")
    if not ok:
        fails.append(name)


def add_qc(status, minutes_ago, error=None):
    ts = (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()
    with db.get_conn() as c:
        cur = c.execute(
            "INSERT INTO qc_runs (date,triggered_by,started_at,status,total,error)"
            " VALUES ('2026-08-26','t',?,?,10,?)", (ts, status, error),
        )
        return cur.lastrowid


def add_sched(status, minutes_ago):
    ts = (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()
    with db.get_conn() as c:
        cur = c.execute(
            "INSERT INTO scheduled_runs (run_date,trigger_date,triggered_by,"
            "started_at,status) VALUES ('2026-08-26','2026-08-27','scheduler',?,?)",
            (ts, status),
        )
        return cur.lastrowid


def row(table, rid):
    with db.get_conn() as c:
        return dict(c.execute(
            f"SELECT status, finished_at, error FROM {table} WHERE id = ?", (rid,)
        ).fetchone())


def clear():
    with db.get_conn() as c:
        c.execute("DELETE FROM qc_runs")
        c.execute("DELETE FROM scheduled_runs")
        c.execute("DELETE FROM run_locks")


print("=== the timeout leaves a young run alone ===")
clear()
young = add_qc("running", 2)
res = db.reap_interrupted_runs()
check("nothing reaped", res["qc"], 0)
check("still running", row("qc_runs", young)["status"], "running")
print(f"   (a real run finishes in ~1 min; the cutoff is {db.STALE_RUN_MINUTES})")

print()
print("=== the timeout closes a run that has gone quiet ===")
clear()
stale = add_qc("running", db.STALE_RUN_MINUTES + 1)
res = db.reap_interrupted_runs()
check("one reaped", res["qc"], 1)
r = row("qc_runs", stale)
check("status is error", r["status"], "error")
check("finished_at set", r["finished_at"] is not None, True)
check("cause explains itself", "Interrupted" in r["error"], True)
check("cause names the timeout", str(db.STALE_RUN_MINUTES) in r["error"], True)
check("cause tells the admin what to do", "re-run" in r["error"].lower(), True)

print()
print("=== startup reaps EVERY running row, regardless of age ===")
clear()
fresh = add_qc("running", 0)
old = add_qc("running", 120)
s_fresh = add_sched("running", 0)
res = db.reap_interrupted_runs(on_startup=True)
check("both qc reaped", res["qc"], 2)
check("scheduled reaped", res["scheduled"], 1)
check("fresh one too", row("qc_runs", fresh)["status"], "error")
check("old one too", row("qc_runs", old)["status"], "error")
check("scheduled too", row("scheduled_runs", s_fresh)["status"], "error")
check("cause names the restart",
      "restart" in row("qc_runs", fresh)["error"].lower(), True)
print("   (a run cannot be alive in a process that no longer exists)")

print()
print("=== finished runs are never touched ===")
clear()
for st in ("success", "partial", "error"):
    rid = add_qc(st, 999)
    db.reap_interrupted_runs(on_startup=True)
    check(f"{st} untouched", row("qc_runs", rid)["status"], st)

print()
print("=== an existing error message is kept, not discarded ===")
clear()
rid = add_qc("running", 999, error="Vertex 429 on batch 3")
db.reap_interrupted_runs(on_startup=True)
err = row("qc_runs", rid)["error"]
check("original evidence survives", "Vertex 429 on batch 3" in err, True)
check("cause appended too", "Interrupted" in err, True)

print()
print("=== stale locks are cleared at startup ===")
clear()
with db.get_conn() as c:
    c.execute(
        "INSERT INTO run_locks (name,holder,acquired_at,expires_at)"
        " VALUES ('daily_run','dead-process',?,?)",
        (datetime.now(timezone.utc).isoformat(),
         (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()),
    )
db.reap_interrupted_runs(on_startup=True)
with db.get_conn() as c:
    left = c.execute("SELECT COUNT(*) AS n FROM run_locks").fetchone()["n"]
check("lock released", left, 0)
print("   (its holder is dead, so its 2h TTL would block every run until noon)")

print()
print("=== the timeout does NOT clear locks (a live run may hold one) ===")
clear()
with db.get_conn() as c:
    c.execute(
        "INSERT INTO run_locks (name,holder,acquired_at,expires_at)"
        " VALUES ('daily_run','live',?,?)",
        (datetime.now(timezone.utc).isoformat(),
         (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()),
    )
db.reap_interrupted_runs()
with db.get_conn() as c:
    left = c.execute("SELECT COUNT(*) AS n FROM run_locks").fetchone()["n"]
check("lock kept", left, 1)

print()
print("=== repeated calls are a no-op ===")
clear()
rid = add_qc("running", 999)
first = db.reap_interrupted_runs(on_startup=True)
second = db.reap_interrupted_runs(on_startup=True)
check("first reaps", first["qc"], 1)
check("second reaps nothing", second["qc"], 0)

print()
if fails:
    print(f"FAILURES ({len(fails)}): {fails}")
    raise SystemExit(1)
print("ALL REAP ASSERTIONS PASSED")
