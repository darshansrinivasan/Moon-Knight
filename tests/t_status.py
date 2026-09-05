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
print("=== what proves an engineering handoff is a choice ===")
# The Rootly/Jira fallback is the reason this is configurable: a reference shows
# a ticket exists somewhere, which is a materially weaker claim than an engineer
# having been told. Whether that satisfies R5 is a judgement about how the team
# works, not a fact about the data.
ENG = {"state": "waiting_on_engg", "custom_fields": {},
       "assignee": {"id": "u1"}, "number": 9}
# An external Jira issue linked on the ticket — the shape _has_rootly_or_jira
# actually recognises, rather than a bare key in message text.
JIRA_MSG = []
JIRA_EXT = [{"source": "jira", "link": "https://spotdraft.atlassian.net/browse/PROD-4821"}]

rules.save({"r5_eng_sources": list(scorer.R5_ENG_SOURCES)}, "test@x")
check("a Jira reference alone passes by default",
      scorer.r5(ENG, JIRA_MSG, JIRA_EXT), "Pass")

rules.save({"r5_eng_sources": ["pylon_thread"]}, "test@x")
check("with only a thread mention accepted, it fails",
      scorer.r5(ENG, JIRA_MSG, JIRA_EXT), "Fail")
calls = []
scorer.r5({"state": "waiting_on_engg", "assignee": {"id": "u1"},
           "custom_fields": {"oncall_slack_chat_link": {"value": "https://slack/x"}}},
          [], [], fetch_thread=lambda url: calls.append(url) or "")
check("with that source unticked the thread is never read", calls, [])

rules.save({"r5_eng_sources": list(scorer.R5_ENG_SOURCES)}, "test@x")
check("restoring the default restores the verdict",
      scorer.r5(ENG, JIRA_MSG, JIRA_EXT), "Pass")

ok("an empty source list is refused",
   any("whatever the team did" in e
       for e in rules.validate({"r5_eng_sources": []})),
   "no accepted evidence fails every engineering ticket regardless of behaviour")
ok("an unknown source is refused",
   rules.validate({"r5_eng_sources": ["telepathy"]}) != [])
ok("every source explains itself",
   all(len(scorer.R5_ENG_SOURCE_LABELS.get(s, "")) > 20
       for s in scorer.R5_ENG_SOURCES))

print()
print("=== a customised legacy r5_group_states survives the matrix ===")
# Before the matrix, r5_group_states was the one status behaviour an admin could
# change. The matrix seeds tags from the shipped constant, so without a fold a
# workspace that had customised it would lose that silently on deploy — the
# state reverting to the default tags and failing tickets at the next fetch,
# with no save, no audit row and nothing on screen to explain it.
import json as _json
import vault as _vault

with db.get_conn() as c:
    c.execute("DELETE FROM app_settings WHERE key IN (?, ?)",
              (rules.RULES_KEY, rules.PREV_KEY))
_vault.set_raw_setting(rules.RULES_KEY, _json.dumps({
    "r5_group_states": {"waiting_on_legal": ["@legal-eu", "@contracts"]}}), "legacy")
rules.invalidate()
check("the customised tags are carried over",
      rules.status_policy("waiting_on_legal")["tags"], ["@legal-eu", "@contracts"])
check("as a tags expectation",
      rules.status_policy("waiting_on_legal")["r5"], "tags")
check("and scoring uses them",
      scorer.r5({"state": "waiting_on_legal", "assignee": {"id": "u"},
                 "custom_fields": {}},
                [{"message_html": "<p>ping @legal-eu</p>", "is_customer": 0,
                  "is_private": 0, "timestamp": "2026-09-01T09:00:00Z"}], []),
      "Pass")
check("the default tag no longer passes on its own",
      scorer.r5({"state": "waiting_on_legal", "assignee": {"id": "u"},
                 "custom_fields": {}},
                [{"message_html": "<p>ping @legal-ops</p>", "is_customer": 0,
                  "is_private": 0, "timestamp": "2026-09-01T09:00:00Z"}], []),
      "Fail")

# An explicit matrix row is the newer statement of intent and must win.
_vault.set_raw_setting(rules.RULES_KEY, _json.dumps({
    "r5_group_states": {"waiting_on_legal": ["@legal-eu"]},
    "status_policy": {"waiting_on_legal": {"r5": "tags", "tags": ["@newer"]}}}),
    "legacy")
rules.invalidate()
check("an explicit row beats the legacy key",
      rules.status_policy("waiting_on_legal")["tags"], ["@newer"])

with db.get_conn() as c:
    c.execute("DELETE FROM app_settings WHERE key = ?", (rules.RULES_KEY,))
rules.invalidate()

print()
print("=== in_scope actually governs scope ===")
# It was rendered, collected, validated and stored — and read by nothing. A
# checkbox that looks like it does something is worse than no checkbox.
check("nothing excluded by default", rules.excluded_states(), [])
rules.save({"excluded_states": ["archived"]}, "test@x")
check("the Admin list still works", rules.excluded_states(), ["archived"])
pol = {k: dict(v) for k, v in scorer.DEFAULT_STATUS_POLICY.items()}
pol["handled_by_ai_donot_use"]["in_scope"] = False
rules.save({"status_policy": pol}, "test@x")
check("unticking a matrix row excludes it too",
      rules.excluded_states(), ["archived", "handled_by_ai_donot_use"])
check("and it reaches the SQL every count uses",
      rules.excluded_state_clause()[1],
      ["archived", "handled_by_ai_donot_use"])
rules.save({"excluded_states": [],
            "status_policy": {k: dict(v)
                              for k, v in scorer.DEFAULT_STATUS_POLICY.items()}},
           "test@x")

print()
print("=== a partial save cannot wipe the statuses it omits ===")
# PUT {"status_policy": {"closed": {...}}} validates — each row is fine alone —
# and top-level merging would have replaced the whole map, dropping the other
# thirteen statuses to the lenient fallback: waiting_on_customer starts owing R4
# replies, waiting_on_engg stops requiring any handoff.
before_cust = rules.status_policy("waiting_on_customer")
before_engg = rules.status_policy("waiting_on_engg")
check("a one-status save is accepted",
      rules.save({"status_policy": {"closed": {"r5": "none"}}}, "test@x"), [])
check("waiting_on_customer is untouched",
      rules.status_policy("waiting_on_customer"), before_cust)
check("waiting_on_engg still requires a handoff",
      rules.status_policy("waiting_on_engg"), before_engg)
check("and the named status took the edit",
      rules.status_policy("closed")["r5"], "none")

print()
print("=== a status shipped in a later deploy still appears ===")
scorer.DEFAULT_STATUS_POLICY["waiting_on_finance"] = dict(scorer.STATUS_FALLBACK)
rules.invalidate()
try:
    ok("a newly shipped status shows up in the table",
       "waiting_on_finance" in rules.all_status_policies(),
       "reading only stored policy would freeze the list at the first save")
    check("while saved edits still win",
          rules.all_status_policies()["closed"]["r5"], "none")
finally:
    del scorer.DEFAULT_STATUS_POLICY["waiting_on_finance"]
    rules.invalidate()

print()
if fails:
    print(f"FAILURES ({len(fails)}): {fails}")
    raise SystemExit(1)
print("ALL STATUS ASSERTIONS PASSED")
