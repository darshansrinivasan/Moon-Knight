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
print("=== POST /api/rules/dry-run is deployed, admin-gated, and cheap to refuse ===")
# The Rules page treats 404/405/501 as "not deployed yet" and hides the button,
# so a route that silently stops existing would degrade to a missing feature
# rather than an error. That is worth pinning.
r = client.post("/api/rules/dry-run", json={"limit": 1})
assert r.status_code not in (404, 405, 501), \
    f"the route must exist — the UI reads {r.status_code} as 'not deployed'"
print("   OK  the route exists ->", r.status_code)

# A draft that could not be saved must not be billable either: validation has to
# reject it before any model call is made.
r = client.post("/api/rules/dry-run", json={"a1_rubric": "x" * 5000})
print("POST with an unsaveable draft ->", r.status_code, "(expect 400)")
assert r.status_code == 400, r.text[:200]
assert "limit is" in r.json()["detail"], r.json()
print("   OK  rejected before spending anything, with the reason")

r = client.post("/api/rules/dry-run", json={"date": "not-a-date"})
print("POST with a bad date ->", r.status_code, "(expect 400)")
assert r.status_code == 400
print("   OK  the date is validated")

# Members may read the Rules page but not spend the workspace's AI quota on it.
with db.get_conn() as c:
    c.execute(
        "INSERT OR REPLACE INTO app_users (email,name,role,is_active,created_at)"
        " VALUES ('m@spotdraft.com','M','member',1,'2026-01-01T00:00:00+00:00')"
    )
member = auth.issue_session({"email": "m@spotdraft.com", "name": "M"})
mc = TestClient(appmod.app, follow_redirects=False)
mc.cookies.set(auth.COOKIE_NAME, member)
r = mc.post("/api/rules/dry-run", json={"limit": 1})
print("POST as a member ->", r.status_code, "(expect 403)")
assert r.status_code == 403, r.text[:200]
print("   OK  a dry-run spends money, so it is admin-only")

print()
print("ROUTING OK")
