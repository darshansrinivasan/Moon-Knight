"""Open-backlog QC: the tab that grades everything not Closed or Archived.

The assertions pinned here are the ones that would quietly corrupt grades if
they regressed: a terminal state leaking into the open set, a state filter
WIDENING the scope instead of narrowing it, a cross-date run stamping its label
on ai_checks.fetch_date, a refresh moving a ticket to a new date, or a 404
deleting a ticket that carries a human sign-off.
"""
import asyncio
import json

import db
import openqc
import pylon
import qc_runner
import rules as qc_rules
import vault

db.init_db()
D1, D2, D3 = "2026-08-20", "2026-08-25", "2026-09-01"
T0 = "2026-09-01T08:00:00+00:00"
PYLON = "https://app.usepylon.com/issues/"

fails = []
_next_number = [5000]


def check(name, got, want):
    ok = got == want
    print(f"  {'OK ' if ok else 'FAIL'} {name}: got {got!r}, want {want!r}")
    if not ok:
        fails.append(name)


def ticket(tid, state, date=D2, assignee="Ann", grade=None,
           deleted=None, fingerprint="fp-1"):
    """Seed one ticket; grade=None leaves it unscored (pending QC)."""
    _next_number[0] += 1
    with db.get_conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO tickets (id,number,fetch_date,title,link,"
            "state,assignee_name,custom_fields,fetched_at,deleted_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (tid, _next_number[0], date, f"Ticket {tid}", PYLON + tid,
             state, assignee, "{}", T0, deleted),
        )
        c.execute(
            "INSERT OR REPLACE INTO rule_checks (ticket_id,fetch_date,r1,r2,r3,"
            "r4,r5,r7,r8,checked_at) VALUES (?,?,'Pass','Pass','Pass','Pass',"
            "'Pass','Pass','Pass',?)",
            (tid, date, T0),
        )
        if grade:
            c.execute(
                "INSERT OR REPLACE INTO ai_checks (ticket_id,fetch_date,a1,a2,"
                "a3,a4,a5,ai_notes,overall_result,checked_at,qc_fingerprint)"
                " VALUES (?,?,'Pass','Neutral','Good','Pass','Pass','n',?,?,?)",
                (tid, date, grade, T0, fingerprint),
            )


def open_ids(**kw):
    return sorted(t["ticket_id"] for t in openqc.list_open(**kw)["tickets"])


vault.set_raw_setting("qc_rules_json", '{"excluded_states": ["spam"]}', "t")
qc_rules.invalidate()

ticket("o1", "new")
ticket("o2", "waiting_on_you", date=D1, grade="Pass")
ticket("o3", "on_hold", date=D3)
ticket("o4", "Closed")                      # terminal, case differs
ticket("o5", "archived")                    # terminal
ticket("o6", "spam")                        # open-shaped but Admin-excluded
ticket("o7", "new", deleted=T0)             # withdrawn at source
ticket("o8", None)                          # state unknown — still not terminal

print("=== the open set is 'not closed, not archived', minus exclusions ===")
check("open ids", open_ids(), ["o1", "o2", "o3"])
check("Closed is terminal regardless of case", "o4" in open_ids(), False)
check("archived is terminal", "o5" in open_ids(), False)
check("an Admin-excluded state stays excluded even while open",
      "o6" in open_ids(), False)
check("a soft-deleted ticket is out", "o7" in open_ids(), False)
# A NULL state rides the app-wide exclusion clause: `state NOT IN (...)` is
# NULL for a NULL state, so the row drops whenever ANY state is excluded —
# exactly as it does for the scorer, the leaderboard and analytics. The tab
# must agree with the scorer's scope, so this pins the shared behaviour in
# both directions rather than inventing a kinder rule here.
check("a NULL state follows the shared exclusion clause (dropped while "
      "exclusions exist)", "o8" in open_ids(), False)
vault.set_raw_setting("qc_rules_json", '{"excluded_states": []}', "t")
qc_rules.invalidate()
check("with nothing excluded, a NULL state is open — unknown is not terminal",
      "o8" in open_ids(), True)
