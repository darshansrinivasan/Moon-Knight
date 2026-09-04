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

## What this fixes, in order

Each phase is independently shippable and independently useful. The ordering is
by how much live noise it removes per unit of risk, not by architectural
tidiness.

### Phase 0 — an off switch, and a required-fields checklist

The smallest change that answers Asha today.

* Every check R1–R8 gets `enabled` in the rules document. Disabled means the
  check returns `N/A` — not `Pass`. A disabled check must never look like a
  passed one, in the matrix, the leaderboard or the Slack report.
* R8's four conditions become individually optional: `does_rootly_exist = Yes`,
  a Rootly reference, a Jira link, an oncall `request_category`. Asha can drop
  the first and keep the rest.
* Disabling a check is recorded in the audit log with who and when, and the
  rules hash moves, so the Runs page attributes the grade shift to the change.

Ships without touching how any check computes. ~150 lines plus tests.

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
reports, seeded from today's behaviour so nothing changes on migration.

Per status: **in scope** (or excluded), and **what R5 expects** — nothing, a
reply from support, or a handoff to a named roster or Slack group.

This is what Anusree asked for. It also makes a new Pylon status appear as a row
to make a decision about, rather than silently defaulting.

### Phase 3 — evidence sources per check

R5's engineering branch tries three sources in order: Pylon thread mentions, the
`oncall_slack_chat_link` thread, then Rootly/Jira as a fallback. That cascade is
policy and belongs in the rules — particularly the fallback, which is the
difference between "an engineer was told" and "a ticket exists somewhere".

### Phase 4 — see the change before saving it

The A-check rubric already has a dry-run: it grades a sample with unsaved text
and shows what would move, writing nothing. R-checks deserve the same, and it is
cheaper — R-checks are deterministic and local, so a dry-run over 30 days costs
nothing but CPU and needs no AI call at all.

*"This change moves 41 tickets: 18 Fail → Pass, 23 Pass → N/A"* is what turns
Darshan's local branch into a save button.

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

| Phase | Answers | Risk |
|---|---|---|
| 0 — off switch, R8 checklist | Asha, today | low; no check logic changes |
| Slack/scheduler guards | phantom alarms | low; independent |
| 1 — field mapping + drift warning | prevents the next Asha | medium; 5 files read slugs |
| 2 — status matrix | Anusree | medium; seed carefully or grades move |
| 3 — evidence sources | tuning depth | medium |
| 4 — R-check dry-run | Darshan's confidence | low; read-only |

Phase 0 and the guards are worth doing before anything else is decided, because
they stop live noise and neither one changes a grade that is not explicitly
asked to change.

## Non-goals

* Auto-tuning rules from review overrides. The suggestions panel proposes and a
  person saves; that stays.
* Making the A-check JSON contract editable. It is mechanism, not policy.
* A rules version history with rollback. The rules hash plus the audit log is
  enough for now; revisit if Phase 2 proves it is not.
