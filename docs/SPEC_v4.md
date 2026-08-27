# Pylon QC — v4 Spec: Nocturne redesign, deletion cleanup, analytics drill-down

Status: **awaiting approval** · Author: design import + planning session, 2026-08-27
Baseline: `main` @ `a5791fb`
Design source: Claude Design project `dcc23f21-93b5-48f3-a021-14c54d2bcca6`
("Nocturne QC dashboard redesign"), artboard `QC Dashboard.dc.html`

---

## 0. What is already done

Of the seven asks in this round, three are wholly or partly covered by work
already on `main`. Stated plainly so nothing is rebuilt.

| Ask | State | Detail |
| --- | --- | --- |
| Calendar weeks in Analytics | **half done** | `leaderboard.week_bounds()` already buckets Monday–Sunday in the schedule timezone, and the new Leaderboard page uses it. The **Analytics page is untouched** — `_buildWeekPills` (`static/index.html:1948`) still walks `day = 1; day += 7`, producing 1–7 / 8–14 / 15–21 / 22–28 / 29–31. That is the bug in the screenshot. |
| Status filter with save, personal only | **DONE** | Confirmed 2026-08-27: the ask is persistence until the user clears it, not named views. That is exactly what ships — status, assignee and group filters persist per viewer in `localStorage` (`qc.filters.v1`), survive day changes and reloads, reconcile against the day's data, and clear on demand. **No further work.** §4 is withdrawn. |
| Analytics drill-down by failed category | **data exists, no UI** | `/api/leaderboard` already returns `rule_fails` per person and per team, which is exactly the bifurcation. What is missing is the drill-down UI and a ticket-list endpoint. §5. |
| Refetch + Run QC in one action | **now safe, not built** | Chaining these was previously a bad idea: a refetch bumped `fetched_at` on every ticket, so the following QC regraded the whole day at full price. The fingerprint work (`f24a421`) removed that, so one button can now refetch and score only what changed. §3. |
| Delete tickets removed at source | **not started** | §2 — and it carries the sharpest failure mode in this spec. |
| Collapse calendar and sidebar | **not started** | §6. |
| The Nocturne redesign itself | **not started** | §7, the largest item. |

---

## 1. Scope and order

```
Step 1  §2  deletion cleanup on refetch      ← data safety; do first, alone
Step 2  §3  Run QC = refetch + score
Step 3  §4  named saved filter views
Step 4  §5  analytics calendar weeks + drill-down
Step 5  §6  collapsible calendar and sidebar   ← lands with the redesign
Step 6  §7  Nocturne dashboard redesign
```

Deletion cleanup goes first and ships on its own, because it is the only item
in this spec that can destroy data. Everything after it is additive.

Not in scope: replacing SQLite, the in-process scheduler, removing dead R6/R9.

---

## 2. Clean up tickets deleted at source

### The requirement

When a refetch runs and a ticket no longer exists in Pylon, it should stop
appearing in the tool.

### The failure mode this must not have

Pylon returns tickets for a date window. A deleted ticket is simply absent from
the response — there is no tombstone. So "deleted" can only be inferred as
*present locally, absent from the fetch*. That inference is only safe if the
fetch was complete, and **right now it can silently be incomplete**:

```python
# pylon.py — _fetch_issues_page
if "data" not in body:
    # Pylon returns {"errors": [...]} for invalid ranges (e.g. future dates)
    return [], None, False
```

An error body becomes an empty page with no exception. A naive
delete-what-was-not-returned would therefore **wipe an entire day's tickets,
messages, grades and human sign-offs** the first time Pylon returned an error
shape. Sign-offs are not recoverable by refetching.

### Required behaviour

- A ticket present locally for the date and absent from a **verified complete**
  fetch is removed, along with its messages, rule checks and AI checks.
- A ticket with a human review (`ticket_reviews` row) is **never** hard-deleted.
  Someone's sign-off is a record of a decision; it is not ours to erase.
- Nothing is deleted when the fetch cannot be proven complete.
- Every deletion is counted, logged, and written to the audit log.

### Implementation

**Make incompleteness explicit.** In `pylon.py`, stop treating a missing `data`
key as an empty page. Return a `complete: bool` on `FetchedDay` that is False if
any page returned a non-`data` body, any pagination step raised after retries,
or the issue list is empty while `fetch_log` records a non-zero count for that
date. `FetchedDay` already carries `failed_messages` / `failed_accounts`; this
is the same idea at the page level.