vault.set_raw_setting("qc_rules_json", '{"excluded_states": ["spam"]}', "t")
qc_rules.invalidate()

print()
print("=== filters narrow the open set; they never widen it ===")
check("date range is inclusive", open_ids(start=D2, end=D2), ["o1"])
check("open start", open_ids(start=D3), ["o3"])
check("open end", open_ids(end=D1), ["o2"])
check("state filter subsets", open_ids(states=["new"]), ["o1"])
check("state filter is case-insensitive", open_ids(states=["NEW"]), ["o1"])
check("a terminal state in the filter selects nothing, not everything",
      open_ids(states=["closed"]), [])
check("an excluded state in the filter stays excluded",
      open_ids(states=["spam"]), [])
try:
    openqc.list_open(start="20-08-2026")
    check("a malformed date is rejected", "returned", "ValueError")
except ValueError:
    check("a malformed date is rejected", "ValueError", "ValueError")

print()
print("=== the listing carries counts the pills and chips read ===")
d = openqc.list_open()
check("summary reconciles",
      (d["summary"]["total"],
       d["summary"]["pass"] + d["summary"]["fail"]
       + d["summary"]["review"] + d["summary"]["pending"]),
      (3, 3))
check("state counts ignore the state filter (pills keep their numbers)",
      openqc.list_open(states=["new"])["state_counts"],
      openqc.list_open()["state_counts"])

print()
print("=== preview uses the run's own eligibility ===")
p = openqc.preview()
check("in scope", p["in_scope"], 3)
# o2 is graded and its content unchanged... but its stored fingerprint is a
# test stub, so recomputation marks it eligible along with the ungraded two.
check("everything with a stub fingerprint needs scoring", p["eligible"], 3)
with db.get_conn() as c:
    real_fp = qc_runner.qc_fingerprint(
        dict(c.execute("SELECT t.*, NULL AS account_name, NULL AS account_type,"
                       " rc.r1, rc.r2, rc.r3, rc.r4, rc.r5, rc.r7, rc.r8"
                       " FROM tickets t JOIN rule_checks rc ON rc.ticket_id=t.id"
                       " WHERE t.id='o2'").fetchone()),
        [], {"r1": "Pass", "r2": "Pass", "r3": "Pass", "r4": "Pass",
             "r5": "Pass", "r7": "Pass", "r8": "Pass"})
    c.execute("UPDATE ai_checks SET qc_fingerprint=? WHERE ticket_id='o2'",
              (real_fp,))
check("an unchanged graded ticket is not re-billed",
      openqc.preview()["eligible"], 2)

print()
print("=== a run with nothing eligible still leaves a record, under 'open' ===")
with db.get_conn() as c:
    c.execute("UPDATE tickets SET state='closed' WHERE id IN ('o1','o3','o8')")
res = openqc.run("t@x.com", states=["waiting_on_you"])
check("already done", res["already_done"], True)
with db.get_conn() as c:
    row = c.execute("SELECT date, config_json FROM qc_runs WHERE id=?",
                    (res["run_id"],)).fetchone()
check("filed under the label, not a date", row["date"], "open")
cfg = json.loads(row["config_json"])
check("filters recorded in the run config",
      cfg.get("filters"), {"start": None, "end": None,
                           "states": ["waiting_on_you"]})
check("marked as a backlog run", cfg.get("open_backlog"), True)
with db.get_conn() as c:
    c.execute("UPDATE tickets SET state='new' WHERE id IN ('o1','o8')")
    c.execute("UPDATE tickets SET state='on_hold' WHERE id='o3'")

print()
print("=== grades land under the ticket's own date, never the run label ===")
loaded = qc_runner._load_in_scope_where(*openqc.open_scope())
by_id = {t["id"]: t for t in loaded}
check("the loader carries each ticket's fetch_date",
      (by_id["o2"]["fetch_date"], by_id["o3"]["fetch_date"]), (D1, D3))
