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
check("/api/weekly/refresh -> 401",
      client.post("/api/weekly/refresh").status_code == 401)
check("/api/weekly/csat -> 401", client.get("/api/weekly/csat").status_code == 401)
check("/api/admin/surveys -> 401", client.get("/api/admin/surveys").status_code == 401)
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
            "period_start", "period_end", "priorities"):
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
r = client.get("/api/weekly?start=2026-08-18&end=2026-08-20")
check("custom dates -> 200", r.status_code == 200, str(r.status_code))
if r.status_code == 200:
    check("custom period_start", r.json()["period_start"] == "2026-08-18")
    check("custom period_end", r.json()["period_end"] == "2026-08-20")
check("reversed dates -> 400",
      client.get("/api/weekly?start=2026-08-20&end=2026-08-18").status_code == 400)
check("start without end -> 400",
      client.get("/api/weekly?start=2026-08-18").status_code == 400)
r = client.get("/api/weekly/csat?start=2026-08-18&end=2026-08-20")
check("csat slice -> 200", r.status_code == 200, str(r.status_code))
if r.status_code == 200:
    check("csat slice has csatCurr", "csatCurr" in r.json())
    check("csat slice does not block on missing Pylon",
          r.json().get("error") in (None, "pylon_not_configured")
          or isinstance(r.json().get("csatCurr"), dict))
    # Response-rate denominator: always present as a key; None until a Pylon
    # count lands (here Pylon is unconfigured, so both sides stay None).
    check("csat slice carries closed_range", "closed_range" in r.json())
    check("unconfigured Pylon leaves the denominators None, not fake zeros",
          r.json()["closed_range"] == {"curr": None, "prev": None})

print()
print("=== response-rate denominator: closed-in-range, by resolution date ===")
import pylon as _pylon


async def fake_count(start, end, exclude_states=(), internal_ids=None,
                     internal_channels=None):
    fake_count.exclude = exclude_states
    fake_count.calls.append((start.isoformat(), end.isoformat()))
    # current period: 30 closed (10 internal); previous: 60 (20 internal)
    if start.isoformat() == "2026-08-18":
        return {"total": 30, "internal": 10, "external": 20}, True
    return {"total": 60, "internal": 20, "external": 40}, True

fake_count.calls = []
real_count = _pylon.count_resolved_issues
_pylon.count_resolved_issues = fake_count
appmod._CLOSED_COUNTS.clear()
try:
    r = client.get("/api/weekly/csat?start=2026-08-18&end=2026-08-20")
    cr = r.json()["closed_range"]
    check("closed counts flow through for both periods",
          (cr["curr"], cr["prev"]) ==
          ({"count": 30, "complete": True}, {"count": 60, "complete": True}),
          str(cr))
    check("both period windows were asked of Pylon, prev before curr window",
          sorted(fake_count.calls) ==
          [("2026-08-15", "2026-08-17"), ("2026-08-18", "2026-08-20")],
          str(fake_count.calls))
    check("archived is always excluded from the denominator",
          "archived" in fake_count.exclude, str(fake_count.exclude))
    # The page-level channel scope picks its own denominator from the same
    # cached split — External must not divide by a total that includes
    # internal tickets which can never produce CSAT.
    r2 = client.get("/api/weekly/csat?start=2026-08-18&end=2026-08-20"
                    "&channels_scope=external")
    cr2 = r2.json()["closed_range"]
    check("external scope uses the external denominator",
          (cr2["curr"]["count"], cr2["prev"]["count"]) == (20, 40), str(cr2))
    check("scope echoed back", r2.json().get("channel_scope") == "external")
    check("bad scope -> 400",
          client.get("/api/weekly/csat?start=2026-08-18&end=2026-08-20"
                     "&channels_scope=nope").status_code == 400)
    check("bad scope on /api/weekly -> 400",
          client.get("/api/weekly?start=2026-08-18&end=2026-08-20"
                     "&channels_scope=nope").status_code == 400)
    # TTL cache: a second view must not crawl Pylon again.
    n = len(fake_count.calls)
    client.get("/api/weekly/csat?start=2026-08-18&end=2026-08-20")
    check("second view reads the cache, no second crawl",
          len(fake_count.calls) == n)
