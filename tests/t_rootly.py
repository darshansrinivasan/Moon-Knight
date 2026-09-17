"""Rootly QC: the second platform's engine.

Pins the decisions that would silently corrupt incident grades: a terminal or
excluded status leaking into the open set, the Pylon link resolving to the
wrong ticket, a threshold reading the wrong severity bucket, an API payload
without role data being graded Fail instead of N/A, and the fingerprint gate
that keeps unchanged incidents from re-billing.
"""
import json
from datetime import datetime, timedelta, timezone

import db
import rootly
import rootlyqc
import vault

db.init_db()
fails = []


def check(name, got, want):
    ok = got == want
    print(f"  {'OK ' if ok else 'FAIL'} {name}: got {got!r}, want {want!r}")
    if not ok:
        fails.append(name)


NOW = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)


def hours_ago(h):
    return (NOW - timedelta(hours=h)).isoformat()


print("=== JSON:API normalization tolerates both severity shapes ===")
nested = rootly._normalize({
    "id": "i1", "attributes": {
        "sequential_id": 7, "title": "DB down", "status": "started",
        "severity": {"data": {"id": "s", "attributes":
                     {"slug": "sev1", "name": "SEV1 - Critical"}}},
        "jira_issue_key": "SPD-99", "slack_channel_id": "C123",
        "created_at": "2026-09-01T00:00:00Z", "updated_at": "2026-09-15T00:00:00Z",
    }})
check("nested severity flattens", (nested["severity"], nested["severity_name"]),
      ("sev1", "SEV1 - Critical"))
flat = rootly._normalize({
    "id": "i2", "attributes": {"severity": {"slug": "sev2", "name": "SEV2"}}})
check("flat severity flattens", flat["severity"], "sev2")
check("missing severity is None", rootly._normalize(
    {"id": "i3", "attributes": {}})["severity"], None)

# Creator and functionalities: the creator rides embedded in attributes; the
# functionality NAMES come only from the page's `included` block via the
# relationship ids — an id with no included entry must drop, not surface raw.
rich = rootly._normalize(
    {"id": "i4",
     "attributes": {"user": {"data": {"id": "9", "attributes":
                    {"full_name": "Mayuri Prakash"}}}},
     "relationships": {"functionalities": {"data":
                       [{"id": "f1"}, {"id": "f-unknown"}]}}},
    {"f1": "Contract Creation : Stamp paper"})
check("creator name flattens", rich["created_by_name"], "Mayuri Prakash")
check("functionality resolves via included, unknown ids drop",
      rich["functionality"], "Contract Creation : Stamp paper")
check("no relationships at all is None, not an empty string",
      rootly._normalize({"id": "i5", "attributes": {}})["functionality"], None)

print()
print("=== rules config: defaults, merge, junk tolerance ===")
vault.set_raw_setting("rootly_rules_json", "", "t")
cfg = rootlyqc.rules_config()
check("defaults fill", (cfg["cadence_hours"]["sev1"], cfg["stuck_hours"]), (4, 72))
vault.set_raw_setting("rootly_rules_json",
                      '{"cadence_hours": {"sev1": 2}, "disabled_checks":'
                      ' ["ir3", "bogus"]}', "t")
cfg = rootlyqc.rules_config()
check("partial cadence keeps the other buckets",
      (cfg["cadence_hours"]["sev1"], cfg["cadence_hours"]["default"]), (2, 24))
check("unknown check keys are dropped", cfg["disabled_checks"], ["ir3"])
vault.set_raw_setting("rootly_rules_json", "{not json", "t")
check("junk JSON falls back to defaults",
      rootlyqc.rules_config()["stuck_hours"], 72)
vault.set_raw_setting("rootly_rules_json", "", "t")

print()
print("=== IR checks: the verdicts that were designed, not implied ===")
base = {"id": "x", "status": "started", "severity": "sev1", "severity_name": "SEV1",
        "jira_key": "SPD-1", "pylon_ticket_number": 76001,
        "pylon_ticket_source": "jira", "summary": "Real summary",
        "started_at": hours_ago(10), "created_at": hours_ago(10),
        "updated_at": hours_ago(1),
        "raw_json": json.dumps({"incident_role_assignments":
                                {"data": [{"id": "r1"}]}})}
cfg = rootlyqc.rules_config()
v, why = rootlyqc.evaluate_incident(base, cfg, NOW)
check("healthy incident passes every rule",
      [v[k] for k in rootlyqc.RULE_KEYS],
      ["Pass"] * len(rootlyqc.RULE_KEYS))

