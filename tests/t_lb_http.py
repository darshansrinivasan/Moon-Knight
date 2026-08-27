"""Leaderboard endpoints over HTTP: auth, validation, page route."""
from fastapi.testclient import TestClient

import app as appmod
import auth
import db

db.init_db()
with db.get_conn() as c:
    c.execute(
        "INSERT OR REPLACE INTO app_users (email,name,role,is_active,created_at)"
        " VALUES ('a@spotdraft.com','A','admin',1,'2026-01-01T00:00:00+00:00')"
    )

client = TestClient(appmod.app, follow_redirects=False)
fails = []


def check(name, ok, detail=""):
    print(f"  {'OK ' if ok else 'FAIL'} {name}{' — ' + detail if detail else ''}")
    if not ok:
        fails.append(name)


print("=== unauthenticated ===")
check("/api/leaderboard -> 401", client.get("/api/leaderboard").status_code == 401)
r = client.get("/leaderboard")
check("/leaderboard -> redirect to login",
      r.status_code == 302 and "/login" in r.headers.get("location", ""))

client.cookies.set(auth.COOKIE_NAME,
                   auth.issue_session({"email": "a@spotdraft.com", "name": "A"}))

print()
print("=== authenticated ===")
r = client.get("/api/leaderboard")
check("no range -> 200", r.status_code == 200, str(r.status_code))
body = r.json()
for key in ("teams", "people", "unteamed", "totals", "range_cost_usd", "warnings"):
    check(f"payload has {key}", key in body)

r = client.get("/api/leaderboard?start=2026-08-01&end=2026-08-31")
check("valid range -> 200", r.status_code == 200, str(r.status_code))

print()
print("=== validation ===")
check("start without end -> 400",
      client.get("/api/leaderboard?start=2026-08-01").status_code == 400)
check("end without start -> 400",
      client.get("/api/leaderboard?end=2026-08-01").status_code == 400)
check("bad date -> 400",
      client.get("/api/leaderboard?start=nope&end=2026-08-01").status_code == 400)
check("start after end -> 400",
      client.get("/api/leaderboard?start=2026-08-31&end=2026-08-01").status_code == 400)

print()
print("=== weekly ===")
r = client.get("/api/leaderboard/weekly?weeks=3")
check("weeks=3 -> 200", r.status_code == 200, str(r.status_code))
check("3 buckets", len(r.json()["buckets"]) == 3)
check("weeks=0 -> 400", client.get("/api/leaderboard/weekly?weeks=0").status_code == 400)
check("weeks=99 -> 400", client.get("/api/leaderboard/weekly?weeks=99").status_code == 400)

print()
print("=== page renders ===")
r = client.get("/leaderboard")
check("/leaderboard -> 200", r.status_code == 200, str(r.status_code))
check("page has the nav host", 'id="app-nav"' in r.text)
check("page marks itself", 'data-page="leaderboard"' in r.text)

print()
if fails:
    print(f"FAILURES ({len(fails)}): {fails}")
    raise SystemExit(1)
print("ALL LEADERBOARD HTTP ASSERTIONS PASSED")
