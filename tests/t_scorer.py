"""R-check behaviour: the cases the fixes were meant to change, and the ones
they must NOT change."""
from datetime import datetime, timedelta, timezone

import scorer

NOW = datetime.now(timezone.utc)


def cust(text, when, private=False):
    return {"message_html": f"<p>{text}</p>",
            "timestamp": when.isoformat(),
            "author": {"contact": {"email": "c@x.com"}},
            "is_private": 1 if private else 0}


def supp(text, when, private=False):
    return {"message_html": f"<p>{text}</p>",
            "timestamp": when.isoformat(),
            "author": {"user": {"id": "u1", "email": "s@spotdraft.com"}},
            "is_private": 1 if private else 0}


fails = []


def check(name, got, want):
    ok = got == want
    print(f"  {'OK ' if ok else 'FAIL'} {name}: got {got!r}, want {want!r}")
    if not ok:
        fails.append(name)


print("=== _parse_ts no longer raises (was: rolled back the whole day) ===")
for bad in [None, "", "garbage", "2026-13-45T99:99:99Z"]:
    got = scorer._parse_ts(bad)
    check(f"_parse_ts({bad!r})", got, None)
naive = scorer._parse_ts("2026-08-26 10:00:00")
check("naive gets UTC tzinfo", naive.tzinfo is not None, True)

print()
print("=== R4 mixed offsets: must pick the true latest instant ===")
# 09:00Z is LATER than 10:00+05:30 (which is 04:30Z).
early_by_clock = supp("support replied", NOW - timedelta(hours=50))
msgs = [
    {"message_html": "<p>customer asks</p>",
     "timestamp": "2026-08-26T10:00:00+05:30",   # 04:30Z
     "author": {"contact": {"email": "c@x.com"}}, "is_private": 0},
    {"message_html": "<p>support answered</p>",
     "timestamp": "2026-08-26T09:00:00Z",        # 09:00Z  <- actually latest
     "author": {"user": {"id": "u1"}}, "is_private": 0},
]
# Latest is support -> Pass. String-max would have picked the customer msg.
check("R4 picks latest by instant", scorer.r4({"state": "waiting_on_you"}, msgs), "Pass")

print()
print("=== R4 naive timestamp must not crash ===")
try:
    got = scorer.r4({"state": "waiting_on_you"},
                    [{"message_html": "<p>help</p>",
                      "timestamp": "2026-08-26 10:00:00",
                      "author": {"contact": {"email": "c@x.com"}},
                      "is_private": 0}])
    check("R4 with naive ts", got in ("Pass", "Fail"), True)
except Exception as e:
    check("R4 with naive ts", f"raised {type(e).__name__}", "no exception")
    fails.append("R4 naive")

print()
print("=== R4 core SLA behaviour preserved ===")
check("closed -> Pass", scorer.r4({"state": "closed"}, []), "Pass")
check("waiting_on_customer -> Pass",
      scorer.r4({"state": "waiting_on_customer"}, []), "Pass")
check("no dated messages -> N/A", scorer.r4({"state": "waiting_on_you"}, []), "N/A")
check("customer waiting 50h -> Fail",
      scorer.r4({"state": "waiting_on_you"}, [cust("help", NOW - timedelta(hours=50))]),
      "Fail")
check("customer waiting 2h -> Pass",
      scorer.r4({"state": "waiting_on_you"}, [cust("help", NOW - timedelta(hours=2))]),
      "Pass")
check("support answered last -> Pass",
      scorer.r4({"state": "waiting_on_you"},
                [cust("help", NOW - timedelta(hours=50)),
                 supp("done", NOW - timedelta(hours=1))]),
      "Pass")

print()
print("=== _is_bot_ack: real customer text must survive ===")
real = {"message_html":
        "<p>Hi, regarding ticket id 12345 the export still fails for us</p>"}
check("real customer msg kept", scorer._is_bot_ack(real), False)
bot = {"message_html": "<p>Your ticket id is 12345</p>"}
check("bot ack still dropped", scorer._is_bot_ack(bot), True)
bot2 = {"message_html": "<p>Ticket #98765</p>"}
check("bare ticket ref dropped", scorer._is_bot_ack(bot2), True)
long = {"message_html": "<p>ticket id 1 </p>" + "<p>" + ("word " * 300) + "</p>"}
check("long msg kept", scorer._is_bot_ack(long), False)

