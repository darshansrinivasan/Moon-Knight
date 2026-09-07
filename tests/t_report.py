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
check("raw categories are reported exactly as tagged",
      dict(d["categories"])["General FAQ - How to questions"], 1)
check("legacy slugs stay verbatim beside curated names",
      dict(d["categories"])["general_question"], 1)
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
print("=== classification survives a label sync: logic on slugs, labels on display ===")
# A Pylon admin can reword any option label; the synced map then renames what
# every page SHOWS. That must never move a ticket between clusters or demand
# groups — 64 alert tickets once vanished from their cluster this way.
import funcheck
vault.set_raw_setting(funcheck.LABELS_SETTING, json.dumps({
    "category": {
        "alerts_automated_tray_sentry_slack": "Automated relays (Tray/Sentry)",
        "support_task_enable_disable_feature_flag": "Ops — feature toggles",
    }, "functionality": {}}), "t")
funcheck.invalidate_labels()
try:
    d2 = report.build_data(M)
    check("alert tickets stay in their demand group under the new wording",
          dict(d2["demand"])["Alerts relayed onward"], 2)
    check("console ops count is unmoved by the rename",
          d2["console_ops"], 2)
    check("clusters are unmoved by the rename",
          sorted(c["key"] for c in d2["clusters"]),
          ["access", "reauth", "toggles", "topfaq"])
    check("but the categories chart now shows the label",
          dict(d2["categories"])["Automated relays (Tray/Sentry)"], 2)
finally:
    vault.set_raw_setting(funcheck.LABELS_SETTING, "", "t")
    funcheck.invalidate_labels()

print()
print("=== generation stores the page; the model is optional ===")
real = report._call_gemini
model_calls = {"n": 0}


def fake(prompt, stats=None, overrides=None, system=None, schema=None, max_output=None):
    model_calls["n"] += 1
    if stats is not None:
        stats.models["gemini-test"] = stats.models.get("gemini-test", 0) + 1
    if schema is report.SUMMARY_SCHEMA:
        n = prompt.count("=== TICKET idx:")
        return json.dumps([{"idx": i, "summary": f"One-line summary {i}."}
                           for i in range(n)])
    if schema is report.TREND_SCHEMA:
        return json.dumps([
            {"section": "summary", "insight": "Volume moved month over month."},
            {"section": "functionalities", "insight": "Invite/remove rose."},
        ])
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
print("=== multi-month comparison: deterministic series + AI narrative ===")
# August seeds: r9 exists (general_question). Add movement to measure.
ticket("c1", "alerts_automated_tray_sentry_slack",
       title="G-Drive re-authentication needed", account="fuze",
       date="2026-08-10")
ticket("c2", "general_question", "invite_remove_users",
       title="Invite a user please", internal=False, account="beckett",
       date="2026-08-11")
ticket("c3", "oncall", "salesforce_sfdc", title="SFDC sync broken",
       account="deepl", date="2026-08-12")

cd = report.compare_data(["2026-09", "2026-08"])
check("months sorted", cd["months"], ["2026-08", "2026-09"])
kpi = {k["name"]: k for k in cd["kpis"]}
check("ticket totals per month", kpi["Tickets in scope"]["values"], [4, 7])
# Neither test month was day-fetched — that must be said, not footnoted.
check("all-backfill months warn about comparability",
      cd["coverage_warning"].startswith("None of these months was day-fetched"),
      True)
with db.get_conn() as c:
    for day in range(1, 11):
        c.execute("INSERT OR REPLACE INTO fetch_log (fetch_date,ticket_count,"
                  "fetched_at) VALUES (?,?,?)", (f"2026-08-{day:02d}", 1, T0))
    c.execute("INSERT OR REPLACE INTO fetch_log (fetch_date,ticket_count,"
              "fetched_at) VALUES ('2026-09-01',1,?)", (T0,))
