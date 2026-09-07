"""Support weekly dashboard: the D contract computed from the ticket store.

Pinned because a wrong week boundary, a deleted ticket leaking into volume, or
an insight that still names last week's top category would silently mislead
the ops review that this tab is for.
"""
import json
from datetime import datetime
from zoneinfo import ZoneInfo

import db
import rules as qc_rules
import weekly

db.init_db()
qc_rules.invalidate()

TZ = ZoneInfo("Asia/Kolkata")
NOW = datetime(2026, 8, 19, 15, 0, tzinfo=TZ)  # Wednesday → current week Mon 17
CURR = "2026-08-17"
PREV_MON = "2026-08-10"

fails = []
_num = [8000]


def check(name, got, want):
    ok = got == want
    print(f"  {'OK ' if ok else 'FAIL'} {name}: got {got!r}, want {want!r}")
    if not ok:
        fails.append(name)


def add(tid, *, created, state="investigating", updated=None, assignee="Ann",
        priority="High", account="Acme", category="Salesforce (SFDC)",
        cat_slug="salesforce_sfdc", extra_cf=None, messages=(),
        deleted=None, link=None, csat=None,
        first_response_seconds=None, resolution_seconds=None,
        bh_first_response_seconds=None):
    _num[0] += 1
    cf = {
        "request_category": {
            "value": cat_slug,
            "interpreted_value": category,
        }
    }
    if extra_cf:
        cf.update(extra_cf)
    acc_id = "acc-" + account.replace(" ", "-").lower()
    with db.get_conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO accounts (id, name, fetched_at) VALUES (?,?,?)",
            (acc_id, account, created),
        )
        c.execute(
            "INSERT OR REPLACE INTO tickets "
            "(id,number,fetch_date,title,link,state,priority,assignee_name,"
            "account_id,custom_fields,created_at,updated_at,deleted_at,"
            "csat_responses,first_response_seconds,resolution_seconds,"
            "business_hours_first_response_seconds)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (tid, _num[0], created[:10], f"Ticket {tid}",
             link or f"https://app.usepylon.com/issues?issueNumber={_num[0]}",
             state, priority, assignee, acc_id, json.dumps(cf),
             created, updated or created, deleted,
             json.dumps(csat) if csat else None,
             first_response_seconds, resolution_seconds,
             bh_first_response_seconds),
        )
        for i, m in enumerate(messages):
            c.execute(
                "INSERT INTO messages (id,ticket_id,message_html,timestamp,"
                "author_name,is_customer,is_private) VALUES (?,?,?,?,?,?,?)",
                (f"{tid}-m{i}", tid, m.get("html", "<p>hi</p>"), m["at"],
                 "Cust" if m.get("customer") else "Ann",
                 1 if m.get("customer") else 0,
                 1 if m.get("private") else 0),
            )
    return _num[0]


def reply_pair(created, hours, *, customer_first=True):
    from datetime import timedelta
    start = datetime.fromisoformat(created)
    first = {
        "at": created,
        "customer": customer_first,
        "html": "<p>please help</p>",
    }
    second = {
        "at": (start + timedelta(hours=hours)).isoformat(),
        "customer": not customer_first,
        "html": "<p>looking into this</p>",
    }
    return [first, second]


add("c1", created="2026-08-18T04:00:00+00:00", state="investigating",
    first_response_seconds=2 * 3600,
    messages=reply_pair("2026-08-18T04:00:00+00:00", 2),
    csat=[{"score": 5, "submitted_at": "2026-08-18T10:00:00+00:00"}])
add("c2", created="2026-08-19T04:00:00+00:00", state="waiting_on_engg",
    category="Integrations", cat_slug="integrations",
    messages=reply_pair("2026-08-19T04:00:00+00:00", 1))
add("c3", created="2026-08-20T04:00:00+00:00", state="waiting_on_you",
    category="Oncall Integration Issues", cat_slug="oncall_integration_issues",
    account="Beta Co",
    messages=reply_pair("2026-08-20T04:00:00+00:00", 1))