**Soft-delete, not hard-delete.** Add to `tickets`:

```sql
ALTER TABLE tickets ADD COLUMN deleted_at TEXT   -- ISO8601, NULL = live
```

`fetch_and_store` then, only when `day.complete`:

1. Compute `local_ids - fetched_ids` for that `fetch_date`.
2. For each, set `deleted_at` (do **not** DELETE).
3. Log `"%d ticket(s) no longer in Pylon for %s"` and audit
   `fetch.cleanup` with the count.

Every read path filters `WHERE t.deleted_at IS NULL`: `get_day_tickets`,
`get_calendar_month` / `get_calendar_day`, `ticket_stats`, `_load_in_scope`,
`leaderboard._load_rows`, the analytics query, `list_assignee_names`.

> Soft-delete over hard-delete because the inference is probabilistic. A ticket
> can vanish from a window for reasons other than deletion — a moved
> `created_at`, a changed window boundary, a Pylon-side bug. A `deleted_at` we
> can clear is recoverable; a `DELETE` that took the messages with it is not.

**Purge is a separate, explicit action.** A ticket soft-deleted more than 30
days ago, with no review, may be hard-deleted by a maintenance command run by
hand. Not automatic, not on the request path.

### Tests (`tests/t_cleanup.py`)

- Complete fetch missing one local ticket → that ticket gets `deleted_at`, the
  others do not.
- **Incomplete fetch missing every local ticket → nothing is marked.** The
  regression that matters.
- A missing ticket that has a `ticket_reviews` row → not marked, and the reason
  is logged.
- A soft-deleted ticket is absent from `get_day_tickets`, both calendar queries,
  `ticket_stats`, `_load_in_scope` and the leaderboard.
- A ticket that reappears in a later fetch has `deleted_at` cleared.
- `"data"`-less page → `complete is False`.

---

## 3. Run QC = refetch + score

### Required behaviour

One primary action on the dashboard refetches the day from Pylon and then scores
it, reporting both halves. The separate Refetch button stays for a fetch-only
pass.

### Why this is safe now and was not before

A refetch used to rewrite `fetched_at` on every ticket, and staleness was
`fetched_at > checked_at`, so the following QC regraded the entire day at full
price — the same $0.10 for 40 unchanged tickets, every time. Since `f24a421`
staleness is a content fingerprint, so a refetch that changes nothing leaves
nothing to score. Chaining them is now cheap by construction.

### Implementation

- `POST /api/qc/{date}?refetch=1` — take the `fetch:{date}` advisory lock, run
  `fetch_and_store`, release, then take `qc:{date}` and score. Two locks in
  series, never nested, so a scheduled run colliding with a human still fails
  cleanly on one of them rather than deadlocking.
- Response gains `fetched` (count) and `deleted` (count from §2) alongside the
  existing `scored` / `skipped` / `cost_split`.
- The preview endpoint gains `will_refetch: true` so the confirm dialog can say
  "Refetch 63 tickets, then score the N that changed."
- Progress copy moves through "Fetching from Pylon…" → "Scoring N tickets…"
  rather than sitting on one label for the whole run.

### Tests

Extend `tests/t_rescore.py`: refetch-and-score on an unchanged day scores 0 and
adds no spend; on a day with one changed ticket, scores exactly 1.

---

## 4. Named saved filter views

Per-viewer filter persistence already exists. This adds **named** views.

### Required behaviour

- Save the current filter combination under a name; recall it in one click;
  delete it.
- Strictly personal — never visible to another user, never server-side.
- The unnamed working filter keeps behaving as it does now.

### Implementation

`localStorage` key `qc.views.v1`, holding at most 12 entries of
`{name, status, assignees[], groups[]}`. Same discipline as the existing filter
code: every read and write in `try/catch`, because private windows throw on
access and the dashboard must still render.

> Deliberately not server-side. "Only personal view" is the requirement, and a
> per-user server table would invite exactly the sharing this asks against —
> plus a migration and an endpoint for something the browser already does.

A recalled view reconciles against the day's data like any restored filter, and
if it matches nothing the existing "N tickets hidden — Clear filters"
affordance appears rather than an empty list.

### Tests

Manual, recorded in the PR — the frontend has no harness. Save a view, switch
day, reload, recall it, delete it; confirm a view naming a since-departed
assignee degrades rather than emptying the list.

