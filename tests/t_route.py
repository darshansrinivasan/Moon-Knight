"""Does /api/calendar-day/{date} collide with /api/calendar/{year}/{month}?"""
from fastapi.testclient import TestClient

import app as appmod
import auth
import db

db.init_db()

# Real signed session so the request reaches routing rather than stopping at 401.
with db.get_conn() as c:
    c.execute(
        "INSERT OR REPLACE INTO app_users (email,name,role,is_active,created_at)"
        " VALUES ('a@spotdraft.com','A','admin',1,'2026-01-01T00:00:00+00:00')"
    )
token = auth.issue_session({"email": "a@spotdraft.com", "name": "A"})

client = TestClient(appmod.app, follow_redirects=False)
client.cookies.set(auth.COOKIE_NAME, token)

r = client.get("/api/calendar-day/2026-08-26")
print("GET /api/calendar-day/2026-08-26 ->", r.status_code)
print("   body:", str(r.json())[:160])
assert r.status_code == 200, f"route collision: {r.status_code} {r.text[:200]}"
assert r.json()["date"] == "2026-08-26"
print("   OK  no collision — the literal path wins")

r = client.get("/api/calendar/2026/8")
print("GET /api/calendar/2026/8 ->", r.status_code)
assert r.status_code == 200, r.text[:200]
assert r.json()["month"] == 8
print("   OK  month route still works")

r = client.get("/api/calendar/2026/13")
print("GET /api/calendar/2026/13 ->", r.status_code, "(expect 400)")
assert r.status_code == 400

r = client.get("/api/calendar-day/not-a-date")
print("GET /api/calendar-day/not-a-date ->", r.status_code, "(expect 400)")
assert r.status_code == 400

print()
print("ROUTING OK")

print()
print("=== Run QC accepts refetch=1 and reports the fetch half ===")
# Not exercised end-to-end here (that would need Pylon); this asserts the
# contract the frontend depends on, and the lock ordering that keeps a
# scheduled run and a human from deadlocking on each other.
import inspect

import app as _a

sig = inspect.signature(_a.run_qc)
assert "refetch" in sig.parameters, "run_qc must accept refetch"
assert sig.parameters["refetch"].default is False, "refetch must default off"
print("   OK  run_qc takes refetch, defaulting off")

src = inspect.getsource(_a.run_qc)
assert '"fetch"' in src, "response must carry a fetch key"
print("   OK  response carries the fetch half")

assert src.index('f"fetch:{date_str}"') < src.index('f"qc:{date_str}"'), \
    "the fetch lock must be taken before the qc lock"
assert src.count("with db.advisory_lock") == 2, \
    "the two locks must be separate statements, not nested"
print("   OK  fetch lock precedes qc lock, and they are not nested")

print()
print("ROUTING OK")
