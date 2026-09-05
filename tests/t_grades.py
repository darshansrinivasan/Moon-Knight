"""Reproduce the production 08-26 shape and check the reporting fixes."""
import db
import review
import rules as qc_rules
import slack
import vault

db.init_db()
NOW = "2026-08-26T10:00:00+00:00"

# 63 tickets: 23 archived (excluded), 40 scored — exactly production's shape.
with db.get_conn() as c:
    for i in range(63):
        archived = i < 23
        state = "archived" if archived else "closed"
        c.execute(
            "INSERT INTO tickets (id,number,fetch_date,title,state,assignee_name)"
            " VALUES (?,?,?,?,?,?)",
            (f"t{i}", 1000 + i, "2026-08-26", f"Ticket {i}", state, "Alice"),
        )
        c.execute(
            "INSERT INTO rule_checks (ticket_id,fetch_date,r1) VALUES (?,?,?)",
            (f"t{i}", "2026-08-26", "Pass"),
        )
        if not archived:
            grade = "Fail" if i == 30 else "Pass"
            c.execute(
                "INSERT INTO ai_checks (ticket_id,fetch_date,a1,a3,a4,a5,"
                "ai_notes,overall_result,checked_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (f"t{i}", "2026-08-26", "Pass", "Good", "Pass", "Pass",
                 "note", grade, NOW),
            )

# A human overrode the one AI Fail to Pass.
with db.get_conn() as c:
    c.execute(
        "INSERT INTO ticket_reviews (ticket_id,decision,kept_ai,reviewer_email,"
        "reviewer_name,note,reviewed_at) VALUES ('t30','Pass',0,'a@x.com','A','',?)",
        (NOW,),
    )

vault.set_raw_setting("qc_rules_json", '{"excluded_states": ["archived"]}', "test")
qc_rules.invalidate()
print("excluded_states:", qc_rules.excluded_states())
print()

s = slack.build_summary("2026-08-26")
print("=== Slack summary ===")
for k in ("total", "pass", "fail", "review", "pending", "excluded", "pass_rate"):
    print(f"  {k:10} = {s[k]}")
print()

assert s["excluded"] == 23, f"expected 23 excluded, got {s['excluded']}"
assert s["pending"] == 0, f"expected 0 pending, got {s['pending']}"
assert s["total"] == 40, f"expected in-scope 40, got {s['total']}"
assert s["fail"] == 0, f"human override should make fail 0, got {s['fail']}"
assert s["pass"] == 40, f"expected 40 pass, got {s['pass']}"
assert s["pass_rate"] == 100
print("PASS: archived no longer counted as 'not scored'")
print("PASS: human sign-off now respected in the Slack report")
print()

# Agreement across surfaces.
st = db.ticket_stats("2026-08-26")
dash = review.apply_effective_grades(db.get_day_tickets("2026-08-26"))
dash_fail = sum(1 for t in dash if t["overall_result"] == "Fail")
print("=== cross-surface agreement on ticket t30 ===")
print(f"  slack fail count      = {s['fail']}")
print(f"  ticket_stats fail     = {st['fail_count']}")
print(f"  dashboard fail count  = {dash_fail}")
assert s["fail"] == st["fail_count"] == dash_fail == 0
print("PASS: all three surfaces agree")
print()

# A genuinely ungraded in-scope ticket must be reported, not hidden.
with db.get_conn() as c:
    c.execute(
        "INSERT INTO tickets (id,number,fetch_date,title,state,assignee_name)"
        " VALUES ('gap',9999,'2026-08-26','Ungraded','closed','Bob')"
    )
s2 = slack.build_summary("2026-08-26")
print("=== after adding 1 in-scope ungraded ticket ===")
print(f"  pending  = {s2['pending']}  (must be 1)")
print(f"  excluded = {s2['excluded']} (must stay 23)")
assert s2["pending"] == 1 and s2["excluded"] == 23
blocks = slack._summary_blocks(s2, "https://x")
text = str(blocks)
assert "could not be graded" in text, "a real gap must raise an alarm line"
print("PASS: real scoring gaps still alarm loudly")
print()

