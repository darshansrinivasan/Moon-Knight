"""Who may spend money, who may configure, who may edit the rubric, who may look.

Running QC is neither a read nor an act of configuration. It bills the
workspace's Vertex quota and overwrites grades reviewers are looking at, so it
cannot sit behind `require_user` with the dashboard — every signed-in member
could spend the AI budget. But it is also routine daily work that should not
require handing someone the credential vault and the user list.

Editing the grading rules is open to operators as well as admins. Admins are
global superusers: there is no page or action they are shut out of, and the
rubric is not an exception. Restricting it to operators was tried and reverted —
admins own the user list, so an admin who wanted the rubric could just grant
someone Operator. It bought no boundary and cost a locked-out administrator.

That revert is why the rubric block below asserts the *admin* row explicitly
rather than folding it in with the operator: it is the row that regressed once,
so it is the row worth naming. The matrix is checked endpoint by endpoint rather
than by trusting the dependency — a permission bug is silent until it is
expensive.
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
check("two may edit the rubric",
      list(auth.CAN_EDIT_RULES), ["admin", "operator"])
check("and the admin is one of them — a superuser is shut out of nothing",
      auth.ROLE_ADMIN in auth.CAN_EDIT_RULES, True)
check("rubric rights and spend rights are named separately even when equal",
      auth.CAN_EDIT_RULES == auth.CAN_RUN_QC, True)
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
print("=== the plumbing: admins only ===")
for name, cl, denied in (("admin", ADMIN, False),
                         ("operator", OPERATOR, True),
                         ("member", MEMBER, True)):
    r = cl.put("/api/admin/settings", json={})
    check(f"{name} edits settings", r.status_code == 403, denied)
    r = cl.post("/api/admin/users", json={"email": "x@spotdraft.com"})
    check(f"{name} invites people", r.status_code == 403, denied)

print()
print("=== session expiry: 0 means never, and the OAuth state still expires ===")
# People were being signed out every day: a 12-hour absolute session that
# nothing renewed. 0 now disables expiry — defensible only because
# `current_user` re-reads the user row on every request, so deactivating
# someone takes effect immediately rather than waiting for a session to lapse.
import importlib
import os as _os
import time as _time

_prev = _os.environ.get("QC_SESSION_HOURS")
try:
    _os.environ["QC_SESSION_HOURS"] = "0"
    importlib.reload(auth)
    check("0 means the browser's maximum, not zero seconds",
          auth.session_seconds(), auth.MAX_COOKIE_DAYS * 86400)
    far = auth._unsign(auth.issue_session({"email": "x@spotdraft.com"}), b"session")
    check("a never-expiring session still verifies", bool(far), True)
    check("and its exp is far future, not absent",
          far["exp"] > _time.time() + 300 * 86400, True)
    # The guard that matters: _unsign is shared, and the OAuth state's
    # ten-minute TTL relies on a missing/past exp being rejected.
    stale = auth._sign({"n": "x", "exp": _time.time() - 1}, b"oauth-state")
    check("an expired OAuth state is still refused",
          auth._unsign(stale, b"oauth-state"), None)
    _os.environ["QC_SESSION_HOURS"] = "12"
    importlib.reload(auth)
    check("a positive value is still honoured", auth.session_seconds(), 12 * 3600)

    # Sliding renewal. A fresh cookie is NOT re-issued on every request (that
    # would put a Set-Cookie on every response for nothing); an aged one is,
    # which is what stops the browser's 400-day cap from ending an active
    # session.
    class _Req:
        def __init__(self, token): self.cookies = {auth.COOKIE_NAME: token} if token else {}
    fresh = auth.issue_session({"email": "x@spotdraft.com"})
    check("a brand-new session is not re-issued",
          auth.needs_renewal(_Req(fresh)), False)
    aged = auth._sign({"email": "x@spotdraft.com", "name": "",
                       "exp": _time.time() + 60}, b"session")
    check("an aged one is", auth.needs_renewal(_Req(aged)), True)
    check("no cookie means nothing to renew", auth.needs_renewal(_Req(None)), False)
    check("a forged cookie is never renewed",
          auth.needs_renewal(_Req("not.a.token")), False)
finally:
    if _prev is None:
        _os.environ.pop("QC_SESSION_HOURS", None)
    else:
        _os.environ["QC_SESSION_HOURS"] = _prev
    importlib.reload(auth)

print()
print("=== the CSM page: every role reads it, and refreshes it ===")
# Deliberately open to members end to end: a CSM checking what is open on their
# own accounts is reading, not administering.
for name, cl in (("admin", ADMIN), ("operator", OPERATOR), ("member", MEMBER)):
    r = cl.get("/api/csm/owners")
    check(f"{name} reads the CSM picker", r.status_code == 403, False)
    r = cl.get("/api/csm/analytics?owner=nobody")
    check(f"{name} reads CSM analytics", r.status_code == 403, False)
    # Refresh is open to members on purpose, unlike /api/open/refetch which
    # does the same Pylon fetch behind an operator gate: a CSM about to join a
    # customer call needs current data, has no operator to ask, and this path
    # spends no money. Asserted so the exception stays a decision, not a drift.
    r = cl.post("/api/csm/refresh")
    check(f"{name} refreshes from Pylon", r.status_code == 403, False)

print()
print("=== the rubric: admins and operators edit, members read ===")
# The admin row is asserted rather than assumed: it was briefly operator-only,
# which locked administrators out of a page they are supposed to own.
for name, cl, denied in (("admin", ADMIN, False),
                         ("operator", OPERATOR, False),
                         ("member", MEMBER, True)):
    r = cl.put("/api/rules", json={"rules": {"r4_sla_hours": 24}})
    check(f"{name} saves rules", r.status_code == 403, denied)
    r = cl.post("/api/rules/undo")
    check(f"{name} undoes a rules save", r.status_code == 403, denied)
    r = cl.post("/api/rules/dry-run", json={"limit": 1})
    check(f"{name} spends on a dry-run", r.status_code == 403, denied)
    r = cl.post("/api/rules/preview", json={"rules": {}})
    check(f"{name} previews a draft", r.status_code == 403, denied)

body = MEMBER.put("/api/rules", json={"rules": {}}).json().get("detail", "")
check("the member's refusal names the role to ask for",
      "operator" in body.lower(), True)
check("and says what is at stake, not just 'forbidden'",
      "grade" in body.lower(), True)

# An admin is a superuser: assert there is no rubric action they are refused.
for label, resp in (("save", ADMIN.put("/api/rules", json={"rules": {}})),
                    ("undo", ADMIN.post("/api/rules/undo")),
                    ("dry-run", ADMIN.post("/api/rules/dry-run", json={"limit": 1})),
                    ("preview", ADMIN.post("/api/rules/preview", json={"rules": {}}))):
    check(f"admin is never 403 on {label}", resp.status_code != 403, True)

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

print()
print("=== the pages are told who may edit the rubric ===")
# Served as its own flag rather than inferred from the role, so the Save button
# and the endpoint cannot disagree — a dead Save button is a support ticket.
for name, cl, expected in (("admin", ADMIN, True),
                           ("operator", OPERATOR, True),
                           ("member", MEMBER, False)):
    check(f"{name} /api/me can_edit_rules",
          cl.get("/api/me").json()["can_edit_rules"], expected)
    check(f"{name} /api/rules can_edit",
          cl.get("/api/rules").json()["can_edit"], expected)

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
