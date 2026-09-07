"""Weekly dashboard endpoints: auth, validation, page chrome."""
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
check("/api/weekly -> 401", client.get("/api/weekly").status_code == 401)
r = client.get("/weekly")
check("/weekly -> redirect to login",
      r.status_code == 302 and "/login" in r.headers.get("location", ""))

client.cookies.set(auth.COOKIE_NAME,
                   auth.issue_session({"email": "a@spotdraft.com", "name": "A"}))

print()
print("=== authenticated ===")
r = client.get("/api/weekly")
check("no week -> 200", r.status_code == 200, str(r.status_code))
body = r.json()
for key in ("generatedAt", "prevWeekLabel", "currWeekLabel", "metrics",
            "dailyData", "allRows", "agentTable", "insights", "week_start",
            "priorities"):
    check(f"payload has {key}", key in body)
check("metrics.cv_total is a scalar", isinstance(body["metrics"].get("cv_total"), int))
check("dailyData.cv_resolved is a week array",
      isinstance(body["dailyData"].get("cv_resolved"), list))
check("week_start is a Monday",
      body["week_start"] and
      __import__("datetime").date.fromisoformat(body["week_start"]).weekday() == 0)

r = client.get("/api/weekly?week=2026-08-19")
check("mid-week snaps -> 200", r.status_code == 200, str(r.status_code))
check("snaps to Monday 17", r.json()["week_start"] == "2026-08-17")

print()
print("=== validation ===")
check("bad week -> 400",
      client.get("/api/weekly?week=nope").status_code == 400)

print()
print("=== page renders ===")
r = client.get("/weekly")
check("/weekly -> 200", r.status_code == 200, str(r.status_code))
check("page has the nav host", 'id="app-nav"' in r.text)
check("page marks itself", 'data-page="weekly"' in r.text)
check("filters are wired", 'id="fWeek"' in r.text and 'id="clearFilters"' in r.text)
check("original KPI and chart ids",
      'id="k-vol"' in r.text and 'id="cTrend"' in r.text and 'id="agTBody"' in r.text)
check("title is Support weekly Dashboard",
      "Support weekly Dashboard" in r.text)

print()
if fails:
    print(f"FAILURES ({len(fails)}): {fails}")
    raise SystemExit(1)
print("ALL WEEKLY HTTP ASSERTIONS PASSED")
