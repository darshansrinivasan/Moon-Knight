import asyncio
import csv
import io
import json
import logging
import os
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import (
    HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse,
)
from fastapi.staticfiles import StaticFiles

load_dotenv()

import auth
import db
import gcp
import pylon
import qc_runner
import resync_overall
import review
import scheduler
import scorer
import slack
import vault

STATIC_DIR = Path(__file__).parent / "static"


logging.basicConfig(
    level=os.getenv("QC_LOG_LEVEL", "INFO").upper(),
    format="%(levelname)s: %(message)s",
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    # Migrate any values still supplied as env vars into the vault, once.
    vault.import_legacy_env()
    vault.log_startup_config()
    scheduler.start()
    try:
        yield
    finally:
        await scheduler.stop()


app = FastAPI(title="Pylon QC", lifespan=lifespan)


def _page(name: str) -> HTMLResponse:
    return HTMLResponse(
        (STATIC_DIR / name).read_text(), headers={"Cache-Control": "no-store"}
    )


# ── auth gate ─────────────────────────────────────────────────────────────────

@app.middleware("http")
async def auth_gate(request: Request, call_next):
    path = request.url.path
    if auth.is_public_path(path):
        return await call_next(request)

    if auth.current_user(request) is None:
        if path.startswith("/api/"):
            return JSONResponse({"detail": "Sign-in required"}, status_code=401)
        nxt = request.url.path or "/"
        return RedirectResponse(f"/login?next={nxt}", status_code=302)

    return await call_next(request)


# ── sign-in ───────────────────────────────────────────────────────────────────

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if auth.current_user(request):
        return RedirectResponse("/", status_code=302)
    if not auth.oauth_configured():
        # Nothing to click through — say exactly which variables are missing.
        return HTMLResponse(
            "<h1>Sign-in is not configured</h1>"
            "<p>This deployment is missing <code>GOOGLE_OAUTH_CLIENT_ID</code> and "
            "<code>GOOGLE_OAUTH_CLIENT_SECRET</code>. Set them where the app is "
            "deployed and restart.</p>"
            f"<p>The OAuth client's redirect URI must be "
            f"<code>{auth.base_url(request)}/auth/callback</code>.</p>",
            status_code=503,
        )
    return _page("login.html")


@app.get("/auth/start")
async def auth_start(request: Request, next: str = "/"):
    if not next.startswith("/"):
        next = "/"          # never redirect off-site
    return RedirectResponse(auth.login_url(request, next), status_code=302)


@app.get("/auth/callback")
async def auth_callback(request: Request, code: str | None = None,
                        state: str | None = None, error: str | None = None):
    if error or not code or not state:
        return HTMLResponse(
            f"<h1>Sign-in cancelled</h1><p>{error or 'Missing authorization code'}.</p>"
            '<p><a href="/login">Try again</a></p>', status_code=400,
        )

    payload = auth._unsign(state, b"oauth-state")
    if not payload:
        return HTMLResponse(
            "<h1>Sign-in expired</h1><p>Please start again.</p>"
            '<p><a href="/login">Back to sign-in</a></p>', status_code=400,
        )

    # The same redirect URI serves sign-in and the Google Cloud connection;
    # the signed state says which flow this is.
    if payload.get("flow") == "gcp":
        return await _finish_cloud_connect(request, code, payload.get("next", "/admin"))

    try:
        identity = await auth.exchange_code(request, code)
    except HTTPException as e:
        return HTMLResponse(
            f"<h1>Access denied</h1><p>{e.detail}</p>"
            '<p><a href="/login">Back to sign-in</a></p>', status_code=e.status_code,
        )

    user = auth.upsert_user(identity["email"], identity["name"], identity["picture"])
    if not user.get("is_active", 1):
        return HTMLResponse(
            "<h1>Account disabled</h1><p>Your access has been revoked. "
            "Contact an administrator.</p>", status_code=403,
        )

    vault.audit(user["email"], "auth.login")
    resp = RedirectResponse(payload.get("next", "/"), status_code=302)
    auth.set_session_cookie(resp, auth.issue_session(user))
    return resp


async def _finish_cloud_connect(request: Request, code: str, next_path: str):
    """Store the Google Cloud refresh token obtained by the connect flow."""
    admin = auth.current_user(request)
    if not admin or admin["role"] != "admin":
        raise HTTPException(403, "Administrator access required")

    try:
        tokens   = await auth.exchange_tokens(request, code)
        identity = await auth.verify_identity(tokens.get("id_token"))
    except HTTPException as e:
        return HTMLResponse(
            f"<h1>Could not connect Google Cloud</h1><p>{e.detail}</p>"
            '<p><a href="/admin">Back to Admin</a></p>', status_code=e.status_code,
        )

    refresh_token = tokens.get("refresh_token")
    if not refresh_token:
        # Google only returns one on first consent; prompt=consent should force it.
        return HTMLResponse(
            "<h1>No refresh token returned</h1><p>Google did not return a long-lived "
            "token, which usually means this app was already authorised. Remove it at "
            '<a href="https://myaccount.google.com/permissions">Google account '
            'permissions</a> and connect again.</p>'
            '<p><a href="/admin">Back to Admin</a></p>', status_code=400,
        )

    vault.set_credential("google_cloud_refresh_token", refresh_token, admin["email"])
    vault.set_settings({"google_cloud_account": identity["email"]}, admin["email"])
    qc_runner.invalidate_vertex_client()
    vault.audit(admin["email"], "gcp.connect", identity["email"])
    return RedirectResponse(next_path or "/admin", status_code=302)


@app.get("/auth/google-cloud")
async def auth_google_cloud(request: Request, user: dict = Depends(auth.require_admin)):
    return RedirectResponse(auth.cloud_connect_url(request), status_code=302)


@app.get("/auth/logout")
async def auth_logout(request: Request):
    user = auth.current_user(request)
    if user:
        vault.audit(user["email"], "auth.logout")
    resp = RedirectResponse("/login", status_code=302)
    auth.clear_session_cookie(resp)
    return resp


@app.get("/healthz")
async def healthz():
    """Liveness plus a readiness summary — safe to expose, names no secrets."""
    r = vault.readiness()
    return {"ok": True, **r}


# ── identity ──────────────────────────────────────────────────────────────────

@app.get("/api/me")
async def me(user: dict = Depends(auth.require_user)):
    covered = review.assignees_for(user)
    return {
        "email": user["email"], "name": user["name"],
        "picture": user["picture"], "role": user["role"],
        "can_review_any": review.is_admin(user) or bool(covered),
    }


# ── dashboard ─────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index(user: dict = Depends(auth.require_user)):
    return _page("index.html")


@app.get("/admin", response_class=HTMLResponse)
async def admin_page(user: dict = Depends(auth.require_user)):
    """Readable by everyone signed in; the page renders read-only for members
    and every mutating endpoint behind it still requires an admin."""
    return _page("admin.html")


@app.get("/api/calendar/{year}/{month}")
async def get_calendar(year: int, month: int, user: dict = Depends(auth.require_user)):
    data = await asyncio.to_thread(db.get_calendar_month, year, month)
    return {"year": year, "month": month, "days": data}


# ── fetch a day from Pylon ────────────────────────────────────────────────────

async def fetch_and_store(target: date) -> int:
    """Fetch one day from Pylon, persist tickets/messages/accounts, score R1–R8.

    Shared by the manual endpoint and the scheduler. Returns the active ticket count.
    """
    date_str = target.isoformat()
    issues, messages_by_id, accounts_by_id = await pylon.fetch_day(target)
    now = datetime.now(timezone.utc).isoformat()

    def store() -> int:
        # build user cache from message authors
        user_cache: dict[str, tuple[str, str]] = {}  # id -> (name, email)
        for msgs in messages_by_id.values():
            for m in msgs:
                u = (m.get("author") or {}).get("user")
                if u and u.get("id"):
                    user_cache[u["id"]] = (
                        (m.get("author") or {}).get("name", ""),
                        u.get("email", ""),
                    )

        with db.get_conn() as conn:
            # accounts
            for acc in accounts_by_id.values():
                conn.execute("""
                    INSERT OR REPLACE INTO accounts
                        (id, name, domain, type, custom_fields, fetched_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (
                    acc["id"], acc.get("name"), acc.get("domain"),
                    acc.get("type"),
                    json.dumps(acc.get("custom_fields") or {}),
                    now,
                ))

            # users
            for uid, (name, email) in user_cache.items():
                conn.execute("""
                    INSERT OR IGNORE INTO users (id, name, email) VALUES (?, ?, ?)
                """, (uid, name, email))

            for issue in issues:
                is_archived = issue.get("state") == "archived"

                msgs     = messages_by_id.get(issue["id"], [])
                cf       = issue.get("custom_fields") or {}
                assignee = issue.get("assignee") or {}
                account  = issue.get("account") or {}

                # resolve assignee name
                assignee_name = None
                if assignee.get("id"):
                    cached = user_cache.get(assignee["id"])
                    if cached:
                        assignee_name = cached[0]

                acc_data = accounts_by_id.get(account.get("id"))

                priority = issue.get("priority")
                if not priority:
                    priority = (cf.get("priority") or {}).get("value") or \
                               (cf.get("priority") or {}).get("interpreted_value")

                ext_issues = issue.get("external_issues") or []

                cpv = issue.get("customer_portal_visible")
                conn.execute("""
                    INSERT OR REPLACE INTO tickets
                        (id, number, fetch_date, title, link, state, source, type,
                         priority, assignee_id, assignee_name, account_id,
                         custom_fields, external_issues, body_html,
                         created_at, updated_at, latest_message_time,
                         customer_portal_visible, fetched_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    issue["id"], issue.get("number"), date_str,
                    issue.get("title"), issue.get("link"),
                    issue.get("state"), issue.get("source"), issue.get("type"),
                    priority, assignee.get("id"), assignee_name, account.get("id"),
                    json.dumps(cf), json.dumps(ext_issues), issue.get("body_html"),
                    issue.get("created_at"), issue.get("updated_at"),
                    issue.get("latest_message_time"),
                    1 if cpv else 0, now,
                ))

                # Skip messages and scoring for archived tickets — state is
                # persisted above so the dashboard shows current assignee/state.
                if is_archived:
                    continue

                # messages
                for m in msgs:
                    author   = m.get("author") or {}
                    contact  = author.get("contact") or {}
                    user_    = author.get("user") or {}
                    is_cust  = "contact" in author
                    email    = contact.get("email") or user_.get("email")
                    conn.execute("""
                        INSERT OR REPLACE INTO messages
                            (id, ticket_id, message_html, timestamp, source,
                             author_name, author_email, is_customer, is_private)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        m["id"], issue["id"], m.get("message_html"),
                        m.get("timestamp"), m.get("source"),
                        author.get("name"), email,
                        1 if is_cust else 0,
                        1 if m.get("is_private") else 0,
                    ))

                # R1–R8 scoring
                scores = scorer.score_all(issue, msgs, acc_data, ext_issues)
                conn.execute("""
                    INSERT OR REPLACE INTO rule_checks
                        (ticket_id, fetch_date, r1, r2, r3, r4, r5, r7, r8, r9, checked_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    issue["id"], date_str,
                    scores["r1"], scores["r2"], scores["r3"],
                    scores["r4"], scores["r5"], scores["r7"],
                    scores["r8"], scores["r9"],
                    now,
                ))

            active_count = sum(1 for i in issues if i.get("state") != "archived")
            conn.execute("""
                INSERT OR REPLACE INTO fetch_log (fetch_date, ticket_count, fetched_at)
                VALUES (?, ?, ?)
            """, (date_str, active_count, now))
            return active_count

    count = await asyncio.to_thread(store)
    # recompute overall_result for any already-QC'd tickets whose R-scores changed
    await asyncio.to_thread(resync_overall.run)
    return count


@app.post("/api/fetch/{date_str}")
async def fetch_day(date_str: str, user: dict = Depends(auth.require_user)):
    try:
        target = date.fromisoformat(date_str)
    except ValueError:
        raise HTTPException(400, "Use YYYY-MM-DD")

    try:
        with db.advisory_lock(f"fetch:{date_str}", user["email"], ttl_seconds=1800):
            count = await fetch_and_store(target)
    except db.LockBusy as e:
        raise HTTPException(409, str(e))
    except pylon.PylonNotConfigured as e:
        raise HTTPException(503, str(e))
    except Exception as e:
        raise HTTPException(502, f"Pylon fetch failed: {e}")

    vault.audit(user["email"], "fetch.day", f"{date_str} ({count} tickets)")
    return {"date": date_str, "ticket_count": count,
            "fetched_at": datetime.now(timezone.utc).isoformat()}


# ── run AI checks (A1–A5) for a day ──────────────────────────────────────────

@app.post("/api/qc/{date_str}")
async def run_qc(date_str: str, user: dict = Depends(auth.require_user)):
    try:
        date.fromisoformat(date_str)
    except ValueError:
        raise HTTPException(400, "Use YYYY-MM-DD")

    log = await asyncio.to_thread(db.get_fetch_log, date_str)
    if not log:
        raise HTTPException(404, "Day not fetched yet — fetch tickets first")

    try:
        with db.advisory_lock(f"qc:{date_str}", user["email"], ttl_seconds=3600):
            result = await asyncio.to_thread(qc_runner.run_qc_date, date_str, user["email"])
    except db.LockBusy as e:
        raise HTTPException(409, str(e))
    except qc_runner.VertexNotConfigured as e:
        raise HTTPException(503, str(e))
    except Exception as e:
        raise HTTPException(502, qc_runner.explain_vertex_error(e))

    vault.audit(user["email"], "qc.run", f"{date_str} scored={result.get('scored')}")
    return {"date": date_str, **result}


# ── tickets for a day ─────────────────────────────────────────────────────────

@app.get("/api/day/{date_str}")
async def get_day(date_str: str, user: dict = Depends(auth.require_user)):
    def load():
        log = db.get_fetch_log(date_str)
        tickets = review.annotate_tickets(db.get_day_tickets(date_str), user)
        return log, tickets, db.latest_qc_run(date_str)

    log, tickets, last_run = await asyncio.to_thread(load)
    return {
        "date": date_str,
        "fetched": log is not None,
        "ticket_count": len(tickets),
        "tickets": tickets,
        "last_run": last_run,
    }


# ── single ticket detail with messages ───────────────────────────────────────

@app.get("/api/ticket/{ticket_id}")
async def get_ticket(ticket_id: str, user: dict = Depends(auth.require_user)):
    def query():
        with db.get_conn() as conn:
            t = conn.execute("""
                SELECT t.*, a.name AS account_name, a.domain AS account_domain,
                       a.type AS account_type,
                       rc.r1, rc.r2, rc.r3, rc.r4, rc.r5, rc.r6, rc.r7,
                       ac.a1, ac.a2, ac.a3, ac.a4, ac.a5, ac.ai_notes, ac.overall_result
                FROM tickets t
                LEFT JOIN accounts    a  ON t.account_id = a.id
                LEFT JOIN rule_checks rc ON t.id = rc.ticket_id
                LEFT JOIN ai_checks   ac ON t.id = ac.ticket_id
                WHERE t.id = ?
            """, (ticket_id,)).fetchone()
            if not t:
                return None, []
            msgs = conn.execute(
                "SELECT * FROM messages WHERE ticket_id = ? ORDER BY timestamp",
                (ticket_id,)
            ).fetchall()
            return dict(t), [dict(m) for m in msgs]

    ticket, messages = await asyncio.to_thread(query)
    if not ticket:
        raise HTTPException(404, "Ticket not found")
    review.annotate_tickets([ticket], user)
    return {"ticket": ticket, "messages": messages}


@app.post("/api/ticket/{ticket_id}/review")
async def review_ticket(ticket_id: str, request: Request,
                        user: dict = Depends(auth.require_user)):
    """Sign off one ticket. Admins: any ticket. Coverage reviewers: their assignees only."""
    body = await request.json()
    decision = body.get("decision") or ""
    note = body.get("note") or ""
    try:
        record = await asyncio.to_thread(review.accept_ticket, ticket_id, user, decision, note)
    except review.ReviewDenied as e:
        raise HTTPException(403, str(e))
    except review.ReviewInvalid as e:
        raise HTTPException(400, str(e))
    vault.audit(
        user["email"], "ticket.review",
        f"{ticket_id} {record['decision']}" + (" (kept AI)" if record["kept_ai"] else ""),
    )
    return {"ok": True, "review": {
        "decision": record["decision"],
        "kept_ai": bool(record["kept_ai"]),
        "reviewer_email": record["reviewer_email"],
        "reviewer_name": record["reviewer_name"],
        "reviewed_at": record["reviewed_at"],
        "note": record["note"],
    }}


@app.get("/api/review/coverages")
async def get_coverages(user: dict = Depends(auth.require_user)):
    def load():
        return {
            "coverages": review.list_coverages(),
            "assignees": review.list_assignee_names(),
            "can_edit": user["role"] == "admin",
        }
    return await asyncio.to_thread(load)


@app.put("/api/review/coverages")
async def put_coverage(request: Request, user: dict = Depends(auth.require_admin)):
    body = await request.json()
    try:
        coverages = await asyncio.to_thread(review.save_coverage, body, user["email"])
    except review.ReviewInvalid as e:
        raise HTTPException(400, str(e))
    vault.audit(user["email"], "review.coverage.save",
                f"{body.get('name')} → {body.get('reviewer_email')}")
    return {"ok": True, "coverages": coverages, "assignees": review.list_assignee_names()}


@app.delete("/api/review/coverages/{coverage_id}")
async def delete_coverage(coverage_id: int, user: dict = Depends(auth.require_admin)):
    coverages = await asyncio.to_thread(review.delete_coverage, coverage_id)
    vault.audit(user["email"], "review.coverage.delete", str(coverage_id))
    return {"ok": True, "coverages": coverages}


@app.get("/api/directory/reviewers")
async def directory_reviewers(q: str = "", user: dict = Depends(auth.require_user)):
    """Slack people with an email on the login domain — pickable as reviewers."""
    try:
        results = await slack.search_reviewers(q, auth.ALLOWED_DOMAIN)
        return {"ok": True, "results": results}
    except slack.SlackNotConfigured:
        return {"ok": False, "message": "Slack bot token is not configured", "results": []}
    except Exception as e:
        return {"ok": False, "message": str(e)[:300], "results": []}


# ── scoring rules ───────────────────────────────────────────────────────────────

RULE_DESCRIPTIONS = {
    "r1": ("R1 — Functionality", "The 'functionalities' custom field must be filled. No parameters."),
    "r2": ("R2 — Request category", "The 'request_category' custom field must be filled. No parameters."),
    "r3": ("R3 — Real customer account", "The ticket must link to a genuine external account — not an internal catch-all, dogfooding, or trial account."),
    "r4": ("R4 — Response time", "No customer message may go unanswered longer than the SLA."),
    "r5": ("R5 — Status ownership", "A ticket's state must match who owns the next action, proven by an @-mention of someone on the right team (or a Rootly/Jira link for engineering)."),
    "r7": ("R7 — Rootly/Jira link", "Engineering tickets must reference a Rootly incident or Jira issue. No parameters."),
    "r8": ("R8 — Oncall completeness", "When escalated to oncall, all four fields must be consistent: rootly exists, reference filled, Jira linked, category is an oncall one."),
    "a":  ("A1–A5 — AI grading", "Category accuracy, customer sentiment, response quality, status-vs-conversation, and premature closure — graded by Gemini against a fixed rubric with pinned generation."),
}


@app.get("/rules", response_class=HTMLResponse)
async def rules_page(user: dict = Depends(auth.require_user)):
    return _page("rules.html")


ROSTER_KEYS = (
    "cs_user_ids", "impl_user_ids", "impl_group_ids",
    "eng_user_ids", "eng_group_ids", "pt_user_ids", "pt_group_ids",
)
ACCOUNT_KEY = "r3_internal_account_ids"


@app.get("/api/rules")
async def get_rules(user: dict = Depends(auth.require_user)):
    import rules as qc_rules

    def states_seen():
        with db.get_conn() as conn:
            return [r["state"] for r in conn.execute(
                "SELECT state, COUNT(*) n FROM tickets WHERE state IS NOT NULL"
                " GROUP BY state ORDER BY n DESC").fetchall()]

    current = qc_rules.current()
    fallback = scorer.default_display_names()

    slack_ids = []
    for key in ROSTER_KEYS:
        for line in current.get(key, []):
            sid, _ = qc_rules.parse_entry(line)
            if sid:
                slack_ids.append(sid)
    try:
        resolved = await slack.resolve_ids(slack_ids)
    except Exception:
        resolved = {}

    account_ids = [qc_rules.parse_entry(x)[0] for x in current.get(ACCOUNT_KEY, [])]
    account_ids = [i for i in account_ids if i]
    acc_names = await asyncio.to_thread(db.account_names, account_ids)

    labels = {key: qc_rules.labeled_entries(key, resolved, fallback) for key in ROSTER_KEYS}
    labels[ACCOUNT_KEY] = qc_rules.labeled_entries(
        ACCOUNT_KEY, acc_names, fallback
    )

    return {
        "rules":        current,
        "defaults":     qc_rules.defaults(),
        "labels":       labels,
        "rules_hash":   qc_rules.rules_hash(),
        "descriptions": RULE_DESCRIPTIONS,
        "states_seen":  await asyncio.to_thread(states_seen),
        "meta":         vault.get_setting_meta(qc_rules.RULES_KEY),
        "can_edit":     user["role"] == "admin",
        "rubric":       qc_runner.SYSTEM_PROMPT,
    }


@app.get("/api/directory/slack")
async def directory_slack(q: str = "", kind: str = "user",
                          user: dict = Depends(auth.require_user)):
    """Name search over Slack users or groups. Used by the Rules picker."""
    if kind not in ("user", "group"):
        raise HTTPException(400, "kind must be user or group")
    try:
        return {"ok": True, "results": await slack.search_directory(q, kind)}
    except slack.SlackNotConfigured:
        return {"ok": False, "message": "Slack bot token is not configured", "results": []}
    except Exception as e:
        return {"ok": False, "message": str(e)[:300], "results": []}


@app.get("/api/directory/accounts")
async def directory_accounts(q: str = "", user: dict = Depends(auth.require_user)):
    """Name search over Pylon accounts already fetched into the local database."""
    rows = await asyncio.to_thread(db.search_accounts, q)
    return {"ok": True, "results": [{"id": r["id"], "name": r["name"]} for r in rows]}


@app.put("/api/rules")
async def put_rules(request: Request, user: dict = Depends(auth.require_admin)):
    import rules as qc_rules
    body = await request.json()
    candidate = body.get("rules")
    if not isinstance(candidate, dict):
        raise HTTPException(400, "Body must be {\"rules\": {...}}")

    errors = await asyncio.to_thread(qc_rules.save, candidate, user["email"])
    if errors:
        # Rejected by validation — nothing was saved.
        return JSONResponse({"ok": False, "errors": errors}, status_code=422)

    vault.audit(user["email"], "rules.update",
                f"hash={qc_rules.rules_hash()} keys={', '.join(sorted(candidate.keys()))}")
    return {"ok": True, "rules": qc_rules.current(), "rules_hash": qc_rules.rules_hash(),
            "meta": vault.get_setting_meta(qc_rules.RULES_KEY)}


# ── run history ───────────────────────────────────────────────────────────────

@app.get("/runs", response_class=HTMLResponse)
async def runs_page(user: dict = Depends(auth.require_user)):
    return _page("runs.html")


@app.get("/api/runs")
async def list_runs(date: str | None = None, user: dict = Depends(auth.require_user)):
    def query():
        with db.get_conn() as conn:
            where, params = ("WHERE date = ?", (date,)) if date else ("", ())
            scoring = [dict(r) for r in conn.execute(
                f"SELECT * FROM qc_runs {where} ORDER BY id DESC LIMIT 50", params
            ).fetchall()]
        return scoring
    return {
        "runs":      await asyncio.to_thread(query),
        "scheduled": scheduler.recent_runs(25),
        "schedule":  scheduler.next_run_description(),
        "settings":  {
            "schedule_enabled": vault.get_setting("schedule_enabled"),
            "schedule_time":    vault.get_setting("schedule_time"),
            "schedule_tz":      vault.get_setting("schedule_tz"),
            "schedule_target":  vault.get_setting("schedule_target"),
        },
        "can_edit":  user["role"] == "admin",
    }


@app.get("/api/runs/{run_id}")
async def run_detail(run_id: int, user: dict = Depends(auth.require_user)):
    def query():
        with db.get_conn() as conn:
            run = conn.execute("SELECT * FROM qc_runs WHERE id = ?", (run_id,)).fetchone()
            if not run:
                return None
            run = dict(run)
            try:
                run["config"] = json.loads(run.pop("config_json") or "{}")
            except json.JSONDecodeError:
                run["config"] = {}

            diff = []
            prev_id = run.get("compared_to")
            if prev_id:
                rows = conn.execute("""
                    SELECT cur.number, cur.ticket_id,
                           t.title, t.assignee_name, t.link,
                           old.overall_result AS before_overall,
                           cur.overall_result AS after_overall,
                           old.a1 b1, cur.a1 n1, old.a3 b3, cur.a3 n3,
                           old.a4 b4, cur.a4 n4, old.a5 b5, cur.a5 n5,
                           old.r_fails AS before_r, cur.r_fails AS after_r
                    FROM qc_run_results cur
                    JOIN qc_run_results old
                      ON old.ticket_id = cur.ticket_id AND old.run_id = ?
                    LEFT JOIN tickets t ON t.id = cur.ticket_id
                    WHERE cur.run_id = ?
                """, (prev_id, run_id)).fetchall()
                for r in rows:
                    d = dict(r)
                    moved = []
                    if d["before_overall"] != d["after_overall"]:
                        moved.append(("Overall", d["before_overall"], d["after_overall"]))
                    for label, b, n in (("A1", d["b1"], d["n1"]), ("A3", d["b3"], d["n3"]),
                                        ("A4", d["b4"], d["n4"]), ("A5", d["b5"], d["n5"])):
                        if b != n:
                            moved.append((label, b, n))
                    if d["before_r"] != d["after_r"]:
                        moved.append(("R-fails", d["before_r"] or "none", d["after_r"] or "none"))
                    if moved:
                        diff.append({
                            "number": d["number"], "title": d["title"],
                            "assignee": d["assignee_name"], "link": d["link"],
                            "changes": [{"check": c, "before": b, "after": a}
                                        for c, b, a in moved],
                        })
                diff.sort(key=lambda x: x["number"] or 0)
            run["diff"] = diff
            return run

    run = await asyncio.to_thread(query)
    if not run:
        raise HTTPException(404, "Run not found")
    return run


# ── CSV export ───────────────────────────────────────────────────────────────

@app.get("/api/export/{date_str}")
async def export_csv(date_str: str, user: dict = Depends(auth.require_user)):
    try:
        date.fromisoformat(date_str)
    except ValueError:
        raise HTTPException(400, "Use YYYY-MM-DD")

    def build_csv():
        tickets = review.annotate_tickets(db.get_day_tickets(date_str), user)
        if not tickets:
            return ""

        for t in tickets:
            rev = t.get("review") or {}
            t["ai_result"] = t.get("ai_result") or ""
            t["reviewed_by"] = rev.get("reviewer_name") or rev.get("reviewer_email") or ""
            t["reviewed_at"] = rev.get("reviewed_at") or ""

        fields = [
            "number", "title", "link", "state",
            "assignee_name", "account_name",
            "r1", "r2", "r3", "r4", "r5", "r7", "r8",
            "a1", "a2", "a3", "a4", "a5",
            "ai_result", "overall_result", "reviewed_by", "reviewed_at",
            "ai_notes",
        ]
        labels = {
            "number": "Ticket #", "title": "Title", "link": "Link",
            "state": "State",
            "assignee_name": "Assignee", "account_name": "Account",
            "r1": "R1 Functionality", "r2": "R2 Category", "r3": "R3 Account",
            "r4": "R4 Response Time", "r5": "R5 Status Owner",
            "r7": "R7 Rootly/Jira", "r8": "R8 Oncall Check",
            "a1": "A1 Cat. Accuracy", "a2": "A2 Sentiment",
            "a3": "A3 Response Quality", "a4": "A4 Status Check",
            "a5": "A5 Closure",
            "ai_result": "AI overall", "overall_result": "Overall",
            "reviewed_by": "Reviewed by", "reviewed_at": "Reviewed at",
            "ai_notes": "AI Notes",
        }

        def safe(v):
            """Neutralise spreadsheet formula injection in exported text."""
            s = "" if v is None else str(v)
            return "'" + s if s[:1] in ("=", "+", "-", "@") else s

        buf = io.StringIO()
        writer = csv.DictWriter(
            buf, fieldnames=fields, extrasaction="ignore",
            lineterminator="\r\n",
        )
        writer.writerow({f: labels[f] for f in fields})
        for t in tickets:
            writer.writerow({f: safe(t.get(f)) for f in fields})
        return buf.getvalue()

    content = await asyncio.to_thread(build_csv)
    filename = f"pylon-qc-{date_str}.csv"
    return StreamingResponse(
        iter([content]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ── analytics (leaderboard) ───────────────────────────────────────────────────

@app.get("/api/analytics")
async def get_analytics(
    month: str | None = None,
    start: str | None = None,
    end: str | None = None,
    user: dict = Depends(auth.require_user),
):
    def query():
        if start and end:
            where  = "WHERE t.fetch_date BETWEEN ? AND ?"
            params = (start, end)
        elif month:
            where  = "WHERE t.fetch_date LIKE ?"
            params = (f"{month}-%",)
        else:
            where  = ""
            params = ()
        with db.get_conn() as conn:
            rows = conn.execute(f"""
                SELECT
                    COALESCE(t.assignee_name, 'Unassigned')                  AS assignee,
                    COUNT(*)                                                  AS total,
                    SUM(CASE WHEN ac.overall_result = 'Pass'         THEN 1 ELSE 0 END) AS pass_count,
                    SUM(CASE WHEN ac.overall_result = 'Fail'         THEN 1 ELSE 0 END) AS fail_count,
                    SUM(CASE WHEN ac.overall_result = 'Needs Review' THEN 1 ELSE 0 END) AS review_count,
                    SUM(CASE WHEN ac.ticket_id IS NULL               THEN 1 ELSE 0 END) AS pending_count,
                    COUNT(ac.ticket_id)                                       AS ai_done
                FROM tickets t
                LEFT JOIN ai_checks ac ON t.id = ac.ticket_id
                {where}
                GROUP BY t.assignee_name
                ORDER BY pass_count DESC, total DESC
            """, params).fetchall()
            return [dict(r) for r in rows]
    return {"month": month, "start": start, "end": end,
            "assignees": await asyncio.to_thread(query)}


# ── stats ─────────────────────────────────────────────────────────────────────

@app.get("/api/stats")
async def get_stats(date: str | None = None, user: dict = Depends(auth.require_user)):
    return await asyncio.to_thread(db.ticket_stats, date)


# ══════════════════════════════════════════════════════════════════════════════
#  ADMIN API — credentials, settings, users, schedule
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/api/admin/overview")
async def admin_overview(user: dict = Depends(auth.require_user)):
    """Configuration overview. Members get a read-only, further-redacted view."""
    is_admin    = user["role"] == "admin"
    credentials = vault.list_credentials()

    if not is_admin:
        # Members can see *whether* something is configured, not any part of it.
        credentials = [
            {**c, "hint": "", "updated_by": None, "updated_at": None}
            for c in credentials
        ]

    return {
        "credentials":     credentials,
        "settings":        vault.get_settings(),
        "setting_sources": vault.get_setting_sources(),
        "schedule":        scheduler.next_run_description(),
        "runs":            scheduler.recent_runs(10),
        "users":           auth.list_users(),
        "audit":           vault.recent_audit(25) if is_admin else [],
        "allowed_domain":  auth.ALLOWED_DOMAIN,
        "readiness":       vault.readiness(),
        "env_admins":      sorted(auth.bootstrap_admins()),
        "can_edit":        is_admin,
    }


@app.put("/api/admin/credentials/{key}")
async def set_credential(key: str, request: Request,
                         user: dict = Depends(auth.require_admin)):
    body = await request.json()
    value = body.get("value")
    if value is None:
        raise HTTPException(400, "Missing value")
    try:
        vault.set_credential(key, value.strip(), user["email"])
    except vault.ConfigLocked as e:
        raise HTTPException(409, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))

    if key.startswith("vertex"):
        qc_runner.invalidate_vertex_client()
    vault.audit(user["email"], "credential.set" if value.strip() else "credential.clear", key)
    return {"ok": True, "credentials": vault.list_credentials()}


@app.post("/api/admin/credentials/{key}/test")
async def test_credential(key: str, user: dict = Depends(auth.require_admin)):
    try:
        if key == "pylon_api_token":
            import httpx
            async with httpx.AsyncClient(timeout=15, headers=pylon._headers()) as c:
                r = await c.get(f"{pylon.BASE_URL}/issues",
                                params={"limit": 1,
                                        "start_time": "2026-01-01T00:00:00Z",
                                        "end_time": "2026-01-02T00:00:00Z"})
            if r.status_code == 401:
                return {"ok": False, "message": "Pylon rejected the token (401)"}
            r.raise_for_status()
            return {"ok": True, "message": "Pylon token works"}

        if key == "slack_bot_token":
            info = await slack.test_auth()
            return {"ok": True, "message": f"Connected to {info['team']} as {info['bot']}"}

        if key == "vertex_service_account_json":
            def _probe():
                client = qc_runner.get_vertex_client()
                model = qc_runner.vertex_models()[0]
                client.models.generate_content(model=model, contents="ping")
                return model
            model = await asyncio.to_thread(_probe)
            return {"ok": True, "message": f"Vertex AI reachable (model {model})"}

        return {"ok": False, "message": "No test available for this credential"}

    except Exception as e:
        if key == "vertex_service_account_json":
            return {"ok": False, "message": qc_runner.explain_vertex_error(e)}
        return {"ok": False, "message": str(e)[:300]}


@app.put("/api/admin/settings")
async def update_settings(request: Request, user: dict = Depends(auth.require_admin)):
    body = await request.json()
    refused = vault.set_settings(body, user["email"])
    qc_runner.invalidate_vertex_client()

    saved = sorted(set(body) - set(refused))
    if saved:
        vault.audit(user["email"], "settings.update", ", ".join(saved))
    return {
        "ok": True,
        "settings":        vault.get_settings(),
        "setting_sources": vault.get_setting_sources(),
        "schedule":        scheduler.next_run_description(),
        # Anything the environment owns is reported back rather than silently dropped.
        "refused":         refused,
    }


# ── Google Cloud discovery ────────────────────────────────────────────────────

@app.get("/api/admin/gcp/status")
async def gcp_status(user: dict = Depends(auth.require_user)):
    return {**gcp.connection_status(), "locations": gcp.LOCATIONS}


@app.delete("/api/admin/gcp/connection")
async def gcp_disconnect(user: dict = Depends(auth.require_admin)):
    vault.set_credential("google_cloud_refresh_token", "", user["email"])
    vault.set_settings({"google_cloud_account": ""}, user["email"])
    qc_runner.invalidate_vertex_client()
    vault.audit(user["email"], "gcp.disconnect")
    return {"ok": True, **gcp.connection_status()}


@app.get("/api/admin/gcp/projects")
async def gcp_projects(user: dict = Depends(auth.require_admin)):
    try:
        return {"ok": True, "projects": await asyncio.to_thread(gcp.list_projects)}
    except gcp.NotConnected as e:
        return {"ok": False, "message": str(e), "projects": []}
    except Exception as e:
        return {"ok": False, "message": str(e)[:300], "projects": []}


@app.get("/api/admin/gcp/models")
async def gcp_models(project: str | None = None, location: str | None = None,
                     user: dict = Depends(auth.require_admin)):
    project  = project  or vault.get_setting("vertex_project")
    location = location or vault.get_setting("vertex_location")
    try:
        models = await asyncio.to_thread(gcp.list_models, project, location)
        return {"ok": True, "models": models, "project": project, "location": location}
    except gcp.NotConnected as e:
        return {"ok": False, "message": str(e), "models": []}
    except Exception as e:
        return {"ok": False, "message": qc_runner.explain_vertex_error(e), "models": []}


@app.post("/api/admin/slack/test")
async def slack_test(user: dict = Depends(auth.require_admin)):
    try:
        await slack.post_test()
        return {"ok": True, "message": "Test message posted"}
    except Exception as e:
        return {"ok": False, "message": str(e)[:300]}


@app.post("/api/admin/run-now")
async def run_now(request: Request, user: dict = Depends(auth.require_admin)):
    body = await request.json() if await request.body() else {}
    date_str = (body or {}).get("date")
    if date_str:
        try:
            target = date.fromisoformat(date_str)
        except ValueError:
            raise HTTPException(400, "Use YYYY-MM-DD")
    else:
        offset = 0 if vault.get_setting("schedule_target") == "today" else 1
        target = date.today() - timedelta(days=offset)

    try:
        result = await scheduler.run_pipeline(target, user["email"])
    except db.LockBusy as e:
        raise HTTPException(409, str(e))
    except Exception as e:
        raise HTTPException(502, str(e))

    vault.audit(user["email"], "pipeline.run_now", target.isoformat())
    return result


@app.put("/api/admin/users/{email}")
async def update_user(email: str, request: Request,
                      user: dict = Depends(auth.require_admin)):
    body = await request.json()
    email = email.lower()

    if email == user["email"]:
        raise HTTPException(400, "You cannot change your own role or access")

    target = auth.get_user(email)
    if not target:
        raise HTTPException(404, "User not found")

    role      = body.get("role", target["role"])
    is_active = body.get("is_active", target["is_active"])
    if role not in ("admin", "member"):
        raise HTTPException(400, "Role must be 'admin' or 'member'")

    with db.get_conn() as conn:
        conn.execute(
            "UPDATE app_users SET role = ?, is_active = ? WHERE email = ?",
            (role, 1 if is_active else 0, email),
        )
    vault.audit(user["email"], "user.update",
                f"{email} role={role} active={bool(is_active)}")
    return {"ok": True, "users": auth.list_users()}


@app.post("/api/admin/users")
async def invite_user(request: Request, user: dict = Depends(auth.require_admin)):
    """Pre-authorise a colleague so their first sign-in lands with the right role."""
    body  = await request.json()
    email = (body.get("email") or "").strip().lower()
    role  = body.get("role", "member")

    if not email.endswith(f"@{auth.ALLOWED_DOMAIN}"):
        raise HTTPException(400, f"Email must be an @{auth.ALLOWED_DOMAIN} address")
    if role not in ("admin", "member"):
        raise HTTPException(400, "Role must be 'admin' or 'member'")

    with db.get_conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO app_users (email, name, role, is_active, created_at)"
            " VALUES (?, ?, ?, 1, ?)",
            (email, email.split("@")[0], role,
             datetime.now(timezone.utc).isoformat()),
        )
        conn.execute("UPDATE app_users SET role = ? WHERE email = ?", (role, email))

    vault.audit(user["email"], "user.invite", f"{email} role={role}")
    return {"ok": True, "users": auth.list_users()}


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
