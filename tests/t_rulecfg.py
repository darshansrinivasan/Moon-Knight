"""Switching a check off must remove it from everywhere, and cost nothing.

Disabling a check is a read-time mask, not a scoring change: stored verdicts are
never rewritten. That decision is what makes it free and reversible, and it is
load-bearing in a way worth spelling out. R-checks are computed exactly once, in
the fetch loop — `scorer.score_all` has no other caller and there is no
recompute path — so a design that wrote N/A at fetch time would have left every
stored Fail counting until someone refetched. And that refetch would have billed
real money: `qc_fingerprint` hashes r8 and r9, but the prompt prints only R1-R5
and R7, so an r8 verdict flipping moves the fingerprint without changing a byte
the model sees, pulling those tickets into the next paid run to produce
identical grades.

So the mask has to hold at every read instead. There are six of them, plus the
places that explain a verdict in prose, and a check left live in any single one
keeps failing tickets somewhere nobody thought to look. Each is asserted
separately below rather than through one aggregate, because that is exactly the
failure this suite exists to catch.
"""
import json
import sys

sys.path.insert(0, "..")

import db
import drilldown
import evidence
import leaderboard
import qc_runner
import resync_overall
import rules
import scorer
import slack
import vault

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


def set_rules(**kw):
    doc = {k: v for k, v in kw.items()}
    errs = rules.save(doc, "test@x")
    assert not errs, errs


def seed():
    """One ticket failing r8 only, graded Fail because of it."""
    with db.get_conn() as c:
        for tbl in ("ai_checks", "rule_checks", "messages", "tickets"):
            c.execute(f"DELETE FROM {tbl}")
        c.execute("INSERT OR REPLACE INTO accounts (id,name,type)"
                  " VALUES ('acc','Acme','customer')")
        c.execute(
            "INSERT INTO tickets (id,number,title,state,account_id,custom_fields,"
            "fetch_date,fetched_at) VALUES ('t1',1,'Oncall thing','closed','acc',"
            "?,'2026-09-01','now')",
            (json.dumps({"resolution_category": {"value": "Escalated to Oncall"}}),))
        c.execute("INSERT INTO rule_checks (ticket_id,fetch_date,r1,r2,r3,r4,r5,r7,r8,r9)"
                  " VALUES ('t1','2026-09-01','Pass','Pass','Pass','Pass','Pass',"
                  "'N/A','Fail','N/A')")
        c.execute("INSERT INTO ai_checks (ticket_id,fetch_date,a1,a2,a3,a4,a5,"
                  "ai_notes,overall_result,checked_at,qc_fingerprint) VALUES "
                  "('t1','2026-09-01','Pass','Neutral','Good','Pass','Pass',"
                  "'note','Fail','2026-09-01T10:00:00Z','fp-1')")


def _ticket_row():
    with db.get_conn() as c:
        row = c.execute("""
            SELECT t.*, a.name AS account_name, a.type AS account_type,
                   rc.r1, rc.r2, rc.r3, rc.r4, rc.r5, rc.r7, rc.r8, rc.r9
            FROM tickets t
            LEFT JOIN accounts a ON t.account_id = a.id
            LEFT JOIN rule_checks rc ON rc.ticket_id = t.id
            WHERE t.id = 't1'
        """).fetchone()
    return dict(row)


def _drill_error():
    try:
        drilldown._resolve_check("r8")
        return ""
    except ValueError as e:
        return str(e)


def _slack_rule_fails():
    return slack.build_summary("2026-09-01")["rule_fails"]


def _breakdown_keys():
    out = drilldown.assignee_breakdown("Unassigned", None, None)
    return [r["check"] if "check" in r else r.get("key") for r in out["rules"]]


def _r8_description():
    import app
    return app.live_rule_descriptions()["r8"][1]


def stored(col):
    with db.get_conn() as c:
        return c.execute(f"SELECT {col} FROM rule_checks WHERE ticket_id='t1'"
                         ).fetchone()[0]


def overall():
    with db.get_conn() as c:
        return c.execute("SELECT overall_result FROM ai_checks WHERE ticket_id='t1'"
                         ).fetchone()[0]


print("=== the toggle list matches the checks that actually run ===")
# r6 is never computed and r9 is hardcoded N/A. A toggle for either would be a
# control that does nothing, so neither may appear in the list.
produced = set(scorer.score_all(
    {"custom_fields": {}, "state": "new"}, [], None, []).keys())
ok("score_all produces no r6", "r6" not in produced)
check("r9 is always N/A", scorer.r9({}, []), "N/A")
for dead in scorer.DEAD_CHECKS:
    ok(f"{dead} is not toggleable", dead not in scorer.TOGGLEABLE_CHECKS)
check("toggleable == produced minus dead",
      set(scorer.TOGGLEABLE_CHECKS), produced - set(scorer.DEAD_CHECKS))

