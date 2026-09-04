# SPEC v5 — Making the rules configurable

Status: proposed, not built. Written 2026-09-04 from feedback in `#support-qc`.

## The feedback

Three messages, one root cause.

**Asha, 3 Sep** — "There are quite a few QC failures because the field *Does
Rootly Exist (Yes/No)* is showing as not updated. Since this field no longer
exists, could you please check why it's still being considered as part of the QC
criteria?"

**Anusree, 2 Sep** — "Will need to update rule for waiting on eng."
**Ashwin** — "Check the rules page once if it allows to config?"
**Darshan** — "I don't think it allows config. I have made multiple changes and
it is in my local. Didn't get confidence to push it."

That last line is the whole problem in one sentence. Tuning a rule requires a
code change, a code change requires courage, and so the tuning sits on a laptop.

## One correction before anything else

`does_rootly_exist` **has not been deleted.** Pylon's issue custom-field API
still returns it:

```
label   Does Rootly Exist?
slug    does_rootly_exist
type    select    options: Yes / No
source  pylon     is_read_only: false
```

So the check is not referencing a field that vanished. Either the field has been
taken off the ticket form that agents actually see, or it is simply no longer
being filled. The distinction matters because it changes the fix — removing a
check versus restoring a form field — and it is Asha's and Anusree's call, not
a thing to guess at.

What is *not* in question: **there is no way to act on that decision without a
deploy.** No per-check off switch exists, and the field slug is a literal in
five files (`scorer.py`, `qc_runner.py`, `evidence.py`, `app.py`,
`static/index.html`). Emptying `r8_oncall_categories` does not disable R8; it
makes R8 fail differently. There is no configuration mitigation. That is the
defect this spec addresses.

## What the Rules page actually exposes

The page offers the dials. It does not offer the wiring.

| | Configurable today | Hardcoded |
|---|---|---|
| **Custom fields** | none | `functionalities`, `request_category`, `does_rootly_exist`, `rootly.incident_reference`, `oncall_slack_chat_link`, `resolution_category` |
| **Ticket statuses** | `waiting_on_legal` only, via `r5_group_states` | the other 12 |
| **Rosters** | CS, Impl, Eng, PT — users and groups | — |
| **Thresholds** | `r4_sla_hours`, `r8_oncall_categories`, `r3_*`, `excluded_states` | — |
| **A-check rubric** | all eight prompt sections | the JSON wire contract (correctly) |
| **Whether a check runs at all** | none | all of R1–R8 |

Pylon reports 13 issue statuses. Exactly one of them can be configured:

```
new                      hardcoded branch          scorer.py:547
waiting_on_you           hardcoded branch          scorer.py:551
waiting_on_customer      hardcoded set             WAITING_CUSTOMER
waiting_on_csm           hardcoded branch          scorer.py:556
waiting_on_product       hardcoded branch          scorer.py:585
waiting_on_engg          hardcoded set             ENGG_STATES
waiting_on_legal         CONFIGURABLE              r5_group_states
investigating            hardcoded set → N/A       _R5_NA_STATES
on_hold                  hardcoded set → N/A       _R5_NA_STATES
closed                   hardcoded set → N/A       _R5_NA_STATES
archived                 hardcoded set → N/A       _R5_NA_STATES
handled_by_ai_donot_use  hardcoded set → N/A       _R5_NA_STATES
migration                unlisted → falls to N/A   nowhere
```

Two bugs fall out of writing that table:

1. `ENGG_STATES` contains `waiting_on_engineering`, which is not a Pylon status.
   Dead config that reads as coverage.
