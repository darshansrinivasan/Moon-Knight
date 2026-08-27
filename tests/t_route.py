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