print()
print("=== validation ===")
ok("an unknown key is refused",
   rules.validate({"disabled_checks": ["r99"]}) != [])
ok("a dead check is refused as a toggle",
   rules.validate({"disabled_checks": ["r9"]}) != [])
check("a real check is accepted", rules.validate({"disabled_checks": ["r8"]}), [])
ok("an empty R8 condition set is refused",
   any("pass every oncall" in e
       for e in rules.validate({"r8_conditions": []})),
   "an R8 with no conditions passes every oncall ticket and looks like it works")
ok("an unknown R8 condition is refused",
   rules.validate({"r8_conditions": ["teleport"]}) != [])

print()
print("=== the mask reaches every consumer ===")
seed()
set_rules(disabled_checks=[], r8_conditions=list(scorer.R8_CONDITIONS))

before_fp = qc_runner.qc_fingerprint(
    {"number": 1, "state": "closed", "title": "x", "custom_fields": "{}"}, [],
    {k: "Fail" for k in ("r8",)})

check("with r8 on, the overall is Fail",
      qc_runner._compute_overall({"r8": "Fail"}, {}), "Fail")
ok("with r8 on, the calendar counts it", "rc.r8='Fail'" in db._rule_fail_or())
ok("with r8 on, the leaderboard reports it", "r8" in leaderboard._keys())
ok("with r8 on, it can be drilled into",
   drilldown._resolve_check("r8")[0] == "r8")

set_rules(disabled_checks=["r8"])

print("  --- r8 switched off ---")
check("the overall stops failing",
      qc_runner._compute_overall({"r8": "Fail"}, {}), "Pass")
ok("the calendar stops counting it", "rc.r8='Fail'" not in db._rule_fail_or())
ok("the leaderboard drops the row", "r8" not in leaderboard._keys())
ok("the drill-down refuses it with a reason",
   "switched off" in _drill_error())
ok("the Slack report drops it", "r8" not in _slack_rule_fails())
ok("the assignee breakdown drops it", "r8" not in _breakdown_keys())
ok("evidence says it is switched off",
   evidence.for_ticket(_ticket_row(), [])["r8"] == evidence.SWITCHED_OFF)
check("the notes stop advising on it",
      "R8" in qc_runner._r_check_notes({"r8": "Fail"}), False)

print()
print("=== but the stored verdict is untouched, so nothing is rebilled ===")
check("the stored r8 verdict survives", stored("r8"), "Fail")
after_fp = qc_runner.qc_fingerprint(
    {"number": 1, "state": "closed", "title": "x", "custom_fields": "{}"}, [],
    {k: "Fail" for k in ("r8",)})
check("the ticket fingerprint does not move", after_fp, before_fp)
ok("so no ticket is marked stale for rescoring", after_fp == before_fp,
   "a moved fingerprint would rebill an identical prompt")

print()
print("=== a save takes effect now, without a refetch ===")
# resync_overall recomputes stored overalls from stored verdicts, no AI calls.
res = resync_overall.run()
check("the stored overall is corrected", overall(), "Pass")
ok("and the resync said so", res["overall_updated"] >= 1, str(res["changes"]))
check("the stored verdict is still untouched", stored("r8"), "Fail")

print()
print("=== re-enabling restores the old grade exactly ===")
set_rules(disabled_checks=[])
resync_overall.run()
check("the overall is Fail again", overall(), "Fail")
check("because the verdict was never lost", stored("r8"), "Fail")
ok("evidence explains it again",
   evidence.for_ticket(_ticket_row(), [])["r8"] != evidence.SWITCHED_OFF)

print()
print("=== R8's conditions are individually optional ===")
ISSUE = {
    "custom_fields": {
        "resolution_category": {"value": "Escalated to Oncall"},
        "rootly.incident_reference": {"value": "ROOT-1"},
        "request_category": {"value": "oncall"},
        # does_rootly_exist deliberately absent — the field Asha reported
    },
    "state": "closed",
}
set_rules(r8_conditions=list(scorer.R8_CONDITIONS))
check("with all four required, the missing field fails it",
      scorer.r8(ISSUE, [], []), "Fail")
set_rules(r8_conditions=["rootly_ref", "oncall_category"])
check("dropping does_rootly_exist and Jira lets it pass",
      scorer.r8(ISSUE, [], []), "Pass")
ok("and the description says which conditions are live",
   "Rootly incident reference" in _r8_description()
   and "does_rootly_exist" not in _r8_description())
set_rules(r8_conditions=list(scorer.R8_CONDITIONS))

print()
print("=== the notes follow configuration, and they are persisted ===")
# _r_check_notes is a third statement of the rules and the only one written into
# ai_checks.ai_notes and read out in Slack, so a stale note is a note that tells
# somebody to fix a rule that is no longer live.
set_rules(r4_sla_hours=24)
ok("the R4 note quotes the configured SLA",
   ">24 hours" in qc_runner._r_check_notes({"r4": "Fail"}))
