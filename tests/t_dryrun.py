"""A dry-run must cost a bounded amount and change nothing.

The whole value of showing an admin what a rubric edit would do is that they can
trust it did not already do it. So the assertions that matter most here are the
negative ones: after a dry-run, no `ai_checks` row moved, no rules document was
written, and no run was recorded. If those ever fail, the feature is worse than
not having it — an admin exploring an edit would be silently regrading tickets
their reviewers are looking at.

The model is stubbed. This tests the sampling, the comparison, and the blast
radius, none of which involve Vertex; spending real money to assert that a diff
is a diff would be its own kind of bug.
"""
import json
import sys

sys.path.insert(0, "..")

import db
import dryrun
import prompts
import qc_runner
import rules
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

def seed(n=3, date="2026-08-26", graded=True):
    with db.get_conn() as c:
        c.execute("DELETE FROM ai_checks")
        c.execute("DELETE FROM rule_checks")
        c.execute("DELETE FROM messages")
        c.execute("DELETE FROM tickets")
        c.execute("INSERT OR REPLACE INTO accounts (id,name,type)"
                  " VALUES ('acc','Acme','customer')")
        for i in range(n):
            tid = f"t{i}"
            c.execute(
                "INSERT INTO tickets (id,number,title,link,state,assignee_name,"
                "account_id,custom_fields,source,customer_portal_visible,"
                "fetch_date,fetched_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,'now')",
                (tid, 100 + i, f"Ticket {i}",
                 f"https://app.usepylon.com/issues?issueNumber={100+i}",
                 "closed", "Ann", "acc", "{}", "email", 1, date),
            )
            c.execute("INSERT INTO rule_checks (ticket_id,r1,r2,r3,r4,r5,r7,r8,r9)"
                      " VALUES (?,'Pass','Pass','Pass','Pass','Pass','N/A','N/A','N/A')",
                      (tid,))
            c.execute("INSERT INTO messages (ticket_id,author_name,is_customer,"
                      "is_private,message_html,timestamp) VALUES (?,?,1,0,?,?)",
                      (tid, "Cust", "<p>please help</p>", "2026-08-26T09:00:00Z"))
            if graded:
                c.execute(
                    "INSERT INTO ai_checks (ticket_id,a1,a2,a3,a4,a5,ai_notes,"
                    "overall_result,checked_at,qc_fingerprint) VALUES "
                    "(?,'Pass','Neutral','Good','Pass','Pass','fine','Pass',"
                    "'2026-08-26T10:00:00Z','fp-old')", (tid,))


def stub(grades):
    """Replace the model with a fixed set of grades per ticket."""
    def _batch(batch, stats=None, overrides=None):
        _batch.overrides = overrides
        _batch.calls += 1
        out = []
        for i, _ in enumerate(batch):
            g = grades[i] if i < len(grades) else grades[-1]
            out.append(dict(g) if g else None)
        return out
    _batch.calls = 0
    _batch.overrides = None
    return _batch


PASS = {"a1": "Pass", "a2": "Neutral", "a3": "Good", "a4": "Pass",
        "a5": "Pass", "ai_notes": "fine"}
POOR = {**PASS, "a3": "Poor", "ai_notes": "A3 Poor: no next step — give one."}


def snapshot():
    with db.get_conn() as c:
        return {
            "ai": [dict(r) for r in c.execute(
                "SELECT ticket_id,a1,a2,a3,a4,a5,overall_result,qc_fingerprint,"
                "checked_at FROM ai_checks ORDER BY ticket_id")],
            "runs": c.execute("SELECT COUNT(*) AS n FROM qc_runs").fetchone()["n"],
            "rules": vault.get_raw_setting(rules.RULES_KEY),
        }


# ── the limit is a spend limit ───────────────────────────────────────────────

print("=== the sample size is bounded in both directions ===")
check("a huge limit is capped", dryrun.clamp(500), dryrun.MAX_LIMIT)
check("zero becomes one", dryrun.clamp(0), 1)
check("a negative becomes one", dryrun.clamp(-4), 1)
check("nonsense falls back to the default", dryrun.clamp("many"),
      dryrun.DEFAULT_LIMIT)
