"""Evidence strings: the real value behind a stored verdict, and the honest
silence where the reason was never recorded.

The assertions pinned here are the ones that turn a helpful panel into a
liability if they regress: a string that contradicts the verdict in
`rule_checks`, a fabricated reason for a Pass that came from a Slack fetch, a
malformed custom_fields document reported as "the field is empty", or an
exception raised while a page renders.
"""
import json
from datetime import datetime, timedelta, timezone

import db
import evidence
import rules as qc_rules
import vault

db.init_db()

NOW = datetime.now(timezone.utc)
INTERNAL_ID = "7ade86a8-6b74-497a-a983-fcd15b785965"   # SpotDraft Internal
CS_ID = "U02RTGHRYJK"                                   # Mohammad Moiz
ENG_ID = "U07AMBE46N7"                                  # Tamoghno Bakshi
ONCALL_LINK = "https://spotdraft.slack.com/archives/C01/p1782126385033799"

fails = []


def check(name, got, want):
    ok = got == want
    print(f"  {'OK ' if ok else 'FAIL'} {name}: got {got!r}, want {want!r}")
    if not ok:
        fails.append(name)


def val(value):
    """A Pylon custom field as it is stored on the ticket."""
    return {"value": value, "interpreted_value": value}


def ticket(**over):
    """One `db.get_day_tickets()` row: custom_fields as a JSON *string*, the
    account joined in, and the stored r1..r9 verdicts."""
    row = {
        "id": "t1", "number": 101, "state": "closed",
        "assignee_id": None, "assignee_name": None,
        "account_id": "acct-1", "account_name": "Acme Corp",
        "account_type": "customer",
        "custom_fields": {}, "external_issues": [], "body_html": "",
        "r1": "Pass", "r2": "Pass", "r3": "Pass", "r4": "Pass",
        "r5": "Pass", "r7": "Pass", "r8": "Pass",
    }
    row.update(over)
    for key in ("custom_fields", "external_issues"):
        if not isinstance(row[key], str):
            row[key] = json.dumps(row[key])
    return row


def msg(html, when, *, customer, private=False):
    """One `messages` row. The author payload scoring read is already flattened
    into is_customer by the time it reaches the DB."""
    return {
        "message_html": html,
        "timestamp": None if when is None else when.isoformat(),
        "author_name": "Someone",
        "is_customer": 1 if customer else 0,
        "is_private": 1 if private else 0,
    }


def cust(text, when, private=False):
    return msg(f"<p>{text}</p>", when, customer=True, private=private)


def supp(text, when, private=False):
    return msg(f"<p>{text}</p>", when, customer=False, private=private)


def mention(slack_id, kind="user"):
    return (f'<p>over to <span data-mention-type="{kind}" '
            f'slackid="{slack_id}">someone</span></p>')


def all_answered(label, ev):
    check(f"{label}: every check answered",
          tuple(sorted(ev)), tuple(sorted(evidence.CHECK_KEYS)))
    for key in evidence.CHECK_KEYS:
        useful = (bool(ev[key].strip())
                  and ev[key] != evidence.UNRECONSTRUCTABLE
                  and ev[key] != evidence.NOT_SCORED)
        check(f"{label}: {key} is useful", useful, True)


def agrees(label, ev):
    """The reconstruction reached the same verdict, so it explains it directly
    instead of falling back to "recorded as …"."""
    for key in evidence.CHECK_KEYS:
        check(f"{label}: {key} explains rather than defers",
              ev[key].startswith("recorded as ")
              or "not recorded at scoring time" in ev[key],
              False)


print("=== every check answers on an all-Pass ticket ===")
# waiting_on_engg with an engineer tagged, a complete oncall escalation, and
# support holding the last word: all seven checks pass for reconstructable
# reasons.
PASS_T = ticket(
    state="waiting_on_engg",
    assignee_name="Ann",
    custom_fields={
        "functionalities": val("Contract Lifecycle"),
        "request_category": val("oncall"),
        "resolution_category": val("Escalated to Oncall"),
        "does_rootly_exist": val("Yes"),
        "rootly.incident_reference": val("ROOT-1234"),
    },
)
PASS_MSGS = [
    cust("export is broken", NOW - timedelta(hours=9)),
    supp("raised SPD-123 with engineering", NOW - timedelta(hours=4)),
    msg(mention(ENG_ID), NOW - timedelta(hours=3), customer=False),
]
ev = evidence.for_ticket(PASS_T, PASS_MSGS)
all_answered("all-Pass", ev)
agrees("all-Pass", ev)
check("r1 quotes the field value", ev["r1"],
      "functionalities = 'Contract Lifecycle'")
