---
name: pylon-qc
description: Set up and run automated QC checks on Pylon support tickets. Use when asked to "run QC", "score tickets", "check a date", or "set up the QC system". Covers full setup from scratch as well as day-to-day scoring.
---

# Pylon Support QC — Full Reference

## Project layout

```
qc/
├── app.py            # FastAPI backend + REST API
├── db.py             # SQLite schema + query helpers
├── scorer.py         # Deterministic R-checks (R1–R8)
├── qc_runner.py      # AI A-checks via Gemini + note generation
├── pylon.py          # Pylon API client
├── resync_overall.py # Recomputes overall_result after refetch
├── static/
│   └── index.html    # Web dashboard
├── requirements.txt
└── .env              # PYLON_API_TOKEN, GEMINI_API_KEY
```

DB path: `./qc.db` (SQLite, created on first startup)
Dashboard: `uvicorn app:app --port 8000` → http://localhost:8000

---

## First-time setup (new machine / new project)

```bash
pip install -r requirements.txt
cp .env.example .env          # then fill in PYLON_API_TOKEN and GEMINI_API_KEY
python3 -c "import db; db.init_db()"   # creates qc.db with full schema
uvicorn app:app --port 8000
```

---

## DB schema (key tables)

```sql
tickets       -- one row per Pylon issue (id, number, fetch_date, title, state,
              --   assignee_name, account_id, custom_fields JSON, external_issues JSON,
              --   body_html, fetched_at)
messages      -- per-message rows (ticket_id, message_html, is_customer, is_private, timestamp)
accounts      -- customer accounts (id, name, domain, type)
rule_checks   -- deterministic scores (ticket_id, r1…r8, checked_at)
ai_checks     -- Gemini scores (ticket_id, a1…a5, ai_notes, overall_result, checked_at)
fetch_log     -- one row per fetched date (fetch_date, ticket_count, fetched_at)
```

---

## Day-to-day workflow

1. **Fetch** — `POST /api/fetch/YYYY-MM-DD` pulls all active (non-archived) tickets from
   Pylon for that day and scores R1–R8 immediately. Stores results in `tickets`,
   `messages`, `accounts`, `rule_checks`, `fetch_log`.

2. **Run QC** — `POST /api/qc/YYYY-MM-DD` runs Gemini AI evaluation (A1–A5) on any
   ticket whose `fetched_at > ai_checks.checked_at` (i.e. new or refetched tickets).
   Writes to `ai_checks` with `overall_result` and `ai_notes`.

3. **Refetch** — same as Fetch. Run `POST /api/fetch/YYYY-MM-DD` again after a ticket
   is updated in Pylon. R-checks update immediately; then Run QC re-evaluates stale tickets.

---

## R-Checks (deterministic — scored in scorer.py)

All R-checks run automatically on every fetch/refetch. Results stored in `rule_checks`.
Any Fail → `overall_result = Fail`.

### R1 — Functionalities
`custom_fields.functionalities` must be filled.
→ `Pass` | `Fail`

### R2 — Request Category
`custom_fields.request_category` must be filled.
→ `Pass` | `Fail`

### R3 — Customer Account
Ticket must be linked to a real external customer account.
Fails for: internal SpotDraft accounts, support catch-alls, dogfooding, sales trial, live chat.
Known internal IDs:
- `3890c56a-d883-49db-8de0-3164657007f6` → Support catch-all
- `7ade86a8-6b74-497a-a983-fcd15b785965` → SpotDraft Internal
→ `Pass` | `Fail`

### R4 — Response Time
No unanswered customer message older than 24 hours.
Only checked when the latest public non-bot message is from the customer.
→ `Pass` | `Fail` | `N/A` (closed, archived, or waiting_on_customer)

### R5 — Status Ownership
Ticket state must match who owns the next action. Delegation states require team mention in thread:

