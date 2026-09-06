"""Product Signals report: deterministic numbers, optional narrative, stored.

Pinned: the numbers never depend on the model; a model failure degrades to
factual lines instead of failing the generation; regeneration replaces the
stored month; the page contract (no recommendations) survives in the rendered
HTML's own framing text.
"""
import json

import db
import report
import vault

db.init_db()
fails = []
_num = [8000]
M, D = "2026-09", "2026-09-03"
T0 = "2026-09-03T08:00:00+00:00"


def check(name, got, want):
    ok = got == want
    print(f"  {'OK ' if ok else 'FAIL'} {name}: got {got!r}, want {want!r}")
    if not ok:
        fails.append(name)


def ticket(tid, cat, func="", title="Ticket", state="closed", internal=True,
           account="acc-1", date=D, msg="Please re-authenticate the drive"):
    _num[0] += 1
    cf = {}
    if cat:
        cf["request_category"] = {"value": cat}
    if func:
        cf["functionalities"] = {"value": func}
    with db.get_conn() as c:
        c.execute("INSERT OR IGNORE INTO accounts (id,name,fetched_at)"
                  " VALUES (?,?,?)", (account, account.title(), T0))
        c.execute(
            "INSERT OR REPLACE INTO tickets (id,number,fetch_date,title,link,"
            "state,source,customer_portal_visible,account_id,custom_fields,fetched_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (tid, _num[0], date, title, f"https://x/{tid}", state,
             "manual" if internal else "email", 0 if internal else 1,
             account, json.dumps(cf), T0))
        c.execute(
            "INSERT OR REPLACE INTO messages (id,ticket_id,message_html,"
            "timestamp,source,author_name,author_email,is_customer,is_private)"
            " VALUES (?,?,?,?,?,?,?,0,1)",
            (f"m-{tid}", tid, f"<p>{msg}</p>", T0, "email", "Sam", "s@x.com"))


vault.set_raw_setting("qc_rules_json", '{"excluded_states": []}', "t")

ticket("r1", "alerts_automated_tray_sentry_slack",
       title="G-Drive re-authentication required", account="fuze")
ticket("r2", "alerts_automated_tray_sentry_slack",
       title="SharePoint re-authenticate needed", account="fuze")
ticket("r3", "Support Task - Enable / Disable Feature Flag", "Others : Feature Flags",
       title="Enable AI module for WS 1", account="snap")
ticket("r4", "support_task_enable_disable_feature_flag", "feature_flags",
       title="Disable intake for Olipop", account="olipop")
ticket("r5", "General FAQ - How to questions", "Access Control : Invite/remove users",
       title="How do I invite a user", internal=False, account="beckett")
ticket("r6", "general_question", "invite_remove_users",
       title="Remove user access please", internal=False, account="zepto")
ticket("r7", "oncall", title="Backend error in prod", account="prod")
ticket("r8", "general_question", title="Archived noise", state="archived")
ticket("r9", "general_question", title="Other month", date="2026-08-02")

print("=== the numbers are computed, deterministic, archived-free ===")
d = report.build_data(M)
check("scope excludes archived and other months", d["total"], 7)
check("accounts counted", d["accounts"], 6)
check("internal share", d["internal_pct"], 71)      # 5 of 7
check("console ops counted across legacy and new category names",
      d["console_ops"], 2)
check("demand grouping unifies slug and curated spellings",
      dict(d["demand"])["Console ops done for customers"], 2)
check("FAQ theme found",
      (d["themes"][0]["name"], d["themes"][0]["n"]),
      ("User & access management", 2))
clusters = {c["key"]: c for c in d["clusters"]}
check("clusters present", sorted(clusters), ["access", "reauth", "toggles", "topfaq"])
check("reauth cluster counts", (clusters["reauth"]["tickets"],
                                clusters["reauth"]["accounts"]), (2, 1))
check("evidence excerpts carried",
      clusters["reauth"]["excerpts"][0]["text"].startswith("Please re-auth"), True)
check("identical output on a second build",
      report.build_data(M) == d, True)

print()
print("=== generation stores the page; the model is optional ===")
real = report._call_gemini
model_calls = {"n": 0}


