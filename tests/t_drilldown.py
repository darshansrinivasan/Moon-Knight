"""Failure drill-down: which tickets are behind one leaderboard number.

The assertions pinned here are the ones that turn a helpful dialog into a
liability if they regress: an unknown check listing every ticket, a
`javascript:` link reaching an href, a page silently cut short, or an A-check
failure semantic invented here instead of read off the backend.
"""
import db
import drilldown as dd
import rules as qc_rules
import vault

db.init_db()
D1, D2, D3 = "2026-08-24", "2026-08-25", "2026-08-26"
T0 = "2026-08-26T08:00:00+00:00"
PYLON = "https://app.usepylon.com/issues/"

fails = []
_next_number = [1000]


def check(name, got, want):
    ok = got == want
    print(f"  {'OK ' if ok else 'FAIL'} {name}: got {got!r}, want {want!r}")
    if not ok:
        fails.append(name)


def ticket(tid, assignee, grade="Pass", date=D2, state="closed",
           r3="Pass", r4="Pass", a1="Pass", a2="Neutral", a3="Good",
           a4="Pass", a5="Pass", link=None, deleted=None, ai=True):
    """Seed one ticket with its R- and A-check rows.

    Numbers come from a counter, not hash(), so ordering assertions are stable
    across runs.
    """
    _next_number[0] += 1
    with db.get_conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO tickets (id,number,fetch_date,title,link,"
            "state,assignee_name,fetched_at,deleted_at)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (tid, _next_number[0], date, f"Ticket {tid}",
             PYLON + tid if link is None else link,
             state, assignee, T0, deleted),
        )
        c.execute(
            "INSERT OR REPLACE INTO rule_checks (ticket_id,fetch_date,r1,r2,r3,"
            "r4,r5,r7,r8,checked_at)"
            " VALUES (?,?,'Pass','Pass',?,?,'Pass','Pass','Pass',?)",
            (tid, date, r3, r4, T0),
        )
        if ai:
            c.execute(
                "INSERT OR REPLACE INTO ai_checks (ticket_id,fetch_date,a1,a2,"
                "a3,a4,a5,ai_notes,overall_result,checked_at)"
                " VALUES (?,?,?,?,?,?,?,'n',?,?)",
                (tid, date, a1, a2, a3, a4, a5, grade, T0),
            )


def ids(result):
    return sorted(t["ticket_id"] for t in result["tickets"])


vault.set_raw_setting("qc_rules_json", '{"excluded_states": ["archived"]}', "t")
qc_rules.invalidate()

# Ann: two R3 fails, one clean. Bob: one R3 fail. Nobody owns t-unowned.
ticket("a1", "Ann", grade="Fail", r3="Fail")
ticket("a2", "Ann", grade="Fail", r3="Fail", date=D1)
ticket("a3", "Ann", grade="Pass")
ticket("b1", "Bob", grade="Fail", r3="Fail")
ticket("b2", "Bob", grade="Fail", r4="Fail")
ticket("u1", None, grade="Fail", r3="Fail")
ticket("u2", "   ", grade="Fail", r3="Fail")
# Out of scope in two different ways: withdrawn at source, and excluded state.
ticket("d1", "Ann", grade="Fail", r3="Fail", deleted=T0)
ticket("x1", "Ann", grade="Fail", r3="Fail", state="archived")

print("=== only tickets where the named check failed come back ===")
r = dd.tickets_failing("r3")
check("r3 fails", ids(r), ["a1", "a2", "b1", "u1", "u2"])
check("count matches the list", r["count"], 5)
check("not truncated", r["truncated"], False)
check("check echoed", r["check"], "r3")
check("label names the check", r["check_label"].startswith("R3"), True)
check("a clean ticket is absent", "a3" in ids(r), False)
check("an r4-only failure is absent from r3", "b2" in ids(r), False)
check("r4 finds its own failure", ids(dd.tickets_failing("r4")), ["b2"])
print("   (d1 soft-deleted and x1 archived are covered below)")