n_c4 = add("c4", created="2026-08-21T04:00:00+00:00", state="closed",
           updated="2026-08-21T10:00:00+00:00",
           first_response_seconds=30 * 3600,
           extra_cf={"resolution_category": {"value": "Escalated to Oncall"}},
           messages=reply_pair("2026-08-21T04:00:00+00:00", 30))
add("c5", created="2026-08-18T06:00:00+00:00", state="investigating",
    assignee="Bob", account="Acme",
    messages=reply_pair("2026-08-18T06:00:00+00:00", 3),
    csat=[{"score": 4, "submitted_at": "2026-08-18T12:00:00+00:00"}])

add("p1", created="2026-08-11T04:00:00+00:00", state="closed",
    updated="2026-08-12T04:00:00+00:00",
    messages=reply_pair("2026-08-11T04:00:00+00:00", 2),
    csat=[{"score": 3, "submitted_at": "2026-08-11T16:00:00+00:00"}])
add("p2", created="2026-08-12T04:00:00+00:00", state="closed",
    updated="2026-08-18T08:00:00+00:00",  # resolved in current week (flow)
    messages=reply_pair("2026-08-12T04:00:00+00:00", 2))
add("p3", created="2026-08-13T04:00:00+00:00", state="investigating",
    messages=reply_pair("2026-08-13T04:00:00+00:00", 2))

add("arch", created="2026-08-18T04:00:00+00:00", state="archived",
    messages=reply_pair("2026-08-18T04:00:00+00:00", 1))
add("gone", created="2026-08-18T04:00:00+00:00", state="investigating",
    deleted="2026-08-18T12:00:00+00:00",
    messages=reply_pair("2026-08-18T04:00:00+00:00", 1))

raw = weekly.build(CURR, now=NOW)
M = raw["metrics"]
DD = raw["dailyData"]
D = raw  # labels, allRows, insights, coverage stay top-level


print("=== week bounds ===")
check("week_start is Monday", D["week_start"], CURR)
check("Wednesday snaps to Monday",
      weekly.resolve_week_start("2026-08-19", now=NOW).isoformat(), CURR)
check("future Monday clamps",
      weekly.resolve_week_start("2026-09-07", now=NOW).isoformat(), CURR)
check("prev label mentions Aug 10", "Aug 10" in D["prevWeekLabel"], True)
check("curr label mentions Aug 17", "Aug 17" in D["currWeekLabel"], True)
check("timezone is the schedule tz", D["timezone"], "Asia/Kolkata")

print()
print("=== volume and flow ===")
# current created: c1 c2 c3 c4 c5  (arch/gone out) = 5
# previous created: p1 p2 p3 = 3
check("cv_total", M["cv_total"], 5)
check("pv_total", M["pv_total"], 3)
check("total_diff", M["total_diff"], 2)
# current open among created this week: c1, c2, c3, c5 (c4 closed) = 4
check("cv_open", M["cv_open"], 4)
# previous open among created last week: p3 only
check("pv_open", M["pv_open"], 1)
# flow resolved: c4 (closed Mon 21) + p2 (updated Aug 18) = 2 current
check("cv_resolved includes prior-week close", M["cv_resolved"], 2)
check("pv_resolved", M["pv_resolved"], 1)

print()
print("=== escalation + SLA ===")
# c2 eng-wait, c3 oncall category, c4 resolution escalat → 3
check("cv_esc", M["cv_esc"], 3)
check("pv_esc", M["pv_esc"], 0)
# c4 FRT 30h > 24h SLA
check("at least one current SLA breach", M["cv_sla_breaches"] >= 1, True)
slow = [r for r in D["allRows"]
        if r["week"] == "Current Week" and r["frt_secs"] and r["frt_secs"] >= 30 * 3600]
check("30h FRT is an SLA breach", slow and slow[0]["sla_breached"], True)
fast = [r for r in D["allRows"]
        if r["week"] == "Current Week" and r["frt_secs"] and 1.5 * 3600 <= r["frt_secs"] <= 2.5 * 3600]
check("2h FRT is recorded", len(fast) >= 1, True)
check("2h FRT is inside SLA", fast[0]["sla_breached"], False)