print()
print("=== the dropped-customer-message consequence is fixed ===")
# Before: this customer message looked like a bot ack, was filtered out, and
# R4 then said "no substantive messages" -> N/A instead of Fail.
msgs = [cust("regarding ticket id 555 our integration is still broken",
             NOW - timedelta(hours=60))]
check("overdue customer msg now scored", scorer.r4({"state": "waiting_on_you"}, msgs),
      "Fail")

print()
print("=== R3 unchanged for genuine cases ===")
iss = {"account": {"id": "a1"}}
check("real customer account", scorer.r3(iss, {"id": "a1", "name": "Acme", "type": "customer"}), "Pass")
check("internal type", scorer.r3(iss, {"id": "a1", "name": "Acme", "type": "internal"}), "Fail")
check("invalid name fragment", scorer.r3(iss, {"id": "a1", "name": "Live Chat", "type": "customer"}), "Fail")
check("no account id", scorer.r3({}, None), "Fail")

print()
print("=== R10 (advisory): SpotAssist trigger on Slack tickets ===")
# Fetch-time shape: Pylon API messages with author dicts.
BOT   = {"author": {"user": {"email": "b@x"}, "name": "SpotAssist"}}
REP   = {"author": {"user": {"email": "r@x"}, "name": "Ram Rep"}}
CUST  = {"author": {"contact": {"email": "c@x"}, "name": "Cus Tomer"}}
NOTE  = {"author": {"user": {"email": "r@x"}, "name": "Ram Rep"}, "is_private": True}
SLACK = {"source": "slack"}

check("SpotAssist engaged -> Pass", scorer.r10(SLACK, [CUST, BOT, REP]), "Pass")
check("rep answered by hand -> Fail", scorer.r10(SLACK, [CUST, REP]), "Fail")
check("no rep reply yet -> N/A", scorer.r10(SLACK, [CUST]), "N/A")
check("internal note is not a reply", scorer.r10(SLACK, [CUST, NOTE]), "N/A")
check("no customer message -> N/A (internal thread)",
      scorer.r10(SLACK, [REP, REP]), "N/A")
check("email tickets auto-engage, nothing to check",
      scorer.r10({"source": "email"}, [CUST, REP]), "N/A")

# Resync-time shape: stored rows with author_name / is_customer columns.
check("stored-row shape scores identically",
      scorer.r10(SLACK, [
          {"author_name": "Cus Tomer", "is_customer": 1, "is_private": 0},
          {"author_name": "Ram Rep",   "is_customer": 0, "is_private": 0},
      ]), "Fail")

# The bot name is configuration, not a literal: rename it and the check follows.
import json

import db as _db
import rules as qc_rules
import vault
_db.init_db()
vault.set_raw_setting("qc_rules_json",
                      json.dumps({"spotassist_author": "HelperBot"}), "t")
qc_rules.invalidate()
check("renamed agent is honoured",
      scorer.r10(SLACK, [CUST, {"author": {"user": {}, "name": "HelperBot"}}]),
      "Pass")
check("the old name no longer counts",
      scorer.r10(SLACK, [CUST, BOT, REP]), "Fail")
vault.set_raw_setting("qc_rules_json", "{}", "t")
qc_rules.invalidate()

print()
print("=== R11 follow-through: support went silent after taking the last word ===")
# Deterministic weekend math: pin the workspace tz to UTC for the test.
vault.set_settings({"schedule_tz": "UTC"}, "t")

INV = {"state": "investigating"}


def at(iso):
    return datetime.fromisoformat(iso).replace(tzinfo=timezone.utc)


def pub(iso, customer):
    return {"timestamp": iso, "is_customer": 1 if customer else 0,
            "is_private": 0, "author_name": "X"}


# Tue 09:00 → Thu 15:00 = 54 raw hours, no weekend involved.
check("silent past the 24h limit -> Fail",
      scorer.r11(INV, [pub("2026-09-01T09:00:00", True),
                       pub("2026-09-01T10:00:00", False)],
                 now=at("2026-09-03T15:00:00")), "Fail")
