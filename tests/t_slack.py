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
if fails:
    print(f"FAILURES ({len(fails)}): {fails}")
    raise SystemExit(1)
print("ALL SLACK ASSERTIONS PASSED")
