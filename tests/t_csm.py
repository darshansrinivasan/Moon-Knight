"""CSM page: whose accounts, which bucket, and what counts as open.

Every assertion here pins something that would show a CSM the wrong book of
business — the failure mode that matters, because this is the one page a
customer-facing owner reads before a customer call.

The load-bearing one is the first: Pylon's STORED value for the bucket people
call "Current Customer" is the string 'Customer/ Churned Customer'. Filtering
on the words a person sees returns zero accounts, and a neighbouring
'Churned Customer' value is one sloppy match away from being swept in.
"""
import json

import csm
import db
import funcheck
import vault

db.init_db()

OWNER_SLUG = "account.hubspot.hubspot_owner_id"
BUCKET_SLUG = "account.hubspot.tam_bucket"
CURRENT = "Customer/ Churned Customer"     # labelled "Current Customer"
CHURNED = "Churned Customer"               # a different bucket entirely
SEETHA, OTHER = "u-seetha", "u-other"
NOW = "2026-09-01T08:00:00+00:00"

fails = []
_n = [8000]


def check(name, got, want):
    ok = got == want
    print(f"  {'OK ' if ok else 'FAIL'} {name}: got {got!r}, want {want!r}")
    if not ok:
        fails.append(name)


vault.set_raw_settings({
    "csm_owner_field": OWNER_SLUG,
    "csm_bucket_field": BUCKET_SLUG,
    "csm_bucket_value": CURRENT,
    "qc_rules_json": '{"excluded_states": ["spam"]}',
    funcheck.ACCOUNT_LABELS_SETTING: json.dumps({"tam_bucket": {
        CURRENT: "Current Customer",
        CHURNED: "Churned Customer",
    }}),
}, "t")
funcheck.invalidate_labels()


def account(aid, name, owner, bucket):
    with db.get_conn() as c:
        c.execute("INSERT OR REPLACE INTO accounts (id,name,domain,type,"
                  "custom_fields,fetched_at) VALUES (?,?,?,'customer',?,?)",
                  (aid, name, f"{aid}.example",
                   json.dumps({OWNER_SLUG: {"value": owner},
                               BUCKET_SLUG: {"value": bucket}}), NOW))


def ticket(tid, aid, state="new", deleted=None):
    _n[0] += 1
    with db.get_conn() as c:
        c.execute("INSERT OR REPLACE INTO tickets (id,number,fetch_date,title,"
                  "link,state,account_id,assignee_name,created_at,fetched_at,"
                  "deleted_at) VALUES (?,?,'2026-08-20',?,?,?,?,'Ann',?,?,?)",
                  (tid, _n[0], f"Ticket {tid}", "https://x/" + tid, state, aid,
                   NOW, NOW, deleted))


def message(tid, who, at, *, customer=0, private=0, body="hi"):
    with db.get_conn() as c:
        c.execute("INSERT OR REPLACE INTO messages (id,ticket_id,message_html,"
                  "timestamp,source,author_name,author_email,is_customer,"
                  "is_private) VALUES (?,?,?,?,'slack',?,?,?,?)",
                  (f"{tid}-{at}", tid, f"<p>{body}</p>", at, who,
                   f"{who}@x.com", customer, private))


print("=== the bucket filter keys on the stored value, not the label ===")
account("a1", "Alpha", SEETHA, CURRENT)
account("a2", "Beta", SEETHA, CHURNED)
account("a3", "Gamma", OTHER, CURRENT)

check("the page names the bucket by its LABEL", csm.bucket_label(),
      "Current Customer")
check("but filters on the raw value", csm.bucket_value(), CURRENT)
check("Seetha's Current Customer accounts",
      [a["name"] for a in csm.accounts_for(SEETHA)], ["Alpha"])
# The bug this pins: a page written against the words on screen.
vault.set_raw_setting("csm_bucket_value", "Current Customer", "t")
check("filtering on the LABEL finds nothing", csm.accounts_for(SEETHA), [])
vault.set_raw_setting("csm_bucket_value", CURRENT, "t")

print()
print("=== the owner picker counts only the covered bucket ===")
owners = {o["id"]: o for o in csm.owners()}
check("both owners listed", sorted(owners), [OTHER, SEETHA])
check("Seetha's churned account is not counted",
      owners[SEETHA]["accounts"], 1)
