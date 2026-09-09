"""Internal/external channel classification: membership-based, one opinion."""
import asyncio
import json
from datetime import datetime, timezone

import channels
import db
import vault

db.init_db()
fails = []


def check(name, got, want):
    ok = got == want
    print(f"  {'OK ' if ok else 'FAIL'} {name}: got {got!r}, want {want!r}")
    if not ok:
        fails.append(name)


print("=== the setting parses and seeds ===")
ids = channels.internal_channel_ids()
check("the shipped default seeds ten channels", len(ids), 10)
check("first seeded channel", ids[0], "C03KBJNNN9X")
vault.set_raw_setting(channels.SETTING_KEY, "CAAA111\nCAAA111, CBBB222\n", "t")
check("parse dedupes and splits on lines and commas",
      channels.internal_channel_ids(), ["CAAA111", "CBBB222"])

print()
print("=== classification is derived: membership x current list ===")
with db.get_conn() as c:
    c.execute("INSERT OR REPLACE INTO tickets (id,number,fetch_date,title,state,"
              "source,customer_portal_visible,fetched_at) VALUES"
              " ('int1',1,'2026-09-01','T','closed','manual',1,'x')")
    c.execute("INSERT OR REPLACE INTO tickets (id,number,fetch_date,title,state,"
              "source,customer_portal_visible,fetched_at) VALUES"
              " ('ext1',2,'2026-09-01','T','closed','slack',1,'x')")
    c.execute("INSERT OR REPLACE INTO channel_index (ticket_id,channel_id,tagged_at)"
              " VALUES ('int1','CAAA111','x')")


def scoped(scope):
    sql, params = channels.channel_scope_clause(scope, "t")
    where = f" AND {sql}" if sql else ""
    with db.get_conn() as c:
        return {r["id"] for r in c.execute(
            f"SELECT id FROM tickets t WHERE 1=1{where}", params)}


check("all is a no-op", scoped("all"), {"int1", "ext1"})
check("external drops the tagged ticket; unknown defaults to external",
      scoped("external"), {"ext1"})
check("internal keeps only the tagged ticket", scoped("internal"), {"int1"})
check("internal_ticket_ids mirrors the same opinion",
      channels.internal_ticket_ids(), {"int1"})

# Source='manual' must not matter — #76226 taught us origin lives in the
# index, never in the ticket record.
check("a manual-source ticket classifies internal via membership alone",
      "int1" in scoped("internal"), True)

vault.set_raw_setting(channels.SETTING_KEY, "COTHER999", "t")
check("removing a channel reclassifies instantly, no data change",
      scoped("internal"), set())
check("empty membership under internal matches nothing, not everything",
      channels.channel_scope_clause("internal", "t")[0] != "", True)
vault.set_raw_setting(channels.SETTING_KEY, "CAAA111\nCBBB222", "t")

print()
print("=== the tagger mirrors Pylon's channel search, with early stop ===")
import pylon


async def fake_refs(search_filter, known_ids=None):
    fake_refs.calls.append((search_filter["value"], known_ids is None))
    return [{"id": "int1", "number": 1}, {"id": "int9", "number": 9}], True

fake_refs.calls = []
real = pylon.search_issue_refs
pylon.search_issue_refs = fake_refs
try:
    res = asyncio.run(channels.tag_all(full=True))
finally:
    pylon.search_issue_refs = real

check("full sweep asks Pylon per configured channel, full pages",
      fake_refs.calls, [("CAAA111", True), ("CBBB222", True)])
check("sweep reports complete", res["complete"], True)
check("a never-fetched ticket id still enters the index (int9 has no row)",
      "int9" in channels.internal_ticket_ids(), True)
state = channels.tag_state()
check("per-channel health is recorded",
      state["CAAA111"]["complete"] and state["CAAA111"]["seen"] == 2, True)

print()
print("=== payload fast-path writes through the caller's connection ===")
with db.get_conn() as c:
    channels.note_payload_channel(
        {"id": "sl1", "slack": {"channel_id": "CAAA111"}}, c)
    channels.note_payload_channel({"id": "sl2", "slack": None}, c)
check("payload channel recorded", "sl1" in channels.internal_ticket_ids(), True)
with db.get_conn() as c:
    n = c.execute("SELECT COUNT(*) n FROM channel_index WHERE ticket_id='sl2'"
                  ).fetchone()["n"]
check("no channel in payload, no row", n, 0)

print()
print("=== weekly cohort honors the page scope ===")
import weekly
from datetime import date
rows_all = weekly._load_tickets(date(2026, 8, 25), "all")
rows_ext = weekly._load_tickets(date(2026, 8, 25), "external")
ids_all = {r["id"] for r in rows_all}
ids_ext = {r["id"] for r in rows_ext}
check("internal ticket present under all", "int1" in ids_all, True)
check("internal ticket absent under external", "int1" in ids_ext, False)
check("external ticket survives the scope", "ext1" in ids_ext, True)

print()
if fails:
    print(f"FAILURES ({len(fails)}): {fails}")
    raise SystemExit(1)
print("ALL CHANNEL ASSERTIONS PASSED")
