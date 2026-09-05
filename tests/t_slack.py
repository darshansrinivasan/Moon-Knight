"""@-mention resolution must never guess.

tickets.assignee_name is a display string; Slack needs an ID. A live directory
search for "Deepak" returns both "Aditya Deepak" and "Deepak Kayala", so fuzzy
matching would eventually @-mention the wrong colleague, in a shared channel,
about someone else's failed ticket. These tests pin that it does not.
"""
import asyncio
import json

import db
import slack
import vault

db.init_db()
fails = []


def check(name, got, want):
    ok = got == want
    print(f"  {'OK ' if ok else 'FAIL'} {name}: got {got!r}, want {want!r}")
    if not ok:
        fails.append(name)


def seed_directory(people):
    """Populate the module's directory cache directly, no Slack calls."""
    slack._dir_people = {p["id"]: p for p in people}
    slack._dir_users = {p["id"]: p["name"] for p in people}
    import time
    slack._dir_at = time.time()          # mark fresh so directory() won't refresh


def run(coro):
    return asyncio.run(coro)


# A real Slack token would be needed to refresh; seeding the cache avoids that,
# but directory() still checks a token exists, so provide one.
vault.set_credential("slack_bot_token", "xoxb-test", "test")

seed_directory([
    {"id": "U0AAAA", "name": "Deepak Kayala", "email": "kayala@spotdraft.com"},
    {"id": "U0BBBB", "name": "Aditya Deepak", "email": "aditya@spotdraft.com"},
    {"id": "U0CCCC", "name": "Ann Unique", "email": "ann@spotdraft.com"},
    {"id": "U0DDDD", "name": "Twin Name", "email": "twin1@spotdraft.com"},
    {"id": "U0EEEE", "name": "Twin Name", "email": "twin2@spotdraft.com"},
])

print("=== an exact, unique name resolves ===")
r = run(slack.resolve_assignee_ids(["Ann Unique"]))
check("Ann Unique", r.get("Ann Unique"), "U0CCCC")
check("mention rendered", slack.mention_or_name("Ann Unique", r), "<@U0CCCC>")

print()
print("=== case and surrounding whitespace do not matter ===")
r = run(slack.resolve_assignee_ids(["  ann unique  "]))
check("normalised match", r.get("  ann unique  "), "U0CCCC")

print()
print("=== an AMBIGUOUS name never mentions ===")
r = run(slack.resolve_assignee_ids(["Twin Name"]))
check("Twin Name unresolved", r.get("Twin Name"), None)
rendered = slack.mention_or_name("Twin Name", r)
check("rendered as plain text", rendered, "Twin Name")
check("no mention syntax", "<@" in rendered, False)
print("   (this is the case that would tag the wrong colleague)")

print()
print("=== a partial name never mentions, even though it is a substring ===")
r = run(slack.resolve_assignee_ids(["Deepak"]))
check("'Deepak' unresolved", r.get("Deepak"), None)
check("no mention", "<@" in slack.mention_or_name("Deepak", r), False)
print("   (two directory entries contain 'Deepak'; neither equals it)")

print()
print("=== an unknown name never mentions ===")
r = run(slack.resolve_assignee_ids(["Nobody At All"]))
check("unknown unresolved", r.get("Nobody At All"), None)

print()
print("=== the explicit map wins over the directory ===")
vault.set_raw_setting(slack.IDENTITY_MAP_KEY,
                      json.dumps({"Twin Name": "U0DDDD",
                                  "Ann Unique": "U0ZZZZ"}), "test")
r = run(slack.resolve_assignee_ids(["Twin Name", "Ann Unique"]))
check("ambiguous name now mapped", r.get("Twin Name"), "U0DDDD")
check("map overrides a directory match", r.get("Ann Unique"), "U0ZZZZ")

print()
print("=== a corrupt map is ignored, not fatal ===")
vault.set_raw_setting(slack.IDENTITY_MAP_KEY, "{not json", "test")
check("corrupt map -> empty", slack.identity_map(), {})
vault.set_raw_setting(slack.IDENTITY_MAP_KEY, json.dumps({"A": ""}), "test")
check("blank id dropped", slack.identity_map(), {})
vault.set_raw_setting(slack.IDENTITY_MAP_KEY, "{}", "test")