finally:
    _pylon.count_resolved_issues = real_count
    appmod._CLOSED_COUNTS.clear()

print()
print("=== refresh: fetch today + refresh the week, no AI path ===")
from types import SimpleNamespace

import openqc


async def fake_fetch_and_store(target):
    fake_fetch_and_store.asked = target
    return SimpleNamespace(count=7, deleted=0, kept_reviewed=0,
                           restored=0, complete=True)


async def fake_backfill(start, end):
    fake_backfill.range = (start, end)
    return {"requested": 3, "stored": 3, "rescored": 1, "deleted": 0,
            "kept_reviewed": 0, "failed": 0, "scoring_failures": None}


fake_fetch_and_store.asked = None
fake_backfill.range = None
real_fs, real_bf = appmod.fetch_and_store, openqc.backfill_range
appmod.fetch_and_store, openqc.backfill_range = fake_fetch_and_store, fake_backfill
try:
    r = client.post("/api/weekly/refresh")
finally:
    appmod.fetch_and_store, openqc.backfill_range = real_fs, real_bf

check("refresh -> 200", r.status_code == 200, str(r.status_code))
if r.status_code == 200:
    body = r.json()
    check("refresh fetched today's count", body.get("fetched") == 7)
    check("refresh reports completeness", body.get("complete") is True)
    check("today was the fetched date",
          fake_fetch_and_store.asked is not None
          and fake_fetch_and_store.asked.isoformat() == body.get("date"))
    # Mondays have no earlier days this week; other days must refresh them.
    if fake_fetch_and_store.asked.weekday() == 0:
        check("Monday skips the week refresh", body.get("week_refreshed") is None)
    else:
        check("earlier week days refreshed by id",
              body.get("week_refreshed", {}).get("stored") == 3)
        check("week refresh stops the day before today",
              fake_backfill.range is not None
              and fake_backfill.range[1] < body.get("date"))

print()
print("=== page renders ===")
r = client.get("/weekly")
check("/weekly -> 200", r.status_code == 200, str(r.status_code))
check("page has the nav host", 'id="app-nav"' in r.text)
check("page marks itself", 'data-page="weekly"' in r.text)
check("filters are wired", 'id="fWeek"' in r.text and 'id="clearFilters"' in r.text)
check("date inputs are present",
      'id="period-start"' in r.text and 'id="period-end"' in r.text)
check("original KPI and chart ids",
      'id="k-vol"' in r.text and 'id="cTrend"' in r.text and 'id="agTBody"' in r.text)
check("title is Support weekly Dashboard",
      "Support weekly Dashboard" in r.text)
check("horizontal charts put beginAtZero on x",
      "function hBarOpts()" in r.text
      and "indexAxis: \"y\"" in r.text
      and "beginAtZero: true" in r.text.split("function hBarOpts()")[1].split("function ")[0])
check("horizontal charts no longer inherit chartBase y-beginAtZero",
      "options: { ...base, indexAxis: \"y\" }" not in r.text)
check("empty store is named, not filled in",
      'id="store-note"' in r.text
      and "does not invent categories or customers" in r.text)

r = client.get("/admin")
check("admin has CSAT section",
      r.status_code == 200 and 'id="csat_survey_id"' in r.text
      and 'data-section="csat"' in r.text)
r = client.get("/api/admin/surveys")
check("surveys list is reachable",
      r.status_code in (200, 502, 503), str(r.status_code))

print()
if fails:
    print(f"FAILURES ({len(fails)}): {fails}")
    raise SystemExit(1)
print("ALL WEEKLY HTTP ASSERTIONS PASSED")