| State | Required evidence |
|---|---|
| `waiting_on_engg` / `waiting_on_engineering` | `@eng`, `@eng-be`, `@eng-fe`, or `@eng--oncall` in thread; OR a Rootly/Jira link |
| `waiting_on_csm` | `@cs` mentioned |
| `waiting_on_product` | `@pt` mentioned |
| `waiting_on_legal` | `@legal-ops` mentioned |
| `waiting_on_you` | always Fail (support hasn't responded) |
| `new` | must have an assignee |

→ `Pass` | `Fail` | `N/A` (investigating, on_hold, closed, archived)

### R7 — Engineering Escalation Evidence
Only for tickets in `waiting_on_engg` / `waiting_on_engineering`.
Checks `does_rootly_exist`, `rootly.incident_reference`, `external_issues`, message/body text.
- `does_rootly_exist = Yes` → Pass immediately
- `does_rootly_exist = No` → Fail immediately
- `rootly.incident_reference` filled → Pass
- Jira or Rootly link anywhere in text → Pass
→ `Pass` | `Fail` | `N/A`

### R8 — Oncall Completeness
Fires when `resolution_category = "Escalated to Oncall"` OR `rootly.incident_reference` has a value.
All four conditions must be met:
1. `does_rootly_exist = Yes`
2. `rootly.incident_reference` is filled (e.g. ROOT-1234)
3. A Jira link is present (in `external_issues` or message text)
4. `request_category` is one of:
   `oncall`, `oncall_integration_issues`, `oncall_oncall_tasks`,
   `oncall_performance_issues`, `oncall_third_party_dependency_issue`

→ `Pass` | `Fail` | `N/A` (no oncall evidence at all)

---

## A-Checks (AI — scored via Gemini in qc_runner.py)

Scored by Gemini in batches of 6. Model cascades through a fallback list on quota errors.
Results stored in `ai_checks`.

### A1 — Category Accuracy · `Pass` / `Fail` / `Needs Review`
Do `functionalities` and `request_category` match what the customer actually asked?
Fail if clearly wrong. Needs Review if multiple categories reasonably apply.

### A2 — Customer Sentiment · `Positive` / `Neutral` / `Concerned` / `Frustrated` / `Urgent`
Informational only — does NOT affect overall result.
Based on customer language, escalation cues, time in queue.

### A3 — Response Quality · `Good` / `Needs Improvement` / `Poor`
- **Good** — clear, accurate, empathetic, assigns ownership, concrete next steps
- **Needs Improvement** — vague or incomplete but serviceable
- **Poor** — incorrect guidance, missed ask, no next step, confusing handoff
**A3 = Poor → overall_result = Fail**

### A4 — Status vs Conversation · `Pass` / `Fail` / `Needs Review`
Qualitative counterpart to R5. Does the state reflect who actually owns the next action?
Cite the latest next action in AI notes.

### A5 — Not Closed Prematurely · `Pass` / `Fail` / `Needs Review` / `N/A`
N/A for open tickets. Pass if closure has a clear resolution or documented no-response follow-up.
Fail if customer's ask was still open at closure.
**A5 = Fail → overall_result = Fail**

---

## Overall result logic

| Result | Condition |
|---|---|
| **Fail** | Any R-check = Fail · OR · A3 = Poor · OR · A5 = Fail |
| **Needs Review** | No Fails, but ≥1 check is Needs Review |
| **Pass** | All checks Pass or N/A |

Note: A1 and A4 failures appear in AI notes but do NOT directly cause a Fail.

---

## AI notes format

Concise, pipe-separated. R-check failures always appear first, then AI observations.
Pattern: `R<n>: <specific reason> | A<n> <grade>: <reason>`

Examples:
- `R8: does_rootly_exist not set to Yes, request_category 'general_question' is not oncall`
- `R2: request_category field not filled | A3 Poor: no next step provided to customer`
- `A5 Fail: ticket closed while customer's signing issue was unresolved`

---

## Querying results

```sql
-- All tickets for a day with full QC results
SELECT t.number, t.title, t.state, t.assignee_name,
       a.name AS account_name,
       rc.r1, rc.r2, rc.r3, rc.r4, rc.r5, rc.r7, rc.r8,
       ac.a1, ac.a2, ac.a3, ac.a4, ac.a5,
       ac.overall_result, ac.ai_notes
FROM tickets t
LEFT JOIN accounts    a  ON t.account_id = a.id
LEFT JOIN rule_checks rc ON t.id = rc.ticket_id
LEFT JOIN ai_checks   ac ON t.id = ac.ticket_id
WHERE t.fetch_date = '2026-07-26'
ORDER BY t.number;

-- Stale tickets (refetched since last AI check) — what Run QC will re-score
SELECT t.number, t.fetched_at, ac.checked_at
FROM tickets t
LEFT JOIN ai_checks ac ON t.id = ac.ticket_id
WHERE t.fetch_date = '2026-07-26'
  AND (ac.ticket_id IS NULL OR t.fetched_at > ac.checked_at);
```

---

## custom_fields JSON keys

`functionalities`, `request_category`, `does_rootly_exist`, `rootly.incident_reference`,
`resolution_category`, `resolution_details`, `oncall_slack_chat_link`, `notes`, `question_type`

`external_issues` JSON array: `[{"source": "jira", "link": "https://spotdraft.atlassian.net/browse/SPD-123"}]`

---

## Utility scripts

| Script | Purpose |
|---|---|
| `resync_overall.py` | Recomputes `overall_result` + `ai_notes` for all QC'd tickets after R-scores change. Called automatically after every refetch. |
| `backfill_notes.py` | One-time: adds R-check failure notes to ai_notes for tickets scored before the notes feature existed. |
| `migrate_r8_r9.py` | One-time: computes R8/R9 for all historical tickets. |