check("an owner with no directory entry is shown, not dropped",
      owners[SEETHA]["named"], False)
with db.get_conn() as c:
    c.execute("INSERT OR REPLACE INTO users (id,name,email)"
              " VALUES (?,?,?)", (SEETHA, "Seetha Preetha", "s@x.com"))
check("and named once the directory has them",
      {o["id"]: o["name"] for o in csm.owners()}[SEETHA], "Seetha Preetha")

print()
print("=== a quiet account is 0 open tickets, never a missing row ===")
account("a4", "Delta", SEETHA, CURRENT)
ticket("t1", "a1")
ticket("t2", "a1")
check("both accounts listed, counted",
      [(a["name"], a["open_tickets"]) for a in csm.accounts_for(SEETHA)],
      [("Alpha", 2), ("Delta", 0)])

print()
print("=== 'open' is openqc's definition, borrowed not restated ===")
# Each of these left the open set for a different reason. A CSM page that
# disagreed with the Open Tickets tab about the same account on the same
# morning is the failure; one predicate is how that is prevented.
ticket("t3", "a1", state="closed")
ticket("t4", "a1", state="archived")
ticket("t5", "a1", state="spam")           # Admin-excluded
ticket("t6", "a1", deleted=NOW)            # gone from Pylon
check("only the live open ones count",
      csm.accounts_for(SEETHA)[0]["open_tickets"], 2)
check("and the ticket list agrees",
      sorted(t["id"] for t in csm.tickets_for(SEETHA)["tickets"]),
      ["t1", "t2"])

print()
print("=== a ticket on someone else's account is never yours ===")
ticket("t7", "a3")
check("Gamma's ticket stays with its owner",
      [t["id"] for t in csm.tickets_for(SEETHA)["tickets"] if t["id"] == "t7"],
      [])

print()
print("=== created-by and last-reply come from the thread, private included ===")
# The regression this pins: private notes were excluded as "internal asides",
# but a ticket a CSM raises in an internal Slack channel has is_private=1 on
# EVERY message — so the two people columns went blank on exactly the tickets
# that CSM had opened themselves.
message("t1", "Devdutt", "2026-08-20T09:00:00Z", private=1)
message("t1", "Mayuri", "2026-08-20T10:00:00Z", customer=1, private=1)
message("t2", "Jen", "2026-08-20T09:00:00Z", customer=1)
message("t2", "Support", "2026-08-20T11:00:00Z")
rows = {t["id"]: t for t in csm.tickets_for(SEETHA)["tickets"]}
check("an all-private thread still names who opened it",
      rows["t1"]["created_by"], "Devdutt")
check("and who answered last", rows["t1"]["last_reply_by"], "Mayuri")
check("last reply carries its time",
      rows["t1"]["last_reply_at"], "2026-08-20T10:00:00Z")
check("a public thread reads the same way",
      (rows["t2"]["created_by"], rows["t2"]["last_reply_by"]),
      ("Jen", "Support"))

print()
print("=== the expanded row marks a private note as private ===")
convo = csm.conversation("t1")
check("thread returned in order",
      [m["author_name"] for m in convo["messages"]], ["Devdutt", "Mayuri"])
check("private flag survives to the page",
      [m["is_private"] for m in convo["messages"]], [True, True])
check("an unknown ticket is not an error page",
      csm.conversation("nope")["ticket"], None)

print()
print("=== an admin-editable field slug cannot rewrite the query ===")
# The slug reaches SQL as a bound json path, never as text spliced into the
# statement. A slug carrying a quote must return nothing, not raise and not
# match everything.
vault.set_raw_setting("csm_owner_field", 'x".value\') OR 1=1 --', "t")
check("an injected slug matches nothing", csm.accounts_for(SEETHA), [])
check("and the picker stays empty rather than erroring", csm.owners(), [])
vault.set_raw_setting("csm_owner_field", OWNER_SLUG, "t")
check("and the real slug still works",
      [a["name"] for a in csm.accounts_for(SEETHA)], ["Alpha", "Delta"])

print()
print("=== several CSMs can be viewed together ===")
# One code path for one owner and for many: the page sends a list, and a caller
# passing a bare string (every caller did, before the picker went multi) must
# keep working rather than iterating the characters of a UUID.
check("a bare string still means one owner",
      [a["name"] for a in csm.accounts_for(SEETHA)], ["Alpha", "Delta"])
