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
print("=== the trend axis is calendar months, gaps included ===")
# A month with no tickets must be a ZERO on the axis, not a missing point.
# Taking the axis from the rows the query returned let a trend line close over
# a quiet month, drawing continuous volume across a gap that was empty.
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
check("the axis is the full six months", a["months"], months)
check("the quiet month is a zero, not a gap", a["created"][1], 0)
check("counts land in their own month",
      (a["created"][0], a["created"][2]), (2, 1))
check("every series is the axis length",
      {len(a["created"]), len(a["still_open"])}, {6})
check("still-open never exceeds raised",
      all(o <= c for o, c in zip(a["still_open"], a["created"])), True)

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
      sum(allv["created"]) >= sum(mine["created"]) > 0, True)
check("an explicit empty selection stays empty",
      (csm.analytics([])["everyone"], sum(csm.analytics([])["created"])),
      (False, 0))
check("the standings table is company-wide either way",
      len(allv["standings"]) == len(mine["standings"]) > 1, True)

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
check("at most the cap plus one folded panel",
      len(a["accounts"]) <= csm.TREND_ACCOUNTS + 1, True)
check("the last panel is the fold",
      a["accounts"][-1]["name"].startswith("Other ("), True)
check("nothing is lost to the fold",
      sum(sum(x["counts"]) for x in a["accounts"]), sum(a["created"]))

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
if fails:
    print(f"FAILED: {len(fails)} — {', '.join(fails)}")
    raise SystemExit(1)
print("ALL CSM ASSERTIONS PASSED")