cd = report.compare_data(["2026-09", "2026-08"])
kpi = {k["name"]: k for k in cd["kpis"]}
check("day-fetch coverage is the first KPI row",
      (cd["kpis"][0]["name"], kpi["Days day-fetched"]["values"]),
      ("Days day-fetched", [10, 1]))
check("uneven coverage names the sparse month",
      "2026-09 (1 day fetched)" in cd["coverage_warning"]
      and "not lower demand" in cd["coverage_warning"], True)
check("delta is last minus first", kpi["Tickets in scope"]["delta"], 3)
cats = {c["name"]: c for c in cd["categories"]}
check("comparison trends raw categories, both spellings verbatim",
      (cats["general_question"]["values"],
       cats["General FAQ - How to questions"]["values"]),
      ([2, 1], [0, 1]))
pat = {p["name"]: p for p in cd["patterns"]}
check("recurring patterns tracked across months",
      pat["Credential & re-authentication relays"]["values"], [1, 2])
funcs = {f["name"]: f for f in cd["functionalities"]}
check("functionality series carries both months",
      funcs["invite_remove_users"]["values"], [1, 1])
check("a functionality present only in the last month is 'appeared'",
      "feature_flags" in cd["appeared"], True)
check("one present only earlier is 'gone quiet' when it had volume",
      "salesforce_sfdc" in cd["vanished"], False)  # only 1 ticket — below the bar
try:
    report.compare_data(["2026-09"])
    check("one month is rejected", "returned", "ValueError")
except ValueError:
    check("one month is rejected", "ValueError", "ValueError")

report._call_gemini = fake
try:
    res3 = report.generate_compare(["2026-09", "2026-08"], "t@x.com")
finally:
    report._call_gemini = real
check("comparison stored under its key", res3["key"], "2026-08,2026-09")
cpage = report.get_html("2026-09,2026-08")   # any order resolves the same key
check("trend page titled and narrated",
      ("Product Signals Trends" in cpage
       and "Volume moved month over month." in cpage
       and "AI-written · gemini-test" in cpage), True)
check("the coverage warning is printed on the page",
      "Coverage warning" in cpage and "not lower demand" in cpage, True)
check("tables carry both month columns",
      "Aug 2026" in cpage and "Sep 2026" in cpage, True)
check("comparison listed alongside monthly reports",
      any(r["month"] == "2026-08,2026-09" for r in report.list_reports()), True)

report._call_gemini = boom
try:
    res4 = report.generate_compare(["2026-09", "2026-08"], "t@x.com")
finally:
    report._call_gemini = real
check("without the model, tables still generate",
      (res4["ai_narrative"], "Functionality trends" in report.get_html(res4["key"])),
      (False, True))

print()
print("=== chat: the model cites, the code counts ===")


chat_system = {}


def fake_chat(prompt, stats=None, overrides=None, system=None, schema=None, max_output=None):
    if stats is not None:
        stats.models["gemini-test"] = 1
    chat_system["text"] = system
    # One valid number, one duplicate, one hallucinated, one garbage.
    first = int(system.split("TICKETS:")[1].strip().split("|")[0])
    return json.dumps({"answer": "There are {count} matching tickets.",
                       "ticket_numbers": [first, first, 999999, "x"]})


report._call_gemini = fake_chat
try:
    out = report.chat(M, "how many are about invites?", [], "t@x.com")
finally:
    report._call_gemini = real

check("the context speaks the report's language — categories AND rollups",
      ("CATEGORY COUNTS" in chat_system["text"]
       and "General FAQ - How to questions: 1" in chat_system["text"]
       and "GROUP ROLLUPS" in chat_system["text"]
       and "|Questions & how-to (FAQ-shaped)|" in chat_system["text"]), True)
check("count is the VERIFIED match count, not the model's list length",
      (out["count"], out["answer"]), (1, "There are 1 matching tickets."))
check("hallucinated and duplicate numbers dropped",
      [t["number"] for t in out["tickets"]], [out["tickets"][0]["number"]])
