# SPEC v6 — Core QC engine: rules and overall-result upgrades

Status: **proposed**. Written 2026-09-07 after indexing `/Users/anusreerajls/Documents/qc`
against this repo. Not built.

This spec is only about the **core scoring engine** — `scorer.py`, `qc_runner.py`,
`rules.py`, `prompts.py`, `evidence.py`, `resync_overall.py`, and the
`_compute_overall` contract. Auth, Slack delivery, the dashboard redesign, and
funcheck stay out.

---

## What was indexed

`/Users/anusreerajls/Documents/qc` is the original single-user QC engine:
fetch → deterministic R-checks → Gemini A-checks → SQLite dashboard. Roughly
2,500 lines. No rules document, no fingerprints, no dry-run, no Vertex, no
multi-user.

Moon-Knight already absorbed almost all of that scoring logic, then went further
(SPEC v2 platform, SPEC v4/v5 configurability). The useful output of the
comparison is therefore **not** "port qc into Moon-Knight". It is the short
list of engine behaviours qc invented, or left unfinished, that this repo
still does not do — plus the overall-result holes both codebases share.

### Already in Moon-Knight (do not rebuild)

| qc practice | Where it lives here |
|---|---|
| Deterministic R on fetch, AI A on demand | `app.py` fetch loop + `qc_runner.run_qc_date` |
| R1–R5, R7, R8; R9 merged into R8 | `scorer.score_all` |
| Slack user/group rosters + live thread fetch | `scorer._fetch_slack_thread`, `rules.id_set` |
| Dual mention parsing (Pylon HTML + Slack `<@U>` / `<!subteam^>`) | `scorer._mentioned_*`, `_slack_text_user_ids` |
| R5 three-tier eng evidence (thread → Slack → Rootly/Jira) | `r5` + configurable `r5_eng_sources` |
| Bot-ack filter so R4 ignores auto "ticket id is N" | `_is_bot_ack` — **stricter here** than qc (filler-word test) |
| Internal-ticket A3 calibration | `prompts.a3_rubric` + `_build_ticket_block` flag |
| Actionable R-notes prepended to AI notes | `_r_check_notes` + `resync_overall` |
| Model cascade + batch→solo fallback | `qc_runner` (plus retry/backoff this repo added) |
| Stale QC + post-refetch overall resync | fingerprint skip path + `resync_overall.run` |

Moon-Knight extras qc does not have, and this spec does not reopen: status
matrix, field maps, disabled-check mask, R8 condition checklist, prompt
sections, dual dry-run, evidence reconstruction, suggestions, open-backlog QC,
Vertex pinned generation.

### What qc still teaches

Three unfinished ideas in qc, plus two semantic differences, plus two overall
holes both engines left on the table. Those are the changes below.

---

## The gaps, named

**G1 — R6 is written and never scored.** Both repos define `scorer.r6`
(priority must be set) and both omit it from `score_all`. The DB column
`rule_checks.r6` and the ticket `priority` field are already populated on
fetch. SPEC v4 and v5 explicitly left this dead. The function is not dead
because it is wrong; it is dead because nobody decided to turn it on.

**G2 — Internal tickets are only half-calibrated.** The AI layer knows a
manual / `customer_portal_visible=0` ticket is a colleague Slack mirror
(A3 must not demand a formal email). The R layer does not. R3 still fails
those tickets for "internal account", which is the correct account for an
internal ticket. Reviewers then override R3. That is the engine lying.

**G3 — R4's exempt grade is Pass here, N/A in qc.** When no reply is owed
(`closed`, `archived`, `waiting_on_customer`), qc returns `N/A` and this
repo returns `Pass` (`r4_reply_owed == False` short-circuit). Pass inflates
the pass rate and the leaderboard. N/A is the honest reading: the clock
did not run.

**G4 — The overall formula is policy hardcoded as code.**
`_compute_overall` fails a ticket on any enabled R Fail, A3=Poor, or
A5=Fail. A1=Fail and A4=Fail are notes only. That is a product decision
both engines copied, and it is not editable. Category-wrong (A1 Fail) is
a real QC miss; status-wrong (A4 Fail) is the qualitative twin of R5.
Neither can become a Fail driver without a deploy.