check("r2 quotes the field value", ev["r2"], "request_category = 'oncall'")
check("r3 names the account", ev["r3"],
      "account 'Acme Corp' is an external customer account")
check("r4 names who spoke last", ev["r4"],
      "last public message was from support, 3h ago")
check("r5 names the engineer", "engineer Tamoghno Bakshi" in ev["r5"], True)
check("r7 names the rootly field", ev["r7"], "does_rootly_exist = 'Yes'")
check("r8 names the request category", "request_category 'oncall'" in ev["r8"],
      True)

print()
print("=== every check answers on an all-Fail ticket ===")
FAIL_T = ticket(
    state="waiting_on_engg",
    account_id=INTERNAL_ID, account_name="SpotDraft Internal",
    custom_fields={"resolution_category": val("Escalated to Oncall")},
    r1="Fail", r2="Fail", r3="Fail", r4="Fail", r5="Fail", r7="Fail", r8="Fail",
)
FAIL_MSGS = [cust("still broken", NOW - timedelta(hours=41))]
ev = evidence.for_ticket(FAIL_T, FAIL_MSGS)
all_answered("all-Fail", ev)
agrees("all-Fail", ev)
check("r1 says the field is empty", ev["r1"], "functionalities is empty")
check("r2 says the field is empty", ev["r2"], "request_category is empty")
check("r3 names the internal account", ev["r3"],
      "account 'SpotDraft Internal' matches the internal-account list")
check("r4 states the overdue wait", ev["r4"],
      "customer's last message has gone 41h unanswered against a 24h SLA")
check("r5 states the missing mention", ev["r5"],
      "state 'waiting_on_engg' and no engineer or eng group is @-mentioned "
      "and the ticket carries no Rootly or Jira reference")
check("r7 states the missing reference", ev["r7"],
      "no Rootly or Jira reference on the ticket or in the thread")
check("r8 lists what is missing", "no Jira link" in ev["r8"], True)

print()
print("=== every check answers on N/A verdicts ===")
NA_T = ticket(state="closed", r5="N/A", r7="N/A", r8="N/A",
              custom_fields={"functionalities": val("Billing"),
                             "request_category": val("bug")})
ev = evidence.for_ticket(NA_T, [supp("all done", NOW - timedelta(hours=2))])
all_answered("N/A state", ev)
agrees("N/A state", ev)
check("r5 N/A names the exempt state",
      ev["r5"], "state 'closed' is exempt from the ownership check, "
                "so the rule does not apply")
check("r7 N/A names the non-engineering state", ev["r7"],
      "state 'closed' is not an engineering state, so the rule does not apply")
check("r8 N/A says there is no oncall evidence", ev["r8"],
      "no oncall evidence on the ticket")

NA_R4 = ticket(state="waiting_on_you", r4="N/A", r5="Fail", r7="N/A", r8="N/A",
               custom_fields={"functionalities": val("Billing"),
                              "request_category": val("bug")})
ev = evidence.for_ticket(NA_R4, [])
all_answered("N/A r4", ev)
check("r4 N/A says there is nothing to measure", ev["r4"],
      "no dated public messages to measure against")
check("r5 Fail on waiting_on_you", ev["r5"],
      "state 'waiting_on_you' — support has not answered the customer yet")

print()
print("=== R3 names the account it judged, whatever the reason ===")
check("typed internal",
      evidence.for_ticket(ticket(account_type="internal", account_name="Acme",
                                 r3="Fail"), [])["r3"],
      "account 'Acme' is typed internal")