print()
print("=== an unknown check raises, and does NOT return everything ===")
for bad in ["r6", "a6", "overall", "1=1", "*", "r3'; --", "", None,
            "r3 OR 1=1", "deleted_at"]:
    try:
        got = dd.tickets_failing(bad)
        check(f"{bad!r} rejected", f"returned {got['count']} tickets", "ValueError")
    except ValueError:
        check(f"{bad!r} rejected", "ValueError", "ValueError")
check("r6 is not drillable (never computed)", "r6" in dd.ALLOWED_CHECKS, False)
check("allowlist is the leaderboard's checks plus A1–A5",
      dd.ALLOWED_CHECKS,
      ("a1", "a2", "a3", "a4", "a5",
       "r1", "r2", "r3", "r4", "r5", "r7", "r8"))
print("   (a silent fallback here would answer a typo with the whole range)")

print()
print("=== soft-deleted and excluded-state tickets are out of scope ===")
check("soft-deleted absent", "d1" in ids(dd.tickets_failing("r3")), False)
check("archived absent", "x1" in ids(dd.tickets_failing("r3")), False)
qc_rules.invalidate()
vault.set_raw_setting("qc_rules_json", '{"excluded_states": []}', "t")
qc_rules.invalidate()
check("archived returns once the state is no longer excluded",
      "x1" in ids(dd.tickets_failing("r3")), True)
check("soft-deleted stays out regardless of rules",
      "d1" in ids(dd.tickets_failing("r3")), False)
vault.set_raw_setting("qc_rules_json", '{"excluded_states": ["archived"]}', "t")
qc_rules.invalidate()

print()
print("=== assignee filtering ===")
check("assignee=None returns every assignee",
      ids(dd.tickets_failing("r3")), ["a1", "a2", "b1", "u1", "u2"])
check("one assignee", ids(dd.tickets_failing("r3", assignee="Ann")),
      ["a1", "a2"])
check("assignee echoed", dd.tickets_failing("r3", assignee="Ann")["assignee"],
      "Ann")
check("Unassigned matches NULL and blank assignee_name",
      ids(dd.tickets_failing("r3", assignee="Unassigned")), ["u1", "u2"])
check("Unassigned is what those rows report as their assignee",
      sorted({t["assignee_name"] for t in
              dd.tickets_failing("r3", assignee="Unassigned")["tickets"]}),
      ["Unassigned"])
check("an unknown assignee returns nothing, not everything",
      dd.tickets_failing("r3", assignee="Nobody")["count"], 0)
check("per-assignee counts sum to the unfiltered count",
      dd.tickets_failing("r3", assignee="Ann")["count"]
      + dd.tickets_failing("r3", assignee="Bob")["count"]
      + dd.tickets_failing("r3", assignee="Unassigned")["count"],
      dd.tickets_failing("r3")["count"])

print()
print("=== the date range filters, and either end may be open ===")
check("D1 only", ids(dd.tickets_failing("r3", start=D1, end=D1)), ["a2"])
check("D2 only", ids(dd.tickets_failing("r3", start=D2, end=D2)),
      ["a1", "b1", "u1", "u2"])
check("D1..D2 is the whole set",
      dd.tickets_failing("r3", start=D1, end=D2)["count"],
      dd.tickets_failing("r3")["count"])
check("open end", ids(dd.tickets_failing("r3", start=D2)),
      ["a1", "b1", "u1", "u2"])
check("open start", ids(dd.tickets_failing("r3", end=D1)), ["a2"])
check("range echoed", dd.tickets_failing("r3", start=D1, end=D2)["range"],
      {"start": D1, "end": D2})
check("a future range is empty",
      dd.tickets_failing("r3", start=D3, end=D3)["count"], 0)
try:
    dd.tickets_failing("r3", start="25-08-2026", end=D2)
    check("a malformed date is rejected", "returned", "ValueError")