fake = [{"idx": i, "a1": "Pass", "a2": "Neutral", "a3": "Good",
         "a4": "Pass", "a5": "Pass", "ai_notes": "t"}
        for i in range(2)]
qc_runner._write_results([by_id["o2"], by_id["o3"]], fake, T0)
with db.get_conn() as c:
    dates = {r["ticket_id"]: r["fetch_date"] for r in c.execute(
        "SELECT ticket_id, fetch_date FROM ai_checks"
        " WHERE ticket_id IN ('o2','o3')")}
check("ai_checks keep per-ticket dates", dates, {"o2": D1, "o3": D3})

print()
print("=== a refresh preserves fetch_date and honours the archived rule ===")


def fake_issue(tid, state, title="fresh title"):
    return {"id": tid, "number": _next_number[0], "title": title,
            "link": PYLON + tid, "state": state, "custom_fields": {},
            "external_issues": []}


refreshed = pylon.FetchedTickets(
    issues=[fake_issue("o1", "waiting_on_you"), fake_issue("o3", "archived")],
    messages_by_id={"o1": [], "o3": []},
    accounts_by_id={},
    missing_ids={"o8"},
)


async def fake_fetch(ids):
    fake_fetch.asked = sorted(ids)
    return refreshed


fake_fetch.asked = None
real_fetch = pylon.fetch_tickets_by_id
pylon.fetch_tickets_by_id = fake_fetch
try:
    out = asyncio.run(openqc.refresh_open())
finally:
    pylon.fetch_tickets_by_id = real_fetch

check("only the open set was asked about", fake_fetch.asked,
      ["o1", "o2", "o3", "o8"])
check("stored what Pylon returned", out["stored"], 2)
with db.get_conn() as c:
    o1 = c.execute("SELECT fetch_date, state, title FROM tickets"
                   " WHERE id='o1'").fetchone()
    o3 = c.execute("SELECT state FROM tickets WHERE id='o3'").fetchone()
    o8 = c.execute("SELECT deleted_at FROM tickets WHERE id='o8'").fetchone()
check("fetch_date survives the refresh", o1["fetch_date"], D2)
check("fresh content lands", (o1["state"], o1["title"]),
      ("waiting_on_you", "fresh title"))
check("a ticket that archived since fetch is recorded as such",
      o3["state"], "archived")
check("and thereby leaves the open set", "o3" in open_ids(), False)
check("a 404 by id soft-deletes without a completeness proof",
      o8["deleted_at"] is not None, True)

print()
print("=== but never a ticket someone signed off on ===")
ticket("o9", "new")
with db.get_conn() as c:
    c.execute("INSERT INTO ticket_reviews (ticket_id,decision,kept_ai,"
              "reviewer_email,reviewer_name,note,reviewed_at)"
              " VALUES ('o9','Pass',0,'r@x.com','R','',?)", (T0,))
marked = openqc._mark_missing({"o9"})
check("reviewed ticket kept", marked, {"deleted": 0, "kept_reviewed": 1})
with db.get_conn() as c:
    check("o9 not deleted",
          c.execute("SELECT deleted_at FROM tickets WHERE id='o9'")
          .fetchone()["deleted_at"], None)

print()
print("=== re-fetch discovers open tickets no day-fetch ever brought in ===")
# The gap this closes: the listing and the by-id refresh can only see ids the
# database already knows, so a ticket created on an unfetched day was
# invisible forever (155 local vs 240+ in Pylon).
# Current open set here: o1 (D2), o2 (D1), o9 (D2, reviewed).
check("open set before discovery", open_ids(), ["o1", "o2", "o9"])

# 20:00 UTC on Aug 30 is 01:30 IST on Aug 31 — the IST day is the file key.
new_issue = dict(fake_issue("n1", "new", title="never fetched"),
                 created_at="2026-08-30T20:00:00Z")
