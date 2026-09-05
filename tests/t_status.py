"""The status policy must reproduce the hardcoded sets exactly.

Five overlapping constants — TERMINAL_STATES, WAITING_CUSTOMER, _R5_NA_STATES,
ENGG_STATES, _R5_GROUP_STATES — became one editable table. That is only safe if
adopting it changes no grade, and "the suite still passes" does not prove it:
the suite exercises a handful of statuses, and Pylon has thirteen.

So this checks every status Pylon actually reports against the original sets,
which are still in scorer as the reference. If the seed and the constants ever
disagree, the seed is wrong — the constants are what production has been
grading with.

The four attributes exist because two could not express what the code does.
`closed` owes no reply but is still fully scored, since A5 exists precisely to
judge closures. `investigating` is R5-N/A but R4 applies. `waiting_on_customer`
is exempt from both. `waiting_on_engg` additionally decides whether R7 applies.
Those are four independent facts about a status and each is asserted here.
"""
import sys

sys.path.insert(0, "..")

import db
import rules
import scorer

db.init_db()
fails = []


def check(name, got, want):
    ok_ = got == want
    print(f"  {'OK ' if ok_ else 'FAIL'} {name}: got {got!r}, want {want!r}")
    if not ok_:
        fails.append(name)


def ok(name, cond, detail=""):
    print(f"  {'OK ' if cond else 'FAIL'} {name}{(' — ' + detail) if detail else ''}")
    if not cond:
        fails.append(name)


# Every status Pylon's API reports, read from the live workspace on 2026-09-04.
PYLON_STATUSES = [
    "new", "waiting_on_you", "waiting_on_customer", "waiting_on_csm",
    "waiting_on_product", "waiting_on_engg", "waiting_on_legal",
    "investigating", "on_hold", "closed", "archived",
    "handled_by_ai_donot_use", "migration",
]

print("=== the policy covers every status Pylon reports ===")
for state in PYLON_STATUSES:
    ok(f"{state} has a row", state in scorer.DEFAULT_STATUS_POLICY)
ok("and migration is now a decision, not an accident",
   "migration" in scorer.DEFAULT_STATUS_POLICY,
   "it reached production unlisted and landed on N/A by luck")

print()
print("=== R4: the reply-owed flag reproduces TERMINAL_STATES | WAITING_CUSTOMER ===")
exempt_before = scorer.TERMINAL_STATES | scorer.WAITING_CUSTOMER
for state in PYLON_STATUSES:
    was_exempt = state in exempt_before
    now_exempt = not rules.status_policy(state).get("r4_reply_owed", True)
    check(f"{state} exempt from R4", now_exempt, was_exempt)

print()
print("=== R7: the engineering flag reproduces ENGG_STATES ===")
for state in PYLON_STATUSES:
    check(f"{state} is an engineering status",
          bool(rules.status_policy(state).get("r7_engineering")),
          state in scorer.ENGG_STATES)

print()
print("=== R5: the expectation reproduces the old branch for every status ===")


def old_r5_shape(state):
    """What the original code would have dispatched to, by state alone."""
    if state in scorer._R5_NA_STATES:
        return "none"
    if state in scorer.WAITING_CUSTOMER:
        return "customer_owns"
    if state == "new":
        return "support_reply"
    if state == "waiting_on_you":
        return "support_reply"
    if state == "waiting_on_csm":
        return "handoff:cs"
    if state == "waiting_on_product":
        return "handoff:pt"
    if state in scorer.ENGG_STATES:
        return "handoff:eng"
    if state in scorer._R5_GROUP_STATES:
        return "tags"
    return "none"          # unknown states were never penalised


for state in PYLON_STATUSES:
    check(f"{state} R5 expectation",
          rules.status_policy(state).get("r5"), old_r5_shape(state))

print()
print("=== the tag list for waiting_on_legal survived the move ===")
check("legal still expects @legal-ops",
      rules.status_policy("waiting_on_legal").get("tags"),
      scorer._R5_GROUP_STATES["waiting_on_legal"])

print()
print("=== end to end: the same ticket grades the same in every status ===")
# Not just the dispatch — the actual verdicts, through the real functions.
BASE_CF = {"functionalities": {"value": "billing"},
           "request_category": {"value": "general_question"}}
MSGS = [{"author_name": "Cust", "is_customer": 1, "is_private": 0,
         "message_html": "<p>please help</p>",
         "timestamp": "2026-09-01T09:00:00Z"}]

for state in PYLON_STATUSES:
    issue = {"state": state, "custom_fields": BASE_CF, "assignee": {"id": "u1"},
             "number": 1, "created_at": "2026-09-01T09:00:00Z"}
    r5 = scorer.r5(issue, MSGS, [])
    r7 = scorer.r7(issue, MSGS, [])
    expected_r5_na = old_r5_shape(state) == "none"
    ok(f"{state}: R5 is N/A exactly when the old code said so",
       (r5 == "N/A") == expected_r5_na, f"r5={r5}")
    ok(f"{state}: R7 applies exactly when the old code said so",
       (r7 != "N/A") == (state in scorer.ENGG_STATES), f"r7={r7}")

print()
print("=== an edited row changes behaviour, and only that row ===")
policy = {k: dict(v) for k, v in scorer.DEFAULT_STATUS_POLICY.items()}
policy["investigating"]["r5"] = "support_reply"
errs = rules.save({"status_policy": policy}, "test@x")
check("the edit saves", errs, [])
issue = {"state": "investigating", "custom_fields": BASE_CF,
         "assignee": {"id": "u1"}, "number": 1}
check("investigating now expects a reply", scorer.r5(issue, MSGS, []), "Fail")
issue["state"] = "on_hold"
check("on_hold is untouched", scorer.r5(issue, MSGS, []), "N/A")

# Marking a status as engineering makes R7 apply to it — the change Anusree
# asked for, without a deploy.
policy["on_hold"]["r7_engineering"] = True
rules.save({"status_policy": policy}, "test@x")
check("on_hold is now an engineering status",
      scorer.r7({"state": "on_hold", "custom_fields": {}}, MSGS, []) != "N/A", True)

rules.save({"status_policy": {k: dict(v)
                              for k, v in scorer.DEFAULT_STATUS_POLICY.items()}},
           "test@x")
check("and restoring the seed restores the old behaviour",
      scorer.r7({"state": "on_hold", "custom_fields": {}}, MSGS, []), "N/A")

print()
print("=== validation refuses rows that would break a check ===")
ok("an unknown expectation is refused",
   rules.validate({"status_policy": {"new": {"r5": "teleport"}}}) != [])
ok("a tag expectation with no tags is refused",
   any("lists none" in e for e in
       rules.validate({"status_policy": {"new": {"r5": "tags", "tags": []}}})),
   "it would fail every ticket in that status and look like a broken check")
ok("a non-boolean flag is refused",
   rules.validate({"status_policy": {"new": {"in_scope": "yes"}}}) != [])

print()
if fails:
    print(f"FAILURES ({len(fails)}): {fails}")
    raise SystemExit(1)
print("ALL STATUS ASSERTIONS PASSED")
