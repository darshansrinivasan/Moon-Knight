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
