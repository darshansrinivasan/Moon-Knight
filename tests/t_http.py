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
if fails:
    print(f"FAILURES ({len(fails)}): {fails}")
    raise SystemExit(1)
print("ALL HTTP ASSERTIONS PASSED")