v, _ = rootlyqc.evaluate_incident({**base, "severity": None}, cfg, NOW)
check("no severity fails ir1", v["ir1"], "Fail")

v, why = rootlyqc.evaluate_incident(
    {**base, "raw_json": "{}"}, cfg, NOW)
check("payload without role data reads N/A, never Fail", v["ir2"], "N/A")
v, _ = rootlyqc.evaluate_incident(
    {**base, "raw_json": json.dumps({"incident_role_assignments":
                                     {"data": []}})}, cfg, NOW)
check("exposed-but-empty roles fail ir2", v["ir2"], "Fail")

v, _ = rootlyqc.evaluate_incident({**base, "jira_key": None}, cfg, NOW)
check("no Jira fails ir3", v["ir3"], "Fail")
v, _ = rootlyqc.evaluate_incident(
    {**base, "pylon_ticket_number": None}, cfg, NOW)
check("no Pylon link fails ir4", v["ir4"], "Fail")

# Cadence keys on the severity bucket: 5h idle fails a sev1 (4h) but passes
# the default bucket (24h).
stale = {**base, "updated_at": hours_ago(5),
         "raw_json": json.dumps({"incident_role_assignments": {"data": [1]}})}
v, _ = rootlyqc.evaluate_incident(stale, cfg, NOW)
check("5h idle fails sev1 cadence", v["ir5"], "Fail")
v, _ = rootlyqc.evaluate_incident({**stale, "severity": "sev3"}, cfg, NOW)
check("same idle passes the default bucket", v["ir5"], "Pass")

# Cadence is an active-response expectation: any status but "started" reads
# N/A — a mitigated incident going quiet is fine (its lingering is IR6's
# finding), and 141 real mitigated incidents were double-punished before this.
v, _ = rootlyqc.evaluate_incident({**stale, "status": "mitigated"}, cfg, NOW)
check("mitigated incidents are exempt from cadence", v["ir5"], "N/A")
v, _ = rootlyqc.evaluate_incident({**stale, "status": "in_triage"}, cfg, NOW)
check("in_triage is exempt too", v["ir5"], "N/A")
v, _ = rootlyqc.evaluate_incident({**stale, "status": None}, cfg, NOW)
check("unknown status is exempt, not guessed at", v["ir5"], "N/A")

# slack_last_message_ts is fresher activity than updated_at and wins.
fresh_slack = {**stale, "raw_json": json.dumps(
    {"incident_role_assignments": {"data": [1]},
     "slack_last_message_ts": str((NOW - timedelta(hours=1)).timestamp())})}
v, _ = rootlyqc.evaluate_incident(fresh_slack, cfg, NOW)
check("a fresh Slack message satisfies cadence", v["ir5"], "Pass")

v, _ = rootlyqc.evaluate_incident(
    {**base, "started_at": hours_ago(100), "created_at": hours_ago(100)},
    cfg, NOW)
check("100h in one stage fails ir6 (limit 72h)", v["ir6"], "Fail")
v, _ = rootlyqc.evaluate_incident(
    {**base, "started_at": hours_ago(100), "created_at": hours_ago(100),
     "mitigated_at": hours_ago(3)}, cfg, NOW)
check("a recent stage move resets ir6", v["ir6"], "Pass")

v, _ = rootlyqc.evaluate_incident({**base, "summary": "  "}, cfg, NOW)
check("blank summary fails ir7", v["ir7"], "Fail")

vault.set_raw_setting("rootly_rules_json",
                      '{"disabled_checks": ["ir3"]}', "t")
v, _ = rootlyqc.evaluate_incident({**base, "jira_key": None},
                                  rootlyqc.rules_config(), NOW)
check("a disabled check is not evaluated at all", "ir3" in v, False)
vault.set_raw_setting("rootly_rules_json", "", "t")

print()
print("=== Pylon link resolution ===")
check("field: Pylon URL", rootlyqc._pylon_from_field(
    "https://app.usepylon.com/issues?issueNumber=76707"), 76707)
check("field: #number", rootlyqc._pylon_from_field("see #76123"), 76123)
check("field: bare number", rootlyqc._pylon_from_field("76123"), 76123)
check("field: no number", rootlyqc._pylon_from_field("n/a"), None)

with db.get_conn() as c:
    c.execute("INSERT OR REPLACE INTO tickets (id,number,fetch_date,title,"
              "external_issues,fetched_at) VALUES ('rt1',76500,'2026-09-01',"
              "'linked', ?, 'x')",
              (json.dumps([{"source": "jira", "external_id": "101521",
                            "link": "https://x.atlassian.net/browse/SPD-777"}]),))
