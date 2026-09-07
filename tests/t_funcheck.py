"""Functionality-tagging review: judgement from the model, discipline from us.

Pinned here because each would corrupt the product analysis silently: a
hallucinated 'existing' option trusted as one, a suggestion that contradicts
itself, unchanged tickets re-billed on every run, or a vocabulary change
freezing old verdicts against a list that no longer exists.
"""
import json

import db
import funcheck
import vault

db.init_db()
fails = []
_num = [7000]

M = "2026-09"
D = "2026-09-03"
T0 = "2026-09-03T08:00:00+00:00"


def check(name, got, want):
    ok = got == want
    print(f"  {'OK ' if ok else 'FAIL'} {name}: got {got!r}, want {want!r}")
    if not ok:
        fails.append(name)


def ticket(tid, func, cat, date=D, title="Contract stuck", msg="It is stuck"):
    _num[0] += 1
    cf = {}
    if func is not None:
        cf["functionalities"] = {"value": func}
    if cat is not None:
        cf["request_category"] = {"value": cat}
    with db.get_conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO tickets (id,number,fetch_date,title,link,"
            "state,assignee_name,custom_fields,fetched_at)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (tid, _num[0], date, title,
             f"https://app.usepylon.com/issues/{tid}", "closed", "Ann",
             json.dumps(cf), T0))
        c.execute(
            "INSERT OR REPLACE INTO messages (id,ticket_id,message_html,"
            "timestamp,source,author_name,author_email,is_customer,is_private)"
            " VALUES (?,?,?,?,?,?,?,1,0)",
            (f"m-{tid}", tid, f"<p>{msg}</p>", T0, "email", "Cust", "c@x.com"))


# QC excludes 'spam' here — and the check below proves funcheck ignores that:
# its scope is fixed (everything but Archived), not the QC setting.
vault.set_raw_setting("qc_rules_json", '{"excluded_states": ["spam"]}', "t")
import rules as qc_rules
qc_rules.invalidate()

ticket("f1", "Workflows", "support_task", msg="Workflow will not publish")
ticket("f2", "HubSpot", "support_task", msg="Signature reminder emails never arrive")
ticket("f3", None, "general_question", msg="How do I download a contract?")
ticket("f4", "Reminders", "oncall", date="2026-08-15")   # another month
ticket("f6", "Workflows", "support_task", msg="Spam-ish but still a ticket")
ticket("f7", "Workflows", "support_task", msg="Archived noise")
with db.get_conn() as c:
    c.execute("UPDATE tickets SET state='spam' WHERE id='f6'")
    c.execute("UPDATE tickets SET state='Archived' WHERE id='f7'")

print("=== scope: everything except Archived, whatever QC excludes ===")
ids = sorted(t["id"] for t in funcheck._load_month(M))
check("archived is out, case-insensitively", "f7" in ids, False)
check("a QC-excluded state is still IN — this is not QC's scope",
      "f6" in ids, True)
check("month scope", ids, ["f1", "f2", "f3", "f6"])

print()
print("=== the vocabulary is the curated catalog, not what people selected ===")
vocab = funcheck.options()
check("catalog sizes", (len(vocab["functionality"]), len(vocab["category"])),
      (266, 26))
check("entries are full display strings",
      "Integrations : Salesforce (SFDC)" in vocab["functionality"], True)
check("categories too",
      "Support Task - Enable / Disable Feature Flag" in vocab["category"], True)
check("the duplicate catalog line was deduped",
      sum(1 for v in vocab["functionality"]
          if v == "Login : User authentication (SAML)"), 1)
check("observed-but-uncurated tags are NOT options",
      "Workflows" in vocab["functionality"], False)

print()
print("=== Admin can override either list; broken edits fall back loudly ===")
vault.set_raw_setting("funcheck_functionalities_json",
                      json.dumps(["Alpha : One", " Alpha : Two "]), "t")
v2 = funcheck.options()
check("a stored override replaces the shipped list, trimmed",
      v2["functionality"], ["Alpha : One", "Alpha : Two"])
check("the other list is untouched by it", len(v2["category"]), 26)
vault.set_raw_setting("funcheck_functionalities_json", "{broken", "t")
check("corrupt override degrades to the shipped list, not to empty",
      len(funcheck.options()["functionality"]), 266)
vault.set_raw_setting("funcheck_functionalities_json", "[]", "t")
check("an empty override is ignored too",
      len(funcheck.options()["functionality"]), 266)