set_rules(r4_sla_hours=48)
ok("and follows it when it changes",
   ">48 hours" in qc_runner._r_check_notes({"r4": "Fail"}),
   "it used to say >24 hours whatever the setting was")
set_rules(r4_sla_hours=24)

print()
print("=== a check reads the field it is mapped to, not a literal ===")
# The slugs were literals in five files, so following a field Pylon renamed
# meant a deploy — and until that deploy the check fails every ticket, because
# an absent custom field reads exactly like an empty one. That is what happened
# to does_rootly_exist.
set_rules(**{f"field_{k}": v for k, v in scorer.DEFAULT_FIELD_MAP.items()})
check("R1 passes on the default slug",
      scorer.r1({"functionalities": {"value": "billing"}}), "Pass")
check("and fails when that field is empty",
      scorer.r1({"functionalities": {"value": ""}}), "Fail")

# Repoint R1 at a different Pylon field and it follows, with no code change.
set_rules(field_functionality="feature")
check("R1 now reads the remapped field",
      scorer.r1({"feature": {"value": "sdc"}}), "Pass")
check("and ignores the old one",
      scorer.r1({"functionalities": {"value": "billing"}}), "Fail")
ok("the map reports the change", rules.field("functionality") == "feature")
set_rules(field_functionality="functionalities")

# Blank means "use the shipped default", never "read a field called nothing" —
# which would fail every ticket silently.
set_rules(field_rootly_exists="")
check("a cleared mapping falls back to the default",
      rules.field("rootly_exists"), scorer.DEFAULT_FIELD_MAP["rootly_exists"])

ok("a bad slug is refused",
   rules.validate({"field_functionality": "Not A Slug!"}) != [])
check("a namespaced slug is accepted",
      rules.validate({"field_rootly_reference": "rootly.incident_reference"}), [])

# The fingerprint prints these two fields into the prompt, so a remap has to
# reach it or a repointed field would not mark grades stale.
tk = {"number": 1, "state": "new", "title": "x",
      "custom_fields": json.dumps({"functionalities": {"value": "a"},
                                   "feature": {"value": "b"}})}
base = qc_runner.qc_fingerprint(tk, [], {})
set_rules(field_functionality="feature")
ok("remapping a printed field moves the fingerprint",
   qc_runner.qc_fingerprint(tk, [], {}) != base,
   "the model sees a different value, so the grade may differ")
set_rules(field_functionality="functionalities")
check("and returns when it is put back",
      qc_runner.qc_fingerprint(tk, [], {}), base)

ok("every mapped field names the checks that read it",
   all(scorer.FIELD_USED_BY.get(n) for n in scorer.DEFAULT_FIELD_MAP),
   "the drift warning is only actionable if it says what breaks")

print()
print("=== one-step undo ===")
set_rules(disabled_checks=[])
set_rules(disabled_checks=["r8", "r7"])
check("the save applied", sorted(rules.disabled_checks()), ["r7", "r8"])
ok("undo is offered", rules.has_previous())
ok("undo restores", rules.restore_previous("test@x"))
check("the previous document is back", sorted(rules.disabled_checks()), [])
ok("and undo is itself reversible", rules.restore_previous("test@x"))
check("returning to the newer document",
      sorted(rules.disabled_checks()), ["r7", "r8"])
set_rules(disabled_checks=[])

print()
print("=== the first save is undoable too ===")
# The state before the first-ever save is "everything at its default", so undo
# has to restore that rather than refusing. Skipping it left the first save —
# often the one an admin most wants to take back — as the only one with no way
# out, which only showed up when the whole flow was exercised over HTTP.
with db.get_conn() as c:
    c.execute("DELETE FROM app_settings WHERE key IN (?, ?)",
              (rules.RULES_KEY, rules.PREV_KEY))
rules.invalidate()
ok("nothing stored, so nothing to undo yet", not rules.has_previous())
set_rules(disabled_checks=["r8"])
ok("after the very first save, undo is offered", rules.has_previous(),
   "the previous state is 'all defaults', not 'no state'")
ok("and it restores", rules.restore_previous("test@x"))
check("back to defaults", sorted(rules.disabled_checks()), [])

print()
print("=== descriptions never contradict the controls ===")
import app
set_rules(disabled_checks=["r8"])
d = app.live_rule_descriptions()
ok("a switched-off check says so first", d["r8"][1].startswith("Switched off"))
ok("R4 states the live SLA", "24 hours" in d["r4"][1])
set_rules(disabled_checks=[])
ok("and stops saying it once re-enabled",
   not app.live_rule_descriptions()["r8"][1].startswith("Switched off"))

print()
if fails:
    print(f"FAILURES ({len(fails)}): {fails}")
    raise SystemExit(1)
print("ALL RULE-CONFIG ASSERTIONS PASSED")