check("invalid name fragment",
      evidence.for_ticket(ticket(account_name="Live Chat", r3="Fail"),
                          [])["r3"],
      "account 'Live Chat' contains the invalid-name fragment 'live chat'")
check("no account at all",
      evidence.for_ticket(ticket(account_id=None, account_name=None,
                                 account_type=None, r3="Fail"), [])["r3"],
      "the ticket has no account")
check("account never fetched",
      evidence.for_ticket(ticket(account_name=None, account_type=None,
                                 r3="Fail"), [])["r3"],
      "no account record is stored for account id 'acct-1'")

print()
print("=== R4 reports the SLA that is configured now ===")
SLA_T = ticket(state="waiting_on_you", r4="Fail")
SLA_MSGS = [cust("still waiting", NOW - timedelta(hours=41))]
at_24 = evidence.for_ticket(SLA_T, SLA_MSGS)["r4"]
check("default SLA quoted", at_24,
      "customer's last message has gone 41h unanswered against a 24h SLA")

vault.set_raw_setting("qc_rules_json", json.dumps({"r4_sla_hours": 72}), "t")
qc_rules.invalidate()
at_72 = evidence.for_ticket(SLA_T, SLA_MSGS)["r4"]
check("SLA change changes the evidence", at_72 != at_24, True)
check("new SLA quoted", "72h" in at_72, True)
# 41h is inside a 72h SLA, so the reconstruction now disagrees with the stored
# Fail. It must report the stored verdict, not silently re-score the ticket.
check("stored Fail still owns the sentence", at_72.startswith("recorded as Fail"),
      True)
vault.set_raw_setting("qc_rules_json", json.dumps({}), "t")
qc_rules.invalidate()
check("SLA restored", qc_rules.sla_hours(), 24.0)

check("terminal state needs no reply",
      evidence.for_ticket(ticket(state="closed"), [])["r4"],
      "state 'closed' leaves no reply owed by support")
check("inside the SLA",
      evidence.for_ticket(ticket(state="waiting_on_you"),
                          [cust("hi", NOW - timedelta(hours=2))])["r4"],
      "customer's last message is 2h old, inside the 24h SLA")

print()
print("=== the stored verdict is never contradicted ===")
# The field was filled in after the ticket was scored.
stale = evidence.for_ticket(
    ticket(custom_fields={"functionalities": val("Billing")}, r1="Fail"), []
)["r1"]
check("stale Pass/Fail separated from the verdict", stale,
      "recorded as Fail at scoring time; the ticket now shows "
      "functionalities = 'Billing'")
check("a stored N/A on r1 is not restated",
      evidence.for_ticket(ticket(r1="N/A"), [])["r1"],
      "recorded as N/A at scoring time; the ticket now shows "
      "functionalities is empty")

# R5 on waiting_on_csm can pass on a Slack thread fetched at scoring time. That
# thread is not stored, so there is nothing to reconstruct and nothing to invent.
csm_link = ticket(state="waiting_on_csm",
                  custom_fields={"oncall_slack_chat_link": val(ONCALL_LINK)})
unrecorded = evidence.for_ticket(csm_link, [supp("looking", NOW)])["r5"]
check("unrecoverable Pass admits it", unrecorded,
      "passed on evidence not recorded at scoring time: the deciding check "
      "was a fetch of the linked oncall Slack thread, whose contents are not "
      "stored")
check("no roster member is invented", "@-mentioned" in unrecorded, False)

# A Fail is different: the fetch found nothing either, so the absence is the
# whole story and can be stated.
csm_fail = dict(csm_link, r5="Fail")
check("Fail with a link is still explained",
      evidence.for_ticket(csm_fail, [supp("looking", NOW)])["r5"],
      "state 'waiting_on_csm' and no CS or Implementation member is "
      "@-mentioned in the thread, and the linked oncall Slack thread "
      "supplied none")
check("Fail without a link",
      evidence.for_ticket(ticket(state="waiting_on_csm", r5="Fail"),
                          [supp("looking", NOW)])["r5"],
      "state 'waiting_on_csm' and no CS or Implementation member is "
      "@-mentioned in the thread")