check("a one-item list is the same thing",
      [a["name"] for a in csm.accounts_for([SEETHA])], ["Alpha", "Delta"])
check("two owners union their accounts",
      sorted(a["name"] for a in csm.accounts_for([SEETHA, OTHER])),
      ["Alpha", "Delta", "Gamma"])
check("duplicates in the selection do not duplicate rows",
      len(csm.accounts_for([SEETHA, SEETHA])), 2)
# The failure this pins: an empty selection falling through to a bare bucket
# filter would hand the reader every account in the company.
check("an empty selection matches nothing, not everything",
      (csm.accounts_for([]), csm.tickets_for([])["tickets"]), ([], []))
check("blank ids are ignored, not matched",
      csm.accounts_for(["", None]), [])

multi = csm.tickets_for([SEETHA, OTHER])
check("tickets from both owners appear",
      sorted(t["id"] for t in multi["tickets"]), ["t1", "t2", "t7"])
# Without this the reader cannot tell whose account a row belongs to; the page
# shows the column only when more than one CSM is selected.
check("every row names its owning CSM",
      {t["id"]: t["owner_name"] for t in multi["tickets"]},
      {"t1": "Seetha Preetha", "t2": "Seetha Preetha", "t7": OTHER})
check("the selection is echoed back", multi["owners"], [SEETHA, OTHER])
check("accounts carry their owner too",
      {a["name"]: a["owner_name"] for a in multi["accounts"]},
      {"Alpha": "Seetha Preetha", "Delta": "Seetha Preetha", "Gamma": OTHER})
check("an unnamed owner falls back to its id, not blank",
      [a["owner_name"] for a in csm.accounts_for(OTHER)], [OTHER])

print()
print("=== the Group column follows the ASSIGNEE's coverage roster ===")
# A coverage owns PEOPLE, not accounts. Deriving the group from the account
# would put a CSM's own region on every ticket they own, which is not what
# Admin -> Review Coverage defines and not what the Open tab's filter means.
import review

def coverage(name, members):
    with db.get_conn() as c:
        cur = c.execute(
            "INSERT INTO review_coverages (name, reviewer_email, reviewer_name,"
            " updated_by, updated_at) VALUES (?,?,?,'t',?)",
            (name, f"{name}@x.com", f"{name} Lead", NOW))
        for m in members:
            c.execute("INSERT OR REPLACE INTO review_coverage_assignees"
                      " (coverage_id, assignee_name) VALUES (?,?)",
                      (cur.lastrowid, m))

check("no rosters means no groups, not an error",
      {t["id"]: t["groups"] for t in csm.tickets_for(SEETHA)["tickets"]},
      {"t1": [], "t2": []})

with db.get_conn() as c:
    c.execute("UPDATE tickets SET assignee_name='Ann'  WHERE id='t1'")
    c.execute("UPDATE tickets SET assignee_name='Bea'  WHERE id='t2'")
coverage("APAC", ["Ann", "Bea"])
coverage("EMEA", ["Bea"])
coverage("NAM", ["Cid"])

check("the roster map is name -> every group covering them",
      review.groups_by_assignee(),
      {"Ann": ["APAC"], "Bea": ["APAC", "EMEA"], "Cid": ["NAM"]})
rows = {t["id"]: t["groups"] for t in csm.tickets_for(SEETHA)["tickets"]}
check("a single-roster assignee shows their group", rows["t1"], ["APAC"])
# Flattening to the first match would have hidden half of what Bea covers.
check("someone on two rosters shows both", rows["t2"], ["APAC", "EMEA"])

with db.get_conn() as c:
    c.execute("UPDATE tickets SET assignee_name=NULL WHERE id='t1'")
check("an unassigned ticket belongs to no group",
      {t["id"]: t["groups"] for t in csm.tickets_for(SEETHA)["tickets"]}["t1"],
      [])
with db.get_conn() as c:
    c.execute("UPDATE tickets SET assignee_name='Zoe' WHERE id='t1'")
check("an assignee on no roster is ungrouped, not dropped from the list",
      sorted(t["id"] for t in csm.tickets_for(SEETHA)["tickets"]), ["t1", "t2"])
