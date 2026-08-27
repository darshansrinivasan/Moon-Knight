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
if fails:
    print(f"FAILURES ({len(fails)}): {fails}")
    raise SystemExit(1)
print("ALL SCORER ASSERTIONS PASSED")