except ValueError:
    check("a malformed date is rejected", "ValueError", "ValueError")
check("newest first", [t["fetch_date"] for t in
                       dd.tickets_failing("r3")["tickets"]][-1], D1)

print()
print("=== links are scheme-checked, not just escaped ===")
ticket("j1", "Eve", grade="Fail", r3="Fail", link="javascript:alert(1)")
ticket("j2", "Eve", grade="Fail", r3="Fail", link="https://ok.example/1")
ticket("j3", "Eve", grade="Fail", r3="Fail", link="HTTP://ok.example/2")
ticket("j4", "Eve", grade="Fail", r3="Fail", link=None)
ticket("j5", "Eve", grade="Fail", r3="Fail", link="")
ticket("j6", "Eve", grade="Fail", r3="Fail", link="  data:text/html,x  ")
links = {t["ticket_id"]: t["link"]
         for t in dd.tickets_failing("r3", assignee="Eve")["tickets"]}
check("javascript: link is dropped", links["j1"], None)
check("https link survives", links["j2"], "https://ok.example/1")
check("HTTP:// survives (scheme is case-insensitive)",
      links["j3"], "HTTP://ok.example/2")
check("a real Pylon link survives", links["j4"], PYLON + "j4")
check("empty link is None", links["j5"], None)
check("data: link is dropped", links["j6"], None)
check("safe_link is the guard, reusable by other callers",
      dd.safe_link("javascript:alert(1)"), None)
print("   (a javascript: URL contains nothing an HTML escaper touches)")

print()
print("=== limit caps the page; count and truncated tell the truth ===")
for i in range(6):
    ticket(f"lim{i}", "Fay", grade="Fail", r4="Fail")
total = dd.tickets_failing("r4", assignee="Fay")["count"]
check("all six seeded", total, 6)
r = dd.tickets_failing("r4", assignee="Fay", limit=2)
check("page is capped", len(r["tickets"]), 2)
check("count is the real total, not the page", r["count"], 6)
check("truncated flagged", r["truncated"], True)
r = dd.tickets_failing("r4", assignee="Fay", limit=6)
check("an exact-fit page is not truncated", r["truncated"], False)
check("exact-fit page length", len(r["tickets"]), 6)
r = dd.tickets_failing("r4", assignee="Fay", limit=99)
check("a generous limit returns all six", len(r["tickets"]), 6)
check("and is not truncated", r["truncated"], False)
check("limit is clamped to a page, never to zero rows",
      len(dd.tickets_failing("r4", assignee="Fay", limit=0)["tickets"]), 1)

print()
print("=== A-check semantics come from qc_runner._compute_overall ===")
ticket("q1", "Gil", grade="Fail", a3="Poor")
ticket("q2", "Gil", grade="Pass", a3="Needs Improvement")
ticket("q3", "Gil", grade="Pass", a3="Good")
ticket("q4", "Gil", grade="Fail", a5="Fail")
ticket("q5", "Gil", grade="Needs Review", a5="Needs Review")
ticket("q6", "Gil", grade="Pass", a1="Fail")
ticket("q7", "Gil", grade="Pass", a4="Fail")
ticket("q8", "Gil", grade="Pass", ai=False)
check("A3 'Poor' is a failure", ids(dd.tickets_failing("a3", assignee="Gil")),
      ["q1"])
check("A3 'Needs Improvement' is not",
      "q2" in ids(dd.tickets_failing("a3", assignee="Gil")), False)
check("A5 'Fail' is a failure", ids(dd.tickets_failing("a5", assignee="Gil")),
      ["q4"])
check("A5 'Needs Review' is not",
      "q5" in ids(dd.tickets_failing("a5", assignee="Gil")), False)
check("A1 'Fail'", ids(dd.tickets_failing("a1", assignee="Gil")), ["q6"])
check("A4 'Fail'", ids(dd.tickets_failing("a4", assignee="Gil")), ["q7"])
check("an ungraded ticket appears in no A-check",
      any("q8" in ids(dd.tickets_failing(k, assignee="Gil"))
          for k in ("a1", "a3", "a4", "a5")), False)