with db.get_conn() as c:
    c.execute("UPDATE tickets SET assignee_name='Ann' WHERE id='t1'")

print()
print("=== the age axis is calendar months, gaps included ===")
# A month with no tickets must be a ZERO on the axis, not a missing point.
# Taking the axis from the rows the query returned let a chart close over a
# quiet month, drawing continuous volume across a gap that was empty.
months = csm._month_axis()
check("six months, oldest first", len(months), 6)
check("ordered", months, sorted(months))
check("the last is this month", months[-1],
      __import__("datetime").datetime.now(
          __import__("datetime").timezone.utc).strftime("%Y-%m"))

with db.get_conn() as c:
    # Two tickets in the oldest month on the axis, none in the next.
    for tid, when in (("m1", months[0]), ("m2", months[0]), ("m3", months[2])):
        c.execute("INSERT OR REPLACE INTO tickets (id,number,fetch_date,title,"
                  "state,account_id,created_at,fetched_at) VALUES"
                  " (?,?,'2026-08-20',?,'new','a1',?,?)",
                  (tid, hash(tid) % 9999, tid, f"{when}-05T00:00:00Z", NOW))
a = csm.analytics(SEETHA)
check("the axis is the older bucket plus the full six months",
      a["buckets"], [csm.OLDER, *months])
# buckets[0] is "older", so month N sits at index N+1.
check("the quiet month is a zero, not a gap", a["open_counts"][2], 0)
check("counts land in their own month",
      (a["open_counts"][1], a["open_counts"][3]), (2, 1))
check("the series is the axis length", len(a["open_counts"]), 7)
check("and every account panel is too",
      {len(x["counts"]) for x in a["accounts"]}, {7})

print()
print("=== Analytics opens on everyone; an empty selection still means none ===")
# Reported as "the analytics page is not loading": with nobody picked the tab
# rendered a blank frame beside a standings table that is company-wide anyway.
# None (no selection sent) now means EVERY CSM; [] (asked for nobody) still
# means nothing, because those are opposite intentions that look alike.
allv = csm.analytics()
mine = csm.analytics([SEETHA])
check("no argument covers everyone", allv["everyone"], True)
check("and a selection does not", mine["everyone"], False)
check("everyone counts at least what one CSM does",
      allv["total_open"] >= mine["total_open"] > 0, True)
check("an explicit empty selection stays empty",
      (csm.analytics([])["everyone"], csm.analytics([])["total_open"]),
      (False, 0))
check("the standings table is company-wide either way",
      len(allv["standings"]) == len(mine["standings"]) > 1, True)

print()
print("=== the age axis has no window: every open ticket is on it ===")
# Reported from production: "all CSMs" showed 241 on Analytics and 268 on the
# Open tickets tab, because the chart counted tickets RAISED in a six-month
# window and a ticket open since December fell outside it. The axis now leads
# with an "older" bucket, so the chart and the ticket list must agree exactly.
with db.get_conn() as c:
    c.execute("INSERT OR REPLACE INTO tickets (id,number,fetch_date,title,state,"
              "account_id,created_at,fetched_at) VALUES"
              " ('anc',9401,'2026-08-20','Ancient','new','a1','2024-01-05T00:00:00Z',?)",
              (NOW,))
a = csm.analytics(SEETHA)
total = len(csm.tickets_for(SEETHA)["tickets"])
check("the axis leads with the older bucket", a["buckets"][0], csm.OLDER)
check("and then the calendar months", a["buckets"][1:], a["months"])
check("a ticket older than the axis lands in that bucket",
      a["open_counts"][0] >= 1, True)
# The assertion the production report was really about.
check("the chart totals exactly what the ticket list shows",
      (sum(a["open_counts"]), a["total_open"]), (total, total))
check("the per-account panels plus the tail reconcile too",
      sum(x["total"] for x in a["accounts"]) + a["other"]["total"], total)
check("the older count respects the owner filter",
      csm.analytics(OTHER)["open_counts"][0], 0)
# Only OPEN tickets: a closed one must not appear anywhere on this tab.
with db.get_conn() as c:
    c.execute("UPDATE tickets SET state='closed' WHERE id='anc'")
