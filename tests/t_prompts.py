"""The grading prompt is editable policy inside a fixed wire contract.

Two invariants matter here and nothing else really does.

First, moving the prompt out of a code literal must not change a single grade.
The extraction was made byte-identical on purpose, and the assembled default is
pinned below so a later "tidy up the wording" commit has to be a deliberate,
visible decision about grading rather than a formatting change.

Second, the grade vocabulary appears in three places — the header on each check,
the JSON return format, and the enum the API enforces — and all three are derived
from `GRADES`. When they were maintained by hand they drifted, and a prompt that
promised a grade the schema rejected surfaced as an empty response three frames
away in the JSON parser. These tests assert the derivation, so an admin editing a
check's prose cannot desynchronise them.
"""
import sys
sys.path.insert(0, "..")

import prompts
import rules
import qc_runner

fails = []


def check(name, got, want):
    ok = got == want
    print(f"  {'OK ' if ok else 'FAIL'} {name}: got {got!r}, want {want!r}")
    if not ok:
        fails.append(name)


def ok(name, cond, detail=""):
    print(f"  {'OK ' if cond else 'FAIL'} {name}{(' — ' + detail) if detail else ''}")
    if not cond:
        fails.append(name)


print("=== the grade vocabulary has exactly one origin ===")
schema_props = prompts.RESPONSE_SCHEMA["items"]["properties"]
for key, grades in prompts.GRADES.items():
    check(f"{key} schema enum matches GRADES",
          schema_props[key]["enum"], list(grades))

body = prompts.system_prompt()
for key, grades in prompts.GRADES.items():
    header = prompts.check_header(key)
    ok(f"{key} header carries every grade", header in body, header)
    # The return-format line is what the model copies; a grade missing there is
    # a grade the model has no way to know it may return.
    fmt = f'"{key}": "{"|".join(grades)}"'
    ok(f"{key} return format lists every grade", fmt in body, fmt)

ok("every grade string appears somewhere in the prompt",
   all(g in body for grades in prompts.GRADES.values() for g in grades))

print()
print("=== the fixed blocks are all present, and are not editable ===")
for title, text in prompts.FIXED_BLOCKS:
    ok(f"fixed block present: {title}", text in body)
for title, _ in prompts.FIXED_BLOCKS:
    ok(f"fixed block is not a rules key: {title}",
       title.lower().replace(" ", "_") not in rules.defaults())

print()
print("=== the assembled default is pinned ===")
# If this changes, grading changed. That is allowed — but it must be the point of
# the commit, not a side effect of one.
check("default prompt length", len(body), 3416)
check("default fingerprint", prompts.fingerprint(), "d79b1cd261d0")
ok("no markdown fence in the default", "```" not in body)
ok("qc_runner assembles the same thing",
   qc_runner._system_prompt(rules.defaults()) == body)

print()
print("=== an override replaces only its own section ===")
draft = dict(rules.defaults())
draft["a3_rubric"] = "HOUSE RULE: only complete sentences count as Good."
out = prompts.system_prompt(draft)
ok("the edit is in the prompt", "HOUSE RULE" in out)
ok("the default it replaced is gone",
   "vague or incomplete but serviceable" not in out)
ok("a neighbouring section is untouched",
   prompts.DEFAULT_SECTIONS["a4_rubric"] in out)
ok("the fixed envelope survives an edit",
   all(text in out for _, text in prompts.FIXED_BLOCKS))
ok("the A3 header still carries the real grades",
   prompts.check_header("a3") in out)

print()
print("=== a blank section means 'use the default', not 'no rubric' ===")
blanked = dict(rules.defaults())
blanked["a1_rubric"] = "   "
ok("blank falls back to the default",
   prompts.system_prompt(blanked) == body,
   "an empty textarea must never send the model an empty check")

print()
print("=== guidance is appended, not substituted ===")
guided = dict(rules.defaults())
guided["a_guidance"] = "Zapier intake tickets are automated; do not penalise them."
g = prompts.system_prompt(guided)
ok("guidance present", "Zapier intake tickets" in g)
ok("rubric still present", prompts.DEFAULT_SECTIONS["a1_rubric"] in g)
ok("guidance comes last", g.rindex("Zapier") > g.rindex("Return format"))
ok("guidance is labelled as admin-set", "set by admins" in g)

print()
print("=== the fingerprint tracks what would change a grade ===")
ok("editing a rubric moves the fingerprint",
   prompts.fingerprint(draft) != prompts.fingerprint())
ok("editing guidance moves the fingerprint",
   prompts.fingerprint(guided) != prompts.fingerprint())
ok("a blank edit does not move it",
   prompts.fingerprint(blanked) == prompts.fingerprint())
t = {"number": 1, "state": "new", "title": "x", "custom_fields": "{}"}
before = qc_runner.qc_fingerprint(t, [], {})
rules.invalidate()
ok("a ticket's fingerprint is stable when nothing changed",
   qc_runner.qc_fingerprint(t, [], {}) == before)

# The point of folding the prompt into the ticket fingerprint: without it an
# admin rewrites what "Poor" means, re-runs the date, and the skip path hands
# back the old grades — the edit appears to do nothing.
real = prompts.fingerprint
try:
    prompts.fingerprint = lambda overrides=None: "different"
    ok("a rubric edit marks a ticket's grade stale",
       qc_runner.qc_fingerprint(t, [], {}) != before)
finally:
    prompts.fingerprint = real
ok("and the fingerprint returns once the prompt is back",
   qc_runner.qc_fingerprint(t, [], {}) == before)

print()
print("=== validation protects the bill and the parser ===")
check("a clean section", prompts.validate_section("a1_rubric", "Be strict."), [])
check("blank is allowed", prompts.validate_section("a1_rubric", ""), [])
ok("an unknown key is rejected",
   prompts.validate_section("a9_rubric", "x") != [])
ok("an over-long section is rejected",
   any("limit is" in e for e in
       prompts.validate_section("a1_rubric", "x" * (prompts.MAX_SECTION_CHARS + 1))))
ok("a markdown fence is rejected",
   any("fence" in e for e in prompts.validate_section("a1_rubric", "```json")),
   "the model is told to return raw JSON; a fence in the rubric invites one back")

print()
print("=== rules owns the sections and validates them ===")
d = rules.defaults()
for key in prompts.SECTION_KEYS:
    ok(f"{key} is a rules key", key in d)
    ok(f"{key} defaults to its real text", d[key] == prompts.DEFAULT_SECTIONS[key])
ok("rules.validate catches a bad section",
   rules.validate({"a1_rubric": "x" * 5000}) != [])
check("rules.validate accepts the defaults", rules.validate(d), [])
ok("every section has a label and help text",
   all(key in prompts.SECTION_LABELS and len(prompts.SECTION_LABELS[key]) == 2
       for key in prompts.SECTION_KEYS))

print()
if fails:
    print(f"FAILURES ({len(fails)}): {fails}")
    raise SystemExit(1)
print("ALL PROMPT ASSERTIONS PASSED")