# Fri 18:00 → Mon 10:00 is 64 raw hours but only 16 working hours:
# Sat+Sun contribute nothing, so the weekend must not fail anyone.
check("weekend hours do not count",
      scorer.r11(INV, [pub("2026-09-04T09:00:00", True),
                       pub("2026-09-04T18:00:00", False)],   # Friday
                 now=at("2026-09-07T10:00:00")), "Pass")     # Monday
check("customer has the last word -> N/A (that is R4's clock)",
      scorer.r11(INV, [pub("2026-09-01T10:00:00", False),
                       pub("2026-09-01T11:00:00", True)],
                 now=at("2026-09-03T15:00:00")), "N/A")
check("customer-owned state -> N/A",
      scorer.r11({"state": "waiting_on_customer"},
                 [pub("2026-09-01T10:00:00", False)],
                 now=at("2026-09-03T15:00:00")), "N/A")
# A private note is invisible to the customer, so it must not reset the clock.
late_note = {"timestamp": "2026-09-03T09:00:00", "is_customer": 0,
             "is_private": 1, "author_name": "X"}
check("an internal note does not reset the silence clock",
      scorer.r11(INV, [pub("2026-09-01T09:00:00", True),
                       pub("2026-09-01T10:00:00", False), late_note],
                 now=at("2026-09-03T15:00:00")), "Fail")
check("no public message at all -> N/A",
      scorer.r11(INV, [late_note], now=at("2026-09-03T15:00:00")), "N/A")

# Threshold and states are configuration, not literals.
vault.set_raw_setting("qc_rules_json",
                      json.dumps({"r11_update_hours": 100,
                                  "r11_states": ["on_hold"]}), "t")
qc_rules.invalidate()
check("a raised limit passes the same silence",
      scorer.r11({"state": "on_hold"},
                 [pub("2026-09-01T09:00:00", True),
                  pub("2026-09-01T10:00:00", False)],
                 now=at("2026-09-03T15:00:00")), "Pass")
check("a state removed from the list stops being judged",
      scorer.r11(INV, [pub("2026-09-01T09:00:00", True),
                       pub("2026-09-01T10:00:00", False)],
                 now=at("2026-09-03T15:00:00")), "N/A")
vault.set_raw_setting("qc_rules_json", "{}", "t")
qc_rules.invalidate()

print()
print("=== R11 grades tickets; the fingerprint still never moves ===")
_t = {"state": "closed", "title": "x", "custom_fields": "{}"}
check("r11 Fail fails the overall (unlike advisory r10)",
      __import__("qc_runner")._compute_overall(
          {"r1": "Pass", "r11": "Fail"}, {"a1": "Good"}), "Fail")
check("r11 is in the graded key set",
      "r11" in __import__("qc_runner").R_CHECK_KEYS, True)
check("fingerprint ignores r11 — silence must not re-bill a prompt",
      __import__("qc_runner").qc_fingerprint(_t, [], {"r1": "Pass"}),
      __import__("qc_runner").qc_fingerprint(_t, [],
                                             {"r1": "Pass", "r11": "Fail"}))

print()
print("=== R10 is advisory: it can never flip a grade or a fingerprint ===")
import qc_runner
check("r10 stays out of the overall verdict",
      qc_runner._compute_overall(
          {"r1": "Pass", "r10": "Fail"}, {"a1": "Good"}), "Pass")
check("r10 is not in the graded key set",
      "r10" in qc_runner.R_CHECK_KEYS, False)
check("r10 rides the bookkeeping set instead",
      "r10" in qc_runner.ALL_CHECK_KEYS, True)
# Adding r10 to a stored row must not change its fingerprint — a changed
# fingerprint re-bills the ticket on the next run for a check the model
# never reads.
_t = {"state": "closed", "title": "x", "custom_fields": "{}"}
check("fingerprint ignores r10",
      qc_runner.qc_fingerprint(_t, [], {"r1": "Pass"}),
      qc_runner.qc_fingerprint(_t, [], {"r1": "Pass", "r10": "Fail"}))

print()
if fails:
    print(f"FAILURES ({len(fails)}): {fails}")
    raise SystemExit(1)
print("ALL SCORER ASSERTIONS PASSED")