# Verified against the backend rather than assumed: a1/a4 'Fail' does not make
# the overall verdict Fail, and this module reports the check, not the verdict.
import qc_runner
r_clean = {k: "Pass" for k in qc_runner.R_CHECK_KEYS}
check("backend: a3 Poor -> Fail",
      qc_runner._compute_overall(r_clean, {"a3": "Poor", "a5": "Pass"}), "Fail")
check("backend: a3 Needs Improvement -> not Fail",
      qc_runner._compute_overall(
          r_clean, {"a1": "Pass", "a3": "Needs Improvement", "a4": "Pass",
                    "a5": "Pass"}) == "Fail", False)
check("backend: a1 Fail does not make the verdict Fail",
      qc_runner._compute_overall(
          r_clean, {"a1": "Fail", "a3": "Good", "a4": "Pass",
                    "a5": "Pass"}) == "Fail", False)
print("   (so an A1 drill-down can list tickets graded Pass overall — the")
print("    check failed, the verdict did not; that is the backend's rule)")

print()
print("=== A2 is sentiment, and is never claimed as a failure ===")
ticket("s1", "Hal", grade="Pass", a2="Frustrated")
ticket("s2", "Hal", grade="Pass", a2="Urgent")
ticket("s3", "Hal", grade="Pass", a2="Concerned")
ticket("s4", "Hal", grade="Pass", a2="Positive")
ticket("s5", "Hal", grade="Pass", a2="Neutral")
r = dd.tickets_failing("a2", assignee="Hal")
check("notable sentiment only", ids(r), ["s1", "s2", "s3"])
check("Positive/Neutral excluded",
      {"s4", "s5"} & set(ids(r)), set())
check("a2 is declared not-a-failure", "a2" in dd.NOT_A_FAILURE, True)
check("no other check is", dd.NOT_A_FAILURE, frozenset({"a2"}))
check("the label carries the caveat", "not a failure" in r["check_label"], True)
check("every A2 match is still graded Pass",
      sorted({t["overall_result"] for t in r["tickets"]}), ["Pass"])
print("   (A2 contributes nothing to the verdict; presenting these as")
print("    failures would invent a rule the backend does not have)")

print()
print("=== the verdict shown is the effective grade, not ai_checks ===")
ticket("e1", "Ivy", grade="Fail", r3="Fail")
before = dd.tickets_failing("r3", assignee="Ivy")["tickets"][0]["overall_result"]
with db.get_conn() as c:
    c.execute(
        "INSERT INTO ticket_reviews (ticket_id,decision,kept_ai,reviewer_email,"
        "reviewer_name,note,reviewed_at) VALUES ('e1','Pass',0,'r@x.com','R','',?)",
        (T0,),
    )
after = dd.tickets_failing("r3", assignee="Ivy")["tickets"][0]["overall_result"]
check("AI verdict before sign-off", before, "Fail")
check("human sign-off wins after", after, "Pass")
check("the ticket is still listed — R3 did fail",
      dd.tickets_failing("r3", assignee="Ivy")["count"], 1)
print("   (a sign-off changes the verdict, not the R-check that failed)")

print()
print("=== the payload shape the dialog reads ===")
t = dd.tickets_failing("r3", assignee="Ann")["tickets"][0]
check("ticket keys", sorted(t),
      ["assignee_name", "fetch_date", "link", "number", "overall_result",
       "state", "ticket_id", "title"])
check("result keys", sorted(dd.tickets_failing("r3")),
      ["assignee", "check", "check_label", "count", "range", "tickets",
       "truncated"])

print()
if fails:
    print(f"FAILURES ({len(fails)}): {fails}")
    raise SystemExit(1)
print("ALL DRILL-DOWN ASSERTIONS PASSED")