check("closing it removes it from the chart",
      csm.analytics(SEETHA)["total_open"], total - 1)
with db.get_conn() as c:
    c.execute("DELETE FROM tickets WHERE id='anc'")

print()
print("=== the by-account panels are capped, the tail folded not dropped ===")
# Identity in these panels is the heading, not a colour — but the cap still
# matters: the fold keeps the tail's volume visible instead of discarding it.
for i in range(csm.TREND_ACCOUNTS + 2):
    aid = f"z{i}"
    account(aid, f"Zed {i}", SEETHA, CURRENT)
    with db.get_conn() as c:
        c.execute("INSERT OR REPLACE INTO tickets (id,number,fetch_date,title,"
                  "state,account_id,created_at,fetched_at) VALUES"
                  " (?,?,'2026-08-20',?,'new',?,?,?)",
                  (f"zt{i}", 7000 + i, aid, aid,
                   f"{months[3]}-05T00:00:00Z", NOW))
a = csm.analytics(SEETHA)
check("never more panels than the cap", len(a["accounts"]) <= csm.TREND_ACCOUNTS, True)
# The tail is a figure, not a seventh panel: folded into one it reached 203
# open against 4-6 per named account, and on the shared scale that made every
# named panel an invisible sliver.
check("no panel is the fold",
      any(x["name"].startswith("Other (") for x in a["accounts"]), False)
check("the tail is reported separately", a["other"]["accounts"] >= 1, True)
check("nothing is lost to the fold",
      sum(x["total"] for x in a["accounts"]) + a["other"]["total"],
      a["total_open"])

print()
print("=== the standings table covers every CSM in the bucket ===")
st = {r["id"]: r for r in csm.standings()}
check("both owners present", sorted(st), [OTHER, SEETHA])
check("the churned account is not counted for Seetha",
      st[SEETHA]["accounts"], 2 + csm.TREND_ACCOUNTS + 2)
check("ordered by open tickets, busiest first",
      [r["open_tickets"] for r in csm.standings()],
      sorted([r["open_tickets"] for r in csm.standings()], reverse=True))
check("a CSM with no open tickets is still listed",
      OTHER in st, True)

print()
print("=== auto-refresh: interval, floor, and the freshness probe ===")
import asyncio

vault.set_raw_setting("csm_auto_refresh", "0", "t")
check("off by default, so a deploy does not start fetching",
      csm.auto_refresh_minutes(), None)
vault.set_raw_settings({"csm_auto_refresh": "1",
                        "csm_auto_refresh_minutes": "10"}, "t")
check("on, at the configured interval", csm.auto_refresh_minutes(), 10)
# The full refetch takes ~105s. An interval near that has a run starting as
# the last one finishes, which is a permanent fetch rather than a schedule.
vault.set_raw_setting("csm_auto_refresh_minutes", "1", "t")
check("an interval below the floor is raised to it",
      csm.auto_refresh_minutes(), csm.MIN_AUTO_MINUTES)
vault.set_raw_setting("csm_auto_refresh_minutes", "banana", "t")
check("an unreadable interval falls back, it does not crash the loop",
      csm.auto_refresh_minutes(), 10)
vault.set_raw_setting("csm_auto_refresh_minutes", "30", "t")
check("a longer interval is honoured", csm.auto_refresh_minutes(), 30)

# The guard that keeps a laptop from fetching alongside production.
check("a non-deployed copy refuses to auto-refresh",
      asyncio.run(csm.auto_refresh_once()).get("skipped"),
      "not the deployed instance")

vault.set_raw_setting("allow_local_side_effects", "1", "t")
with db.advisory_lock("fetch:open", "someone-else", ttl_seconds=60):
    # Skips rather than queues: a refresh that lands twenty minutes late
    # serves nobody, and blocking here holds the loop past its next tick.
    res = asyncio.run(csm.auto_refresh_once())
check("a refresh already in flight is skipped, not queued",
      bool(res.get("skipped")) and "not the deployed" not in res["skipped"], True)
vault.set_raw_setting("allow_local_side_effects", "0", "t")

f = csm.freshness()
check("the probe reports the store's stamp and the interval",
      (set(f) == {"tickets_at", "accounts_at", "auto_minutes"},
       f["auto_minutes"]),
      (True, 30))
