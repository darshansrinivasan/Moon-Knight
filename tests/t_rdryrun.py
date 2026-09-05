"""An R-check dry-run must write nothing and report only what the edit caused.

Two failure modes make this feature worse than not having it, and both are the
kind that look fine in a screenshot:

  * it writes. An admin exploring "what if we dropped the Rootly condition"
    would be regrading tickets their reviewers are reading. The negative
    assertions here take a full before/after snapshot of `rule_checks` and
    `ai_checks`, the way t_dryrun does for the AI grades.

  * it reports movement no rules change caused. R4 measures against
    `datetime.now`, so every old ticket fails it today whatever the SLA says;
    R5's deciding input is a live Slack fetch this must never make; and the
    tickets themselves have moved on since scoring, because QC is what makes
    agents go and fill in the fields it flagged. Each of those produces a
    confident diff that is pure noise, and an admin who acts on it is acting on
    nothing.

Nothing here is stubbed except the Slack call, which is stubbed with an
exception: the assertion is that it is never reached.
"""
import json
import sys

sys.path.insert(0, "..")

import db
import dryrun

import qc_runner
import rcheck_dryrun as rdry
import rules
import scorer
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


# ── fixtures ─────────────────────────────────────────────────────────────────

PASS_R = {"r1": "Pass", "r2": "Pass", "r3": "Pass", "r4": "Pass",
          "r5": "N/A", "r7": "N/A", "r8": "N/A", "r9": "N/A"}
PASS_A = {"a1": "Pass", "a2": "Neutral", "a3": "Good", "a4": "Pass", "a5": "Pass"}

# Filled so R1 and R2 pass on reconstruction as well as in the stored row —
# a fixture that drifts by accident would make every other assertion ambiguous.
FILLED_CF = {"functionalities": {"value": "CLM"},
             "request_category": {"value": "How to"}}

# rules.validate insists an internal-account entry looks like an account id,
# so the fixture account carries a real-shaped one.
ACCOUNT_ID = "3f2b91ac-1111-4c22-9d33-aaaabbbbcccc"

_next_number = [100]


def reset():
    with db.get_conn() as c:
        for table in ("ai_checks", "rule_checks", "messages", "tickets",
                      "accounts", "qc_runs"):
            c.execute(f"DELETE FROM {table}")
        c.execute("INSERT INTO accounts (id,name,type) VALUES "
                  f"('{ACCOUNT_ID}','Acme Ltd','customer')")
    rules.invalidate()


def add(tid, *, state="investigating", cf=None, ext=None, r=None, a=None,
        overall="Pass", messages=(), date="2026-06-01",
        scored_at="2026-06-01T10:00:00Z", account_id=ACCOUNT_ID, graded=True):
    """One stored ticket, complete with the R-verdicts a real fetch would leave."""
    _next_number[0] += 1
    number = _next_number[0]
    verdicts = {**PASS_R, **(r or {})}
    with db.get_conn() as c:
        c.execute(
            "INSERT INTO tickets (id,number,title,link,state,assignee_id,"
            "assignee_name,account_id,custom_fields,external_issues,body_html,"
            "fetch_date,fetched_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (tid, number, f"Ticket {number}",
             f"https://app.usepylon.com/issues?issueNumber={number}",
             state, "u1", "Ann", account_id,
             json.dumps(FILLED_CF if cf is None else cf),
             json.dumps(ext or []), "<p>body</p>", date, scored_at))
        c.execute(
            "INSERT INTO rule_checks (ticket_id,fetch_date,r1,r2,r3,r4,r5,r7,"
            "r8,r9,checked_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (tid, date, verdicts["r1"], verdicts["r2"], verdicts["r3"],
             verdicts["r4"], verdicts["r5"], verdicts["r7"], verdicts["r8"],
             verdicts["r9"], scored_at))
        for i, m in enumerate(messages):
            c.execute(
                "INSERT INTO messages (id,ticket_id,message_html,timestamp,"
                "author_name,is_customer,is_private) VALUES (?,?,?,?,?,?,?)",
                (f"{tid}-m{i}", tid, m["html"], m["at"],
                 "Cust" if m.get("customer") else "Ann",
                 1 if m.get("customer") else 0, 1 if m.get("private") else 0))
        if graded:
            grades = {**PASS_A, **(a or {})}
            c.execute(
                "INSERT INTO ai_checks (ticket_id,fetch_date,a1,a2,a3,a4,a5,"
                "ai_notes,overall_result,checked_at,qc_fingerprint) VALUES "
                "(?,?,?,?,?,?,?,?,?,?,?)",
                (tid, date, grades["a1"], grades["a2"], grades["a3"],
                 grades["a4"], grades["a5"], "notes", overall, scored_at, "fp"))
    return tid