**G5 — Closed tickets are not required to record a resolution.** Both
skill docs list `resolution_category` and `resolution_details` as custom
fields. R8 reads the former only as an oncall trigger. Nothing requires
either field when the ticket is closed. A5 judges premature closure in
prose; it cannot fail an empty resolution form.

**G6 — Slack evidence is ephemeral.** R5 may Pass because
`_fetch_slack_thread` found an engineer in the linked thread. That text
is not stored. `evidence.py` then has to say "passed on evidence not
recorded at scoring time". A later dry-run cannot reproduce the Pass
without another live Slack call, which it is forbidden to make.

**G7 — The prompt and the fingerprint disagree about R8.**
`qc_fingerprint` hashes R8 and R9. `_build_ticket_block` prints only
R1–R5 and R7. SPEC v5 D1 documented this and deferred the fix because
trimming keys would invalidate every stored fingerprint at once. It is
still true. A disabled-R8 flip, or an R8 verdict change, can rebill a
date for grades the model never sees.

---

## Decisions (binding)

**D1 — New checks default off.** Wiring R6, adding a resolution check, or
changing R4's exempt grade must not move a single stored overall on
deploy. Each lands as a rules setting seeded to today's behaviour.
Turning it on is an admin save, which already runs `resync_overall` for
free.

**D2 — Internal tickets skip R3, and only R3.** Detection stays the
existing one: `customer_portal_visible == 0` OR `source == "manual"`.
R1, R2, R4, R5, R7, R8 keep applying — an internal escalation still
needs a Jira link. R4's SLA may later get its own exemption; that is a
separate decision and is out of this spec.

**D3 — R4's exempt grade becomes a status-policy attribute, seeded to
Pass.** The matrix already has `r4_reply_owed`. Add `r4_exempt_grade`
(`Pass` | `N/A`), default `Pass`, so today's leaderboard does not move.
Flipping a status to `N/A` is the qc-faithful reading and is a dry-run
away from being safe.

**D4 — Overall fail-drivers become a rules list, seeded to today's
set.** The authority stays `_compute_overall`. The inputs it treats as
Fail become `overall_fail_on`, a list of tokens:

```
r*          any enabled R-check Fail
a3:Poor
a5:Fail
```

Optional tokens an admin may add later, not seeded:

```
a1:Fail
a4:Fail
a3:Needs Improvement
```

Needs-Review drivers stay as they are (`a1`/`a3`/`a4`/`a5` == Needs
Review) unless listed under a parallel `overall_review_on`. A2 never
appears in either list. Validation rejects an empty Fail list — an
overall that cannot fail is not a QC engine.

