# Pylon QC — Multi-User Platform Spec (v2)

Status: **approved for build** · Author: audit + build session, 2026-08-26

Turns the single-user localhost QC tool into a shared internal application for
~10 SpotDraft users, with Google SSO, admin-managed credentials, Vertex AI,
an automated daily run, and Slack delivery.

---

## 1. Goals

| # | Goal | Outcome |
|---|------|---------|
| G1 | Multi-user, ≥10 people | Google OAuth SSO, per-user identity, roles, audit log, safe concurrency |
| G2 | Domain-restricted access | Only `@spotdraft.com` Google accounts may sign in |
| G3 | No hard-coded credentials | All secrets live encrypted in the DB, managed through an Admin UI |
| G4 | Gemini via Vertex AI | OAuth/ADC service-account auth against a customer-owned GCP project — no API keys |
| G5 | No manual daily run | Built-in scheduler runs fetch → QC on a cron-like schedule |
| G6 | Results delivered, not pulled | Scheduled run posts a summary to Slack automatically |

### Non-goals
Not rebuilding the QC logic (R1–R8 / A1–A5 semantics are unchanged), not migrating
off SQLite, not changing the Pylon data model, not building a mobile UI.

---

## 2. Architecture

```
Browser ──► Google OAuth ──► /auth/callback ──► signed session cookie (HttpOnly)
   │
   ▼
FastAPI app ── AuthMiddleware (every route except /auth/*, /healthz, /setup)
   │
   ├── Dashboard API      (role: member+)   fetch / QC / export / analytics
   ├── Admin API          (role: admin)     credentials, settings, users, schedule
   ├── Scheduler (asyncio task)  ── daily ──► fetch → QC → Slack
   │
   ▼
SQLite (WAL)  ── credentials (Fernet-encrypted) · app_settings · app_users
                 audit_log · scheduled_runs · run_locks · existing QC tables
   │
   ├──► Pylon REST      (token from vault)
   ├──► Vertex AI       (service-account / ADC credentials, project + location from settings)
   └──► Slack Web API   (bot token from vault)
```

**Principle:** the process holds *no* secrets in environment variables except one
master encryption key. Everything else is administered at runtime through the UI.

---

## 3. Authentication (G1, G2)

**Flow:** OAuth 2.0 Authorization Code, confidential client.

1. `GET /auth/login?next=/` → redirect to Google with
   `scope=openid email profile`, `hd=spotdraft.com`, signed `state` (nonce + next path, 10-min TTL).
2. `GET /auth/callback?code&state` → verify state signature and TTL → exchange
   code at `oauth2.googleapis.com/token` → verify `id_token` signature against
   Google's JWKS via `google.oauth2.id_token.verify_oauth2_token`.
3. **Domain gate:** reject unless `hd == spotdraft.com` *and* `email` ends with
   `@spotdraft.com` *and* `email_verified` is true. Rejections render a clear
   "not authorized" page, never a stack trace.
4. Upsert `app_users`, issue session cookie, redirect to `next`.

**Session cookie** — `qc_session`, HttpOnly, SameSite=Lax, `Secure` when
`QC_COOKIE_SECURE=1`, 12-hour expiry. Value is `base64url(payload).hmac_sha256`,
signed with the master key. Stateless: no server-side session table.
Because SameSite=Lax suppresses cookies on cross-site POSTs, CSRF risk on the
mutating endpoints is mitigated without a separate token.

**Bootstrap (chicken-and-egg).** OAuth cannot be configured through an
OAuth-protected UI. Resolution: `/setup` is reachable **only** when no OAuth
client is configured yet **and** the request originates from loopback. It accepts
the Client ID/Secret and the first admin email, writes them to the vault, and
then permanently locks itself. Client ID/Secret may alternatively be seeded from
`GOOGLE_OAUTH_CLIENT_ID` / `GOOGLE_OAUTH_CLIENT_SECRET` env vars.

**Roles** — `admin` (everything, incl. credentials/users/schedule) and `member`
(dashboard: view, fetch, run QC, export). The first user ever to sign in is made
admin; additional bootstrap admins via `QC_BOOTSTRAP_ADMINS`. Admins can promote,
demote and deactivate users from the Admin UI. A deactivated user is rejected at
the next request even if their cookie is still valid. An admin cannot demote or
deactivate their own account (prevents lockout).

---

## 4. Credential vault (G3)

`credentials(key TEXT PK, value_enc BLOB, updated_by, updated_at)`.

* Encryption: **Fernet** (AES-128-CBC + HMAC-SHA256) from `cryptography`.
* Master key: `QC_MASTER_KEY` env var; if absent, auto-generated once to
  `.master.key` with mode `600`. Losing it invalidates stored credentials
  (recoverable by re-entering them).
* **Plaintext never leaves the server.** `GET /api/admin/credentials` returns only
  `{key, is_set, hint: "••••abcd", updated_by, updated_at}`.