def save_rules(doc):
    errs = rules.save(doc, "test")
    if errs:
        raise SystemExit(f"fixture rules would not save: {errs}")


def snapshot():
    with db.get_conn() as c:
        return {
            "rule_checks": [dict(r) for r in c.execute(
                "SELECT * FROM rule_checks ORDER BY ticket_id")],
            "ai_checks": [dict(r) for r in c.execute(
                "SELECT * FROM ai_checks ORDER BY ticket_id")],
            "runs": c.execute("SELECT COUNT(*) AS n FROM qc_runs").fetchone()["n"],
            "rules": vault.get_raw_setting(rules.RULES_KEY),
        }


def explode(url):
    raise AssertionError(f"a dry-run reached the live Slack API for {url}")


# ── the checks it re-runs are the ones the scorer produces ───────────────────

print("=== the re-run set matches what the scorer actually computes ===")
produced = set(scorer.score_all(
    {"state": "closed", "custom_fields": {}}, [], None, []))
check("every re-runnable check is re-run", set(rdry.RECHECKED_KEYS),
      produced - {"r9"})
ok("r9 is left alone", "r9" not in rdry.RECHECKED_KEYS,
   "it is hardcoded to N/A, so re-running it could only produce N/A")
ok("the overall still reads r9 from the stored row",
   "r9" in qc_runner.R_CHECK_KEYS)

print()
print("=== the sample size is bounded ===")
check("a huge limit is capped", rdry.clamp(10 ** 6), rdry.MAX_LIMIT)
check("zero becomes one", rdry.clamp(0), 1)
check("nonsense falls back to the default", rdry.clamp("lots"), rdry.DEFAULT_LIMIT)
check("None falls back to the default", rdry.clamp(None), rdry.DEFAULT_LIMIT)
ok("the cap covers a month of tickets", rdry.MAX_LIMIT >= 1000,
   f"MAX_LIMIT={rdry.MAX_LIMIT}")

print()
print("=== a partial draft does not reset the sections it omits ===")
merged = rdry.dryrun.draft_rules({"r4_sla_hours": 48})
ok("the merge is dryrun's, not a second copy of it",
   rdry.dryrun.draft_rules is dryrun.draft_rules)
check("the supplied key is applied", merged["r4_sla_hours"], 48)
ok("an omitted roster keeps its saved value",
   merged["eng_group_ids"] == rules.current()["eng_group_ids"])
ok("an unknown key is dropped", "nonsense" not in dryrun.draft_rules({"nonsense": 1}))

print()
print("=== the draft is scoped to this thread, not to the process ===")
# The fetch loop runs in another thread and *writes* what it scores. If the
# draft leaked out of the dry-run, an admin poking at a rules edit would be
# rewriting production verdicts from an unsaved document.
before_doc = rules.current()
with rdry._under_rules({**before_doc, "r4_sla_hours": 999}):
    check("inside the scope the draft answers", rules.sla_hours(), 999.0)
ok("outside the scope the saved rules answer", rules.current() == before_doc)
saved_fetch = rdry._live_fetch_slack_thread
rdry._live_fetch_slack_thread = lambda url: f"delegated:{url}"
check("outside a dry-run the Slack fetch still reaches Slack",
      scorer._fetch_slack_thread("u"), "delegated:u")
rdry._live_fetch_slack_thread = saved_fetch


# ── NOTHING IS WRITTEN ───────────────────────────────────────────────────────

print()
print("=== NOTHING IS WRITTEN ===")
reset()
add("t1", r={"r3": "Pass"})
add("t2", r={"r3": "Pass"})
rdry._live_fetch_slack_thread = explode
before = snapshot()
res = rdry.run({"r3_internal_account_ids": [f"{ACCOUNT_ID}  Acme Ltd"]})
after = snapshot()
ok("every stored R-verdict is untouched",
   after["rule_checks"] == before["rule_checks"],
   f"{len(after['rule_checks'])} rows compared column by column")