print()
print("=== exclusions ===")
weeks = {r["week"] for r in D["allRows"]}
check("both week labels present", weeks, {"Previous Week", "Current Week"})
check("allRows is created-in-either-week only",
      M["cv_total"] + M["pv_total"], len(D["allRows"]))
check("archived absent",
      any("arch" in str(r["pylon_link"]) for r in D["allRows"]), False)
check("deleted absent",
      any("gone" in str(r.get("issue")) for r in D["allRows"]), False)

print()
print("=== breakdowns ===")
check("priority labels fixed", D["priorities"]["labels"],
      ["Urgent", "High", "Medium", "Low", "Unknown"])
check("High is the current-week priority",
      D["priorities"]["curr"][D["priorities"]["labels"].index("High")], 5)
check("Salesforce leads categories",
      D["categories"]["labels"][0], "Salesforce (SFDC)")
check("Acme is a customer", "Acme" in D["customers"]["labels"], True)
# Pylon select fields can store the slug only in `values`. Dropping that
# is how a week of real tickets became Unknown on the category chart.
check("values-only slug is read",
      weekly._cf({"request_category": {"values": ["how_to"]}}, "request_category"),
      "how_to")
check("values-only is not Unknown",
      weekly._canon_category(
          weekly._cf({"request_category": {"values": ["how_to"]}},
                     "request_category")),
      "How To")
check("status chart has Closed", "Closed" in M["cv_status"], True)
check("Waiting on Engg counted", M["cv_status"]["Waiting on Engg"], 1)

print()
print("=== agents ===")
names = D["agents"]["names"]
check("Ann and Bob present", set(names) >= {"Ann", "Bob"}, True)
ann = next(a for a in D["agentTable"] if a["agent"] == "Ann")
bob = next(a for a in D["agentTable"] if a["agent"] == "Bob")
check("Bob assigned 1 this week", bob["cv_assigned"], 1)
# Ann's current FRTs are 2h and 30h. Linear interpolation invented 23h;
# Pylon picks the real 30h ticket (nearest-rank).
check("Ann p75 is the slower real ticket, not an interpolated 23h",
      ann["cv_frt_p75"], 30 * 3600)
check("Ann p90 is the slower real ticket",
      ann["cv_frt_p90"], 30 * 3600)
ann_i = names.index("Ann")
check("Agent Performance FRT is P75 minutes",
      D["agents"]["cv_frt"][ann_i], 30 * 60)
check("CSAT current total", D["csatCurr"]["total"], 2)
check("CSAT current avg", D["csatCurr"]["avg"], 4.5)
check("CSAT previous total", D["csatPrev"]["total"], 1)
check("CSAT path is wired", D["coverage"]["csat"], True)
check("Bob CSAT is 4",
      next(a for a in D["agentTable"] if a["agent"] == "Bob")["cv_csat_avg"], 4.0)
check("reopen is zero and flagged unavailable",
      (M["cv_reopen"], D["coverage"]["reopen"]), (0, False))

print()
print("=== insights are derived ===")
src = open(weekly.__file__).read()
check("source does not hardcode Salesforce copy",
      "Salesforce (SFDC) remains" not in src, True)
check("insights exist", len(D["insights"]) >= 1, True)
joined = " ".join(i["body"] for i in D["insights"])
check("insights mention the actual top category",
      "Salesforce (SFDC)" in joined, True)

print()
print("=== daily series length ===")
for key in ("currDays", "cv_created", "pv_created", "cv_frt_mins", "cv_res_hrs"):
    check(f"{key} has 7 entries", len(DD[key]), 7)