* Writes are audit-logged (who, when, which key — never the value).
* Every credential has a **Test** action that proves the secret works before it is relied on.

| Key | Used by | Test |
|-----|---------|------|
| `pylon_api_token` | Pylon fetch | `GET /issues?limit=1` |
| `slack_bot_token` | Slack posting | `auth.test` |
| `google_oauth_client_id` / `_secret` | Login | validated at next sign-in |
| `vertex_service_account_json` | Vertex AI | 1-token generate call |

Adding a future integration = one row in the vault + one entry in the Admin UI
registry. No code change to the storage layer.

---

## 5. Vertex AI (G4)

Replaces `genai.Client(api_key=…)` with:

```python
genai.Client(vertexai=True, project=<setting>, location=<setting>, credentials=<resolved>)
```

**Credential resolution order**
1. Service-account JSON from the vault → `service_account.Credentials.from_service_account_info(..., scopes=["…/auth/cloud-platform"])`
2. Application Default Credentials (`gcloud auth application-default login`, or the
   VM/Cloud Run attached service account) — the recommended production path.

Settings: `vertex_project` (required), `vertex_location` (default `us-central1`),
`vertex_models` (ordered fallback list, defaults to Vertex-valid model IDs).
The client is cached per (project, location, credential-fingerprint) so we don't
rebuild it per batch; the cache invalidates when an admin changes any of those.

Failure modes are surfaced as human-readable admin errors ("Vertex project not
configured", "no credentials available") rather than `KeyError`.

---

## 6. Scheduler (G5)

An asyncio task started with the app; ticks every 30 s.

Settings: `schedule_enabled`, `schedule_time` (`HH:MM`), `schedule_tz`
(default `Asia/Kolkata`, matching the IST fetch window the scorer already uses),
`schedule_target` (`yesterday` | `today`).

Each tick: compute local time; if the scheduled minute has passed for today and
no successful run exists for today's trigger date, fire a run:

```
acquire lock → fetch_day(target) → run_qc_date(target) → post to Slack → record
```

* **Locking:** DB-backed advisory locks (`run_locks`, with expiry) so a scheduled
  run and a human clicking "Run QC" can never collide — correct even across workers.
* **History:** every run (scheduled or manual-triggered) is written to
  `scheduled_runs` with counts, duration, status and any error, and is visible in
  the Admin UI. Failures are reported to Slack too, not silently swallowed.
* **Catch-up:** a missed day (laptop asleep, deploy) is picked up on the next tick
  because the guard is "no successful run for this trigger date", not "exactly at HH:MM".
* Admins can **Run now** from the Admin UI to test the whole pipeline on demand.

---

## 7. Slack delivery (G6)

`chat.postMessage` with Block Kit. Content:

* Header: `QC Report — <date>`
* Totals: tickets, Pass / Fail / Needs Review, pass rate
* Rule-failure breakdown (which of R1–R8 fired most)
* Up to 5 failing tickets with number, title, assignee and the first note line
* Link back to the dashboard for that day

Settings: `slack_enabled`, `slack_channel` (e.g. `#support-qc`),
`dashboard_base_url` (for deep links). A **Send test message** button in the Admin
UI verifies token + channel before the first real run.

---

## 8. Concurrency & multi-user safety

| Risk | Mitigation |
|------|-----------|
| Two users run QC for the same day | DB advisory lock; second caller gets `409 Run already in progress` with the holder's name |
| Scheduler and human collide | Same lock |
| SQLite writer contention | WAL + `busy_timeout=30s` + short transactions (connections now close properly) |
| Who changed what | `audit_log(ts, user_email, action, detail)` for all mutations |
| Stale cookie after access revoked | Role and `is_active` re-checked from the DB on every request |

---

## 9. Data model additions

```sql
app_users(email PK, name, picture, role, is_active, created_at, last_login_at)
credentials(key PK, value_enc, updated_by, updated_at)
app_settings(key PK, value, updated_by, updated_at)
audit_log(id PK, ts, user_email, action, detail)
scheduled_runs(id PK, run_date, trigger_date, triggered_by, started_at,
               finished_at, status, fetched, scored, skipped, error, slack_ok)
run_locks(name PK, holder, acquired_at, expires_at)
```

All existing QC tables are unchanged — no migration risk to historical scores.

---

## 10. Rollout

1. `pip install -r requirements.txt` (adds `cryptography`)
2. Create the Google OAuth client (Web application) with redirect URI
   `<base-url>/auth/callback`; enable Vertex AI API on the GCP project.
3. Start the app → `/setup` on loopback → paste Client ID/Secret + first admin email.
4. Sign in → Admin → paste Pylon token, Slack bot token; set Vertex project; **Test** each.
5. Configure schedule + Slack channel → **Run now** to validate end-to-end.
6. Invite the team: they sign in with Google; admin grants roles as needed.

**Deployment note:** run a single uvicorn worker (SQLite writer). For >10 users or
multi-worker, the migration path is Postgres — the locking is already DB-backed
and portable.