# ── every surface must agree about what is in scope ──────────────────────────
# /api/analytics carried the soft-delete guard but not the excluded-state
# filter, so with 'archived' excluded from scoring, archived tickets still
# counted toward every assignee's total and sat as "pending" forever: waiting
# for a grade the scorer would never give them, because it agreed they were out
# of scope. In production that read as 1,691 tickets with 860 pending on the
# Analytics page against 877 and 83 on the leaderboard, same month, same data.
# The predicate now has one home in rules.excluded_state_clause; this asserts
# every reader uses it.
import leaderboard

IN_SCOPE, ARCHIVED = 41, 23     # 40 seeded + the 'gap' ticket added above

print("=== every surface agrees about scope ===")

cal = db.get_calendar_day("2026-08-26")
print(f"  calendar cell count   = {cal['ticket_count']}")
assert cal["ticket_count"] == IN_SCOPE, cal["ticket_count"]

month = {c["fetch_date"]: c for c in db.get_calendar_month(2026, 8)}
print(f"  calendar month cell   = {month['2026-08-26']['ticket_count']}")
assert month["2026-08-26"]["ticket_count"] == IN_SCOPE

day = db.get_day_tickets("2026-08-26")
print(f"  day list length       = {len(day)}")
assert len(day) == IN_SCOPE
assert not any(t["state"] == "archived" for t in day), \
    "an archived ticket must not appear in the work queue"

print(f"  slack total           = {s2['total']}")
assert s2["total"] == IN_SCOPE

lb = leaderboard.build("2026-08-26", "2026-08-26")
print(f"  leaderboard in_scope  = {lb['totals']['in_scope']}")
assert lb["totals"]["in_scope"] == IN_SCOPE, lb["totals"]

print(f"  excluded, counted once= {db.excluded_ticket_count('2026-08-26')}")
assert db.excluded_ticket_count("2026-08-26") == ARCHIVED
print("PASS: calendar, day list, Slack and leaderboard all count the same day")
print()

# Turning the exclusion off must bring them back everywhere, together — the
# setting is the single control, not four independent ones.
vault.set_raw_setting("qc_rules_json", '{"excluded_states": []}', "test")
qc_rules.invalidate()
print("=== with nothing excluded, every surface includes them again ===")
both = IN_SCOPE + ARCHIVED
print(f"  calendar={db.get_calendar_day('2026-08-26')['ticket_count']} "
      f"day={len(db.get_day_tickets('2026-08-26'))} "
      f"slack={slack.build_summary('2026-08-26')['total']} "
      f"leaderboard={leaderboard.build('2026-08-26','2026-08-26')['totals']['in_scope']}")
assert db.get_calendar_day("2026-08-26")["ticket_count"] == both
assert len(db.get_day_tickets("2026-08-26")) == both
assert slack.build_summary("2026-08-26")["total"] == both
assert leaderboard.build("2026-08-26", "2026-08-26")["totals"]["in_scope"] == both
assert db.excluded_ticket_count("2026-08-26") == 0
print("PASS: one setting moves every surface together")

# Restore, so the file leaves the DB as it found it.
vault.set_raw_setting("qc_rules_json", '{"excluded_states": ["archived"]}', "test")
qc_rules.invalidate()

print()
print("=== a sign-off requires a note; a revert does not ===")
# Enforced server-side, not just by the modal: the Accept button used to
# submit with an empty note, and any client could.
_admin = {"email": "a@x.com", "name": "A", "role": "admin"}
for decision in ("Pass", "Fail", "accept"):
    for bare in ("", "   ", None):
        try:
            review.accept_ticket("t30", _admin, decision, bare)
            raise AssertionError(f"{decision!r} with note {bare!r} was accepted")
        except review.ReviewInvalid as e:
            assert "note is required" in str(e).lower(), str(e)
print("PASS: bare Pass/Fail/accept all refused")

rec = review.accept_ticket("t30", _admin, "Fail", "premature closure")
assert rec["note"] == "premature closure"
rec = review.accept_ticket("t30", _admin, "revert", "")
assert rec["decision"] == "Revert"
print("PASS: noted sign-off recorded; bare revert still allowed")

print()
print("ALL GRADE ASSERTIONS PASSED")