**D5 — A closed-ticket resolution check is a new toggleable R-check,
not a mutation of A5.** A5 is qualitative ("was the ask still open?").
The form-completeness question ("did anyone fill resolution_category /
resolution_details?") is deterministic and belongs next to R1/R2. Call
it **R10** so R6 can keep the number the function already has. Default
off. Applies only when `status_policy(state).r4_reply_owed` is false
*and* the status is a terminal one (`closed`; `archived` stays
fetch-skipped). Open tickets → N/A.

**D6 — Slack thread text is snapshotted onto `rule_checks` at score
time.** A nullable `r5_slack_excerpt` (truncated, say 4k chars) plus
`r5_slack_fetched_at`. `evidence.py` reads the excerpt. The dry-run
already refuses live Slack; with a snapshot it can replay the Pass
instead of reporting UNKNOWN. Tokens and URLs are not stored — only
the fetched message text the check already used.

**D7 — Fix the fingerprint by printing what we hash, not by silently
dropping keys.** Add R8 (and R6/R10 once enabled) to the R-checks line
in `_build_ticket_block`. Keep r9 out of both the print and, on the
next fingerprint version bump, the hash. Version the payload
(`PROMPT_VERSION` is already `"v2"`) so the change is an explicit
stale-mark, not a surprise rebill the first time someone re-runs a
date. Document the one-time cost on the Runs page.

**D8 — Structured fail reasons are in scope; a second notes language
is not.** Today `_r_check_notes` concatenates prose into `ai_notes`.
Store the same reasons as JSON on `rule_checks.reasons` (one object,
keys = check ids, values = the sentence). `resync_overall` already
rebuilds the prose; it should write both. Slack and the dashboard keep
reading `ai_notes`. The JSON is for evidence, suggestions, and a
future per-check drill-down that does not have to regex a pipe string.

**D9 — Phase 3 of SPEC v5 (per-check evidence cascade) stays deferred
except where this spec already touches it.** `r5_eng_sources` exists.
R6, R10, and the overall list do not need a cascade. Do not expand the
Rules page into a source-ordering editor in this round.

**D10 — `score_all` grows only by calling functions it already has, or
new functions that follow the same shape.** `r6` is added to the
returned dict. `r10` is added the same way. `r9` stays, always N/A,
until a later cleanup spec drops it from `R_CHECK_KEYS` (still deferred
per SPEC v5 D3). `TOGGLEABLE_CHECKS` becomes
`("r1","r2","r3","r4","r5","r6","r7","r8","r10")`. `DEAD_CHECKS`
becomes `("r9",)` only.

---

## Changes

Each change is independently shippable. Order is noise-removed per unit
of risk, same rule as SPEC v5.

### C1 — Wire R6 (priority filled)

`scorer.r6` already reads `issue.priority`, then `custom_fields.priority`
/ `issue_priority`. Return it from `score_all`. Add `r6` to
`TOGGLEABLE_CHECKS`, `enabled_rule_keys`, evidence handlers, notes,
leaderboard `RULE_KEYS`, calendar SQL, drill-down, suggestions labels,
and the Rules page description.

**Seed:** `disabled_checks` includes `"r6"` on migrate so existing
overalls do not move. An admin who wants priority hygiene removes it
and dry-runs.

**Notes copy:** `R6 Fail: priority is not set — set the ticket priority`.

**Field map:** add `field_priority` defaulting to `priority`, used only
as the custom-field fallback. The native `issue.priority` stays first.

### C2 — Internal tickets: R3 = N/A

In `r3`, if the ticket is internal (same predicate `_build_ticket_block`
uses), return `N/A` before the account checks. Thread the flag in:
`score_all` already receives the issue; `customer_portal_visible` and
`source` are on the stored row and on the Pylon payload.

`_r_check_notes` must not emit an R3 sentence when the check is N/A.
`evidence._r3` must say the ticket is internal, not "account missing".

**This is the one change in this spec that may move grades on deploy**,
because it is a correctness fix, not a new dial. Dry-run it over 30
days before merge. Expected movement: Fail → N/A on internal tickets
only; overall Fail → Pass/Needs Review when R3 was the sole Fail
driver. If the dry-run shows external tickets moving, stop.

### C3 — `r4_exempt_grade` on the status matrix

Add the attribute to `DEFAULT_STATUS_POLICY` for every status where
`r4_reply_owed` is false, seeded to `"Pass"`. `r4()` returns that grade
instead of the hardcoded `"Pass"`. Validation: if `r4_reply_owed` is
true, the attribute is ignored (or rejected — ignored is enough).

Rules UI: one extra cell on the status row, visible only when the
clock is off. Dry-run will show the Fail/Pass/N/A movement if anyone
flips `closed` to N/A.

### C4 — Configurable overall fail-drivers

New rules keys:

```
overall_fail_on:   ["r*", "a3:Poor", "a5:Fail"]
overall_review_on: ["a1:Needs Review", "a3:Needs Review",
                    "a4:Needs Review", "a5:Needs Review"]
```

`_compute_overall` reads these instead of the literal `a3 == "Poor"` /
`a5 == "Fail"` tests. `suggestions._drivers` must use the same list
(import it; do not re-duplicate — the comment in `suggestions.py` that
justifies the copy is no longer true once the list is data).

Seed equals today's formula. Adding `a1:Fail` is the interesting
admin action this unlocks; it is not taken for them.

`resync_overall` already recomputes from stored A-grades, so a save
that adds `a1:Fail` rewrites overalls with no AI calls. That is the
same D1 contract as disabling a check.

### C5 — R10: resolution completeness on close

```
r10(issue) -> Pass | Fail | N/A
```

- N/A unless the status is `closed` (or any future status the matrix
  marks `terminal: true` — add that flag, seeded true only for
  `closed`; `archived` is fetch-skipped and never scored).
- Pass if `resolution_category` is filled **and** `resolution_details`
  is filled. Both slugs go through the field map
  (`field_resolution_category` already exists; add
  `field_resolution_details`, default `resolution_details`).
- Fail if either is empty.

Default: in `disabled_checks`. Conditions independently optional later
if needed; not in this round — both fields or neither.

A5 stays. A ticket can Pass R10 (form filled) and Fail A5 (ask still
open), or the reverse. That is the point of keeping them apart.

### C6 — Snapshot Slack evidence

On every fetch that runs `r5`, if the oncall Slack link was fetched,
write `r5_slack_excerpt` / `r5_slack_fetched_at` onto that
`rule_checks` row. Empty string + null timestamp when no fetch
happened (no link, no token, or source not consulted).

`evidence._r5` prefers the excerpt. `rcheck_dryrun` may pass a reader
that returns the stored excerpt, so Slack-dependent Passes become
reproducible without a live call.

Do not fetch Slack at evidence-read time. That is how the current
"not recorded" sentence happened, and a dashboard click must not
rate-limit Slack.

### C7 — Prompt / fingerprint alignment

1. `_build_ticket_block` prints every **enabled** R-check, including
   R6/R8/R10 when on. Disabled checks stay off the line so the model
   is not shown a verdict we have asked it to ignore.
2. `qc_fingerprint` hashes the same set — enabled keys only — plus a
   `fp_version: 3` field. First re-run of a date after deploy will
   rescore (the version bump is the honest stale-mark). Subsequent
   no-op refetches stay free.
3. Drop `r9` from the hash. It is always N/A and is not printed.

Runs page: one-line note the first time `fp_version` changes, so a
full-day rescore is expected rather than alarming.

### C8 — Structured reasons on `rule_checks`

Column `reasons TEXT` (JSON object). Written in the fetch loop from
the same function that builds note sentences — extract the per-check
map from `_r_check_notes` rather than parsing the joined string back
apart. `resync_overall` updates it whenever it rewrites notes.

No UI change in this spec. Consumers that want it (evidence,
suggestions, a later drill-down) read JSON; everyone else keeps
`ai_notes`.

---

## What this does to the overall result

Today:

```
Fail          ← any enabled R Fail  OR  A3=Poor  OR  A5=Fail
Needs Review  ← else any A1/A3/A4/A5 Needs Review
Pass          ← everything else
```

After this spec, with **defaults** (no admin save):

```
same formula
+ R3 is N/A on internal tickets (C2 — the one deploy-time move)
+ R6, R10 exist as stored verdicts but are masked off (C1, C5)
```

After an admin turns the new dials on, possible new Fail drivers:

| Dial | New Fail |
|---|---|
| Enable R6 | priority empty |
| Enable R10 | closed, resolution form empty |
| Add `a1:Fail` to `overall_fail_on` | category clearly wrong |
| Add `a4:Fail` to `overall_fail_on` | status does not match the thread |
| Set `r4_exempt_grade=N/A` on `closed` | no grade movement on Fail; Pass rate drops because those tickets leave the Pass bucket |

A2 remains informational.

---

## Best practices from qc to keep as engine invariants

These are not new work. They are the bar any change above has to meet.

1. **R-checks are pure functions of stored ticket data** (plus one
   optional Slack read). No LLM. `score_all` stays the only writer of
   `rule_checks` verdicts.
2. **Overall is computed in Python**, never by the model. The prompt
   already says so (`prompts._NO_OVERALL`). C4 must not put the
   formula back in the rubric.
3. **Notes name the miss and the fix.** `_r_check_notes` and
   `a_notes_rubric` already require this. New checks ship a sentence
   in that shape on day one, or they do not ship.
4. **Refetch updates R immediately; AI is skipped when the fingerprint
   matches.** C7 may stale fingerprints once. It must not bring back
   `fetched_at > checked_at` as the rescore trigger.
5. **A rules save never calls Gemini.** `resync_overall` is the only
   writer after C1–C5. Dry-run before save for anything that can move
   a grade (already true for R-checks; C2/C3/C4/C5 must use it).
6. **Unknown statuses are not failed.** `STATUS_FALLBACK` stays
   lenient. C5's `terminal` flag defaults false for any status Pylon
   adds later.

---

## Call sites that must move together

Miss one and a new check, or a new overall driver, is real in one
surface and invisible in another.

| Surface | C1 R6 | C2 R3-internal | C3 R4 grade | C4 overall | C5 R10 | C6 Slack snap | C7 fp | C8 reasons |
|---|---|---|---|---|---|---|---|---|
| `scorer.score_all` | • | • | • | | • | • | | |
| `qc_runner._compute_overall` | mask | | | • | mask | | | |
| `qc_runner._r_check_notes` | • | • | sla already | | • | | | • |
| `qc_runner._build_ticket_block` / `qc_fingerprint` | | | | | | | • | |
| `rules.TOGGLEABLE` / defaults / validate | • | | • | • | • | | | |
| `evidence.py` | • | • | • | | • | • | | • |
| `resync_overall` | mask | | | • | mask | | | • |
| `rcheck_dryrun` | • | • | • | | • | excerpt reader | | |
| `suggestions._drivers` / labels | • | | | • | • | | | |
| `leaderboard` / `drilldown` / calendar SQL | • | | | | • | | | |
| Rules UI + `RULE_DESCRIPTIONS` | • | | • | • | • | | | |
| tests: `t_scorer`, `t_rulecfg`, `t_rdryrun`, `t_status`, `t_grades` | • | • | • | • | • | • | • | • |

---

## Sequencing

| Order | Change | Moves grades on deploy? | Risk |
|---|---|---|---|
| 1 | C8 structured reasons | no | low; additive column |
| 2 | C6 Slack snapshot | no | low; additive columns |
| 3 | C7 prompt/fingerprint alignment | only on next AI re-run of a date | medium; one-time rescore cost, must be labelled |
| 4 | C1 wire R6, default off | no | low |
| 5 | C5 R10, default off | no | low |
| 6 | C4 overall_fail_on, seeded to today | no | medium; `_compute_overall` + suggestions must share the list |
| 7 | C3 `r4_exempt_grade`, seeded Pass | no | low |
| 8 | C2 R3 N/A on internal | **yes, internals only** | medium; dry-run is the merge gate |

C2 goes last because it is the only correctness fix that rewrites
overalls without an admin save. Everything else is a dial.

---

## Non-goals

* Porting qc's Gemini-API-key path, or its undocumented
  `SLACK_USER_TOKEN` env var. Vault already holds the user token.
* Auto-enabling R6 or R10. Default off (D1).
* Making A1 or A4 Fail drivers without an admin save. The list is
  seeded to today's formula (D4).
* Dropping `r9` from the schema or `R_CHECK_KEYS`. Still deferred.
* Configurable evidence-source ordering beyond `r5_eng_sources`
  (SPEC v5 Phase 3 / D9).
* Changing A2 so it affects overall.
* A different SLA for internal tickets (called out in D2 as a later
  decision).
* Teaching the model to compute overall again.
* UI redesign, Nocturne, analytics calendar weeks (SPEC v4).
* Funcheck vocabulary, open-QC scope, scheduler/Slack guards.

---

## Test plan (when this is built)

- [ ] `t_scorer`: R6 Pass/Fail/empty; R10 N/A on open, Fail on closed-empty, Pass on closed-filled; R3 N/A on internal, still Fail on external-internal-account.
- [ ] `t_status`: `r4_exempt_grade` Pass vs N/A; ignored when `r4_reply_owed`.
- [ ] `t_grades` / `t_rulecfg`: `overall_fail_on` seed matches today's overalls exactly; adding `a1:Fail` flips only A1-Fail tickets; empty list rejected.
- [ ] `t_rdryrun`: C1/C3/C5/C2 movement counts; Slack-dependent R5 Pass replays from excerpt (not UNKNOWN).
- [ ] `t_rescore`: `fp_version` 3 changes the hash; enabled-only R keys; r9 absent.
- [ ] `t_evidence`: R6/R10 sentences; internal R3; excerpt preferred over "not recorded".
- [ ] `t_suggestions`: drivers come from `overall_fail_on`, not a local tuple.
- [ ] 30-day R-check dry-run of C2 on production data before merge; zero external tickets moved.