print()
print("=== 'Unassigned' is never mentioned ===")
r = run(slack.resolve_assignee_ids(["Unassigned"]))
check("Unassigned skipped", r, {})

print()
print("=== mention modes ===")
for value, want in [("off", "off"), ("leads", "leads"), ("all", "all"),
                    ("", "leads"), ("nonsense", "leads")]:
    vault.set_settings({"slack_mention_mode": value}, "test")
    check(f"mode {value!r}", slack.mention_mode(), want)

print()
print("=== section blocks: mentions only when a mode asks for them ===")
summary = {
    "date": "2026-08-26",
    "groups": [("Ann Unique", [
        {"number": 1, "title": "A ticket", "overall_result": "Fail",
         "ai_notes": "R3 Fail: bad account", "link": "https://x/1"},
    ])],
}
resolved = run(slack.resolve_assignee_ids(["Ann Unique"]))
with_mentions = str(slack._assignee_sections(summary, "https://q", resolved))
without = str(slack._assignee_sections(summary, "https://q", None))
check("mentions present when passed", "<@U0CCCC>" in with_mentions, True)
check("absent when not passed", "<@" in without, False)
check("plain name used instead", "Ann Unique" in without, True)

print()
print("=== ticket titles stay escaped in mention mode ===")
summary["groups"] = [("Ann Unique", [
    {"number": 2, "title": "<script>alert(1)</script> & <b>", "overall_result": "Fail",
     "ai_notes": "note", "link": "https://x/2"},
])]
blocks = str(slack._assignee_sections(summary, "https://q", resolved))
check("angle brackets escaped", "&lt;script&gt;" in blocks, True)
check("raw script tag absent", "<script>" in blocks, False)
check("ampersand escaped", "&amp;" in blocks, True)

print()
print("=== a failure alarm names the instance that raised it ===")
# Any deployment holding the same bot token posts to the same channel. An
# unconfigured second instance alarmed about a run that had actually succeeded
# in production, and the alarm carried nothing to tell the two apart — so the
# obvious reading was that production had broken, which it had not.
import asyncio

import vault

sent = {}


async def fake_post(method, payload):
    sent["method"] = method
    sent["payload"] = payload
    return {"ok": True}


real_post = slack._post
try:
    slack._post = fake_post
    vault.set_settings({"slack_channel": "#support-qc",
                        "dashboard_base_url": "https://qc-prod.example.app"},
                       "test")
    asyncio.run(slack.post_failure("2026-08-27", "No Google Cloud project configured"))
    body = str(sent["payload"])
    check("the date is named", "2026-08-27" in body, True)
    check("the cause is quoted", "No Google Cloud project" in body, True)
    check("the instance is named", "qc-prod.example.app" in body, True)
    check("labelled as provenance", "Reported by" in body, True)

    # With no base URL configured there is simply no line — never a broken one.
    vault.set_settings({"dashboard_base_url": ""}, "test", allow_clear=True)
    sent.clear()
    asyncio.run(slack.post_failure("2026-08-27", "boom"))
    check("no empty provenance line", "Reported by" in str(sent["payload"]), False)
    check("the alarm still goes out", sent["payload"]["channel"], "#support-qc")
finally:
    slack._post = real_post

print()
print("=== the daily report warns when a check's field has gone ===")
# A check whose field has been retired does not fail loudly: an absent custom
# field reads exactly like an empty one, so the check fails every ticket until
# someone notices the counts. That is how does_rootly_exist surfaced.
import pylon as _pylon
import rules as _rules
import scorer as _scorer

_real_fetch = _pylon.fetch_custom_fields


async def _fields_missing_rootly():
    return [{"slug": s, "label": s} for s in
            ("functionalities", "request_category", "oncall_slack_chat_link",
             "resolution_category", "rootly.incident_reference")]


async def _fields_all_present():
    return [{"slug": s, "label": s} for s in _rules.field_map().values()]