with db.get_conn() as c:
    c.execute("UPDATE tickets SET fetched_at='2099-01-01T00:00:00Z' WHERE id='t1'")
check("and it moves when the store does",
      csm.freshness()["tickets_at"], "2099-01-01T00:00:00Z")
vault.set_raw_setting("csm_auto_refresh", "0", "t")

print()
print("=== the Slack column is the ON-CALL channel, not the origin thread ===")
# Reported after shipping: the column linked the thread the ticket came from,
# but a CSM needs the incident channel support opened for it — Pylon records
# that in its own field. Both are Slack URLs in the same workspace, which is
# precisely why the wrong one read as correct for a whole release.
ONCALL = "https://spotdraft.slack.com/archives/C0C5L6S1E6A"
ORIGIN = "https://spotdraft.slack.com/archives/C0BCD5N5QJ0/p1790760344496259"
with db.get_conn() as c:
    c.execute("UPDATE tickets SET custom_fields=?, slack_url=? WHERE id='t1'",
              (json.dumps({"oncall_slack_chat_link": {"value": ONCALL}}), ORIGIN))
    c.execute("UPDATE tickets SET custom_fields='{}', slack_url=? WHERE id='t2'",
              (ORIGIN,))
rows = {t["id"]: t for t in csm.tickets_for(SEETHA)["tickets"]}
check("the on-call link comes from the Pylon field", rows["t1"]["oncall_url"], ONCALL)
# The failure worth pinning: falling back to the origin thread would put a
# plausible Slack link under a column that promises the on-call channel.
check("a ticket without one shows nothing, it does not fall back",
      rows["t2"]["oncall_url"], None)
check("the origin thread is still stored, just not what this column shows",
      rows["t2"]["slack_url"], ORIGIN)

with db.get_conn() as c:
    c.execute("UPDATE tickets SET custom_fields=? WHERE id='t2'",
              (json.dumps({"oncall_slack_chat_link": {"value": "javascript:alert(1)"}}),))
check("a non-http value never reaches an href",
      csm.tickets_for(SEETHA)["tickets"] and
      {t["id"]: t["oncall_url"] for t in csm.tickets_for(SEETHA)["tickets"]}["t2"],
      None)
with db.get_conn() as c:
    c.execute("UPDATE tickets SET custom_fields='not json' WHERE id='t2'")
check("unparseable stored fields degrade to no link, not a 500",
      {t["id"]: t["oncall_url"] for t in csm.tickets_for(SEETHA)["tickets"]}["t2"],
      None)
# The slug is admin-editable like every other Pylon field the app reads.
vault.set_raw_setting("csm_oncall_link_field", "some_other_field", "t")
check("repointing the setting moves where the link is read from",
      {t["id"]: t["oncall_url"] for t in csm.tickets_for(SEETHA)["tickets"]}["t1"],
      None)
vault.set_raw_setting("csm_oncall_link_field", "oncall_slack_chat_link", "t")
check("and back", {t["id"]: t["oncall_url"]
                   for t in csm.tickets_for(SEETHA)["tickets"]}["t1"], ONCALL)
# The payload must not carry the whole custom_fields blob to the browser.
check("the raw field blob is not shipped to the page",
      "custom_fields" in csm.tickets_for(SEETHA)["tickets"][0], False)

print()
print("=== rows carry account_id, so the page never joins on a display name ===")
# Pylon auto-creates an account per email domain, so duplicate names are
# routine — there are 20 in the live store and one CSM owns two accounts both
# called "Meesho". The By-company view counted tickets by matching
# account_name, which collapsed every duplicate onto whichever row it found
# first. The identity has to travel with the row for that join to be safe.
account("dup1", "Twin Corp", SEETHA, CURRENT)
account("dup2", "Twin Corp", SEETHA, CURRENT)
ticket("d1", "dup1")
ticket("d2", "dup2")
ticket("d3", "dup2")
rows = csm.tickets_for(SEETHA)["tickets"]
check("every ticket carries its account_id",
      all(t.get("account_id") for t in rows), True)
dup = {t["id"]: t["account_id"] for t in rows if t["id"] in ("d1", "d2", "d3")}
check("same-named accounts stay distinguishable on the rows",
      (dup["d1"] != dup["d2"], dup["d2"] == dup["d3"]), (True, True))
