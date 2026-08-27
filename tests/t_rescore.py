"""Refetch must not trigger a rescore unless graded content actually changed.

Before this, `tickets.fetched_at > ai_checks.checked_at` meant any refetch
marked the whole day stale and the next run rescored it at full price —
production had three runs for 2026-08-26, two of them byte-identical.
"""
import db
import qc_runner as q
import rules as qc_rules
import vault

db.init_db()
DATE = "2026-08-26"
T0 = "2026-08-26T08:00:00+00:00"

fails = []


def check(name, got, want):
    ok = got == want
    print(f"  {'OK ' if ok else 'FAIL'} {name}: got {got!r}, want {want!r}")
    if not ok:
        fails.append(name)


def add_ticket(tid, number, state="closed", title="T", msg="hello"):
    with db.get_conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO tickets (id,number,fetch_date,title,state,"
            "assignee_name,custom_fields,source,customer_portal_visible,fetched_at)"
            " VALUES (?,?,?,?,?,'Alice','{}','email',1,?)",
            (tid, number, DATE, title, state, T0),
        )
        c.execute(
            "INSERT OR REPLACE INTO rule_checks (ticket_id,fetch_date,r1,r2,r3,r4,r5)"
            " VALUES (?,?,'Pass','Pass','Pass','Pass','Pass')",
            (tid, DATE),
        )
        c.execute("DELETE FROM messages WHERE ticket_id = ?", (tid,))
        c.execute(
            "INSERT INTO messages (id,ticket_id,message_html,timestamp,is_customer,is_private)"
            " VALUES (?,?,?,?,1,0)",
            (f"m-{tid}", tid, f"<p>{msg}</p>", T0),
        )


def mark_scored(tid, fingerprint):
    """Write an ai_checks row as a completed grade with the given fingerprint."""
    with db.get_conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO ai_checks (ticket_id,fetch_date,a1,a2,a3,a4,a5,"
            "ai_notes,overall_result,checked_at,qc_fingerprint)"
            " VALUES (?,?,'Pass','Neutral','Good','Pass','Pass','n','Pass',?,?)",
            (tid, DATE, T0, fingerprint),
        )


def current_fp(tid):
    tickets = q._load_in_scope(DATE)
    t = next(x for x in tickets if x["id"] == tid)
    return q.qc_fingerprint(t, t["messages"], {k: t.get(k) for k in q.R_CHECK_KEYS})


def touch_fetched_at(tid, when="2026-08-27T09:00:00+00:00"):
    """Simulate a refetch: fetched_at moves, content does not."""
    with db.get_conn() as c:
        c.execute("UPDATE tickets SET fetched_at = ? WHERE id = ?", (when, tid))


vault.set_raw_setting("qc_rules_json", '{"excluded_states": ["archived"]}', "test")
qc_rules.invalidate()

print("=== never scored -> eligible ===")
add_ticket("t1", 1)
eligible, in_scope = q.eligible_for_scoring(DATE)
check("in scope", in_scope, 1)
check("eligible", len(eligible), 1)

print()
print("=== scored, then refetched with IDENTICAL content -> NOT eligible ===")
mark_scored("t1", current_fp("t1"))
touch_fetched_at("t1")
eligible, in_scope = q.eligible_for_scoring(DATE)
check("still in scope", in_scope, 1)
check("eligible after no-op refetch", len(eligible), 0)
print("   (this is the bug: fetched_at moved but nothing regrades)")

print()
print("=== a changed MESSAGE -> eligible again ===")
add_ticket("t1", 1, msg="hello, and the export is still broken")
eligible, _ = q.eligible_for_scoring(DATE)
check("eligible after content change", len(eligible), 1)

print()
print("=== a changed IRRELEVANT field -> NOT eligible ===")
mark_scored("t1", current_fp("t1"))
with db.get_conn() as c:
    # `link` is never shown to the model, so it must not force a regrade.
    c.execute("UPDATE tickets SET link = ?, fetched_at = ? WHERE id = 't1'",
              ("https://example.com/changed", "2026-08-28T10:00:00+00:00"))
eligible, _ = q.eligible_for_scoring(DATE)
check("eligible after irrelevant change", len(eligible), 0)

print()
print("=== a changed R-CHECK -> eligible (it is printed into the prompt) ===")
with db.get_conn() as c:
    c.execute("UPDATE rule_checks SET r3 = 'Fail' WHERE ticket_id = 't1'")
eligible, _ = q.eligible_for_scoring(DATE)
check("eligible after R-check change", len(eligible), 1)

print()
print("=== a changed STATE -> eligible ===")
mark_scored("t1", current_fp("t1"))
with db.get_conn() as c:
    c.execute("UPDATE tickets SET state = 'waiting_on_you' WHERE id = 't1'")
eligible, _ = q.eligible_for_scoring(DATE)
check("eligible after state change", len(eligible), 1)

print()
print("=== NULL fingerprint (graded before this existed) -> eligible ONCE ===")
mark_scored("t1", None)
eligible, _ = q.eligible_for_scoring(DATE)
check("legacy row is eligible", len(eligible), 1)
mark_scored("t1", current_fp("t1"))     # as the run would write it
eligible, _ = q.eligible_for_scoring(DATE)
check("then stabilises", len(eligible), 0)

print()
print("=== excluded states are out of scope entirely ===")
add_ticket("t2", 2, state="archived")
eligible, in_scope = q.eligible_for_scoring(DATE)
check("archived not in scope", in_scope, 1)
check("archived not eligible", len(eligible), 0)

print()
print("=== cumulative spend never decreases ===")
with db.get_conn() as c:
    for cost in (0.02, 0.10, 0.05):
        c.execute(
            "INSERT INTO qc_runs (date,triggered_by,started_at,finished_at,status,"
            "total,scored,skipped,cost_usd,prompt_tokens,output_tokens)"
            " VALUES (?,'t',?,?,'success',1,1,0,?,10,10)",
            (DATE, T0, T0, cost),
        )
spend = db.qc_spend_for_date(DATE)
check("runs counted", spend["runs"], 3)
check("total is the sum", spend["total_cost_usd"], 0.17)
check("total >= largest single run", spend["total_cost_usd"] >= 0.10, True)

print()
print("=== failed runs are excluded from spend ===")
with db.get_conn() as c:
    c.execute(
        "INSERT INTO qc_runs (date,triggered_by,started_at,status,cost_usd)"
        " VALUES (?,'t',?,'error',9.99)", (DATE, T0),
    )
check("errored run not counted", db.qc_spend_for_date(DATE)["total_cost_usd"], 0.17)

print()
if fails:
    print(f"FAILURES ({len(fails)}): {fails}")
    raise SystemExit(1)
print("ALL RESCORE ASSERTIONS PASSED")