2. `migration` is a real status no rule mentions. It happens to land on the
   safe default (N/A, don't penalise), but by accident rather than by decision —
   and it is still scored and still billed.

## Decisions taken after review

Reviewed adversarially against the code before implementation. The review found
the direction sound and the downstream N/A plumbing already correct, but the
first draft had not decided several things an implementer would have had to
invent. Those decisions are recorded here; they are binding on the phases below.

**D1 — Disabling a check is a read-time mask, not a scoring change.**

The first draft said "disabled returns N/A" and claimed it answered Asha "today".
Both were wrong. R-checks are computed exactly once, in the fetch loop
(`app.py:489`); `scorer.score_all` has no other caller and there is no recompute
path. So flipping a check off would have left every stored `r8 = 'Fail'` in
place, still failing its overall, still counted in the leaderboard, the calendar
and the Slack report — until somebody refetched.

Worse, the refetch that finally applied it would have cost money. `qc_fingerprint`
hashes all of `R_CHECK_KEYS` including r8 and r9, but `_build_ticket_block`
prints only R1–R5 and R7 (`qc_runner.py:630-631`). An r8 verdict flipping to N/A
therefore moves the fingerprint without changing one byte the model sees, and
`_needs_scoring` pulls those tickets into the next paid AI run to produce
identical grades.

So: `disabled_checks` lives in the rules document, stored verdicts are **never
touched**, and every consumer reads through one helper, `rules.enabled_rule_keys()`.
Saving runs `resync_overall.run()` dateless, which recomputes `overall_result`
and the R-notes from stored verdicts with no AI calls at all. The effect is
immediate, it costs nothing, and re-enabling is a second save because the
underlying verdicts were preserved.

The trade-off, stated because it is a real one: the model keeps seeing a
disabled check's stored verdict on the R-checks line of the prompt. That is
harmless context, and it is what buys us a fingerprint that never moves. The
alternative — writing N/A at fetch time — is rejected for the billing reason
above.

This also sidesteps the r8/r9-in-fingerprint mismatch rather than fixing it.
Trimming those keys from the payload would be correct but would invalidate every
stored fingerprint at once, costing a full rescore wave on the next re-run of
any date. Left as a documented inefficiency; it cannot bite while verdicts are
never rewritten.

**D2 — `qc_fingerprint` must not fold in an R-config hash.** The `prompts.fingerprint()`
precedent is the wrong pattern to copy here. The model consumes R-*verdicts*,
not R-*config*, so hashing the verdicts is already the correct contract. Folding
in a config hash would mark every ticket in the database stale and rebill every
subsequently re-run date at full price for grades that cannot change.

**D3 — The enable list is r1, r2, r3, r4, r5, r7, r8.** Not "all of R1–R8" as the
first draft said. `score_all` never computes r6 (`scorer.py:721`), and r9 is
hardcoded to N/A (`scorer.py:714`). A toggle for either would be a control that
does nothing. Dropping them from `R_CHECK_KEYS` entirely stays deferred, as
SPEC_v4 deferred it — the keys are still read from existing rows.

**D4 — All four R8 conditions off means R8 is disabled**, not R8 passing
vacuously. Validation rejects the empty set rather than silently turning the
check into a free pass on every oncall ticket.

**D5 — Attribution belongs on `rule_checks`, not the AI run.** The first draft
claimed the rules hash would let the Runs page attribute a grade shift. It would
not: `rules_hash` is recorded into `qc_runs.config_json` at AI-run time
(`qc_runner.py:869`), whereas R-verdicts are written at fetch and overalls are
rewritten by `resync_overall` with no run record at all. Fix: a `rules_hash`
column on `rule_checks` written in the fetch loop, plus an audit row from
`resync_overall` carrying its change counts and the hash it applied.

**D6 — One-step undo is in scope.** The first draft declared version history a
non-goal on the grounds that the rules hash plus the audit log was enough. It is
not: `vault.set_raw_setting` is `INSERT OR REPLACE` and the audit stores only
`hash=` and `keys=`, so nothing can be restored. That was tolerable while edits
were rosters and prose. Once one save can flip months of grades, "nothing
breaks" has to include "a bad save is recoverable". Full history stays out of
scope; keeping the previous document for a single undo is a handful of lines and
is in.

**D7 — `_r_check_notes` is a third independent copy of the rules and must read
config.** It is not mentioned in the first draft at all, and its output is
*persisted* into `ai_checks.ai_notes` and posted to Slack. It already lies
today: the R4 note says "&gt;24 hours" regardless of the configurable
`r4_sla_hours` (`qc_runner.py:476`), and the R5 note names literal Slack groups
while `eng_group_ids` is editable. Any change that makes rules configurable
without fixing this ships notes that contradict the Rules page.

**D8 — `RULE_DESCRIPTIONS` must be derived from the live rules document.** It is
prose in `app.py:845` duplicated again in `static/index.html`, describing rules
the phases below make editable. "The 'functionalities' custom field must be
filled" is false the day the field is remapped.

**D9 — `evidence.py` reads five `scorer.*` state sets directly** and knows
nothing of enabled flags. It is an in-scope call site for every phase, not just
Phase 1.

**D10 — Phase 4 moves ahead of Phase 2.** The dry-run is the instrument that
proves Phase 2's seed is a no-op before anyone saves it. Building the verifier
after the riskiest migration is backwards.

**D11 — Phase 3 is demoted to "revisit".** Nobody asked for a configurable
evidence cascade, the audience is one or two admins, and the test surface is
large. The cascade is stable policy and stays in code until there is a concrete
request.

**D12 — The status matrix needs four attributes per status, not two.** The
current sets encode three independent dimensions with different combinations per
status, so the first draft's "in scope + what R5 expects" cannot represent
today's behaviour, let alone seed to a no-op. See Phase 2 below.

**D13 — A dry-run is not free and must not be honest-looking-but-wrong.** R5
makes live Slack calls (`scorer._fetch_slack_thread`) and R4 measures against
`datetime.now`, so a naive 30-day dry-run is slow, rate-limited, and reports R4
movement that no rules change caused. It must pin the comparison clock and never
touch Slack.

**D14 — The scheduler guard is already half-true.** `schedule_enabled` defaults
to `"0"` (`vault.py:222`), so a fresh database does not schedule anything. The
actual leak is the `legacy_env` fallback reading `SCHEDULE_ENABLED` from a copied
`.env`. That is what the guard should address.

## What this fixes, in order

Each phase is independently shippable and independently useful. The ordering is
by how much live noise it removes per unit of risk, not by architectural
tidiness.

### Phase 0 — an off switch, and a required-fields checklist

Per **D1**, this is a read-time mask. `disabled_checks` in the rules document;
stored verdicts untouched; one helper, `rules.enabled_rule_keys()`, drives every
consumer. Saving runs `resync_overall.run()` dateless so stored overalls stop
counting a disabled check's failures immediately, with no AI calls.

Call sites that must read through the helper — miss one and a disabled check
still fails tickets somewhere:

* `_compute_overall` (`qc_runner.py`) — the key set it scans
* `leaderboard.RULE_KEYS` and its `rc.{k} = 'Fail'` sums
* `drilldown._R_FILTERS` and `assignee_breakdown`
* `slack.py`'s rule-failure counts
* `db._CALENDAR_CELL_SQL` — currently a hardcoded `r1='Fail' OR … OR r9='Fail'`
  chain, so it has to become a built string
* the dashboard matrix columns, via `/api/rules`
* `evidence.for_ticket` — returns "this check is switched off" for a masked key,
  rather than a stale reason that reads as a scoring bug (**D9**)

Enable list is r1–r5, r7, r8 (**D3**). R8's four conditions become individually
optional, with the empty set rejected (**D4**). `_r_check_notes` starts reading
config in the same change (**D7**), which `resync_overall` then rewrites into
stored notes for free, since it already re-derives them.

### Phase 1 — fields chosen from what Pylon actually has

Every hardcoded slug becomes a setting, and the editor is a dropdown populated
from Pylon's live `GET /custom-fields` — not a text box you type a slug into.

* A configured field that Pylon no longer returns shows a warning on the Rules
  page and in the daily Slack report: *"R8 reads `does_rootly_exist`, which
  Pylon no longer defines."*
* That warning is the real prize. It would have caught Asha's issue before she
  had to notice it in the failure counts, and it turns the next field rename
  from an investigation into a banner.
* `evidence.py` must read the same mapping, or the reasons shown on a ticket
  will name fields the checks no longer use.

### Phase 2 — one status matrix, seeded from Pylon

Replace `ENGG_STATES`, `TERMINAL_STATES`, `WAITING_CUSTOMER`, `_R5_NA_STATES`
and `_R5_GROUP_STATES` with a single editable table, one row per status Pylon
reports, seeded from today's behaviour so nothing moves on migration.

Four attributes per status, not two (**D12**). Two would not represent what the
code does now: `closed` and `archived` short-circuit R4 to Pass while R5 is N/A
and R1–R3, R8 and the A-checks still run — A5 exists *specifically* to judge
closures, so marking `closed` "excluded" would delete a working check.
`investigating` and `on_hold` are R5-N/A but R4 fully applies. `waiting_on_customer`
is Pass for both. `waiting_on_engg` additionally gates R7's applicability.

| Attribute | Governs |
|---|---|
| `in_scope` | AI scoring and every count (today's `excluded_states`) |
| `r4_reply_owed` | whether R4's SLA clock runs, or short-circuits to Pass |
| `r5_expectation` | `none` / `support_reply` / `handoff` + which roster or tags |
| `r7_engineering` | whether R7 applies at all |

R8 consults no states, so the matrix must not pretend to govern it. Two further
constraints: `archived` is separately special-cased in the fetch path
(`app.py:452` skips messages and scoring entirely), so a row marking it in-scope
would be a lie the fetch path overrides — that behaviour stays fixed and is
documented as such. And the `in_scope` column *replaces* `excluded_states`
rather than competing with it; the migration folds one into the other so
`excluded_state_clause` keeps one source.

Precedence comes from the table, not from code order, and validation rejects
contradictory rows.

### Phase 3 — evidence sources per check (deferred, see D11)

R5's engineering branch tries three sources in order: Pylon thread mentions, the
`oncall_slack_chat_link` thread, then Rootly/Jira as a fallback. That cascade is
policy and would in principle belong in the rules — particularly the fallback,
which is the difference between "an engineer was told" and "a ticket exists
somewhere".

Deferred. Nobody asked for it, the audience is one or two admins, and the test
surface is ordering × three sources × two checks. Revisit on a concrete request.

### Phase 4 — see the change before saving it *(build before Phase 2, per D10)*

The A-check rubric already has a dry-run: it grades a sample with unsaved text
and shows what would move, writing nothing. R-checks deserve the same, and it is
cheaper — R-checks are deterministic and local, so a dry-run over 30 days costs
nothing but CPU and needs no AI call at all.

*"This change moves 41 tickets: 18 Fail → Pass, 23 Pass → N/A"* is what turns
Darshan's local branch into a save button — and it is how Phase 2's seed is
proven to move zero tickets before anyone saves it.

Two corrections it must carry (**D13**): pin the comparison clock to each
ticket's original `checked_at` rather than `datetime.now`, or R4 reports age
drift no rules change caused; and never call `_fetch_slack_thread`, reporting
R5's Slack-dependent branch as "would consult the linked thread" instead. It
also needs a stored-row → scorer-input adapter, because `score_all` expects
API-shaped input while stored rows are flattened with JSON-string fields — the
same impedance `evidence.py` already absorbs.

## The thing worth fixing regardless of this spec

Darshan is running a local copy against the shared Slack workspace. On 28 Aug at
09:40 IST, `#support-qc` received *"Scheduled QC run failed — 2026-08-27: No
Google Cloud project configured"* nine minutes after production had fetched and
scored that date cleanly, 44 of 44. There is no matching row in production's
`scheduled_runs`, and Railway has exactly one QC service — so the alarm came
from a database production cannot see. A local instance with the Pylon and Slack
tokens but no Vertex project produces precisely that message. It matches an
earlier report that "the 2 scheduler failures do not show up on the runs page".

Two guards, small and independent of everything above:

* Refuse to post to Slack when the instance is not the deployment — no
  `RAILWAY_PUBLIC_DOMAIN`, or `dashboard_base_url` pointing at localhost —
  unless a dev channel is explicitly configured. A laptop should not be able to
  alarm the team's channel.
* The scheduler should be off by default on a fresh database, so a local copy
  does not start running dailies just because it booted.

This is a symptom of the same disease: config that needs a deploy pushes people
onto local forks, and local forks reach production's channel.

## Sequencing

| Order | Phase | Answers | Risk |
|---|---|---|---|
| 1 | 0 — mask, R8 checklist, notes, undo | Asha, today | low; verdicts never rewritten |
| 2 | Guards — Slack, scheduler `legacy_env` | phantom alarms | low; independent |
| 3 | 1 — field mapping + drift warning | prevents the next Asha | medium; 6 call sites read slugs |
| 4 | 4 — R-check dry-run | Darshan; verifies Phase 2 | low; read-only |
| 5 | 2 — status matrix | Anusree | medium; seed proven by the dry-run |
| — | 3 — evidence sources | tuning depth | deferred |

Phase 0 and the guards are worth doing before anything else is decided, because
they stop live noise and neither one changes a grade that is not explicitly
asked to change.

## Non-goals

* Auto-tuning rules from review overrides. The suggestions panel proposes and a
  person saves; that stays.
* Making the A-check JSON contract editable. It is mechanism, not policy.
* A full rules version history with rollback. **One-step undo is in scope** —
  see D6; the first draft was wrong to call the audit log a substitute, since it
  records only a hash and cannot restore anything.
