"""Learned suggestions: the confidence gate, what counts as a disagreement, scope.

Every assertion here pins a reading that is easy to get backwards. Counting an
"accept" as an override, or a Revert, or a superseded review, would each produce
a confident suggestion to change a rule on the strength of agreement.
"""
from datetime import datetime, timedelta, timezone

import db
import rules as qc_rules
import suggestions as sg
import vault

db.init_db()

# fetch_date is irrelevant to suggestions — the window is measured on
# reviewed_at — so one fixed day is enough for every ticket.
DATE = "2026-08-26"

fails = []


def check(name, got, want):
    ok = got == want
    print(f"  {'OK ' if ok else 'FAIL'} {name}: got {got!r}, want {want!r}")
    if not ok:
        fails.append(name)


def utc_iso(days_ago=0):
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


def utc_date(days_ago=0):
    return (datetime.now(timezone.utc).date() - timedelta(days=days_ago)).isoformat()


_number = [4000]


def graded(tid, overall, state="closed", link="https://app.usepylon.com/i/x",
           deleted=None, **checks):
    """A fetched ticket, every check passing, graded `overall` by the AI.

    `checks` overrides individual check values (r3="Fail", a3="Poor"…) so each
    fixture states only the thing that drove the verdict.
    """
    _number[0] += 1
    r = {k: "Pass" for k in sg.R_KEYS}
    r["r9"] = "N/A"          # never computed any more; matches real rows
    a = {"a1": "Pass", "a2": "Neutral", "a3": "Good", "a4": "Pass", "a5": "Pass"}
    for key, val in checks.items():
        (r if key.startswith("r") else a)[key] = val

    with db.get_conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO tickets (id,number,fetch_date,title,link,"
            "state,assignee_name,fetched_at,deleted_at)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (tid, _number[0], DATE, f"Ticket {tid}", link, state,
             "Alice", utc_iso(), deleted),
        )
        c.execute(
            f"INSERT OR REPLACE INTO rule_checks (ticket_id,fetch_date,{','.join(r)})"
            f" VALUES (?,?,{','.join('?' * len(r))})",
            (tid, DATE, *r.values()),
        )
        c.execute(
            "INSERT OR REPLACE INTO ai_checks (ticket_id,fetch_date,a1,a2,a3,a4,a5,"
            "ai_notes,overall_result,checked_at) VALUES (?,?,?,?,?,?,?,'n',?,?)",
            (tid, DATE, a["a1"], a["a2"], a["a3"], a["a4"], a["a5"],
             overall, utc_iso()),
        )
    return tid


def sign_off(tid, decision, kept_ai=0, days_ago=0):
    with db.get_conn() as c:
        c.execute(
            "INSERT INTO ticket_reviews (ticket_id,decision,kept_ai,reviewer_email,"
            "reviewer_name,note,reviewed_at) VALUES (?,?,?,'r@x.com','R','',?)",
            (tid, decision, kept_ai, utc_iso(days_ago)),
        )


def group(prefix, n, ai, decision, kept_ai=0, days_ago=0, **fixture):
    """`n` tickets the AI graded `ai`, each signed off `decision`."""
    for i in range(n):
        tid = f"{prefix}{i}"
        graded(tid, ai, **fixture)
        sign_off(tid, decision, kept_ai=kept_ai, days_ago=days_ago)


def find(result, check_key, ai_said=None, human_said=None):
    for o in result["observations"]:
        if o["check"] != check_key:
            continue
        if ai_said is not None and o["ai_said"] != ai_said:
            continue
        if human_said is not None and o["human_said"] != human_said:
            continue
        return o
    return None


def count_of(result, check_key, ai_said=None, human_said=None):
    o = find(result, check_key, ai_said, human_said)
    return o["count"] if o else 0


def row_counts():
    with db.get_conn() as c:
        return {
            t: c.execute(f"SELECT COUNT(*) n FROM {t}").fetchone()["n"]
            for t in ("tickets", "ticket_reviews", "ai_checks", "rule_checks")
        }