ok("every stored grade is untouched",
   after["ai_checks"] == before["ai_checks"],
   f"{len(after['ai_checks'])} rows compared column by column")
check("no run was recorded", after["runs"], before["runs"])
ok("no rules document was written", after["rules"] == before["rules"])
ok("the saved rules still answer afterwards",
   ACCOUNT_ID not in rules.internal_account_ids(),
   "the draft account id must not have leaked into the live configuration")
ok("but the dry-run did report the change", res["checks"]["r3"]["moved"] == 2,
   "writing nothing must not mean seeing nothing")

print()
print("=== a rules change is reported as movement, with the transition named ===")
check("both tickets moved", res["moved_tickets"], 2)
check("the transition is spelled out",
      res["checks"]["r3"]["transitions"], {"Pass → Fail": 2})
check("the verdict people act on moved too", res["overall"]["moved"], 2)
check("and it says which way", res["overall"]["transitions"], {"Pass → Fail": 2})
ok("the draft is flagged as a real change", res["rules_changed"] is True)
ok("the moved tickets are linkable",
   all(r["link"].startswith("https://app.usepylon.com/") for r in res["sample"]))
row = res["sample"][0]
check("the sample shows all three columns",
      sorted(row["checks"]["r3"]), ["draft", "reason", "saved", "stored"])
check("stored and saved agree — no drift here",
      (row["checks"]["r3"]["stored"], row["checks"]["r3"]["saved"]),
      ("Pass", "Pass"))
check("no AI call was made", res["ai_calls"], 0)
check("no Slack call was made", res["slack_calls"], 0)
check("and the bill is zero", res["cost_usd"], 0.0)

print()
print("=== an unchanged draft moves nothing ===")
res = rdry.run(None)
check("nothing moved", res["moved_tickets"], 0)
check("no per-check movement", res["checks"]["r3"]["moved"], 0)
check("no verdict movement", res["overall"]["moved"], 0)
ok("and it says the rules did not change", res["rules_changed"] is False)


# ── R4: the clock is pinned to when the ticket was scored ────────────────────

print()
print("=== R4 measures the ticket as it was when it was scored ===")
# 30h between the customer's last message and the moment scoring ran, on a day
# three months ago. Under the live clock that gap reads as ~90 days, so every
# SLA from 1h to 336h returns Fail and an SLA edit appears to move nothing.
reset()
add("slow", state="investigating", r={"r4": "Fail"}, overall="Fail",
    scored_at="2026-06-02T16:00:00Z",
    messages=[{"html": "<p>still broken</p>", "at": "2026-06-01T10:00:00Z",
               "customer": True}])
rdry._live_fetch_slack_thread = explode

res = rdry.run(None)
check("the stored Fail is reproduced under the saved 24h SLA",
      res["checks"]["r4"]["drift"], 0)
check("so nothing is reported as moved", res["checks"]["r4"]["moved"], 0)

res = rdry.run({"r4_sla_hours": 48})
check("a 48h SLA rescues the 30h gap", res["checks"]["r4"]["moved"], 1)
check("named as what it is",
      res["checks"]["r4"]["transitions"], {"Fail → Pass": 1})
ok("this is only visible because the clock is pinned", True,
   "against datetime.now the gap is ~90 days and no SLA change moves it")

res = rdry.run({"r4_sla_hours": 24})
check("re-stating the saved SLA moves nothing", res["checks"]["r4"]["moved"], 0)

# And the other direction: tightening below the real gap must fail it.
res = rdry.run({"r4_sla_hours": 8})
check("an 8h SLA fails a 30h gap", res["checks"]["r4"]["moved"], 0)
check("because it already failed", res["checks"]["r4"]["drift"], 0)

reset()
add("quick", state="investigating", r={"r4": "Pass"},
    scored_at="2026-06-01T20:00:00Z",
    messages=[{"html": "<p>hello?</p>", "at": "2026-06-01T10:00:00Z",
               "customer": True}])
res = rdry.run({"r4_sla_hours": 8})
check("a 10h gap passes at 24h and fails at 8h",
      res["checks"]["r4"]["transitions"], {"Pass → Fail": 1})
ok("the ticket is 3 months old and this still works", True,
   "the whole point of pinning the clock")