discovered = pylon.FetchedTickets(
    issues=[fake_issue("o1", "investigating"), new_issue],
    messages_by_id={"o1": [], "n1": []},
    accounts_by_id={},
    issues_complete=True,
)
# o2 and o9 are known-open but absent from the (complete) search: o2 closed
# since, o9 is gone from Pylon entirely — but carries a review.
leftover_refresh = pylon.FetchedTickets(
    issues=[fake_issue("o2", "closed")],
    messages_by_id={"o2": []},
    accounts_by_id={},
    missing_ids={"o9"},
)


async def fake_open_fetch(exclude):
    fake_open_fetch.excluded = exclude
    return discovered


async def fake_by_id(ids):
    fake_by_id.asked = sorted(ids)
    return leftover_refresh


fake_open_fetch.excluded = None
fake_by_id.asked = None
real_open, real_by_id = pylon.fetch_open_issues, pylon.fetch_tickets_by_id
pylon.fetch_open_issues, pylon.fetch_tickets_by_id = fake_open_fetch, fake_by_id
try:
    out = asyncio.run(openqc.refetch_open())
finally:
    pylon.fetch_open_issues, pylon.fetch_tickets_by_id = real_open, real_by_id

check("search excludes exactly the terminal states",
      fake_open_fetch.excluded, openqc.TERMINAL_STATES)
check("found and new counted", (out["found_open"], out["new"]), (2, 1))
with db.get_conn() as c:
    n1 = c.execute("SELECT fetch_date, state FROM tickets WHERE id='n1'").fetchone()
    o1d = c.execute("SELECT fetch_date, state FROM tickets WHERE id='o1'").fetchone()
    o2s = c.execute("SELECT state, fetch_date FROM tickets WHERE id='o2'").fetchone()
    o9d = c.execute("SELECT deleted_at FROM tickets WHERE id='o9'").fetchone()
check("a discovered ticket is filed under its IST creation day",
      dict(n1), {"fetch_date": "2026-08-31", "state": "new"})
check("a known ticket keeps its fetch_date through discovery",
      dict(o1d), {"fetch_date": D2, "state": "investigating"})
check("known-open tickets absent from a complete search get checked by id",
      fake_by_id.asked, ["o2", "o9"])
check("one that closed since is recorded as closed, same date",
      dict(o2s), {"state": "closed", "fetch_date": D1})
check("one that 404s but carries a review is kept",
      (o9d["deleted_at"], out["kept_reviewed"]), (None, 1))
check("the open set now tells the truth", open_ids(), ["n1", "o1", "o9"])

print()
print("=== an incomplete search never declares anything 'no longer open' ===")
partial = pylon.FetchedTickets(
    issues=[dict(fake_issue("n2", "new"), created_at="2026-09-02T05:00:00Z")],
    messages_by_id={"n2": []},
    accounts_by_id={},
    issues_complete=False,
)


async def fake_partial(exclude):
    return partial


async def must_not_run(ids):
    raise AssertionError("leftover check ran on an incomplete search")


pylon.fetch_open_issues, pylon.fetch_tickets_by_id = fake_partial, must_not_run
try:
    out2 = asyncio.run(openqc.refetch_open())
finally:
    pylon.fetch_open_issues, pylon.fetch_tickets_by_id = real_open, real_by_id

check("incomplete flag surfaces", out2["search_complete"], False)
check("nothing was checked or deleted",
      (out2["no_longer_open_checked"], out2["deleted"]), (0, 0))
check("the discovered ticket still landed", "n2" in open_ids(), True)
check("garbage created_at falls back to a date, not a crash",
      len(openqc._ist_fetch_date("not-a-time")), 10)

print()
print("=== the listing names who signed a ticket off ===")
rows_by_id = {t["ticket_id"]: t for t in openqc.list_open()["tickets"]}
check("reviewer named on a signed-off ticket",
      rows_by_id["o9"]["signed_off_by"], "R")
check("and the sign-off is the grade shown",
      rows_by_id["o9"]["overall_result"], "Pass")
check("unsigned tickets carry no name", rows_by_id["o1"]["signed_off_by"], None)

print()
if fails:
    print(f"FAILURES ({len(fails)}): {fails}")
    raise SystemExit(1)
print("ALL OPEN-BACKLOG ASSERTIONS PASSED")