vault.set_raw_setting("qc_rules_json", '{"excluded_states": ["archived"]}', "t")
qc_rules.invalidate()


print("=== the confidence gate: four disagreements are noise ===")
group("gate", 4, "Fail", "Pass", r3="Fail")
r = sg.build()
check("MIN_OVERRIDES is 5", sg.MIN_OVERRIDES, 5)
check("4 overrides on R3 surface nothing", find(r, "r3"), None)
check("the suppressed group is reported, not hidden", r["gated_out"], 1)
check("they are still counted as overrides", r["total_overrides"], 4)
check("total_reviews is the denominator", r["total_reviews"], 4)

print()
print("=== six of the same disagreement clears the gate ===")
group("gate2", 2, "Fail", "Pass", r3="Fail")
r = sg.build()
o = find(r, "r3")
check("R3 surfaces with all six", count_of(r, "r3"), 6)
check("nothing left gated", r["gated_out"], 0)
check("a failing R-check is a rules problem", o["kind"], "rule_suspect")
check("it points at an editable rules key", o["target_rule_key"],
      "r3_internal_account_ids")
check("labelled for a human", o["check_label"], "Real customer account")
check("direction recorded", (o["ai_said"], o["human_said"]), ("Fail", "Pass"))
check("evidence is capped at SAMPLE_SIZE", len(o["sample"]), sg.SAMPLE_SIZE)
check("evidence identifies tickets", set(o["sample"][0]) >= {"number", "title",
                                                            "ticket_id"}, True)
check("implies names the edit", "r3_internal_account_ids" in o["implies"], True)

print()
print("=== kept_ai = 1 means the human agreed; it is not evidence ===")
before = sg.build()
group("keep", 6, "Fail", "Pass", kept_ai=1, r4="Fail")
r = sg.build()
check("six kept_ai rows surface nothing", find(r, "r4"), None)
check("and add no overrides", r["total_overrides"] - before["total_overrides"], 0)
check("but they were read as reviews",
      r["total_reviews"] - before["total_reviews"], 6)
print("   (the app never writes kept_ai=1 with a contradicting decision;")
print("    the filter is explicit because kept_ai is the recorded intent)")

print()
print("=== a sign-off that lands on the AI's own verdict is agreement ===")
before = sg.build()
group("acc", 6, "Fail", "Fail", kept_ai=1, r3="Fail")
r = sg.build()
check("accepting an AI Fail adds no overrides",
      r["total_overrides"] - before["total_overrides"], 0)
check("and does not inflate the existing R3 group", count_of(r, "r3"), 6)

print()
print("=== a Revert clears a sign-off; it is not an override ===")
before = sg.build()
group("rev", 6, "Fail", "Revert", r5="Fail")
r = sg.build()
check("Revert surfaces nothing", find(r, "r5"), None)
check("Revert is not an override",
      r["total_overrides"] - before["total_overrides"], 0)
check("Revert is not a review either",
      r["total_reviews"] - before["total_reviews"], 0)

print()
print("=== only the latest review per ticket counts ===")
group("late", 6, "Fail", "Pass", r7="Fail")
check("six overrides surface R7", count_of(sg.build(), "r7"), 6)
for i in range(6):
    sign_off(f"late{i}", "Revert")      # each override is later reverted
r = sg.build()
check("the newer Revert replaces the override", find(r, "r7"), None)
print("   (MAX(id) is taken over every review, not just the ones in the window,")
print("    or a superseded override would outvote the Revert that replaced it)")

print()
print("=== soft-deleted tickets are not evidence ===")
group("del", 6, "Fail", "Pass", r8="Fail", deleted=utc_iso())
check("deleted tickets surface nothing", find(sg.build(), "r8"), None)
with db.get_conn() as c:
    c.execute("UPDATE tickets SET deleted_at = NULL WHERE id LIKE 'del%'")
check("the same six count once they are live again",
      count_of(sg.build(), "r8"), 6)