print()
print("=== a ticket with no scoring time is not guessed at ===")
reset()
add("nowhen", state="investigating", r={"r4": "Pass"}, scored_at=None,
    messages=[{"html": "<p>hi</p>", "at": "2026-06-01T10:00:00Z",
               "customer": True}])
res = rdry.run({"r4_sla_hours": 48})
check("R4 comes back Unknown", res["checks"]["r4"]["unknown"], 1)
check("not moved", res["checks"]["r4"]["moved"], 0)
ok("and the reason is stated",
   "no recorded scoring time" in (res["checks"]["r4"]["unknown_reason"] or ""),
   res["checks"]["r4"].get("unknown_reason"))
check("the verdict refuses to resolve too", res["overall"]["unknown"], 1)


# ── R5: the Slack thread is never fetched, and never guessed at ──────────────

print()
print("=== R5's Slack branch is reported, not fetched and not guessed ===")
reset()
add("csm", state="waiting_on_csm", r={"r5": "Pass"}, overall="Pass",
    cf={**FILLED_CF, "oncall_slack_chat_link": {
        "value": "https://slack.com/archives/C0123/p1782126385033799"}},
    messages=[{"html": "<p>can someone look at this</p>",
               "at": "2026-06-01T09:00:00Z"}])
rdry._live_fetch_slack_thread = explode
res = rdry.run(None)          # would raise if the live API were reached
check("R5 is counted as unknown", res["checks"]["r5"]["unknown"], 1)
check("it is not counted as moved", res["checks"]["r5"]["moved"], 0)
ok("and the reason names the thread",
   "Slack" in (res["checks"]["r5"]["unknown_reason"] or ""),
   res["checks"]["r5"].get("unknown_reason"))
check("the verdict refuses to resolve on it", res["overall"]["unknown"], 1)
ok("the resolved verdict is not a plausible Pass",
   res["overall"]["transitions"].get("Pass → Pass") is None)

print()
print("=== a Pass that never needed the thread is still a Pass ===")
reset()
add("tagged", state="waiting_on_csm", r={"r5": "Pass"},
    cf={**FILLED_CF, "oncall_slack_chat_link": {"value": "https://slack.com/x"}},
    messages=[{"html": '<p><span data-mention-type="user" '
                       'slackid="U02RTGHRYJK">Moiz</span> please look</p>',
               "at": "2026-06-01T09:00:00Z"}])
res = rdry.run(None)
check("no unknown", res["checks"]["r5"]["unknown"], 0)
check("no drift", res["checks"]["r5"]["drift"], 0)
ok("because the Slack branch can only turn a Fail into a Pass", True,
   "a Pass reached without it is certain")

print()
print("=== switching R5 off removes the blind spot from the verdict ===")
reset()
add("csm2", state="waiting_on_csm", r={"r5": "Fail"}, overall="Fail",
    cf={**FILLED_CF, "oncall_slack_chat_link": {"value": "https://slack.com/x"}},
    messages=[{"html": "<p>nobody tagged</p>", "at": "2026-06-01T09:00:00Z"}])
res = rdry.run({"disabled_checks": ["r5"]})
check("R5 is still reported unknown", res["checks"]["r5"]["unknown"], 1)
ok("but it is marked off", res["checks"]["r5"]["enabled"] is False)
check("so the verdict resolves", res["overall"]["unknown"], 0)
check("and it moves the way the mask says",
      res["overall"]["transitions"], {"Unknown → Pass": 1})
ok("the blind spot is named on both sides of that move", True,
   "with R5 live the verdict cannot be determined without Slack; with R5 off "
   "it can, and the transition says exactly that")


# ── the enabled-check mask is read under the DRAFT, not the saved rules ──────

print()
print("=== the mask is evaluated under the draft rules ===")
# `_compute_overall` reads `rules.enabled_rule_keys`, which reads the rules
# document. Evaluated against the saved document it would report no movement at
# all for a disable, which is the single most likely edit an admin makes.
reset()
add("failing", r={"r4": "Fail"}, overall="Fail",
    scored_at="2026-06-02T16:00:00Z",
    messages=[{"html": "<p>waiting</p>", "at": "2026-06-01T10:00:00Z",
               "customer": True}])
res = rdry.run({"disabled_checks": ["r4"]})
check("switching R4 off clears the stored Fail",
      res["applied"]["transitions"], {"Fail → Pass": 1})