check("weekday labels", DD["currDays"],
      ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"])
check("daily resolved does not overwrite KPI scalar",
      isinstance(M["cv_resolved"], int) and isinstance(DD["cv_resolved"], list), True)

print()
print("=== CSAT store from survey rows ===")
stored = weekly.store_csat_responses([
    {
        "id": "surv-orphan",
        "submitted_at": "2026-08-19T08:00:00+00:00",
        "answers": [{"question_type": "", "value": "5"}],
    },
    {
        "id": "surv-c1",
        "issue_id": "c1",
        "submitted_at": "2026-08-19T09:00:00+00:00",
        "answers": [{"value": "4"}],
    },
])
check("survey rows persist even without issue_id", stored >= 2, True)
again = weekly.build(CURR, now=NOW)
check("orphan survey score is counted", again["csatCurr"]["total"] >= 3, True)

print()
print("=== CSAT period is submitted_at, not ticket created ===")
before = again["csatCurr"]["total"]
add("old_july", created="2026-07-01T04:00:00+00:00", state="closed",
    updated="2026-07-02T04:00:00+00:00",
    csat=[{"score": 5, "submitted_at": "2026-08-19T11:00:00+00:00"}])
dated = weekly.build(CURR, now=NOW)
check("old ticket, submitted this week, counts now",
      dated["csatCurr"]["total"], before + 1)
check("that response is not in the previous week",
      dated["csatPrev"]["total"], again["csatPrev"]["total"])
add("created_only_csat", created="2026-07-03T08:00:00+00:00",
    updated="2026-07-03T09:00:00+00:00",
    csat=[{"score": 1, "created_at": "2026-08-18T09:00:00+00:00"}])
no_created = weekly.build(CURR, now=NOW)
check("score without submitted_at is not bucketed by created",
      no_created["csatCurr"]["total"], dated["csatCurr"]["total"])
custom = weekly.build(start="2026-08-18", end="2026-08-20", now=NOW)
check("custom From/To uses submitted dates",
      custom["csatCurr"]["total"] >= dated["csatCurr"]["total"], True)

print()
print("=== CSAT agents come from the ticket, not Unassigned ===")
add("acct_t", created="2026-08-10T04:00:00+00:00", state="closed",
    updated="2026-08-12T04:00:00+00:00", assignee="Chitra", account="Gamma Co")
weekly.store_csat_responses([{
    "id": "surv-gamma",
    "account_id": "acc-gamma-co",
    "submitted_at": "2026-08-19T12:00:00+00:00",
    "answers": [{"question_type": "score", "value": "5"}],
}])
named = weekly.build(CURR, now=NOW)
curr_names = [a["name"] for a in named["csatCurr"]["agents"]]
check("account-matched survey names the agent", "Chitra" in curr_names, True)
check("Unassigned is not a CSAT agent", "Unassigned" not in curr_names, True)
check("blank is not a CSAT agent", "" not in curr_names, True)
check("Chitra has the matched score",
      next(a for a in named["csatCurr"]["agents"] if a["name"] == "Chitra")["total"] >= 1,
      True)

print()
print("=== custom dates ===")
P = weekly.build(start="2026-08-18", end="2026-08-20", now=NOW)
check("custom period_start", P["period_start"], "2026-08-18")
check("custom period_end", P["period_end"], "2026-08-20")
check("custom current created (c1 c2 c3 c5)", P["metrics"]["cv_total"], 4)
check("custom daily length is 3", len(P["dailyData"]["cv_created"]), 3)
check("previous is the 3 days before", P["period_start"] > "2026-08-14", True)
try:
    weekly.build(start="2026-08-01", end="2026-09-05", now=NOW)
    check("32-day range rejected", False, True)
except ValueError:
    check("32-day range rejected", True, True)
try:
    weekly.build(start="2026-08-20", end="2026-08-18", now=NOW)
    check("reversed range rejected", False, True)
except ValueError:
    check("reversed range rejected", True, True)
try:
    weekly.build(start="2026-08-25", end="2026-08-27", now=NOW)
    check("future start rejected", False, True)
except ValueError:
    check("future start rejected", True, True)

print()
print("=== Pylon clocks are the only clocks ===")
# Wall-clock and business-hours live in separate columns: they are different
# clocks (weekend ticket: 4011s wall, 0s business) and a coalesced value
# cannot be split apart afterwards.
with db.get_conn() as c:
    _cols = {r["name"] for r in c.execute("PRAGMA table_info(tickets)")}
check("business-hours clocks have their own columns",
      {"business_hours_first_response_seconds",
       "business_hours_resolution_seconds"} <= _cols, True)
check("negative duration is not a clock",
      weekly.pylon_duration_seconds(
          {"first_response_seconds": -5}, "first_response_seconds"),
      None)
check("numeric-string duration parses",
      weekly.pylon_duration_seconds(
          {"first_response_seconds": "120.4"}, "first_response_seconds"),
      120)
# Slack-style: a support-side line at create, customer 1m later, real reply
# hours later. created → first support used to report 1 minute.
n_pylon = add(
    "frt_pylon", created="2026-08-18T07:00:00+00:00",
    first_response_seconds=4 * 3600, bh_first_response_seconds=0,
    account="FRT Pylon Co",
    messages=[
        {"at": "2026-08-18T07:00:00+00:00", "customer": False,
         "html": "<p>thread opened</p>"},
        {"at": "2026-08-18T07:01:00+00:00", "customer": True,
         "html": "<p>please help</p>"},
        {"at": "2026-08-18T11:00:00+00:00", "customer": False,
         "html": "<p>on it</p>"},
    ])
n_recon = add(
    "frt_recon", created="2026-08-18T07:05:00+00:00",
    account="FRT Recon Co",
    messages=[
        {"at": "2026-08-18T07:05:00+00:00", "customer": False,
         "html": "<p>thread opened</p>"},
        {"at": "2026-08-18T07:06:00+00:00", "customer": True,
         "html": "<p>please help</p>"},
        {"at": "2026-08-18T10:06:00+00:00", "customer": False,
         "html": "<p>on it</p>"},
    ])
n_none = add(
    "frt_none", created="2026-08-18T07:10:00+00:00",
    account="FRT None Co",
    messages=[
        {"at": "2026-08-18T07:10:00+00:00", "customer": False,
         "html": "<p>thread opened</p>"},
    ])
n_res = add(
    "res_pylon", created="2026-08-18T07:15:00+00:00", state="closed",
    updated="2026-08-18T13:15:00+00:00",
    resolution_seconds=2 * 3600, account="Res Pylon Co",
    messages=reply_pair("2026-08-18T07:15:00+00:00", 1))
clocked = weekly.build(CURR, now=NOW)
by_n = {r["issue"]: r for r in clocked["allRows"]}
check("Pylon first_response_seconds is the FRT",
      by_n[n_pylon]["frt_secs"], 4 * 3600)
# Both clocks ride along per row so wall vs business hours can be compared
# against whichever Pylon report the team reads; 0 is a value (a weekend
# ticket answered before business hours resume), not absence.
check("business-hours clock rides along, 0 kept as 0",
      by_n[n_pylon]["bh_frt_secs"], 0)
# Reconstruction from messages counted SpotAssist / chat auto-replies as first
# responses (6-second FRTs in production while the issue page showed 39 min),
# so no stored clock means no FRT — never an invented one.
check("no Pylon clock means no reconstructed FRT",
      by_n[n_recon]["frt_secs"], None)
check("no messages and no Pylon field means no invented FRT",
      by_n[n_none]["frt_secs"], None)
check("Pylon resolution_seconds is the resolution time",
      by_n[n_res]["res_secs"], 2 * 3600)
# updated_at − created_at is a wall-clock span; Pylon's resolution clock
# pauses on hold/waiting (2.6h vs 52h on a real ticket). Closed without a
# stored clock reports no duration rather than the wrong one.
check("closed without Pylon resolution clock reports no duration",
      by_n[n_c4]["res_secs"], None)

print()
print("=== Pylon nearest-rank percentiles ===")
# Shreekaran-shaped week: interpolation sat between 627s and 1009s (722s)
# while Pylon reported the real 627s / 3915s tickets.
_shree = [34, 38, 56, 56, 113, 627, 1009, 3915]
check("p75 is a real observation", weekly._pctile(_shree, 75, min_n=2), 627)
check("p90 is a real observation", weekly._pctile(_shree, 90, min_n=2), 3915)
_mohd = [45, 54, 369, 458, 599, 1140, 1854]
check("p75 does not sit between two tickets",
      weekly._pctile(_mohd, 75, min_n=2), 1140)
check("n=1 is the only sample", weekly._pctile([674], 75), 674)
check("too few samples is null", weekly._pctile([674], 75, min_n=2), None)

print()
if fails:
    print(f"FAILURES ({len(fails)}): {fails}")
    raise SystemExit(1)
print("ALL WEEKLY ASSERTIONS PASSED")