---

## 5. Analytics: calendar weeks, and failure drill-down

### 5a. Calendar weeks (the screenshot)

`_buildWeekPills` walks from day 1 in 7-day steps, so every month restarts the
week. Replace it with true Monday–Sunday weeks that overlap the month, reusing
the boundary logic already proven in `leaderboard.week_bounds()` — do not write
a second week implementation.

- A week is Monday–Sunday in the **schedule timezone**, not UTC. SQLite's
  `strftime('%W')` is UTC-based and would shift the boundary for an IST team.
- Weeks that straddle a month boundary appear in both months' pill rows, and the
  label carries the crossing (`Jul 28 – Aug 3`).
- Pills are labelled by date range, never "Week 3", which means nothing to a
  reader scanning for a specific day.

### 5b. Drill-down (marked "only if easy — don't push")

It is easy now, because the bifurcation data already ships: `/api/leaderboard`
returns `rule_fails` per person. Two additions:

- Clicking a person expands their row into per-check failure counts, from the
  existing payload. No new endpoint.
- Clicking one of those counts opens a dialog listing the tickets, each linking
  to Pylon. This needs one endpoint:
  `GET /api/analytics/tickets?assignee=&check=&start=&end=` returning
  `{number, title, link, state, fetch_date}`, filtered to that check failing,
  excluded states removed, `deleted_at IS NULL`.

`tickets.link` comes from Pylon. `index.html` validates it against
`/^https?:\/\//i` before rendering; `runs.html` did not until `cb7739b`. The new
dialog must apply the same guard — a `javascript:` URL survives HTML escaping.

If 5b runs long, ship 5a alone. It is the actual complaint.

### Tests (`tests/t_analytics.py`)

- Week pills for a month starting mid-week include the straddling week, and each
  pill's start is a Monday in the schedule timezone.
- The drill-down endpoint returns only tickets where the named check failed,
  respects the date range, excludes excluded states and soft-deleted tickets.
- Rejects an unknown `check` value rather than returning everything.

---

## 6. Collapsible calendar and sidebar

The Nocturne artboard carries `showRail` as a prop, so the rail is collapsible
by design.

- The left nav collapses to an icon rail (~56px); the right rail hides entirely.
- Both states persist per viewer (`qc.chrome.v1`), same `try/catch` discipline.
- Below 1100px the right rail hides by default and the calendar moves above the
  ticket list; below 720px the nav starts collapsed. The dashboard is used on
  laptops, so this is a real breakpoint, not a formality.
- The toggle is a real focusable button with an `aria-expanded` state and a
  visible `:focus-visible` ring — Nocturne requires the 2px accent ring and
  forbids the browser default.

---

## 7. The Nocturne dashboard redesign

### What the design actually changes

Comparing the artboard against the current dashboard, the substantive win is
**row density**. Today each ticket prints twelve full-width chips
(`Functionality: Pass`, `Category: Pass`, …) which consume the row and push the
verdict off-screen. Nocturne replaces them with a 12-cell matrix — one 16×20
cell per check — so a row is scannable and the verdict is always visible.

The twelve columns map exactly onto the existing checks:

| Cell | Check | Cell | Check |
| --- | --- | --- | --- |
| Fn | R1 functionality | OC | R8 oncall |
| Ct | R2 category | CA | A1 category accuracy |
| Ac | R3 account | Sn | A2 sentiment |
| RT | R4 response time | Rs | A3 response |
| SO | R5 status owner | SC | A4 status check |
| RJ | R7 Rootly/Jira | Cl | A5 closure |

Seven R-checks plus five A-checks. R6 and R9 are dead and stay out.

### Token adoption

Nocturne's ground (`#161826`), text (`#e9e9ed`) and blurple accent (`#9184d9`)
replace the current slate/indigo values. The cheapest coherent route:
**remap the existing variable names in `shell.css` to Nocturne values** rather
than rewriting five pages. `--bg`, `--surface`, `--border`, `--text`,
`--muted`, `--accent` are already used everywhere, so every page inherits the
new look with a small diff, and the redesigned dashboard uses the Nocturne
token names directly.

Rules to honour, from the system's own readme:

- Buttons are **outlined**, never filled. `.btn-primary` is an accent border on
  transparent.
- Never flood an area with the accent; it is a line, a mark, a glow.
- Freestanding rules fade to transparent over 48px at each end; box outlines
  and in-control separators stay solid.