check("counted once", res["applied"]["moved"], 1)
check("against the tickets that have a verdict", res["applied"]["comparable"], 1)
ok("R4 is reported as off", res["checks"]["r4"]["enabled"] is False)
ok("R1 is still on", res["checks"]["r1"]["enabled"] is True)

print()
print("=== and re-enabling one moves it back ===")
# The direction a value-mask shortcut cannot do: the saved document hides r4, so
# reading the mask from the saved rules would show nothing here.
save_rules({"disabled_checks": ["r4"]})
# resync_overall has already run for that save, so the stored verdict is Pass.
with db.get_conn() as c:
    c.execute("UPDATE ai_checks SET overall_result = 'Pass'")
res = rdry.run(None)
check("with R4 off there is nothing left to move", res["applied"]["moved"], 0)
res = rdry.run({"disabled_checks": []})
check("re-enabling it brings the stored Fail back",
      res["applied"]["transitions"], {"Pass → Fail": 1})
ok("nothing was written to get there",
   vault.get_raw_setting(rules.RULES_KEY) is not None
   and "r4" in json.loads(vault.get_raw_setting(rules.RULES_KEY))["disabled_checks"],
   "the saved document still has R4 switched off")
save_rules({"disabled_checks": []})

print()
print("=== `applied` is the only number that predicts today's dashboard ===")
# A rules save does not rewrite stored R-verdicts (SPEC_v5 D1). Only the mask,
# through resync_overall, changes anything retroactively — so the counterfactual
# and the applied number are reported separately and must not be the same field.
reset()
add("acct", r={"r3": "Pass"}, overall="Pass")
res = rdry.run({"r3_internal_account_ids": [f"{ACCOUNT_ID}  Acme Ltd"]})
check("the counterfactual sees the R3 change", res["overall"]["moved"], 1)
check("the applied number does not, because nothing rewrites R3",
      res["applied"]["moved"], 0)
res = rdry.run({"disabled_checks": ["r3"]})
check("a mask edit shows up in both", res["applied"]["moved"], 0,
      )  # stored r3 is Pass, so masking it changes no verdict


# ── drift is not movement ────────────────────────────────────────────────────

print()
print("=== a ticket that moved on since scoring is drift, not movement ===")
# QC is the reason agents go back and fill in `functionalities`, so a stored
# 'Fail' whose field is now filled is the common case, not the exotic one.
# Diffing a draft against the stored verdict alone would credit hundreds of
# these to a rules change that touched nothing.
reset()
add("fixed", r={"r1": "Fail"}, overall="Fail")
res = rdry.run(None)
check("the reconstruction disagrees with history", res["checks"]["r1"]["drift"], 1)
check("but nothing is reported as moved", res["checks"]["r1"]["moved"], 0)
check("and no ticket is listed as a mover", res["moved_tickets"], 0)
res = rdry.run({"r3_invalid_name_fragments": ["acme"]})
check("an unrelated edit still reports only its own effect",
      res["checks"]["r1"]["moved"], 0)
check("the edit itself is reported", res["checks"]["r3"]["moved"], 1)


# ── the case the spec was written for ────────────────────────────────────────

print()
print("=== dropping an R8 condition, which is what Asha asked for ===")
reset()
add("oncall", r={"r8": "Fail"}, overall="Fail",
    cf={**FILLED_CF,
        "resolution_category": {"value": "Escalated to Oncall"},
        "rootly.incident_reference": {"value": "ROOT-91"},
        "does_rootly_exist": {"value": "No"}},
    ext=[{"source": "jira", "link": "https://x.atlassian.net/browse/SPD-1"}])
res = rdry.run({"r8_conditions": ["rootly_ref", "jira_link"]})
check("dropping the dead field passes the ticket",
      res["checks"]["r8"]["transitions"], {"Fail → Pass": 1})
check("and the verdict follows", res["overall"]["transitions"], {"Fail → Pass": 1})


# ── loud on incoherent input ─────────────────────────────────────────────────

print()
print("=== incoherent input fails loudly rather than plausibly ===")
try:
    rdry.run({"r8_conditions": []})
    ok("an unsaveable draft is refused", False)
except ValueError as e:
    ok("an unsaveable draft is refused", "could not be saved" in str(e), str(e)[:80])

try:
    rdry.run({"r4_sla_hours": 10000})
    ok("an out-of-range SLA is refused", False)
