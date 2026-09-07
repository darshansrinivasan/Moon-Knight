# Pylon QC — contributor guide

FastAPI + SQLite + Vertex Gemini. One process, one worker, deployed on
Railway; the scheduler is an in-process asyncio task. Several people push to
this repo — these are the rules that keep their changes from contradicting
each other. Each one exists because its violation shipped a real bug.

## Run the tests

```
./tests/run.sh
```

Plain scripts, not pytest — each suite exits non-zero on the first failure and
pins behaviour that actually broke once. A red assertion usually means a real
regression, not a stale test. New behaviour ships with a pin in the matching
`tests/t_*.py`.

## Money and outward actions

- Vertex calls bill real money. Never trigger scoring/report/chat paths in
  automated checks; fingerprints (`qc_fingerprint`, funcheck/report
  fingerprints) exist so unchanged content is never re-billed — preserve that
  property when touching prompts or stored fields.
- Slack posts and Drive/Sheets writes go through `vault.may_act_outward()` /
  `slack.may_post()`. A local copy must refuse; never bypass the guard.
- Every admin mutation writes `vault.audit(...)`. If you add a setting, add it
  to `SETTING_SPECS` in vault.py — that registry is the only settings surface.
- Settings that must change together go through `vault.set_raw_settings({...})`
  (one transaction), never a sequence of `set_raw_setting` calls.

## Tags: raw values vs labels

Pylon stores option VALUES (slugs like `general_question`) on tickets; the UI
shows LABELS ("General FAQ - How to questions"). The synced map
(`funcheck.option_labels()`) translates value → label.

**Classify on raw slugs. Display labels.** Any predicate, cluster, rollup or
filter keys on the raw field (`category_raw`, `functionality_raw`); only
charts, CSVs and prose show `funcheck.canon(...)` output. Labels are wording a
Pylon admin can change at any time — logic written against them fails silently
on the next rename (64 alert tickets once vanished from a report cluster this
way). One translator: `funcheck.canon`. Do not add another `_humanize`-style
formatter.

## One opinion per rule

- Scope: `rules.excluded_state_clause()` is the only excluded-status
  predicate. Every counting/reporting query uses it (via `db._scope_clause`);
  never write `state NOT IN (...)` by hand.
- Check semantics: `scorer` decides, `evidence` explains, `qc_runner`'s notes
  advise — all three read the SAME configuration (`rules.field(...)`,
  `status_policy`, `r8_conditions`, `enabled_rule_keys`). Never hardcode a
  field slug, a Slack group name, or a threshold that Rules/Admin can edit.
- Effective grade lives in `leaderboard.LATEST_REVIEW_SQL` /
  `EFFECTIVE_GRADE_SQL`. Reuse it; don't re-derive review-over-AI precedence.

## Concurrency

- Long work takes a `db.advisory_lock`. Per-date work uses the shared names
  `fetch:{date}` / `qc:{date}` — a new entry point that scores a date must
  hold the same names or it runs concurrently with the scheduler.
- `db.STALE_RUN_MINUTES` must stay greater than the scheduler's
  `LOCK_TTL_SECONDS` (see the comments on both).
- Sync httpx calls (gcp.py, anything network-bound) run under
  `asyncio.to_thread(...)` from async endpoints — never directly on the loop.

## SQLite habits

- `t.state` can be NULL: `NOT IN` drops NULL rows silently — use
  `LOWER(COALESCE(t.state,''))` when writing state predicates (or better, the
  shared clause above).
- WAL, single writer: keep transactions short; batch related writes into one
  `get_conn()` block.

## Frontend (static/*.html, shell.js)

- Escape everything interpolated into HTML with `esc()`/`QC.esc`; guard hrefs
  with `/^https?:\/\//i` before using stored links.
- Async loaders that race (day panels, stats, analytics) carry a sequence
  token; check it after every `await` before rendering.
- Theme: only design tokens from shell.css; `t_theme` sweeps every page.
- Skeletons are `aria-hidden="true"`; error states must clear ALL skeletons
  the loader started.
- A failed side action (Slack send, export) reports into its own status line —
  never replace the content someone is working over.

## Style

- Comments state the constraint the code can't show — most modules carry the
  "why it was broken" in the docstring. Match that: evidence-first, no
  narration of what the next line does.
- Commit messages: `feat:`, `fix:`, `enhance:` prefix plus a sentence a
  reviewer can act on. (History currently mixes `Feat:`, `Enhanc:`, bare
  sentences — converge, don't add a fourth style.)
- The user-facing wording conventions live in the pages themselves — buttons
  say what they do, errors say what to do next, roles are named (member /
  operator / admin) rather than described.