- Density is 0.7× — use `--space-*`, never raw px.
- Focus is `2px solid var(--color-accent)` at `2px` offset, everywhere.
- Accent-on-ground is tuned to 3:1: fine for chrome, icons and large text, **not
  for body copy**. Paragraph text in the accent uses `--color-accent-300`.

### Deviations I am proposing, and why

The artboard is a static mock with ten hard-coded tickets. Three places where I
intend to depart from it:

1. **The check matrix needs a non-colour channel.** The mock separates pass /
   fail / warn / N/A / not-run largely by fill: grey, `#d9737f`, `#d9a869`,
   outline, dashed. Red-green colour blindness affects roughly 8% of men, and
   this grid is the primary signal on the page. Each state keeps its glyph
   (`✓ ✕ !` and empty) so it is never colour alone, and every cell carries a
   `title` — which the mock already does. I will also give each cell an
   `aria-label` so the row is legible to a screen reader.
2. **`sentiment` is not pass/fail.** A2 returns Positive / Neutral / Concerned /
   Frustrated / Urgent. The mock renders Concerned-and-worse as `warn`. I will
   keep that mapping but never call it a failure in the verdict logic — the
   backend's `_compute_overall` does not treat A2 as failing, and the UI must
   not contradict the stored grade.
3. **The mock's verdict is computed client-side.** The real verdict is
   `overall_result`, already computed server-side with the human sign-off
   overlaid. The UI must render the stored grade, not recompute it — recomputing
   is how Slack and the dashboard came to disagree before `6a0fc70`.

### What must not regress

The dashboard carries real behaviour the mock knows nothing about. All of it
survives:

- Human review / sign-off, including Revert, and coverage-based permission
  (`can_review`).
- Coverage group filtering and the default group filter for members.
- Filter persistence and the hidden-count affordance.
- Cumulative cost display, the `est.` labelling, the unpriced-model marker.
- Single-cell calendar patching with its request token.
- CSV export, the analytics view, the ticket detail with messages.
- The Re-run QC label and its preview gate.

### Implementation shape

`static/index.html` is 2,100 lines and holds all of the above. I will not
rewrite it wholesale. The change is scoped to:

- the ticket row renderer (chips → matrix),
- a new right rail (calendar moves into it, plus fail-reason bars and the check
  legend),
- the metric cards row, absorbing today's status pills so there is one filter
  control rather than two,
- the detail panel, becoming a slide-over that reuses the existing review
  actions rather than duplicating them.

The fail-reason bars need aggregate counts over 7 days. `/api/leaderboard`
already returns `rule_fails`; the rail can call it with a 7-day range instead of
adding an endpoint.

### Tests

Rendering has no automated harness, so the guard is behavioural and manual,
recorded in the PR: sign off a ticket and confirm the verdict pill follows the
*stored* grade; confirm a member sees only their coverage; confirm the matrix
renders all five cell states; confirm keyboard focus is visible on every
interactive element; confirm the page does not scroll horizontally at 1280px.

Backend suites must stay green — `./tests/run.sh`, eleven suites plus the new
`t_cleanup` and `t_analytics`.

---

## 8. Decisions (answered 2026-08-27)

1. **Saved views** — persistence until the user clears it, not named views. That
   already ships, so §4 is **withdrawn**.
2. **Purge** — manual only for now. No automatic hard-delete.
3. **Theme scope** — **all five pages** migrate to Nocturne. Every menu, page and
   control moves to the new experience; the sidebars follow the new flow
   everywhere, not only on the dashboard.
4. **Drill-down** — ship §5a (calendar weeks) alone if §5b runs long.

---

## 10. Rules page: editable layout, prompt support, and learned suggestions

Raised after the first draft: *"the rules are not laid out properly — I cannot
change what I want; need prompting support there, and auto-learning for future
suggestions."*

Three separate problems behind one sentence. Taking them in order of how much
they block the user.

### 10a. The layout does not expose what is editable

`rules.py` already validates and persists eleven distinct tunables, and
`PUT /api/rules` already accepts all of them. So the *capability* exists; the
page does not surface it as editable. The current page renders rosters as
opaque line lists (`U02RTGHRYJK  Mohammad Moiz`) where the ID is the fact and
the name is a label, which is correct as storage and wrong as an interface.

Required:

- One section per rule, headed by the rule's own name and its plain-English
  statement (already in `RULE_DESCRIPTIONS`), showing **what changing it does**.
- Rosters become add/remove pickers backed by the existing
  `/api/directory/slack` search — never a textarea of raw IDs. The ID stays the
  persisted fact; the UI only ever shows names.
- The SLA (`r4_sla_hours`), excluded states, oncall categories and invalid-name
  fragments each get a control matched to their type, not a text field.
- Every section shows its **default** and a one-click revert to it, since
  `rules.defaults()` already computes them.
- Validation errors from `rules.validate()` are shown against the field that
  caused them. They are currently returned as a flat list and rendered as one
  blob.

### 10b. Prompt support

`a_guidance` is already appended to `SYSTEM_PROMPT` and already recorded per run
(`config_json.custom_guidance`), so the hook exists and is auditable. What is
missing is help using it.

Required:

- The effective prompt is **visible**: the base rubric (read-only, already
  returned as `rubric`) with the guidance shown inline where it is actually
  injected, so the user can see what the model receives rather than guessing.
- A character budget against the validated 4000-character cap, live.
- **Dry-run before saving.** Score a small sample of already-graded tickets with
  the draft guidance and show, per ticket, the current grade beside the draft
  grade. Guidance is the highest-leverage and least-reversible knob on the page —
  a wording change silently re-grades every future run — so it should not be
  saveable blind.
- A dry-run is metered: it costs real Vertex calls. Cap the sample (10 tickets),
  show the estimated cost before running, and label the result an estimate.

> Deliberately **not** an LLM that writes the guidance for you. The guidance is
> the workspace's grading policy; having a model draft its own instructions
> removes the human from the one place their judgement is the whole point.

### 10c. Learned suggestions

The signal already exists and is unused: `ticket_reviews` records every time a
human overrode the AI. A reviewer changing Fail→Pass is a labelled example of
the AI being wrong.

Required:

- `GET /api/rules/suggestions` returning, over a window:
  - per check, how often a human override contradicted it, as
    `{check, ai_said, human_said, count, sample_ticket_numbers}`;
  - the same for R-checks, which is a rules problem rather than a prompt one
    (e.g. R3 failing on accounts that are actually fine points at
    `r3_internal_account_ids`, not at guidance);
  - a **confidence gate**: nothing is surfaced below 5 overrides for a given
    check in the window, because two disagreements are noise.
- The page renders these as observations with evidence, each linking to the
  tickets, and each offering the concrete edit it implies — pre-filled, **never
  auto-applied**.

> The line I am drawing: the system may *propose* a change and show its evidence.
> It must not *make* one. Auto-tuning a grading rubric from its own past
> disagreements is a feedback loop with no human in it, and the failure mode is
> silent drift in what "Pass" means — with no one able to say when it changed.
> Every suggestion lands as a pre-filled diff the user accepts or dismisses,
> and accepting it writes a normal audited rules change.

### Implementation notes

- Suggestions are a **read-only** aggregate over existing tables. No new
  writes, no schema change, so this cannot corrupt anything.
- The dry-run reuses `qc_runner._call_gemini` with the draft guidance
  substituted, and must **not** write `ai_checks` — it is a preview, not a run.
  Pass an explicit `persist=False` rather than relying on a caller to remember.
- Suggestion counts respect `deleted_at IS NULL` (§2) and excluded states.

### Tests (`tests/t_suggestions.py`)

- Four overrides on one check → nothing surfaced (below the gate); six → surfaced.
- An override that *agrees* with the AI (`kept_ai = 1`) is not counted as a
  disagreement.
- A Revert is not counted as an override.
- Suggestions exclude soft-deleted tickets and excluded states.
- The dry-run writes no `ai_checks` row.
- A suggestion's proposed edit passes `rules.validate()` — a suggestion that
  cannot be saved is worse than none.

---

## 9. Definition of done

- `./tests/run.sh` green, including `t_cleanup` and `t_analytics`.
- Schema changes go through the ALTER-if-missing block, verified against a
  **pre-migration** database: column added, existing rows preserved, repeated
  `init_db()` idempotent. Production holds real data.
- No read path returns soft-deleted tickets. Grep for `fetch_date = ?` and
  confirm each has the filter.
- Every user-facing interpolation escaped; every new URL scheme-checked.
- New endpoints carry `require_user` or `require_admin`.
- Manual verification steps recorded in the PR for §4, §6 and §7.
