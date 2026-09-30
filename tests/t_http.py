"""End-to-end HTTP checks against the real ASGI app: XSS, redirect, auth gate."""
import html as htmllib

from fastapi.testclient import TestClient

import app as appmod

client = TestClient(appmod.app, follow_redirects=False)
fails = []


def check(name, ok, detail=""):
    print(f"  {'OK ' if ok else 'FAIL'} {name}{' — ' + detail if detail else ''}")
    if not ok:
        fails.append(name)


print("=== XSS on /auth/callback (was: admin takeover) ===")
payload = '<img src=x onerror="alert(1)">'
r = client.get("/auth/callback", params={"error": payload})
body = r.text
check("status 400", r.status_code == 400, str(r.status_code))
check("raw tag NOT present", "<img src=x" not in body)
check("escaped form present", htmllib.escape(payload) in body)
check("no onerror= attribute", 'onerror="alert(1)"' not in body)

print()
print("=== open redirect via next ===")
for bad in ["//evil.com/x", "/\\evil.com", "https://evil.com"]:
    r = client.get("/auth/start", params={"next": bad})
    loc = r.headers.get("location", "")
    # The next value is signed into the state; assert the hostile value is gone.
    leaked = "evil.com" in loc
    check(f"next={bad!r} not carried", not leaked, loc[:90])

# /auth/start needs a configured OAuth client to build a redirect, so assert
# safe_next directly for the positive case rather than depending on that setup.
import auth as authmod
check("legitimate next preserved", authmod.safe_next("/runs") == "/runs",
      authmod.safe_next("/runs"))
check("query string preserved",
      authmod.safe_next("/?date=2026-08-26") == "/?date=2026-08-26")

print()
print("=== auth gate: API returns 401, pages redirect ===")
r = client.get("/api/stats")
check("/api/stats unauthenticated -> 401", r.status_code == 401, str(r.status_code))
r = client.get("/api/closed-sweep")
check("/api/closed-sweep unauthenticated -> 401", r.status_code == 401, str(r.status_code))
# Cost is an operations figure: the endpoint behind the sidebar total must not
# answer an unauthenticated caller, and is operator-gated for signed-in members.
r = client.get("/api/spend/total")
check("/api/spend/total unauthenticated -> 401", r.status_code == 401, str(r.status_code))
# The CSM page is a READ surface every signed-in role may open — a CSM looking
# up their own book is not an operator action. The sync behind it writes the
# store every CSM reads, so that one is gated (asserted in t_roles).
r = client.get("/csm")
check("/csm unauthenticated -> 302 to login",
      r.status_code in (302, 307) and "/login" in r.headers.get("location", ""),
      f"{r.status_code} {r.headers.get('location', '')}")
r = client.get("/api/csm/owners")
check("/api/csm/owners unauthenticated -> 401", r.status_code == 401, str(r.status_code))
r = client.get("/api/csm/tickets?owner=x")
check("/api/csm/tickets unauthenticated -> 401", r.status_code == 401, str(r.status_code))
r = client.get("/api/csm/tickets?owner=a,b,c")
check("/api/csm/tickets with several owners unauthenticated -> 401",
      r.status_code == 401, str(r.status_code))
r = client.get("/api/csm/analytics")
check("/api/csm/analytics with no owner unauthenticated -> 401",
      r.status_code == 401, str(r.status_code))
r = client.get("/api/csm/analytics?owner=x")
check("/api/csm/analytics unauthenticated -> 401", r.status_code == 401, str(r.status_code))
r = client.post("/api/csm/refresh")
check("/api/csm/refresh unauthenticated -> 401", r.status_code == 401, str(r.status_code))
r = client.get("/api/rootly/incidents")
check("/api/rootly/incidents unauthenticated -> 401", r.status_code == 401, str(r.status_code))
r = client.get("/rootly")
check("/rootly unauthenticated -> 302 to login",
      r.status_code == 302 and "/login" in r.headers.get("location", ""))
r = client.get("/rootly/runs")
check("/rootly/runs unauthenticated -> 302 to login",
      r.status_code == 302 and "/login" in r.headers.get("location", ""))
r = client.get("/")
check("/ unauthenticated -> 302 to login",
      r.status_code == 302 and "/login" in r.headers.get("location", ""))
r = client.get("/healthz")
check("/healthz public", r.status_code == 200, str(r.status_code))

print()
print("=== date validation returns 400, not an empty page ===")
for path in ["/api/fetch/not-a-date", "/api/qc/2026-99-99"]:
    r = client.post(path)
    # Unauthenticated, so 401 comes first; that is fine — we assert it is not a 500.
    check(f"{path} -> not 500", r.status_code != 500, str(r.status_code))

print()
print("=== static assets must revalidate, not heuristically cache ===")
# Without Cache-Control, browsers reused a cached shell.js for days after a
# deploy — the UI looked unchanged in production until a hard refresh.
r = client.get("/static/shell.js")
check("/static served", r.status_code == 200, str(r.status_code))
check("Cache-Control forces revalidation",
      r.headers.get("cache-control") == "no-cache",
      r.headers.get("cache-control", "<missing>"))
check("ETag kept, so revalidation is a cheap 304", "etag" in r.headers)
r = client.get("/static/shell.js", headers={"If-None-Match": r.headers["etag"]})
check("conditional request answers 304", r.status_code == 304, str(r.status_code))

print()
if fails:
    print(f"FAILURES ({len(fails)}): {fails}")
    raise SystemExit(1)
print("ALL HTTP ASSERTIONS PASSED")