check("None falls back to the default", dryrun.clamp(None),
      dryrun.DEFAULT_LIMIT)
ok("the cap is small enough to be an experiment, not a run",
   dryrun.MAX_LIMIT <= 10, f"MAX_LIMIT={dryrun.MAX_LIMIT}")

print()
print("=== a partial draft does not reset the sections it omits ===")
merged = dryrun.draft_rules({"a_guidance": "only this"})
check("guidance applied", merged["a_guidance"], "only this")
check("an omitted rubric keeps its saved text",
      merged["a1_rubric"], prompts.DEFAULT_SECTIONS["a1_rubric"])
ok("an unknown key is dropped", "nonsense" not in dryrun.draft_rules({"nonsense": 1}))
ok("the merged draft assembles a valid prompt",
   prompts.check_header("a3") in prompts.system_prompt(merged))

print()
print("=== the draft reaches the model, and only as an override ===")
seed(2)
qc_runner._score_batch = stub([PASS, PASS])
dryrun.run({"a3_rubric": "HOUSE RULE"}, limit=2)
sent = qc_runner._score_batch.overrides
ok("the draft was passed to the scorer", sent is not None)
check("the draft section is in it", sent["a3_rubric"], "HOUSE RULE")
ok("the assembled prompt carries the draft",
   "HOUSE RULE" in qc_runner._system_prompt(sent))
ok("the saved rules were NOT changed",
   rules.current()["a3_rubric"] == prompts.DEFAULT_SECTIONS["a3_rubric"],
   "a dry-run is a question, not an edit")

print()
print("=== NOTHING IS WRITTEN ===")
seed(3)
before = snapshot()
qc_runner._score_batch = stub([POOR, POOR, POOR])
res = dryrun.run({"a3_rubric": "much stricter"}, limit=3)
after = snapshot()
check("every stored grade is untouched", after["ai"], before["ai"])
check("no run was recorded", after["runs"], before["runs"])
check("no rules document was written", after["rules"], before["rules"])
ok("but the dry-run did report the change",
   all("a3" in r["changed"] for r in res["results"]),
   "writing nothing must not mean seeing nothing")

print()
print("=== the comparison shows the verdict, not just the A-grades ===")
seed(1)
qc_runner._score_batch = stub([POOR])
r = dryrun.run(None, limit=1)["results"][0]
check("stored grade shown as current", r["current"]["a3"], "Good")
check("draft grade shown", r["draft"]["a3"], "Poor")
check("current verdict from the stored row", r["current"]["overall"], "Pass")
check("draft verdict recomputed", r["draft"]["overall"], "Fail")
ok("the verdict is listed as changed", "overall" in r["changed"],
   "a rubric edit that flips a verdict is the case that matters most")
ok("overall is compared", "overall" in dryrun.COMPARED)
ok("the note comes back for review", r["ai_notes"].startswith("A3 Poor"))
ok("the ticket is linkable", r["link"].startswith("https://app.usepylon.com/"))

print()
print("=== the draft verdict is computed from every R-check ===")
# The sample query missed r9 at first. A missing column reads as "not Fail", so
# the draft verdict came out kinder than the one the admin would actually get —
# a comparison that quietly under-reports is worse than no comparison.
seed(1)
qc_runner._score_batch = stub([PASS])
sampled = dryrun._sample(1)
for key in qc_runner.R_CHECK_KEYS:
    ok(f"_sample selects {key}", key in sampled[0],
       "absent columns silently soften the verdict")

# And if it ever stops selecting one, that must fail loudly rather than skew.
thin = {k: v for k, v in sampled[0].items() if k != "r9"}
try:
    dryrun._draft_grades(thin, PASS)
    ok("an incomplete rule set is refused", False)
except KeyError as e:
    ok("an incomplete rule set is refused", "r9" in str(e), str(e)[:70])