print()
print("=== excluded states are out of scope entirely ===")
group("exc", 6, "Fail", "Pass", r1="Fail", state="archived")
check("archived tickets surface nothing", count_of(sg.build(), "r1"), 0)
vault.set_raw_setting("qc_rules_json", '{"excluded_states": []}', "t")
qc_rules.invalidate()
check("the same six count once the state is in scope",
      count_of(sg.build(), "r1"), 6)
check("R1 has no parameters, so there is nothing to pre-fill",
      find(sg.build(), "r1")["target_rule_key"], None)
vault.set_raw_setting("qc_rules_json", '{"excluded_states": ["archived"]}', "t")
qc_rules.invalidate()
check("and drop out again when it is excluded", count_of(sg.build(), "r1"), 0)

print()
print("=== an A-check verdict is a prompt problem, not a rules one ===")
# The javascript: ticket is created first, so it has the lowest number and is
# certain to be in the sample.
graded("aqjs", "Fail", a3="Poor", link="javascript:alert(1)")
sign_off("aqjs", "Pass")
group("aq", 5, "Fail", "Pass", a3="Poor")
r = sg.build()
o = find(r, "a3")
check("A3 surfaces", count_of(r, "a3"), 6)
check("an AI judgement points at the guidance", o["kind"], "ai_overridden")
check("target is a_guidance", o["target_rule_key"], "a_guidance")
check("labelled for a human", o["check_label"], "Response quality")
check("a javascript: link is never emitted", "link" in o["sample"][0], False)
check("the sample is still the right ticket", o["sample"][0]["ticket_id"], "aqjs")
check("an https link is emitted",
      o["sample"][1]["link"].startswith("https://"), True)

print()
print("=== an AI Pass the humans failed blames the rubric, not a check ===")
group("miss", 6, "Pass", "Fail")
r = sg.build()
o = find(r, "overall", "Pass", "Fail")
check("attributed to the overall verdict", o and o["count"], 6)
check("kind", o["kind"], "ai_overridden")
check("target is a_guidance", o["target_rule_key"], "a_guidance")
print("   (no check objected, so there is nothing to blame but the rubric)")

print()
print("=== the window boundary is respected ===")
group("winin", 6, "Needs Review", "Pass", days_ago=29, a4="Needs Review")
group("winout", 6, "Needs Review", "Pass", days_ago=30, a4="Needs Review")
r30 = sg.build(30)
r31 = sg.build(31)
check("range is 30 days inclusive of today",
      (r30["range"]["start"], r30["range"]["end"]), (utc_date(29), utc_date(0)))
check("window_days echoed", r30["window_days"], 30)
check("the oldest day inside the window counts", count_of(r30, "a4"), 6)
check("one more day picks up the older six", count_of(r31, "a4"), 12)
check("a hedged verdict overridden to Pass is a disagreement",
      find(r30, "a4")["ai_said"], "Needs Review")

print()
print("=== one ticket with two failing rules counts toward both ===")
before = sg.build()
group("both", 6, "Fail", "Pass", r2="Fail", r4="Fail")
r = sg.build()
check("six tickets, six overrides",
      r["total_overrides"] - before["total_overrides"], 6)
check("R2 sees all six", count_of(r, "r2"), 6)
check("R4 sees the same six", count_of(r, "r4"), 6)
print("   (either rule could be the wrong one; suppressing one would hide it)")

print()
print("=== observations are ordered by evidence, and reconcile ===")
r = sg.build(365)
counts = [o["count"] for o in r["observations"]]
check("strongest evidence first", counts, sorted(counts, reverse=True))
check("every surfaced observation clears the gate",
      all(c >= sg.MIN_OVERRIDES for c in counts), True)
check("overrides never exceed reviews",
      r["total_overrides"] <= r["total_reviews"], True)

print()
print("=== read-only: building suggestions writes nothing ===")
snapshot = row_counts()
sg.build()
sg.build(1)
sg.build(365)
check("no rows written or removed", row_counts(), snapshot)

print()
if fails:
    print(f"FAILURES ({len(fails)}): {fails}")
    raise SystemExit(1)
print("ALL SUGGESTION ASSERTIONS PASSED")