vault.set_raw_setting("funcheck_functionalities_json", "", "t")
check("clearing the setting is the reset",
      funcheck.options() == vocab, True)

print()
print("=== validation: vocabulary membership is checked in code ===")
t2 = [t for t in funcheck._load_month(M) if t["id"] == "f2"][0]
row = funcheck._clean_result(
    {"functionality_ok": False, "category_ok": True,
     "note": "  Conversation is about reminder emails,   not HubSpot.  ",
     "suggested_functionality": "Workflow Manager : Signature Reminders",
     "suggested_category": ""},
    t2, vocab)
check("a catalog suggestion is not flagged new",
      (row["func_ok"], row["suggested_functionality"], row["func_suggestion_new"]),
      (0, "Workflow Manager : Signature Reminders", 0))
check("note is one collapsed line",
      row["note"], "Conversation is about reminder emails, not HubSpot.")
row = funcheck._clean_result(
    {"functionality_ok": False, "category_ok": False, "note": "n",
     "suggested_functionality": "Email Digest Engine",
     "suggested_category": "Support Task - Manual Task"},
    t2, vocab)
check("an invented option is flagged as a new-option proposal",
      (row["func_suggestion_new"], row["cat_suggestion_new"]), (1, 0))
row = funcheck._clean_result(
    {"functionality_ok": False, "category_ok": True, "note": "n",
     "suggested_functionality": "HubSpot", "suggested_category": ""},
    t2, vocab)
check("suggesting the tag itself collapses to ok",
      (row["func_ok"], row["suggested_functionality"], row["note"]),
      (1, "", ""))
long = funcheck._clean_result(
    {"functionality_ok": False, "category_ok": True, "note": "x" * 500,
     "suggested_functionality": "Reminders", "suggested_category": ""},
    t2, vocab)
check("note capped at one line's worth", len(long["note"]), 140)

print()
print("=== a run checks the month, stores verdicts, records its cost ===")
CANNED = {
    "f1": {"functionality_ok": True, "category_ok": True, "note": "",
           "suggested_functionality": "", "suggested_category": ""},
    "f2": {"functionality_ok": False, "category_ok": True,
           "note": "Thread is about reminder emails, not HubSpot.",
           "suggested_functionality": "Workflow Manager : Signature Reminders",
           "suggested_category": ""},
    "f3": {"functionality_ok": False, "category_ok": True,
           "note": "Download question; functionality is empty.",
           "suggested_functionality": "Download Center",
           "suggested_category": ""},
    "f6": {"functionality_ok": True, "category_ok": True, "note": "",
           "suggested_functionality": "", "suggested_category": ""},
}
calls = {"n": 0}


def fake_gemini(prompt, stats=None, overrides=None, system=None, schema=None):
    calls["n"] += 1
    out = []
    for i, tid in enumerate(tid for tid in ("f1", "f2", "f3", "f6")
                            if f"#{dict((t['id'], t['number']) for t in funcheck._load_month(M))[tid]}" in prompt):
        out.append({"idx": i, **CANNED[tid]})
    return json.dumps(out)


real_call, real_client = funcheck._call_gemini, funcheck.get_vertex_client
funcheck._call_gemini = fake_gemini
funcheck.get_vertex_client = lambda: None
try:
    res = funcheck.run(M, "t@x.com")
    check("all four checked", (res["checked"], res["status"]), (4, "success"))
    first_calls = calls["n"]

    res2 = funcheck.run(M, "t@x.com")
    check("second run is free — fingerprints held", res2["already_done"], True)
    check("and made no model calls", calls["n"], first_calls)

    # A changed conversation re-opens exactly that ticket. (A changed TAG can
    # re-open more: if the old value was the vocabulary's only holder, the
    # vocabulary itself changes — and that is by design, covered next.)
    with db.get_conn() as c:
        c.execute("UPDATE messages SET message_html='<p>now about billing</p>'"
                  " WHERE ticket_id='f1'")
    check("a changed conversation re-opens exactly that ticket",
          funcheck.preview(M)["eligible"], 1)
    # A new ticket adds only itself — the vocabulary is the curated catalog
    # now, so a never-before-seen tag no longer moves everyone's fingerprints.
    ticket("f5", "Brand New Option", "oncall")
    check("a new ticket re-opens only itself (plus the changed one)",
          funcheck.preview(M)["eligible"], 2)
    real_opts = funcheck.options
    funcheck.options = lambda: {
        **real_opts(),
        "functionality": real_opts()["functionality"] + ["Zed : New Thing"]}
    try:
        check("a CATALOG change re-opens every verdict",
              funcheck.preview(M)["eligible"], 5)
    finally:
        funcheck.options = real_opts