check("cost and model attached to the answer",
      (out["cost_usd"] >= 0, out["model"]), (True, "gemini-test"))
check("tokens broken out for the cost line",
      sorted(out["tokens"]), ["cached", "output", "prompt"])
with db.get_conn() as c:
    chat_run = c.execute("SELECT date, config_json FROM qc_runs"
                         " WHERE date LIKE 'chat:%'").fetchone()
check("every chat call is filed on the Runs ledger",
      chat_run["date"], f"chat:{M}")
check("the question is in the run record",
      "invites" in chat_run["config_json"], True)
check("the serialized context is cached in-process",
      M in report._chat_ctx_cache, True)
try:
    report.chat(M, "   ")
    check("an empty question is rejected", "returned", "ValueError")
except ValueError:
    check("an empty question is rejected", "ValueError", "ValueError")

# "Deep classify" questions: the model returns segments; every segment count
# and the total are computed from verified numbers, never from its prose.
rows_now = {r["ticket_id"] for r in report.evidence_rows(M)}
two = sorted(rows_now)[:2]


def fake_breakdown(prompt, stats=None, overrides=None, system=None, schema=None, max_output=None):
    if stats is not None:
        stats.models["gemini-test"] = 1
    return json.dumps({
        "answer": "The {count} tickets split into two segments.",
        "ticket_numbers": [],
        "breakdown": [
            {"label": "Access asks", "ticket_numbers": [two[0], two[0], 999999]},
            {"label": "Alerts", "ticket_numbers": [two[1]]},
            {"label": "Hallucinated only", "ticket_numbers": [424242]},
        ]})


report._call_gemini = fake_breakdown
try:
    bd = report.chat(M, "deep classify these", [], "t@x.com")
finally:
    report._call_gemini = real
check("segment counts are verified per label",
      [(s["label"], s["count"]) for s in bd["breakdown"]],
      [("Access asks", 1), ("Alerts", 1)])
check("the total is the union of everything cited",
      (bd["count"], bd["answer"]),
      (2, "The 2 tickets split into two segments."))
check("a segment with no real tickets is dropped entirely",
      any(s["label"] == "Hallucinated only" for s in bd["breakdown"]), False)

print()
print("=== report generation buys the missing days from Pylon first ===")
import asyncio as aio

import app as appmod

fetched_days = []


async def fake_fetch(target):
    fetched_days.append(target.isoformat())
    with db.get_conn() as c:
        c.execute("INSERT OR REPLACE INTO fetch_log (fetch_date,ticket_count,"
                  "fetched_at) VALUES (?,0,?)", (target.isoformat(), T0))
    return appmod.FetchResult(count=0)


real_fetch = appmod.fetch_and_store
appmod.fetch_and_store = fake_fetch
try:
    info = aio.run(appmod._ensure_month_fetched("2026-08", "t@x.com"))
    info2 = aio.run(appmod._ensure_month_fetched("2026-08", "t@x.com"))
    future = aio.run(appmod._ensure_month_fetched("2030-01", "t@x.com"))
finally:
    appmod.fetch_and_store = real_fetch

check("August: 10 days covered, the other 21 fetched",
      (info["days"], info["missing"], info["fetched"], info["failed"]),
      (31, 21, 21, []))
check("fetches hit exactly the missing days, in order",
      (fetched_days[0], fetched_days[-1], len(fetched_days)),
      ("2026-08-11", "2026-08-31", 21))
check("a now-covered month fetches nothing",
      (info2["missing"], info2["fetched"]), (0, 0))
check("a future month has no days to fetch", future["days"], 0)
check("the dashboard sees the backfill — same fetch_log, same database",
      report._days_fetched("2026-08"), 31)

print()
if fails:
    print(f"FAILURES ({len(fails)}): {fails}")
    raise SystemExit(1)
print("ALL PRODUCT-REPORT ASSERTIONS PASSED")