try:
    with db.get_conn() as c:
        c.execute("DELETE FROM app_settings WHERE key = ?", (slack._DRIFT_KEY,))

    _pylon.fetch_custom_fields = _fields_missing_rootly
    blocks = str(asyncio.run(slack._field_drift_blocks()))
    check("the missing field is named", "does_rootly_exist" in blocks, True)
    check("and the checks that read it", "R7, R8" in blocks, True)
    check("with what it means", "empty value on every ticket" in blocks, True)

    # Once, not every morning: a daily repeat trains people to ignore it.
    repeat = asyncio.run(slack._field_drift_blocks())
    check("the same drift is not re-reported", repeat, [])

    # A newly missing field is new information and must get through.
    async def _worse():
        return [{"slug": "request_category", "label": "x"}]
    _pylon.fetch_custom_fields = _worse
    more = str(asyncio.run(slack._field_drift_blocks()))
    check("a newly missing field still alarms", "functionalities" in more, True)

    # Recovery clears the memory, so a future regression alarms again.
    _pylon.fetch_custom_fields = _fields_all_present
    check("nothing to say once every field is back",
          asyncio.run(slack._field_drift_blocks()), [])
    _pylon.fetch_custom_fields = _worse
    check("and a later regression alarms again",
          "functionalities" in str(asyncio.run(slack._field_drift_blocks())), True)

    # Pylon being unreachable must never cost the report the team needs.
    async def _boom():
        raise RuntimeError("Pylon down")
    _pylon.fetch_custom_fields = _boom
    check("an unreachable Pylon is skipped, not fatal",
          asyncio.run(slack._field_drift_blocks()), [])
finally:
    _pylon.fetch_custom_fields = _real_fetch

print()
print("=== a non-deployed copy must not write into the workspace ===")
# A local copy holding the same bot token reaches the same channel as
# production. On 28 August that put a "No Google Cloud project configured"
# alarm into #support-qc nine minutes after production had scored the same date
# 44 of 44, with no matching row in production's database.
import os

import vault

saved_env = os.environ.pop("RAILWAY_PUBLIC_DOMAIN", None)
try:
    vault.set_settings({"allow_local_side_effects": "0"}, "test")
    check("this process is not the deployment", vault.is_deployment(), False)
    check("so it may not act outward", vault.may_act_outward(), False)
    check("and Slack agrees", slack.may_post(), False)

    async def _attempt(method):
        try:
            await slack._post(method, {"channel": "#x", "text": "hi"})
            return "posted"
        except slack.NotTheDeployment as e:
            return str(e)

    refusal = asyncio.run(_attempt("chat.postMessage"))
    check("a write is refused", refusal != "posted", True)
    check("the refusal explains why", "not the deployed instance" in refusal, True)
    check("and names the override", "allow_local_side_effects" in refusal, True)

    # Reads stay available — checking a token from a laptop is legitimate.
    check("auth.test stays available", "auth.test" not in slack._WRITE_METHODS, True)

    # Explicit opt-in, because someone testing delivery should know they did.
    vault.set_settings({"allow_local_side_effects": "1"}, "test")
    check("the override is respected", slack.may_post(), True)
    vault.set_settings({"allow_local_side_effects": "0"}, "test")

    # A deployment is recognised by the platform variable, nothing else.
    os.environ["RAILWAY_PUBLIC_DOMAIN"] = "qc-production-d634.up.railway.app"
    check("a deployment may post", slack.may_post(), True)
finally:
    os.environ.pop("RAILWAY_PUBLIC_DOMAIN", None)
    if saved_env is not None:
        os.environ["RAILWAY_PUBLIC_DOMAIN"] = saved_env

# The setting must not be grantable from a copied .env — that file is exactly
# how a laptop comes to believe it is production.
spec = next(s for s in vault.SETTING_SPECS
            if s["key"] == "allow_local_side_effects")
check("no legacy_env on the override", "legacy_env" not in spec, True)

print()
if fails:
    print(f"FAILURES ({len(fails)}): {fails}")
    raise SystemExit(1)
print("ALL SLACK ASSERTIONS PASSED")