def fake(prompt, stats=None, overrides=None, system=None, schema=None):
    model_calls["n"] += 1
    if stats is not None:
        stats.models["gemini-test"] = stats.models.get("gemini-test", 0) + 1
    if schema is report.SUMMARY_SCHEMA:
        n = prompt.count("=== TICKET idx:")
        return json.dumps([{"idx": i, "summary": f"One-line summary {i}."}
                           for i in range(n)])
    if schema is report.SUGGEST_SCHEMA:
        return json.dumps([{"title": "Self-serve re-authentication",
                            "suggestion": "An idea entirely from the model.",
                            "evidence": "Credential relays — #8001"}])
    keys = [c["key"] for c in json.loads(prompt)]
    return json.dumps([{"key": k, "title": f"T-{k}",
                        "scenario": f"Scenario for {k}."} for k in keys])


report._call_gemini = fake
try:
    res = report.generate(M, "t@x.com")
finally:
    report._call_gemini = real
check("generated with narratives",
      (res["tickets"], res["cases"], res["ai_narrative"]), (7, 4, True))
check("every ticket got a one-line summary", res["summaries"], 7)
page = report.get_html(M)
check("page stored and titled",
      "Product Signals — September 2026" in page, True)
check("narrative used", "Scenario for reauth." in page, True)
check("the no-recommendations contract is printed on the page",
      "deliberately makes no feature recommendations" in page, True)
check("the evidence toolbar is on the page",
      f"/api/reports/{M}/evidence.csv" in page and "Save as PDF" in page, True)
check("AI suggestions rendered and attributed to the model",
      ("Self-serve re-authentication" in page
       and "Purely AI-generated · gemini-test" in page
       and "written entirely by <b>gemini-test</b>" in page), True)
check("suggestion count and model reported",
      (res["suggestions"], res["suggestion_model"]), (1, "gemini-test"))
check("print CSS forces colors (PDF export keeps them)",
      "print-color-adjust:exact" in page, True)
# No dashboard_base_url configured in tests, so links are app-relative —
# the same deep link the Open tab rows use.
check("trail ticket numbers deep-link to the dashboard sheet",
      'href="/?date=2026-09-03&amp;ticket=r1"' in page
      or 'href="/?date=2026-09-03&ticket=r1"' in page, True)
check("run cost filed under its label",
      db.qc_spend_for_date(f"report:{M}")["runs"], 1)

print()
print("=== the evidence CSV rows: Pylon's fields verbatim + the summary ===")
rows = {r["ticket_id"]: r for r in report.evidence_rows(M)}
check("one row per in-scope ticket", len(rows), 7)
r5 = [r for r in rows.values() if r["title"] == "How do I invite a user"][0]
check("tags carried verbatim",
      (r5["functionality"], r5["request_category"]),
      ("Access Control : Invite/remove users", "General FAQ - How to questions"))
check("summary attached", r5["ai_summary"].startswith("One-line summary"), True)
check("resolution fields present even when blank",
      (r5["resolution_details"], r5["resolution_category"]), ("", ""))

print()
print("=== summaries are fingerprinted — a regeneration re-bills nothing ===")
before = model_calls["n"]
report._call_gemini = fake
try:
    report.generate(M, "t@x.com")
finally:
    report._call_gemini = real
# Only the narrative and suggestion calls repeat (both are regenerated by
# design); no summary batches were re-billed for unchanged tickets.
check("no summary batches on unchanged content",
      model_calls["n"] - before, 2)

# Model failure degrades, never fails.
def boom(*a, **k):
    raise RuntimeError("vertex down")


report._call_gemini = boom
try:
    res2 = report.generate(M, "t2@x.com")
finally:
    report._call_gemini = real
check("regeneration without the model still succeeds",
      (res2["tickets"], res2["ai_narrative"]), (7, False))
page2 = report.get_html(M)
check("fallback is a factual line, not the old narrative",
      "Scenario for reauth." in page2, False)
check("without a model the opinion section is absent, not faked",
      (res2["suggestions"], "Purely AI-generated" in page2), (0, False))
check("regeneration replaced the stored row (one per month)",
      len([r for r in report.list_reports() if r["month"] == M]), 1)
check("listing carries the latest author",
      report.list_reports()[0]["generated_by"], "t2@x.com")
check("an unknown month reads as absent", report.get_html("2020-01"), None)
try:
    report.generate("2026-13")
    check("a bad month is rejected", "returned", "ValueError")
except ValueError:
    check("a bad month is rejected", "ValueError", "ValueError")

print()
if fails:
    print(f"FAILURES ({len(fails)}): {fails}")
    raise SystemExit(1)
print("ALL PRODUCT-REPORT ASSERTIONS PASSED")