finally:
    funcheck._call_gemini = real_call
    funcheck.get_vertex_client = real_client

with db.get_conn() as c:
    row = c.execute("SELECT * FROM func_checks WHERE ticket_id='f2'").fetchone()
check("verdict stored with the suggestion",
      (row["func_ok"], row["suggested_functionality"], row["func_suggestion_new"]),
      (0, "Workflow Manager : Signature Reminders", 0))
with db.get_conn() as c:
    f3row = c.execute("SELECT func_suggestion_new, suggested_functionality"
                      " FROM func_checks WHERE ticket_id='f3'").fetchone()
check("f3's suggestion is flagged new",
      (f3row["suggested_functionality"], f3row["func_suggestion_new"]),
      ("Download Center", 1))
with db.get_conn() as c:
    run_row = c.execute("SELECT date, status, scored FROM qc_runs"
                        " WHERE date LIKE 'func:%' ORDER BY id LIMIT 1").fetchone()
check("run filed under its own label", run_row["date"], f"func:{M}")

print()
print("=== the page payload: issues first, month-scoped ===")
out = funcheck.results(M)
check("summary", {k: out["summary"][k] for k in ("checked", "ok", "issues")},
      {"checked": 4, "ok": 2, "issues": 2})
check("another month's ticket is absent",
      any(t["ticket_id"] == "f4" for t in out["tickets"]), False)
order = [t["ticket_id"] for t in out["tickets"]]
check("issues come before the clean and unchecked tail",
      set(order[:2]), {"f2", "f3"})
try:
    funcheck.results("2026-13")
    check("a bad month is rejected", "returned", "ValueError")
except ValueError:
    check("a bad month is rejected", "ValueError", "ValueError")

print()
print("=== Pylon stores values, people read labels — the map bridges them ===")
import asyncio as _aio

import pylon as _pylon

check("without a synced map, values pass through verbatim",
      funcheck.canon("category", "general_question"), "general_question")

FAKE_FIELDS = [
    {"slug": "functionalities", "select_metadata": {"options": [
        {"slug": "salesforce_sfdc", "label": "Integrations : Salesforce (SFDC)"},
        {"slug": "HubSpot", "label": "Integrations : HubSpot"},
    ]}},
    {"slug": "request_category", "select_metadata": {"options": [
        {"slug": "general_question", "label": "General FAQ - How to questions"},
        {"slug": "general_faq_misconfiguration",
         "label": "General FAQ - Misconfiguration"},
    ]}},
]


async def fake_fields():
    return FAKE_FIELDS


real_fields = _pylon.fetch_custom_fields
_pylon.fetch_custom_fields = fake_fields
try:
    counts = _aio.run(funcheck.sync_catalog_from_pylon())
finally:
    _pylon.fetch_custom_fields = real_fields

check("sync counts what Pylon offers", counts,
      {"functionality": 2, "category": 2})
check("the value now reads as its label",
      funcheck.canon("category", "general_question"),
      "General FAQ - How to questions")
check("an unknown value still passes through, never blanks",
      funcheck.canon("category", "retired_thing"), "retired_thing")
check("the catalog lists became Pylon's labels",
      funcheck.options()["category"],
      ["General FAQ - How to questions", "General FAQ - Misconfiguration"])
f2b = [t for t in funcheck._load_month(M) if t["id"] == "f2"][0]
check("tagged values on tickets read as labels everywhere",
      funcheck._tagged(f2b)[0], "Integrations : HubSpot")
# Restore for anything after: clear the synced state.
vault.set_raw_setting(funcheck.LABELS_SETTING, "", "t")
vault.set_raw_setting(funcheck.CATALOG_SETTINGS["functionality"], "", "t")
vault.set_raw_setting(funcheck.CATALOG_SETTINGS["category"], "", "t")

print()
if fails:
    print(f"FAILURES ({len(fails)}): {fails}")
    raise SystemExit(1)
print("ALL FUNCTIONALITY-CHECK ASSERTIONS PASSED")
