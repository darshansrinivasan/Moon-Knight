---
name: pylon-qc
description: Set up and run automated QC checks on Pylon support tickets. Use when asked to "run QC", "score tickets", "check a date", or "set up the QC system". Covers full setup from scratch as well as day-to-day scoring.
---

# Pylon Support QC — Full Reference

## Project layout

```
qc/
├── app.py            # FastAPI backend, auth gate, dashboard + admin REST API
├── auth.py           # Google OAuth sign-in, signed sessions, roles
├── vault.py          # Encrypted credential store, settings, audit log
├── db.py             # SQLite schema, query helpers, advisory locks
├── scorer.py         # Deterministic R-checks (R1–R8)
├── qc_runner.py      # AI A-checks on Gemini via Vertex AI
├── pylon.py          # Pylon API client (token from the vault)
├── slack.py          # Slack report delivery
├── scheduler.py      # Automated daily fetch → QC → Slack
├── resync_overall.py # Recomputes overall_result after refetch
├── static/
│   ├── index.html    # Dashboard
│   ├── admin.html    # Admin console
│   └── login.html    # Google sign-in
├── gcp.py            # Google Cloud project/model discovery
├── requirements.txt
├── railway.json      # start command, single replica, /healthz check
└── .env              # 5 bootstrap variables — everything else lives in the vault
```

DB path: `./qc.db` (SQLite, created on first startup)
Run: `uvicorn app:app --port 8000` → http://localhost:8000

**Single worker only** — SQLite has one writer. For more than ~10 users, move to Postgres.

---

## Configuration model

One rule decides where a setting lives:

> **The environment holds what is needed *before* the UI exists. Everything else is
> managed in the UI.**

That is five variables, and no more:

| Variable | Why it can't live in the UI |
|---|---|
| `QC_MASTER_KEY` | Encrypts the settings the UI stores, and signs the session that gets you to the UI. |
| `GOOGLE_OAUTH_CLIENT_ID` | Needed to render a sign-in button at all. |
| `GOOGLE_OAUTH_CLIENT_SECRET` | Same. |
| `QC_ADMIN_EMAILS` | Decides who may open Admin in the first place. |
| `QC_DB_PATH` | The UI is stored *in* the database, so it can't say where the database is. |

Everything else — Pylon token, Slack token and channel, Vertex service account, project,
region, models, and the whole daily schedule — is set in **Admin**, encrypted at rest,
and changeable without a redeploy. The API never returns a secret's plaintext, only a
masked hint like `••••5f2b`.

Values found in the environment under their old names (`PYLON_API_TOKEN`,
`SLACK_BOT_TOKEN`, `VERTEX_PROJECT`, `SCHEDULE_*`, …) are imported into the vault **once**,
on first boot, so an existing `.env` migrates itself. After that the vault is
authoritative: removing a credential in the UI keeps it removed.

---

## Deploying to Railway (or any container platform)

```bash
railway up          # railway.json sets the start command and /healthz check
```

Set the five variables above. Two of them cause **silent data loss** if missed, so
`/healthz` and the Admin banner both report them as blocking rather than letting the
app look healthy:

* **`QC_MASTER_KEY`** — container filesystems are writable but ephemeral, so a generated
  key file *saves* and then vanishes on the next deploy, signing everyone out and
  orphaning stored credentials.
* **`QC_DB_PATH`** — mount a volume (e.g. `/data`) and set `QC_DB_PATH=/data/qc.db`.
  Without it the database lives inside the container and every deploy erases all QC history.

`QC_BASE_URL` is auto-detected from `RAILWAY_PUBLIC_DOMAIN`, so you only need it for a
custom domain — but the OAuth redirect URI in Google Cloud must match it exactly
(`<base-url>/auth/callback`). Cookies are marked `Secure` automatically when the base URL
is https. Run **one replica**: SQLite has a single writer.

Then sign in and finish setup in **Admin**: paste the Pylon and Slack tokens, connect
Google Cloud, pick the project and models, and set the schedule.

Check the deploy logs — startup prints exactly what is configured, where each value came
from, and what is blocking.

---

## Vertex AI: connecting and choosing a model

There are two ways to authenticate, resolved in this order:
**service account → connected Google account → Application Default Credentials.**

