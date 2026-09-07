# SPEC weekly — Support Weekly Dashboard

Status: **building**. Written 2026-09-07 from the extracted source in
`/Users/anusreerajls/Downloads/Support data` (`README.md.docx`,
`DATA_SCHEMA.md.docx`). Those files document the live SpotDraft Support
Trend Analysis site (pulled 2026-09-07). The original `index.html` /
`assets/app.js` / baked `const D` were not in the folder — only the
contract and the render-layer notes. This spec rebuilds that dashboard
as a QC tab, with numbers computed from the local ticket store.

This is a new surface. It does not change scoring, overall, Slack, or
funcheck.

---

## What we are porting

The original artifact is a week-over-week operations dashboard. Every
chart and table reads one generated object, `D`. Durations in `D` are
seconds unless the key says otherwise; the render layer converts.

The original render layer had three known defects this port must not
copy:

1. **Filters were decorative.** `#fWeek`, `#fAgent`, `#fPriority`,
   `#fCategory`, `#fStatus`, `#fEsc` were populated and `#clearFilters`
   reset them, but no change handler was bound. `D.allRows` is the
   row-level grain and is enough to make them real.
2. **Insights were hardcoded** (agent names, "Salesforce (SFDC) remains
   the highest-volume category", CSAT warn-notes). They go stale on the
   next refresh. Insights must be derived from `D`.
3. **One chart ignored theme** (`initFRTPercChart` hardcoded a grid
   colour). Chart colours come from Nocturne tokens via
   `getComputedStyle`.

CSAT is fetched from Pylon: `GET /surveys` lists templates, Admin saves
`csat_survey_id`, and `/api/weekly/csat` pages
`GET /surveys/{id}/responses` for every submission. Scores are stored on
`csat_events` / `tickets.csat_responses`. Reopen history is still not in
the store.

---

## Decisions (binding)

**D1 — New tab, not a view on Dashboard.** Nav id `weekly`, href
`/weekly`, label `Weekly Dashboard`. Page title **Support weekly
Dashboard**. `data-page="weekly"`. Same chrome as Leaderboard / Open
Tickets.

**D2 — `D` is computed, not baked.** `/api/weekly` returns the contract
below, generated from `tickets` + `messages` + `accounts`. No 249 KB
`data.js`. Regeneration is a page load.

**D3 — Dates are chosen by the operator.** Default is this Monday–Sunday
in the schedule timezone. Query `start`+`end` set the current period
(max 31 days); the previous period is the same length immediately
before `start`. Query `week` still snaps to that week's Monday when
dates are omitted. A future `start` is rejected.

**D4 — Cohort vs flow.**

| Metric | Rule |
|---|---|
| created / total | `created_at` (else `fetch_date`) falls in the week |
| resolved | `state == closed` and `updated_at` (resolved proxy) falls in the week |
| open | created in the week and state is not `closed` / `archived` |
| escalation | created in the week and `_is_escalated` |
| SLA breach | FRT > `rules.sla_hours()`, or no first reply and age > SLA while a customer reply is still owed |
| FRT | created → first public non-bot support (`is_customer=0`) message, seconds |
| resolution time | created → `updated_at` on closed tickets, seconds |

Deleted (`deleted_at`) and `archived` tickets are out. QC
`excluded_states` is a scoring scope and does **not** apply here — this
is an operations dashboard.

**D5 — Escalation predicate.** True if any of:

- state in `waiting_on_engg`, `waiting_on_engineering`
- `request_category` (field map) is in `rules.oncall_categories()` or
  starts with `oncall`
- `resolution_category` contains `escalat`
- `does_rootly_exist` is Yes, or `rootly.incident_reference` is filled

**D6 — Filters re-aggregate `allRows`.** Changing a filter rebuilds
KPIs, daily series, breakdowns, the agent table (from rows), and the
ticket list. CSAT stays the stored object for the loaded period. `#clearFilters` resets
to both weeks, all agents/priorities/categories/statuses, escalations
unrestricted.

**D7 — Insights are functions of `D`.** No agent names or category
strings in source. Empty weeks produce an empty list, not leftover
copy.

**D8 — Nocturne only.** Inter, shell tokens, outlined buttons, no old
palette hexes, no `rgba(r,g,b)` except ambient `rgba(0,0,0,…)`. Chart.js
4.4.0 from the jsDelivr CDN. Skeleton via `QC.skeleton` on period swap.

**D9 — New checks default off does not apply.** This tab does not
score. It must not call Gemini, refetch, or write tickets.

---

## `D` contract

`assets/app.js` reads a **nested** object, not a flat map. Confirmed against
the extracted source (2026-09-07 zip):

```
D.generatedAt / D.prevWeekLabel / D.currWeekLabel
D.metrics.*          scalars + pv_status / cv_status
D.dailyData.*        7-entry series (cv_resolved here is an array)
D.priorities / categories / customers / escCategories
D.agents / D.agentTable
D.csatPrev / D.csatCurr
D.allRows
```

Extra keys (`week_start`, `timezone`, `coverage`, `insights`) are allowed.
`cv_resolved` / `cv_esc` exist on **both** `metrics` (number) and
`dailyData` (length-7 array). Flattening them was a collision.

```
generatedAt      string   "2026-08-17 20:21:29 IST"
prevWeekLabel    string   "Aug 2-8, 2026"
currWeekLabel    string   "Aug 9-15, 2026"
```

### metrics (scalars + status maps)

Prefix `pv_` = previous week, `cv_` = current week.

```
totals       pv_total, cv_total, total_diff, total_pct
open         pv_open, cv_open, open_diff, open_pct
resolved     pv_resolved, cv_resolved
escalations  pv_esc, cv_esc, esc_diff, esc_pct, pv_esc_rate, cv_esc_rate
reopens      pv_reopen, cv_reopen, pv_reopen_rate, cv_reopen_rate
             (always 0 — not in the store; UI labels unavailable)
sla          pv_sla_breaches, cv_sla_breaches
frt (secs)   {pv,cv}_frt_avg, _med, _p90, _min, _max  + frt_pct
res (secs)   {pv,cv}_res_avg, _med, _p90, _min, _max  + res_pct
status       pv_status, cv_status  -> { "<display name>": count }
```

Status display names and chart order (hardcoded, matching the original):

```
Closed, On customer, Waiting on Engg, Investigating, On you,
Waiting on CSM, On hold, Waiting on Legal, Waiting on Product
```

Internal state → display:

```
closed                         → Closed
waiting_on_customer            → On customer
waiting_on_engg
waiting_on_engineering         → Waiting on Engg
investigating                  → Investigating
waiting_on_you, new            → On you
waiting_on_csm                 → Waiting on CSM
on_hold                        → On hold
waiting_on_legal               → Waiting on Legal
waiting_on_product             → Waiting on Product
```

Unknown states are omitted from the chart, not forced into "On you".

Percent fields are `null` when the previous denominator is 0.
`esc_rate` / `reopen_rate` are 0–100 against that week's `*_total`.

### dailyData (7 entries per array)

```
currDays, prevDays        string[7]   weekday labels Mon…Sun
                                      (x-axis is currDays for both series)
cv_created, pv_created    number[7]
cv_resolved, pv_resolved  number[7]
cv_esc, pv_esc            number[7]
cv_frt_mins, pv_frt_mins  number|null[7]   already minutes (daily mean)
cv_res_hrs, pv_res_hrs    number|null[7]   already hours (daily mean)
```

Daily created / FRT / esc use the created day. Daily resolved / res
use the resolved-proxy day.

### priorities / categories / customers / escCategories

```
labels string[]
prev   number[]
curr   number[]
```

- priorities: exactly `Urgent, High, Medium, Low, Unknown` (ticket
  `priority`, case-insensitive; anything else → Unknown)
- categories: top 12 by current-week created volume, then the rest as
  `Other` if needed
- customers: top 10 accounts by current-week created volume
- escCategories: top 8 categories among current-week escalations

Category / customer labels prefer Pylon `interpreted_value`, then
`value`, then a title-cased slug. Truncation (30 / 22 / 28 chars) stays
client-side.

### agents (parallel arrays)

Every assignee with a created ticket in either week, sorted by
`cv_assigned` desc. `Unassigned` when `assignee_name` is empty.

```
names
pv_assigned, cv_assigned
pv_resolved, cv_resolved     resolved-in-week, by current assignee
pv_frt, cv_frt               minutes (mean); null if none
pv_res, cv_res               hours (mean); null if none
pv_csat_avg, cv_csat_avg     mean score or null
```

### agentTable (objects, same people, same order)

```
agent
pv_assigned  cv_assigned  assigned_diff  assigned_pct
pv_resolved  cv_resolved  resolved_diff
pv_frt_avg   cv_frt_avg   frt_diff          (secs)
pv_res_avg   cv_res_avg   res_diff          (secs)
pv_frt_p75   cv_frt_p75                     (secs; null if < 2 samples)
pv_frt_p90   cv_frt_p90                     (secs; null if < 2 samples)
pv_escalations  cv_escalations
pv_sla_breaches cv_sla_breaches
pv_reopened     cv_reopened                 (0)
cv_backlog                                  created this week, still open
pv_csat_* / cv_csat_*                       from stored responses
```

Nulls are expected. The percentile chart drops agents whose p75/p90
are null. CSV export reads this array (current filter, if any).

### csatPrev / csatCurr

```
total, avg, star5, star4, low, positivePct
agents [{name, total, scores, star5, star4, low, positivePct, avg, award}]
```

### allRows

One object per ticket that was **created** in either week. 19 keys:

```
issue, account, priority, category, issue_type, assignee, status
created_str, resolved_str, created_day
frt_secs, res_secs
is_resolved, is_open, is_escalated, is_reopened, sla_breached
pylon_link
week          "Previous Week" | "Current Week"
```

`#fWeek` / `#fEsc` option values match `week` and `is_escalated`.

### insights (new, required)

Array of `{ title, body }` derived at generate time. No literals that
name a person, account, or category.

---

## Surfaces

| Surface | Work |
|---|---|
| `weekly.py` | `build(week_start=None, *, start=None, end=None, now=None) -> D` |
| `app.py` | `GET /weekly`, `GET /api/weekly?week=&start=&end=`, `GET /api/weekly/csat` |
| `static/weekly.html` | page + render + live filters + CSV |
| `static/shell.js` | nav entry + `currentPage` path |
| `tests/t_weekly.py` | generator contract |
| `tests/t_weekly_http.py` | auth, validation, page chrome |
| `tests/t_theme.py` | skeleton + Inter (automatic via glob) |
| `tests/run.sh` | both new suites |

`GET /api/weekly` 400s on an unparseable `week`. Auth: signed-in, same
as every other tab.

---

## Non-goals

* Porting the original inline CSS palette or light-mode toggle. The
  app is Nocturne-dark.
* Storing or fetching reopen history.
* Calling Gemini from this tab. CSAT survey pull on `/api/weekly` is
  allowed and is skipped when Pylon is not configured.
* Changing R-checks, overall, or the leaderboard.
* Baking a weekly dump into git.

---

## Test plan

- [ ] Monday–Sunday buckets in the schedule timezone; non-Monday `week`
      snaps; future Monday clamps.
- [ ] Created this week → `cv_total`; last week → `pv_total`.
- [ ] Closed + `updated_at` this week → `cv_resolved` even if created
      earlier (flow). Open = created this week and not closed.
- [ ] Escalation predicate hits eng-wait, oncall category, resolution
      "escalat", Rootly yes.
- [ ] FRT is first support reply; SLA breach when FRT > configured SLA.
- [ ] Deleted and archived tickets are absent from `allRows`.
- [ ] Insights name the actual top category from the fixture, not a
      hardcoded string in source.
- [ ] `/weekly` 200 + `data-page="weekly"`; `/api/weekly` 401 then 200;
      bad `week` → 400.
- [ ] `t_theme`: no old hexes / orphan vars / white fills; page uses
      `QC.skeleton.kpis`.