except ValueError as e:
    ok("an out-of-range SLA is refused", "336" in str(e), str(e)[:80])

for bad in ({"start": "2026-06-01"}, {"end": "2026-06-01"},
            {"start": "2026-06-09", "end": "2026-06-01"}):
    try:
        rdry.run(None, **bad)
        ok(f"refused: {bad}", False)
    except ValueError as e:
        ok(f"refused: {bad}", True, str(e)[:60])

# A query that stops selecting a check would silently soften every verdict it
# computes, because _compute_overall reads a missing key as "not Fail".
thin = {k: "Pass" for k in qc_runner.R_CHECK_KEYS if k != "r9"}
try:
    rdry._stored_r_checks(thin)
    ok("an incomplete rule set is refused", False)
except KeyError as e:
    ok("an incomplete rule set is refused", "r9" in str(e), str(e)[:70])

print()
print("=== a ticket whose stored JSON is unreadable is named, not scored ===")
reset()
add("broken", r={"r1": "Pass"})
with db.get_conn() as c:
    c.execute("UPDATE tickets SET custom_fields = '{not json' WHERE id='broken'")
res = rdry.run(None)
check("it is not counted as scanned", res["scanned"], 0)
check("it is reported", len(res["errors"]), 1)
ok("with a reason", "custom_fields" in res["errors"][0]["reason"],
   res["errors"][0]["reason"])
check("and not as a movement", res["moved_tickets"], 0)


# ── the population and the sample ────────────────────────────────────────────

print()
print("=== only tickets with stored verdicts are scanned ===")
reset()
add("scored")
with db.get_conn() as c:
    c.execute("INSERT INTO tickets (id,number,state,account_id,custom_fields,"
              "fetch_date,fetched_at) VALUES ('unscored',9,'closed',?,'{}',"
              "'2026-06-01','2026-06-01T10:00:00Z')", (ACCOUNT_ID,))
res = rdry.run(None)
check("the ticket that was never R-scored is left out", res["scanned"], 1)

with db.get_conn() as c:
    c.execute("UPDATE tickets SET deleted_at='2026-07-01' WHERE id='scored'")
res = rdry.run(None)
check("a ticket deleted at source is left out", res["scanned"], 0)
ok("and it says there is nothing to compare",
   "nothing to compare" in res["note"], res["note"])

print()
print("=== the date range narrows the scan, and is stable across runs ===")
reset()
add("june", date="2026-06-01")
add("july", date="2026-07-01")
res = rdry.run(None, start="2026-06-01", end="2026-06-30")
check("only that range", res["scanned"], 1)
res = rdry.run(None)
check("without a range, everything with a verdict", res["scanned"], 2)

reset()
for i in range(4):
    add(f"s{i}", date="2026-06-01")
first = [r["id"] for r in rdry.run({"r3_invalid_name_fragments": ["acme"]},
                                   limit=2)["sample"]]
second = [r["id"] for r in rdry.run({"r3_invalid_name_fragments": ["acme"]},
                                    limit=2)["sample"]]
check("the same tickets both times", second, first)
check("the limit bounds the scan",
      rdry.run(None, limit=2)["scanned"], 2)

print()
print("=== the counts cover the scan even when the sample is cut ===")
reset()
for i in range(4):
    add(f"m{i}")
old_cap, rdry.MAX_SAMPLE = rdry.MAX_SAMPLE, 2
res = rdry.run({"r3_invalid_name_fragments": ["acme"]})
rdry.MAX_SAMPLE = old_cap
check("every mover is counted", res["moved_tickets"], 4)
check("only the cap is listed", len(res["sample"]), 2)
ok("and the payload says so", res["sample_truncated"] is True)

print()
print("=== an ungraded ticket has no verdict to move ===")
reset()
add("nograde", r={"r3": "Pass"}, graded=False)
res = rdry.run({"r3_invalid_name_fragments": ["acme"]})
check("the check itself still moves", res["checks"]["r3"]["moved"], 1)
check("but it is not counted against a stored verdict",
      res["applied"]["comparable"], 0)
check("nor is one invented", res["overall"]["comparable"], 0)

print()
if fails:
    print(f"FAILURES ({len(fails)}): {fails}")
    raise SystemExit(1)
print("ALL R-CHECK DRY-RUN ASSERTIONS PASSED")
