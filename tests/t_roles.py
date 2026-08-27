"""Who may spend money, who may configure, who may only look.

Running QC is neither a read nor an act of configuration. It bills the
workspace's Vertex quota and overwrites grades reviewers are looking at, so it
cannot sit behind `require_user` with the dashboard — every signed-in member
could spend the AI budget. But it is also routine daily work that should not
require handing someone the credential vault, the grading rules and the user
list. Hence three roles: admins configure, operators run, members read.

The matrix below is the whole point of the change, so it is asserted endpoint by
endpoint rather than by trusting the decorator. A permission bug is silent until
it is expensive.
"""
from fastapi.testclient import TestClient

import app as appmod
import auth
import db

db.init_db()
fails = []


def check(name, got, want):
    ok = got == want
    print(f"  {'OK ' if ok else 'FAIL'} {name}: got {got!r}, want {want!r}")
    if not ok:
        fails.append(name)


def client_for(email, role):
    with db.get_conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO app_users"
            " (email,name,role,is_active,created_at)"
            " VALUES (?,?,?,1,'2026-01-01T00:00:00+00:00')",
            (email, email.split("@")[0], role),
        )
    cl = TestClient(appmod.app, follow_redirects=False)
    cl.cookies.set(auth.COOKIE_NAME,
                   auth.issue_session({"email": email, "name": email}))
    return cl


ADMIN    = client_for("admin@spotdraft.com",    auth.ROLE_ADMIN)
OPERATOR = client_for("operator@spotdraft.com", auth.ROLE_OPERATOR)
MEMBER   = client_for("member@spotdraft.com",   auth.ROLE_MEMBER)

# A second admin, so the last-admin guard never masks a role-change assertion.
client_for("admin2@spotdraft.com", auth.ROLE_ADMIN)

print("=== the roles are declared in one place ===")
check("three roles", list(auth.ROLES),
      ["admin", "operator", "member"])
check("only two may run QC", list(auth.CAN_RUN_QC), ["admin", "operator"])
check("every role has a label",
      all(r in auth.ROLE_LABELS for r in auth.ROLES), True)
check("every role explains itself",
      all(len(auth.ROLE_DESCRIPTIONS.get(r, "")) > 20 for r in auth.ROLES), True)

print()
print("=== running QC: operators and admins only ===")
# 403 vs anything-else is the assertion. A 400/404/500 means the request got
# past authorisation, which is the failure this suite exists to catch.
for name, cl, denied in (("admin", ADMIN, False),
                         ("operator", OPERATOR, False),
                         ("member", MEMBER, True)):
    for label, path in (("run QC", "/api/qc/2026-08-26"),
                        ("refetch", "/api/fetch/2026-08-26"),
                        ("scheduled run now", "/api/admin/run-now")):
        r = cl.post(path, json={})
        check(f"{name} {label}", r.status_code == 403, denied)

r = MEMBER.post("/api/qc/2026-08-26", json={})
body = r.json().get("detail", "")
check("the refusal names the role to ask for", "Operator" in body, True)
check("and says why it is restricted",
      "spends" in body and "budget" in body, True)

print()
print("=== configuring: admins only ===")
for name, cl, denied in (("admin", ADMIN, False),
                         ("operator", OPERATOR, True),
                         ("member", MEMBER, True)):
    r = cl.put("/api/rules", json={"rules": {"r4_sla_hours": 24}})
    check(f"{name} edits rules", r.status_code == 403, denied)
    r = cl.put("/api/admin/settings", json={})
    check(f"{name} edits settings", r.status_code == 403, denied)
    r = cl.post("/api/rules/dry-run", json={"limit": 1})
    check(f"{name} spends on a dry-run", r.status_code == 403, denied)
    r = cl.post("/api/admin/users", json={"email": "x@spotdraft.com"})
    check(f"{name} invites people", r.status_code == 403, denied)

print()
print("=== reading: everyone signed in ===")
for name, cl in (("admin", ADMIN), ("operator", OPERATOR), ("member", MEMBER)):
    for label, path in (("dashboard data", "/api/day/2026-08-26"),
                        ("analytics", "/api/analytics?month=2026-08"),
                        ("leaderboard", "/api/leaderboard"),
                        ("rules", "/api/rules"),
                        ("runs", "/api/runs"),
                        ("statuses", "/api/ticket-states")):
        r = cl.get(path)
        check(f"{name} reads {label}", r.status_code, 200)

print()
print("=== the pages themselves stay readable for everyone ===")
# Read-only is enforced per action, not by walling off pages. A member who
# cannot see Admin cannot see how the system is configured, which is worse.
for name, cl in (("operator", OPERATOR), ("member", MEMBER)):
    for path in ("/", "/admin", "/rules", "/runs", "/leaderboard"):
        check(f"{name} can open {path}", cl.get(path).status_code, 200)

print()
print("=== /api/me tells the page what it may offer ===")
for name, cl, expected in (("admin", ADMIN, True),
                           ("operator", OPERATOR, True),
                           ("member", MEMBER, False)):
    me = cl.get("/api/me").json()
    check(f"{name} can_run_qc", me["can_run_qc"], expected)
    check(f"{name} has a readable role label",
          me["role_label"], auth.ROLE_LABELS[me["role"]])

runs = OPERATOR.get("/api/runs").json()
check("an operator may run but not reschedule",
      (runs["can_run"], runs["can_edit"]), (True, False))
runs = MEMBER.get("/api/runs").json()
check("a member may do neither", (runs["can_run"], runs["can_edit"]),
      (False, False))
runs = ADMIN.get("/api/runs").json()
check("an admin may do both", (runs["can_run"], runs["can_edit"]),
      (True, True))

print()
print("=== the role list is served, so the UI cannot drift from validation ===")
ov = ADMIN.get("/api/admin/overview").json()
check("overview serves the roles", [r["value"] for r in ov["roles"]],
      list(auth.ROLES))
check("with labels and descriptions",
      all(r.get("label") and r.get("description") for r in ov["roles"]), True)

print()
print("=== assigning the role ===")
r = ADMIN.put("/api/admin/users/member@spotdraft.com",
              json={"role": "operator", "is_active": True})
check("an admin can promote to operator", r.status_code, 200)
check("and it took", MEMBER.get("/api/me").json()["can_run_qc"], True)
check("the promoted user can now run QC",
      MEMBER.post("/api/qc/2026-08-26", json={}).status_code != 403, True)

r = ADMIN.put("/api/admin/users/member@spotdraft.com",
              json={"role": "member", "is_active": True})
check("and can be demoted again", r.status_code, 200)
check("losing the permission with it",
      MEMBER.post("/api/qc/2026-08-26", json={}).status_code, 403)

r = ADMIN.put("/api/admin/users/member@spotdraft.com",
              json={"role": "wizard", "is_active": True})
check("an unknown role is refused", r.status_code, 400)
check("and the refusal lists the real ones",
      "operator" in r.json()["detail"], True)

# The last-admin guard predates this and must survive a third role: demoting the
# only admin to operator would leave nobody able to configure anything.
with db.get_conn() as c:
    c.execute("DELETE FROM app_users WHERE email = 'admin2@spotdraft.com'")
r = ADMIN.put("/api/admin/users/admin@spotdraft.com",
              json={"role": "operator", "is_active": True})
check("an admin cannot demote themselves", r.status_code, 400)

print()
if fails:
    print(f"FAILURES ({len(fails)}): {fails}")
    raise SystemExit(1)
print("ALL ROLE ASSERTIONS PASSED")