check("jira join by browse key",
      rootlyqc._pylon_from_jira("SPD-777", None), 76500)
check("jira join by numeric id",
      rootlyqc._pylon_from_jira(None, "101521"), 76500)
check("no probe, no join", rootlyqc._pylon_from_jira(None, None), None)

print()
print("=== the open set excludes terminal and Admin-excluded statuses ===")
now = datetime.now(timezone.utc).isoformat()
with db.get_conn() as c:
    for iid, seq, status in [("inc1", 1, "started"), ("inc2", 2, "mitigated"),
                             ("inc3", 3, "resolved"), ("inc4", 4, "cancelled"),
                             ("inc5", 5, "in_triage")]:
        c.execute("INSERT OR REPLACE INTO incidents (id, sequential_id, title,"
                  " status, fetched_at) VALUES (?,?,?,?,?)",
                  (iid, seq, f"I{seq}", status, now))
ids = [i["id"] for i in rootlyqc.open_incidents()]
check("terminal statuses stay out", sorted(ids), ["inc1", "inc2", "inc5"])
vault.set_raw_setting("rootly_rules_json",
                      '{"excluded_statuses": ["in_triage"]}', "t")
ids = [i["id"] for i in rootlyqc.open_incidents()]
check("an Admin-excluded status drops out", sorted(ids), ["inc1", "inc2"])
vault.set_raw_setting("rootly_rules_json", "", "t")

print()
print("=== the linked Pylon ticket's status rides along ===")
# rt1 (#76500, seeded above) is the Jira-joined ticket; give it a state and
# link inc1 to it. inc2 keeps a number nobody has fetched.
with db.get_conn() as c:
    c.execute("UPDATE tickets SET state='waiting_on_customer',"
              " link='https://app.usepylon.com/issues?issueNumber=76500'"
              " WHERE id='rt1'")
    c.execute("UPDATE incidents SET pylon_ticket_number=76500 WHERE id='inc1'")
    c.execute("UPDATE incidents SET pylon_ticket_number=99999 WHERE id='inc2'")
by_id = {i["id"]: i for i in rootlyqc.open_incidents()}
check("linked + synced carries the ticket's state and link",
      (by_id["inc1"]["pylon_state"], bool(by_id["inc1"]["pylon_link"])),
      ("waiting_on_customer", True))
check("linked but never-fetched reads NULL, not an invented state",
      by_id["inc2"]["pylon_state"], None)
check("unlinked reads NULL too", by_id["inc5"]["pylon_state"], None)

print()
print("=== the linked ticket's functionality: raw slug in, label out ===")
# Tags discipline: the stored VALUE is the slug; only display translates.
check("raw slug extracted from custom_fields",
      rootlyqc._pylon_functionality_raw(
          json.dumps({"functionalities": {"value": "user_authentication_saml"}})),
      "user_authentication_saml")
check("junk custom_fields reads None",
      rootlyqc._pylon_functionality_raw("{not json"), None)
check("missing field reads None",
      rootlyqc._pylon_functionality_raw("{}"), None)

print()
print("=== overall grade: rules fail, AI flags, pending stays pending ===")
graded = {"rules_checked_at": now, "ir1": "Pass", "ir3": "Fail"}
check("any rule Fail is overall Fail", rootlyqc.overall(dict(graded)), "Fail")
check("AI Poor is Needs Review, not Fail",
      rootlyqc.overall({"rules_checked_at": now, "ir1": "Pass",
                        "ia1": "Poor"}), "Needs Review")
check("Inconsistent status is Needs Review",
      rootlyqc.overall({"rules_checked_at": now, "ia4": "Inconsistent"}),
      "Needs Review")
check("clean incident passes",
      rootlyqc.overall({"rules_checked_at": now, "ir1": "Pass",
                        "ia1": "Good"}), "Pass")
check("never rule-checked stays pending (None)",
      rootlyqc.overall({"ir1": "Pass"}), None)
vault.set_raw_setting("rootly_rules_json",
                      '{"disabled_checks": ["ir3"]}', "t")
check("a disabled check's Fail cannot flip the grade",
      rootlyqc.overall(dict(graded)), "Pass")
vault.set_raw_setting("rootly_rules_json", "", "t")

print()
print("=== the fingerprint gate ===")
inc = {"title": "t", "status": "started", "summary": "s", "severity": "sev1",
       "updated_at": "u", "jira_key": None, "pylon_ticket_number": None}