# A failing R-check must drag the draft verdict down even when every A-grade is
# clean, because that is what a real run would do.
with db.get_conn() as c:
    c.execute("UPDATE rule_checks SET r9 = 'Fail' WHERE ticket_id = 't0'")
r = dryrun.run(None, limit=1)["results"][0]
check("a failing R-check fails the draft verdict", r["draft"]["overall"], "Fail")
with db.get_conn() as c:
    c.execute("UPDATE rule_checks SET r9 = 'N/A' WHERE ticket_id = 't0'")

print()
print("=== an unchanged draft reports no change ===")
seed(1)
qc_runner._score_batch = stub([PASS])
r = dryrun.run(None, limit=1)["results"][0]
check("nothing moved", r["changed"], [])
check("both sides still shown", (r["current"]["a3"], r["draft"]["a3"]),
      ("Good", "Good"))

print()
print("=== a ticket the model failed on is reported, not dropped ===")
seed(2)
qc_runner._score_batch = stub([PASS, None])
res = dryrun.run(None, limit=2)
check("both tickets came back", res["sampled"], 2)
failed = [r for r in res["results"] if r.get("error")]
check("one is marked as failed", len(failed), 1)
check("it claims no changes", failed[0]["changed"], [])
ok("it still shows what is stored", failed[0]["current"]["a3"] == "Good")
ok("the reason is stated", "no grade" in failed[0]["error"])

print()
print("=== ungraded tickets are not sampled ===")
seed(3, graded=False)
qc_runner._score_batch = stub([PASS])
res = dryrun.run(None, limit=3)
check("nothing to compare against", res["sampled"], 0)
check("no model call was made", qc_runner._score_batch.calls, 0)
ok("and it says why", "No graded tickets" in res.get("note", ""),
   "padding the sample with tickets that have no baseline costs money "
   "and tells the admin nothing")

print()
print("=== the date filter narrows the sample ===")
seed(2, date="2026-08-26")
with db.get_conn() as c:
    c.execute("INSERT INTO tickets (id,number,title,state,account_id,"
              "custom_fields,fetch_date,fetched_at) VALUES "
              "('other',999,'Elsewhere','closed','acc','{}','2026-08-20','now')")
    c.execute("INSERT INTO ai_checks (ticket_id,a1,a2,a3,a4,a5,overall_result,"
              "checked_at) VALUES ('other','Pass','Neutral','Good','Pass',"
              "'Pass','Pass','2026-08-20T10:00:00Z')")
qc_runner._score_batch = stub([PASS, PASS, PASS])
res = dryrun.run(None, limit=10, date="2026-08-26")
check("only that date's tickets", sorted(r["id"] for r in res["results"]),
      ["t0", "t1"])
res = dryrun.run(None, limit=10)
ok("without a date, every graded ticket is in scope",
   "other" in {r["id"] for r in res["results"]})

print()
print("=== the sample is stable across repeated dry-runs ===")
seed(4)
qc_runner._score_batch = stub([PASS])
first = [r["id"] for r in dryrun.run(None, limit=2)["results"]]
second = [r["id"] for r in dryrun.run(None, limit=2)["results"]]
check("the same tickets both times", second, first)
ok("newest first", first == ["t3", "t2"],
   "an admin must be able to tell a rubric change from a sampling change")

print()
print("=== the cost is reported and labelled ===")
seed(1)
qc_runner._score_batch = stub([PASS])
res = dryrun.run(None, limit=1)
ok("a cost figure is present", isinstance(res["cost_usd"], float))
ok("its confidence is stated", isinstance(res["cost_estimated"], bool))
ok("the draft/no-draft distinction is reported",
   res["prompt_changed"] is False)
res = dryrun.run({"a_guidance": "be strict"}, limit=1)
ok("a real draft reports prompt_changed", res["prompt_changed"] is True)

print()
if fails:
    print(f"FAILURES ({len(fails)}): {fails}")
    raise SystemExit(1)
print("ALL DRY-RUN ASSERTIONS PASSED")