check("CS mention is named",
      evidence.for_ticket(ticket(state="waiting_on_csm"),
                          [msg(mention(CS_ID), NOW, customer=False)])["r5"],
      "state 'waiting_on_csm' and CS roster member Mohammad Moiz is "
      "@-mentioned in the thread")

print()
print("=== R7 / R8 name the reference they found ===")
check("rootly reference quoted",
      evidence.for_ticket(ticket(
          state="waiting_on_engg",
          custom_fields={"rootly.incident_reference": val("ROOT-1234")}), [])["r7"],
      "rootly.incident_reference = 'ROOT-1234'")
check("does_rootly_exist = No is the reason",
      evidence.for_ticket(ticket(
          state="waiting_on_engg", r7="Fail",
          custom_fields={"does_rootly_exist": val("No")}), [])["r7"],
      "does_rootly_exist = 'No'")
check("jira in the thread is quoted",
      evidence.for_ticket(ticket(state="waiting_on_engg"),
                          [supp("tracked in SPD-4321", NOW)])["r7"],
      "the thread references 'SPD-4321'")
check("r8 fires on a rootly reference alone",
      "rootly.incident_reference = 'ROOT-9'" in evidence.for_ticket(ticket(
          custom_fields={"rootly.incident_reference": val("ROOT-9")},
          r8="Fail"), [])["r8"],
      True)

print()
print("=== nothing raises, whatever the ticket looks like ===")
broken = ticket(custom_fields="{not json at all", r1="Pass", r2="Fail")
ev = evidence.for_ticket(broken, [])
check("malformed custom_fields is not read as empty",
      ev["r1"], "passed on evidence not recorded at scoring time: "
                "the ticket's stored custom fields could not be read")
check("malformed custom_fields does not assert a value",
      "is empty" in ev["r1"] or "=" in ev["r1"], False)
check("malformed custom_fields on a Fail",
      ev["r2"].startswith("failed on evidence not recorded"), True)

check("custom_fields holding the wrong JSON type",
      evidence.for_ticket(ticket(custom_fields="[1, 2, 3]"), [])["r1"],
      "passed on evidence not recorded at scoring time: "
      "the ticket's stored custom fields could not be read")

check("a field that is not an object",
      evidence.for_ticket(ticket(custom_fields={"functionalities": "plain"},
                                 r1="Fail"), [])["r1"],
      "functionalities is empty")

# A missing or unparseable timestamp must not be read as a dated message.
undated = evidence.for_ticket(
    ticket(state="waiting_on_you", r4="N/A"),
    [cust("no date on this one", None),
     msg("<p>garbage date</p>", None, customer=True)],
)
check("undated messages leave nothing to measure", undated["r4"],
      "no dated public messages to measure against")
bad_ts = ticket(state="waiting_on_you", r4="N/A")
messy = [{"message_html": "<p>hi</p>", "timestamp": "2026-13-45T99:99:99Z",
          "is_customer": 1, "is_private": 0}]
check("unparseable timestamp is skipped",
      evidence.for_ticket(bad_ts, messy)["r4"],
      "no dated public messages to measure against")

empty = evidence.for_ticket({}, None)
check("an empty ticket answers every check",
      tuple(sorted(empty)), tuple(sorted(evidence.CHECK_KEYS)))
check("an unscored ticket says so", set(empty.values()), {evidence.NOT_SCORED})

junk = evidence.for_ticket(
    ticket(external_issues="not json", body_html=None),
    ["not a message", None, {"message_html": None, "timestamp": None}],
)
check("junk messages and external_issues survive",
      all(v.strip() for v in junk.values()), True)

print()
print("=== long values are truncated, not dumped ===")
long_value = "Contract Lifecycle Management " * 10
shown = evidence.for_ticket(
    ticket(custom_fields={"functionalities": val(long_value)}), [])["r1"]
check("truncated with an ellipsis", shown.endswith("…'"), True)
check("truncated to the cap",
      len(shown) <= len("functionalities = ''") + evidence.MAX_VALUE_CHARS, True)

print()
if fails:
    print(f"FAILURES ({len(fails)}): {fails}")
    raise SystemExit(1)
print("ALL EVIDENCE ASSERTIONS PASSED")