fp1 = rootlyqc._fingerprint(inc, "slack text")
check("same content, same fingerprint",
      rootlyqc._fingerprint(dict(inc), "slack text") == fp1, True)
check("a new Slack message changes it",
      rootlyqc._fingerprint(inc, "slack text + more") == fp1, False)
check("a status change changes it",
      rootlyqc._fingerprint({**inc, "status": "mitigated"}, "slack text") == fp1,
      False)

print()
print("=== Slack digest resolves EVERY mention form to a name ===")
# Raw ids leak straight into the coaching notes otherwise — "pending
# validation from S0B0X5BP3BK" reached a real 1:1 surface before this pin.
msgs = [{"text": "<@U1> check with <!subteam^S0AAA>", "user": "U2", "ts": "1757900000.0"},
        {"text": "waiting on <!subteam^S0BBB|@legal-team> and <@W7>",
         "user": "U1", "ts": "1757900001.0"},
        {"text": "<!here> shipped <https://ex.com/f|the fix> per <https://ex.com/d>",
         "user": "B9", "username": "Rootly", "ts": "1757900002.0"},
        {"text": "", "user": "U1", "ts": "1757900003.0"}]
digest = rootlyqc._slack_digest(
    msgs, {"U1": "Asha", "U2": "Gourav", "W7": "Sam"}, {"S0AAA": "@oncall"})
check("user mentions become names", "@Asha" in digest and "Gourav:" in digest, True)
check("usergroup ids resolve via the directory", "@oncall" in digest, True)
check("a mention's own label wins", "@legal-team" in digest, True)
check("enterprise W-ids resolve too", "@Sam" in digest, True)
check("no raw id survives", "S0AAA" in digest or "S0BBB" in digest
      or "<@" in digest or "W7>" in digest, False)
check("bot authors use their username, not their id",
      "Rootly: @here" in digest, True)
check("link markup unwraps to its label", "the fix" in digest
      and "|" not in digest.split("shipped")[1].split("per")[0], True)
check("empty messages are dropped", digest.strip().count("\n"), 2)

print()
print("=== scoped rerun: only the asked-for ids, forced past the gate ===")
# The Run-QC-on-filtered button exists to re-judge doubted grades: it must
# score exactly the filtered ids even when their content is unchanged, while
# an unscoped run keeps skipping unchanged incidents.
import asyncio as _aio2
import qc_runner as _qcr2

async def fake_gather(rows):
    return {r["id"]: {"text": "", "note": "t"} for r in rows}

def fake_score(todo, stats):
    fake_score.got = [inc["id"] for inc, _ in todo]
    return len(todo), 0, []

class _Stats:
    prompt_tokens = output_tokens = cached_tokens = thought_tokens = 0
    def model_summary(self): return "fake"
    def cost_usd(self): return 0.0
    def cost_is_estimated(self): return False

real_r = (rootlyqc._gather_slack, rootlyqc._score_incidents,
          _qcr2.RunStats, _qcr2.get_vertex_client)
rootlyqc._gather_slack = fake_gather
rootlyqc._score_incidents = fake_score
_qcr2.RunStats = _Stats
_qcr2.get_vertex_client = lambda: None
try:
    # Store AI rows whose fingerprints MATCH current content, so nothing is
    # "changed" and the gate alone decides.
    for inc in rootlyqc.open_incidents():
        with db.get_conn() as c:
            c.execute("INSERT OR REPLACE INTO incident_ai (incident_id,"
                      " fingerprint, checked_at) VALUES (?,?, 'x')",
                      (inc["id"], rootlyqc._fingerprint(inc, "")))
    fake_score.got = None
    res = _aio2.run(rootlyqc.run_qc("t"))
    check("unscoped run skips every unchanged incident",
          (res["scored"], fake_score.got), (0, None))
    res = _aio2.run(rootlyqc.run_qc("t", only_ids=["inc1"], force=True))
    check("scoped+forced run scores exactly the asked-for id",
          fake_score.got, ["inc1"])
    check("and reports it as scored, not skipped",
          (res["scored"], res["skipped"]), (1, 0))
finally:
    (rootlyqc._gather_slack, rootlyqc._score_incidents,
     _qcr2.RunStats, _qcr2.get_vertex_client) = real_r

print()
if fails:
    print(f"FAILURES ({len(fails)}): {fails}")
    raise SystemExit(1)
print("ALL ROOTLY QC ASSERTIONS PASSED")