**Connect a Google account** (Admin → AI → *Connect Google Cloud*). This runs an OAuth
consent for the `cloud-platform` scope and stores the resulting refresh token encrypted.
It reuses the app's existing OAuth client *and redirect URI*, so there is nothing extra to
register in Google Cloud Console — but the `cloud-platform` scope must be added to the
OAuth consent screen. For an **Internal** Workspace app that needs no Google verification.

**Or paste a service account** in Admin → Credentials, raw JSON or base64
(`base64 -i sa.json | tr -d '\n'`). Better for unattended hosting since it never expires
or needs re-consent, and it takes precedence if both are present.

Either way, the **project, region and models become pickers** rather than typed strings:

* Projects come from Cloud Resource Manager — only ones the active credential can actually
  use are listed, so a typo can't reach production. If listing is blocked (the API isn't
  enabled, or the service account lacks the permission), the current value stays selected
  and you can still type it in.
* Models come from Vertex's publisher-model list for the chosen project **and region**,
  filtered to Gemini models that support text generation. Changing region re-queries,
  because availability genuinely differs by region.
* Click model chips to include or exclude them; the number on each shows its position in
  the fallback order used when one is rate-limited.

Both are stored in the vault like every other runtime setting — no environment variable
involved, and changeable without a redeploy.

---

## Running locally

```bash
pip install -r requirements.txt
cp .env.example .env      # fill in the OAuth client and QC_ADMIN_EMAILS
uvicorn app:app --port 8000
```

`QC_MASTER_KEY` and `QC_DB_PATH` are optional locally — a git-ignored `.master.key` file
is generated and the database sits next to the code. Everything else is configured in
**Admin** after signing in, using the **Test** button on each credential.

---

## Access control

All routes require a signed-in `@spotdraft.com` Google account except `/login`,
`/healthz` and `/auth/*`. Sessions are HMAC-signed HttpOnly cookies valid 12 hours; role
and active status are re-read from the database on every request, so disabling someone in
Admin → People revokes their access immediately.

| Role | Can |
|---|---|
| `member` | Use the dashboard; **view** Admin read-only — configuration state, schedule and recent runs, but no secret hints and no activity log |
| `admin` | Everything, plus credentials, settings, schedule, people and the audit log |

Admin is granted by `QC_ADMIN_EMAILS` and reapplied at every sign-in, so that list stays
authoritative. Every mutating endpoint checks the role server-side — the read-only UI is
a convenience, not the control.

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

-- platform tables
app_users      -- email, name, role ('admin'|'member'), is_active, last_login_at
credentials    -- key, value_enc (Fernet), updated_by, updated_at
app_settings   -- key, value (Vertex project, schedule, Slack channel…)
audit_log      -- ts, user_email, action, detail  (never secret values)
scheduled_runs -- run_date, triggered_by, status, fetched, scored, error, slack_ok
run_locks      -- advisory locks: name, holder, expires_at
```

---

## Day-to-day workflow

Normally there is nothing to do: the scheduler runs the whole pipeline daily and posts
the result to Slack. The manual paths remain for backfills and re-checks.

1. **Fetch** — `POST /api/fetch/YYYY-MM-DD` pulls all active (non-archived) tickets from
   Pylon for that day and scores R1–R8 immediately. Stores results in `tickets`,
   `messages`, `accounts`, `rule_checks`, `fetch_log`.

2. **Run QC** — `POST /api/qc/YYYY-MM-DD` runs AI evaluation (A1–A5) via Vertex AI on any
   ticket whose `fetched_at > ai_checks.checked_at` (i.e. new or refetched tickets).
   Writes to `ai_checks` with `overall_result` and `ai_notes`.

3. **Refetch** — same as Fetch. Run `POST /api/fetch/YYYY-MM-DD` again after a ticket
   is updated in Pylon. R-checks update immediately; then Run QC re-evaluates stale tickets.

4. **Automated run** — the scheduler fires at the configured local time, running
   fetch → QC → Slack for the target day. A run that fails retries up to 3 times with a
   15-minute gap, then stops until the next day; failures are reported to Slack and shown
   in Admin → Recent runs. Admins can trigger the same pipeline any time with **Run now**.

Every fetch/QC takes a per-date advisory lock, so two people (or the scheduler and a
person) can never process the same day at once — the second caller gets `409`.


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