counts = {(a["id"], a["name"]): a["open_tickets"]
          for a in csm.accounts_for(SEETHA) if a["name"] == "Twin Corp"}
check("and the server counts them apart, 1 and 2",
      sorted(counts.values()), [1, 2])

print()
print("=== the page filters and sorts on what the server actually sends ===")
# The status filter is client-side, so what it keys on has to be present on
# every row. Pinned server-side because a filter built against a field the
# listing stopped sending would silently match nothing.
rows = csm.tickets_for([SEETHA, OTHER])["tickets"]
check("every ticket carries a state to filter on",
      all("state" in t for t in rows), True)
check("and the states are RAW values, not the spellings the page shows",
      all("_" in (t["state"] or "") or " " not in (t["state"] or "")
          for t in rows), True)
# Every column the table offers a sort on must exist on the row, or the sort
# silently compares undefined to undefined and does nothing.
for field in ("created_at", "account_id", "account_name", "number", "title", "created_by",
              "assignee_name", "groups", "state", "last_reply_at",
              "last_reply_by", "oncall_url", "link", "owner_name"):
    if field not in rows[0]:
        fails.append(f"sortable column {field} missing from the listing")
check("every sortable ticket column is present on the row",
      [f for f in fails if "sortable column" in f], [])
acct = csm.accounts_for([SEETHA, OTHER])[0]
check("and every sortable account column too",
      all(k in acct for k in ("name", "owner_name", "domain", "open_tickets")),
      True)
stand = csm.standings()[0]
check("and every sortable standings column",
      all(k in stand for k in ("name", "accounts", "open_tickets")), True)

print()
print("=== the CSM page joins tickets to accounts by id, not by name ===")
# A static pin because this lives in page JS with no runtime coverage. Matching
# on account_name here is the bug above, reintroduced.
import pathlib as _pl
PAGE = (_pl.Path(__file__).resolve().parent.parent / "static" / "csm.html").read_text()
counts_fn = PAGE.split("function companyCounts()", 1)[1].split("\n}", 1)[0]
check("company counts key on account_id", "t.account_id" in counts_fn, True)
check("and never on the display name", "account_name" in counts_fn, False)
# The cache has to remember whose data it holds, or a tab switch repaints the
# previous CSM's tickets under the new CSM's name.
check("the ticket cache records which selection it belongs to",
      "dataFor" in PAGE, True)

print()
print("=== the Runs page actually SENDS every setting it offers ===")
# The bug this pins: csm_auto_refresh was listed in the Save button's
# data-save attribute, but that attribute is a label — the handler builds its
# payload from a hardcoded list. The key was never sent, the save returned the
# unchanged value, and the page re-rendered the box as unticked. It read as a
# checkbox that refused to stay on.
import pathlib
import re

RUNS = pathlib.Path(__file__).resolve().parent.parent / "static" / "runs.html"
html = RUNS.read_text()
declared = set()
for attr in re.findall(r'data-save="([^"]+)"', html):
    declared |= {k.strip() for k in attr.split(",") if k.strip()}
# maxsplit=1: the selector appears twice in the handler, and splitting on all
# occurrences hands back the sliver between them instead of the body.
handler = html.split('document.querySelector("[data-save]")', 1)[1]
# Whole-token, not substring: "csm_auto_refresh" occurs inside
# "csm_auto_refresh_minutes", so a plain `in` reported the key as present
# while the handler had dropped it — the pin passed against the very bug it
# was written for. \b does not break on "_", which is what makes this work.
missing = sorted(k for k in declared
                 if not re.search(rf"\b{re.escape(k)}\b", handler))
check("every advertised setting appears in the save handler", missing, [])
check("the CSM controls are advertised",
      {"csm_auto_refresh", "csm_auto_refresh_minutes"} <= declared, True)
# And read back, or a saved value never reaches the form on the next load.
fill = html.split("function fillSchedule")[1].split("\n}")[0]
check("and are read back into the form",
      all(re.search(rf"\b{k}\b", fill)
          for k in ("csm_auto_refresh", "csm_auto_refresh_minutes")),
      True)

print()
if fails:
    print(f"FAILED: {len(fails)} — {', '.join(fails)}")
    raise SystemExit(1)
print("ALL CSM ASSERTIONS PASSED")
