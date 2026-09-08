import asyncio
import csv
import html
import io
import json
import logging
import os
import re
import time
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import NamedTuple

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import (
    FileResponse, HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse,
)
from fastapi.staticfiles import StaticFiles

load_dotenv()

import auth
import db
import drilldown
import dryrun
import evidence
import funcheck
import gcp
import leaderboard
import openqc
import prompts
import pylon
import report
import reportcard
import share
import qc_runner
import resync_overall
import review
import scheduler
import scorer
import slack
import suggestions
import vault
import weekly

STATIC_DIR = Path(__file__).parent / "static"

DATE_HINT = "Use YYYY-MM-DD"

# The latest human sign-off per ticket, and the grade that actually applies:
# a review decision when there is one, else the AI verdict. Every surface that
# reports a grade must use these, or the same ticket reads Pass in one place and
# Fail in another. Constant SQL — no caller input is interpolated.
_LATEST_REVIEW = """
    SELECT r.ticket_id, r.decision
    FROM ticket_reviews r
    JOIN (SELECT ticket_id, MAX(id) AS max_id
          FROM ticket_reviews GROUP BY ticket_id) x ON x.max_id = r.id
"""
_EFFECTIVE_GRADE = (
    "COALESCE(CASE WHEN rev.decision IN ('Pass','Fail') THEN rev.decision END,"
    " ac.overall_result)"
)


def _require_date(value: str) -> date:
    """Parse a YYYY-MM-DD path/query value or reject the request."""
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise HTTPException(400, DATE_HINT) from None


def _require_month(value: str) -> str:
    """Validate a YYYY-MM filter. Unvalidated, a typo returned an empty page."""
    try:
        datetime.strptime(value, "%Y-%m")
    except ValueError:
        raise HTTPException(400, "Use YYYY-MM") from None
    return value


logging.basicConfig(
    level=os.getenv("QC_LOG_LEVEL", "INFO").upper(),
    format="%(levelname)s: %(message)s",
)

# Every other module has one; this one used `logger` in ten places without ever
# binding it. The worst of them sat in `lifespan`, on the branch that reports
# runs closed by a restart — so the first deploy that interrupted a run would
# have raised NameError during startup and taken the app down instead of
# logging one line. t_logger.py now checks every module for this.
logger = logging.getLogger("qc.app")


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()

    # Any run still marked 'running' belongs to a process that no longer exists,
    # because a run can only be alive inside the process that started it. Every
    # deploy restarts the container mid-run, so without this they sit at
    # "running" forever and the day looks like it is still being scored.
    reaped = db.reap_interrupted_runs(on_startup=True)
    if reaped["scheduled"] or reaped["qc"]:
        logger.warning(
            "Closed %d scheduled and %d scoring run(s) interrupted by a restart "
            "— those dates need re-running",
            reaped["scheduled"], reaped["qc"],
        )

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
    return RedirectResponse(
        auth.login_url(request, auth.safe_next(next)), status_code=302
    )


@app.get("/auth/callback")
async def auth_callback(request: Request, code: str | None = None,
                        state: str | None = None, error: str | None = None):
    if error or not code or not state:
        # `error` is attacker-controllable and this route is reachable without a
        # session, so it must never reach the response unescaped: the payload
        # would execute on our own origin, where SameSite=Lax still sends the
        # admin's session cookie to any fetch() it makes.
        reason = html.escape(error) if error else "Missing authorization code"
        return HTMLResponse(
            f"<h1>Sign-in cancelled</h1><p>{reason}.</p>"
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
        return await _finish_cloud_connect(
            request, code, auth.safe_next(payload.get("next")) or "/admin"
        )

    try:
        identity = await auth.exchange_code(request, code)
    except HTTPException as e:
        return HTMLResponse(
            f"<h1>Access denied</h1><p>{html.escape(str(e.detail))}</p>"
            '<p><a href="/login">Back to sign-in</a></p>', status_code=e.status_code,
        )

    user = auth.upsert_user(identity["email"], identity["name"], identity["picture"])
    if not user.get("is_active", 1):
        return HTMLResponse(
            "<h1>Account disabled</h1><p>Your access has been revoked. "
            "Contact an administrator.</p>", status_code=403,
        )

    vault.audit(user["email"], "auth.login")
    resp = RedirectResponse(auth.safe_next(payload.get("next")), status_code=302)
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
            f"<h1>Could not connect Google Cloud</h1><p>{html.escape(str(e.detail))}</p>"
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


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    """Browsers request /favicon.ico regardless of the <link> tags.

    `/favicon.ico` is already a public path, but nothing served it, so every
    page load logged a 404. One SVG covers both this and the explicit links.
    """
    return FileResponse(
        STATIC_DIR / "favicon.svg",
        media_type="image/svg+xml",
        headers={"Cache-Control": "public, max-age=86400"},
    )


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
        "role_label": auth.ROLE_LABELS.get(user["role"], user["role"]),
        "can_review_any": review.is_admin(user) or bool(covered),
        # Sent so the dashboard can disable Refetch and Run QC rather than
        # offering buttons that come back 403.
        "can_run_qc": auth.can_run_qc(user),
        # Separate right, separate flag: editing the rubric and spending the
        # budget to apply it are two questions with the same answer today. A page
        # that inferred one from the other would offer a dead Save button the day
        # they diverge.
        "can_edit_rules": auth.can_edit_rules(user),
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
    if not 1 <= month <= 12:
        raise HTTPException(400, "Month must be 1–12")
    data = await asyncio.to_thread(db.get_calendar_month, year, month)
    return {"year": year, "month": month, "days": data}


# Not /api/calendar/day/{date}: that is matched first by {year}/{month} above,
# which then fails parsing "day" as an int and returns 422.
@app.get("/api/calendar-day/{date_str}")
async def get_calendar_day(date_str: str, user: dict = Depends(auth.require_user)):
    """One day's calendar counts, for updating a single square after a fetch."""
    _require_date(date_str)
    day = await asyncio.to_thread(db.get_calendar_day, date_str)
    return {"date": date_str, "day": day}


# ── fetch a day from Pylon ────────────────────────────────────────────────────

class FetchResult(NamedTuple):
    """What one day's fetch stored, and what it removed.

    `complete` records whether the issue list could be trusted as the
    authoritative set for the date. When it is False no deletion was inferred,
    and callers should say so rather than reporting a clean sweep.
    """

    count: int
    deleted: int = 0
    kept_reviewed: int = 0
    restored: int = 0
    complete: bool = True


async def fetch_and_store(target: date) -> FetchResult:
    """Fetch one day from Pylon, persist tickets/messages/accounts, score R1–R8.

    Shared by the manual endpoint and the scheduler. Returns the active ticket count.
    """
    date_str = target.isoformat()
    day = await pylon.fetch_day(target)
    issues          = day.issues
    messages_by_id  = day.messages_by_id
    accounts_by_id  = day.accounts_by_id
    now = datetime.now(timezone.utc).isoformat()

    if day.failed_messages or day.failed_accounts:
        logger.warning(
            "Incomplete fetch for %s: %d ticket(s) missing messages, "
            "%d account(s) unavailable — those tickets will not be rule-scored",
            date_str, len(day.failed_messages), len(day.failed_accounts),
        )

    scoring_failures: list = []

    def store() -> int:
        # The rules document these verdicts were produced under, stamped on
        # every row so a grade can be traced to the version that graded it.
        # Read once per fetch rather than per ticket — it is one document for
        # the whole batch, and reading it per ticket would let a save halfway
        # through a fetch stamp two different hashes on the same day.
        import rules as qc_rules
        rules_hash = qc_rules.rules_hash()

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
                prior = conn.execute(
                    "SELECT csat_responses FROM tickets WHERE id = ?",
                    (issue["id"],),
                ).fetchone()
                csat_json = weekly.csat_json_for_store(
                    issue, prior["csat_responses"] if prior else None)
                conn.execute("""
                    INSERT OR REPLACE INTO tickets
                        (id, number, fetch_date, title, link, state, source, type,
                         priority, assignee_id, assignee_name, account_id,
                         custom_fields, external_issues, body_html,
                         created_at, updated_at, latest_message_time,
                         customer_portal_visible, fetched_at, csat_responses,
                         first_response_seconds, resolution_seconds,
                         business_hours_first_response_seconds,
                         business_hours_resolution_seconds)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    issue["id"], issue.get("number"), date_str,
                    issue.get("title"), issue.get("link"),
                    issue.get("state"), issue.get("source"), issue.get("type"),
                    priority, assignee.get("id"), assignee_name, account.get("id"),
                    json.dumps(cf), json.dumps(ext_issues), issue.get("body_html"),
                    issue.get("created_at"), issue.get("updated_at"),
                    issue.get("latest_message_time"),
                    1 if cpv else 0, now, csat_json,
                    # Each clock in its own column: wall and business hours
                    # disagree whenever a ticket spans off-hours, and a
                    # coalesced value cannot be split apart afterwards.
                    weekly.pylon_duration_seconds(
                        issue, "first_response_seconds"),
                    weekly.pylon_duration_seconds(
                        issue, "resolution_seconds"),
                    weekly.pylon_duration_seconds(
                        issue, "business_hours_first_response_seconds"),
                    weekly.pylon_duration_seconds(
                        issue, "business_hours_resolution_seconds"),
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

                # R-checks read absence as evidence: no messages looks like an
                # unanswered thread, a missing account like an invalid one. If
                # the fetch was incomplete for this ticket, leave the previous
                # scores alone rather than recording a guess as fact.
                if not day.is_complete(issue):
                    logger.warning(
                        "Skipping rule scoring for #%s — incomplete fetch",
                        issue.get("number"),
                    )
                    continue

                # R1–R8 scoring. One malformed ticket must not cost the day:
                # this used to propagate out and roll back the whole transaction.
                try:
                    scores = scorer.score_all(issue, msgs, acc_data, ext_issues)
                except Exception:
                    logger.exception(
                        "Rule scoring failed for #%s", issue.get("number")
                    )
                    scoring_failures.append(issue.get("number"))
                    continue

                conn.execute("""
                    INSERT OR REPLACE INTO rule_checks
                        (ticket_id, fetch_date, r1, r2, r3, r4, r5, r7, r8, r9,
                         r10, r11, checked_at, rules_hash)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    issue["id"], date_str,
                    scores["r1"], scores["r2"], scores["r3"],
                    scores["r4"], scores["r5"], scores["r7"],
                    scores["r8"], scores["r9"], scores["r10"], scores["r11"],
                    now, rules_hash,
                ))

            active_count = sum(1 for i in issues if i.get("state") != "archived")
            conn.execute("""
                INSERT OR REPLACE INTO fetch_log (fetch_date, ticket_count, fetched_at)
                VALUES (?, ?, ?)
            """, (date_str, active_count, now))
            return active_count

    count = await asyncio.to_thread(store)

    # Tickets deleted at source. Only ever inferred from a fetch that proved
    # itself complete: Pylon gives no tombstone, so an incomplete fetch looks
    # exactly like a mass deletion, and being wrong here destroys grades and
    # human sign-offs that a refetch cannot restore.
    if day.may_infer_deletions():
        cleanup = await asyncio.to_thread(
            db.mark_deleted_tickets, date_str, [i["id"] for i in issues]
        )
        if cleanup["deleted"] or cleanup["restored"]:
            logger.info(
                "Cleanup for %s: %d no longer in Pylon, %d reappeared",
                date_str, cleanup["deleted"], cleanup["restored"],
            )
        if cleanup["kept_reviewed"]:
            logger.info(
                "Kept %d ticket(s) for %s that Pylon no longer returns because "
                "they carry a human review: %s",
                cleanup["kept_reviewed"], date_str,
                ", ".join(cleanup["kept_reviewed_ids"][:10]),
            )
    else:
        cleanup = {"deleted": 0, "kept_reviewed": 0, "restored": 0,
                   "skipped_incomplete": True}
        logger.warning(
            "Skipping deletion cleanup for %s — the issue list was incomplete, "
            "so absence cannot be read as deletion", date_str,
        )

    if scoring_failures:
        logger.error(
            "Rule scoring failed for %d ticket(s) on %s: %s",
            len(scoring_failures), date_str, scoring_failures,
        )
    # Recompute overall_result for this day's already-QC'd tickets whose
    # R-scores just changed. Scoped to the fetched date: resyncing the whole
    # table on every fetch grew without bound and rewrote unrelated days.
    await asyncio.to_thread(resync_overall.run, date_str)
    return FetchResult(
        count=count,
        deleted=cleanup["deleted"],
        kept_reviewed=cleanup["kept_reviewed"],
        restored=cleanup["restored"],
        complete=day.may_infer_deletions(),
    )


@app.post("/api/fetch/{date_str}")
async def fetch_day(date_str: str, user: dict = Depends(auth.require_operator)):
    target = _require_date(date_str)

    try:
        with db.advisory_lock(f"fetch:{date_str}", user["email"], ttl_seconds=1800):
            res = await fetch_and_store(target)
    except db.LockBusy as e:
        raise HTTPException(409, str(e))
    except pylon.PylonNotConfigured as e:
        raise HTTPException(503, str(e))
    except Exception as e:
        raise HTTPException(502, f"Pylon fetch failed: {e}")

    detail = f"{date_str} ({res.count} tickets)"
    if res.deleted:
        detail += f", {res.deleted} removed at source"
    vault.audit(user["email"], "fetch.day", detail)
    if res.deleted or res.kept_reviewed:
        vault.audit(
            user["email"], "fetch.cleanup",
            f"{date_str} deleted={res.deleted} kept_reviewed={res.kept_reviewed}"
            f" restored={res.restored}",
        )
    return {"date": date_str, "ticket_count": res.count,
            "deleted": res.deleted, "kept_reviewed": res.kept_reviewed,
            "restored": res.restored, "fetch_complete": res.complete,
            "fetched_at": datetime.now(timezone.utc).isoformat()}


# ── run AI checks (A1–A5) for a day ──────────────────────────────────────────

@app.get("/api/qc/{date_str}/preview")
async def preview_qc(date_str: str, user: dict = Depends(auth.require_user)):
    """What running QC on this date would do, and what it has already cost.

    Uses the same eligibility function as the run itself, so the count shown
    here cannot disagree with what actually happens.
    """
    _require_date(date_str)

    def load():
        eligible, in_scope = qc_runner.eligible_for_scoring(date_str)
        return len(eligible), in_scope, db.qc_spend_for_date(date_str)

    eligible, in_scope, spend = await asyncio.to_thread(load)
    if in_scope == 0:
        reason = "No tickets in scope for this date."
    elif eligible == 0:
        reason = "Every ticket on this date is already scored and unchanged."
    elif eligible == in_scope:
        reason = f"All {in_scope} in-scope tickets need scoring."
    else:
        reason = (f"{eligible} of {in_scope} tickets changed or were never "
                  "scored; the rest are unchanged and will be skipped.")

    return {
        "date": date_str,
        "eligible": eligible,
        "in_scope": in_scope,
        "reason": reason,
        "has_run": spend["runs"] > 0,
        "spend": spend,
    }


@app.post("/api/qc/{date_str}")
async def run_qc(date_str: str, refetch: bool = False,
                 user: dict = Depends(auth.require_operator)):
    """Score a day. With `refetch=1`, pull from Pylon first.

    Refetch-then-score is only cheap because staleness is a content
    fingerprint: a refetch that changes nothing leaves nothing to score. Before
    that, this would have regraded the whole day at full price every time.
    """
    target = _require_date(date_str)

    fetch_res = None
    if refetch:
        # Two locks in series, never nested: a scheduled run colliding with a
        # human fails cleanly on one of them rather than deadlocking on both.
        try:
            with db.advisory_lock(f"fetch:{date_str}", user["email"],
                                  ttl_seconds=1800):
                fetch_res = await fetch_and_store(target)
        except db.LockBusy as e:
            raise HTTPException(409, str(e))
        except pylon.PylonNotConfigured as e:
            raise HTTPException(503, str(e))
        except Exception as e:
            raise HTTPException(502, f"Pylon fetch failed: {e}")

    log = await asyncio.to_thread(db.get_fetch_log, date_str)
    if not log:
        raise HTTPException(404, "Day not fetched yet — fetch tickets first")

    # Captured before the run so the response can show what this run added
    # versus what the date had already cost.
    spend_before = await asyncio.to_thread(db.qc_spend_for_date, date_str)

    try:
        with db.advisory_lock(f"qc:{date_str}", user["email"], ttl_seconds=3600):
            result = await asyncio.to_thread(qc_runner.run_qc_date, date_str, user["email"])
    except db.LockBusy as e:
        raise HTTPException(409, str(e))
    except qc_runner.VertexNotConfigured as e:
        raise HTTPException(503, str(e))
    except Exception as e:
        raise HTTPException(502, qc_runner.explain_vertex_error(e))

    spend_after = await asyncio.to_thread(db.qc_spend_for_date, date_str)
    vault.audit(
        user["email"], "qc.run",
        f"{date_str} scored={result.get('scored')}"
        + (f" refetched={fetch_res.count}" if fetch_res else ""),
    )
    return {
        "date": date_str,
        **result,
        # Present only when this call also refetched, so the UI can report both
        # halves rather than implying the fetch always happens.
        "fetch": None if fetch_res is None else {
            "ticket_count": fetch_res.count,
            "deleted": fetch_res.deleted,
            "kept_reviewed": fetch_res.kept_reviewed,
            "restored": fetch_res.restored,
            "complete": fetch_res.complete,
        },
        "cost_split": {
            "before": spend_before["total_cost_usd"],
            "this_run": round(
                spend_after["total_cost_usd"] - spend_before["total_cost_usd"], 6
            ),
            "total": spend_after["total_cost_usd"],
            "runs": spend_after["runs"],
            "any_estimated": spend_after["any_estimated"],
        },
    }


# ── open backlog: list, preview and QC regardless of fetch date ──────────────

@app.get("/open", response_class=HTMLResponse)
async def open_page(user: dict = Depends(auth.require_user)):
    return _page("open.html")


def _open_args(start: str | None, end: str | None,
               states: str | None) -> tuple:
    """Validate the open tab's filters. `states` is a comma list from the URL;
    it narrows within the open set and can never widen it — openqc's NOT IN
    on the terminal states is unconditional."""
    if start:
        _require_date(start)
    if end:
        _require_date(end)
    if start and end and _require_date(start) > _require_date(end):
        raise HTTPException(400, "start must not be after end")
    state_list = [s.strip().lower() for s in (states or "").split(",")
                  if s.strip()] or None
    return start, end, state_list


@app.get("/api/open/tickets")
async def open_tickets(start: str | None = None, end: str | None = None,
                       states: str | None = None,
                       user: dict = Depends(auth.require_user)):
    s, e, st = _open_args(start, end, states)
    return await asyncio.to_thread(openqc.list_open, s, e, st)


@app.get("/api/open/preview")
async def open_preview(start: str | None = None, end: str | None = None,
                       states: str | None = None,
                       user: dict = Depends(auth.require_user)):
    s, e, st = _open_args(start, end, states)
    return await asyncio.to_thread(openqc.preview, s, e, st)


@app.post("/api/open/refetch")
async def refetch_open_tickets(user: dict = Depends(auth.require_operator)):
    """Discover and pull the whole open backlog from Pylon.

    The listing and the by-id refresh can only see tickets some day-fetch
    already brought in; this is the one call that finds open tickets created
    on days nobody fetched. Not filtered on purpose: discovery is about what
    exists, and the tab's filters then narrow the view of it.
    """
    try:
        with db.advisory_lock("fetch:open", user["email"], ttl_seconds=1800):
            res = await openqc.refetch_open()
    except db.LockBusy as e:
        raise HTTPException(409, str(e))
    except pylon.PylonNotConfigured as e:
        raise HTTPException(503, str(e))
    except Exception as e:
        raise HTTPException(502, f"Pylon re-fetch failed: {e}")

    vault.audit(
        user["email"], "fetch.open",
        f"found={res['found_open']} new={res['new']} stored={res['stored']}"
        f" no_longer_open={res['no_longer_open_checked']}"
        f" deleted={res['deleted']}"
        + ("" if res["search_complete"] else " (search incomplete)"),
    )
    return res


@app.post("/api/weekly/refresh")
async def refresh_weekly_from_pylon(user: dict = Depends(auth.require_operator)):
    """Fetch today's tickets from Pylon and refresh the rest of this week by id.

    The day pipeline fetches a date the morning AFTER it ends, so the weekly's
    current period undercounts (and skews every percentile over) tickets
    created since the last fetch. Fetch plus local rule scoring only — no AI
    scoring, no Slack post, no Vertex spend.
    """
    from pylon import _IST  # the day pipeline keys dates by IST; match it

    today = datetime.now(_IST).date()
    today_str = today.isoformat()
    monday = today - timedelta(days=today.weekday())
    try:
        with db.advisory_lock(f"fetch:{today_str}", user["email"],
                              ttl_seconds=1800):
            fetch_res = await fetch_and_store(today)
        week_refreshed = None
        if monday < today:
            with db.advisory_lock("fetch:backfill", user["email"],
                                  ttl_seconds=1800):
                week_refreshed = await openqc.backfill_range(
                    monday.isoformat(),
                    (today - timedelta(days=1)).isoformat())
    except db.LockBusy as e:
        raise HTTPException(409, str(e))
    except pylon.PylonNotConfigured as e:
        raise HTTPException(503, str(e))
    except Exception as e:
        raise HTTPException(502, f"Pylon refresh failed: {e}")

    vault.audit(
        user["email"], "fetch.weekly_refresh",
        f"today={today_str} fetched={fetch_res.count}"
        + (f" week_refreshed={week_refreshed['stored']}"
           if week_refreshed else ""),
    )
    return {
        "date": today_str,
        "fetched": fetch_res.count,
        "complete": fetch_res.complete,
        "week_refreshed": week_refreshed,
    }


@app.post("/api/backfill")
async def backfill_tickets(start: str, end: str,
                           user: dict = Depends(auth.require_operator)):
    """Refetch every stored ticket in a fetch-date range, closed included.

    The open refetch never touches terminal states, so a ticket answered or
    closed after its one day-fetch keeps a frozen state and NULL Pylon clocks;
    this brings a range's history up to Pylon's current truth. By id, so the
    first-response/resolution clocks are always in the payload. Rule scoring
    on the refreshed content is local — no Vertex spend.
    """
    if _require_date(start) > _require_date(end):
        raise HTTPException(400, "start must not be after end")
    try:
        with db.advisory_lock("fetch:backfill", user["email"],
                              ttl_seconds=3600):
            res = await openqc.backfill_range(start, end)
    except db.LockBusy as e:
        raise HTTPException(409, str(e))
    except pylon.PylonNotConfigured as e:
        raise HTTPException(503, str(e))
    except Exception as e:
        raise HTTPException(502, f"Pylon backfill failed: {e}")

    vault.audit(
        user["email"], "fetch.backfill",
        f"{start}..{end} requested={res['requested']} stored={res['stored']}"
        f" deleted={res['deleted']} failed={res['failed']}",
    )
    return res


@app.post("/api/open/qc")
async def run_open_qc(refresh: bool = True,
                      start: str | None = None, end: str | None = None,
                      states: str | None = None,
                      user: dict = Depends(auth.require_operator)):
    """Score the open backlog. With `refresh=1` (the default), refetch it from
    Pylon by id first — scoring an open ticket's fetch-time snapshot grades
    stale evidence, so freshness is opt-out here where the day run's is opt-in.
    """
    s, e, st = _open_args(start, end, states)

    refresh_res = None
    try:
        with db.advisory_lock("qc:open", user["email"], ttl_seconds=3600):
            if refresh:
                refresh_res = await openqc.refresh_open(s, e, st)
            result = await asyncio.to_thread(openqc.run, user["email"], s, e, st)
    except db.LockBusy as e2:
        raise HTTPException(409, str(e2))
    except pylon.PylonNotConfigured as e2:
        raise HTTPException(503, str(e2))
    except qc_runner.VertexNotConfigured as e2:
        raise HTTPException(503, str(e2))
    except Exception as e2:
        raise HTTPException(502, qc_runner.explain_vertex_error(e2))

    vault.audit(
        user["email"], "qc.open",
        f"scored={result.get('scored')} skipped={result.get('skipped')}"
        + (f" refreshed={refresh_res['stored']}" if refresh_res else "")
        + (f" filters start={s} end={e} states={st}"
           if (s or e or st) else " (all time)"),
    )
    return {"refresh": refresh_res, **result}


# ── functionality-tagging check ───────────────────────────────────────────────

@app.get("/funcheck", response_class=HTMLResponse)
async def funcheck_page(user: dict = Depends(auth.require_user)):
    return _page("funcheck.html")


@app.get("/api/funcheck")
async def get_funcheck(month: str, user: dict = Depends(auth.require_user)):
    _require_month(month)
    return await asyncio.to_thread(funcheck.results, month)


@app.get("/api/funcheck/preview")
async def funcheck_preview(month: str, user: dict = Depends(auth.require_user)):
    _require_month(month)
    return await asyncio.to_thread(funcheck.preview, month)


@app.get("/api/admin/catalog")
async def get_catalog(user: dict = Depends(auth.require_user)):
    """The tagging vocabulary the Functionality Check validates against."""
    import funcheck_catalog

    def load():
        current = funcheck.options()
        return {
            "functionality": current["functionality"],
            "category": current["category"],
            "overridden": {
                key: bool(vault.get_raw_setting(setting))
                for key, setting in funcheck.CATALOG_SETTINGS.items()},
            "shipped_counts": {
                "functionality": len(funcheck_catalog.FUNCTIONALITIES),
                "category": len(funcheck_catalog.REQUEST_CATEGORIES)},
            "can_edit": user["role"] == "admin",
        }
    return await asyncio.to_thread(load)


@app.post("/api/admin/catalog/sync")
async def sync_catalog(user: dict = Depends(auth.require_admin)):
    """Pull the option lists AND the value→label map from Pylon itself.

    Pylon's API stores option values (slugs) on tickets while its UI shows
    labels; this sync is what lets every page translate one into the other —
    and it makes Pylon the catalog's source of truth in one click.
    """
    try:
        counts = await funcheck.sync_catalog_from_pylon()
    except pylon.PylonNotConfigured as e:
        raise HTTPException(503, str(e))
    except Exception as e:
        raise HTTPException(502, f"Sync failed: {str(e)[:300]}")
    vault.audit(user["email"], "catalog.sync",
                f"functionality={counts['functionality']} "
                f"category={counts['category']}")
    # The page re-reads the lists through loadCatalog(); counts are enough here.
    return {"ok": True, **counts}


@app.put("/api/admin/catalog")
async def put_catalog(request: Request,
                      user: dict = Depends(auth.require_admin)):
    """Replace one vocabulary list, or reset it to the shipped catalog.

    Body: {"list": "functionality"|"category", "entries": [...]} to save, or
    {"list": ..., "reset": true} to fall back to funcheck_catalog.py. Entries
    are trimmed and case-insensitively deduplicated, order preserved — the
    order is the dropdown's order in spirit, so it is the editor's to keep.
    """
    body = await request.json()
    which = str(body.get("list") or "")
    if which not in funcheck.CATALOG_SETTINGS:
        raise HTTPException(400, "list must be 'functionality' or 'category'")

    # funcheck owns the write path (cleaning, caps, audit, cache invalidation)
    # so the editor and the Pylon sync can never disagree about what a valid
    # list is.
    def write():
        if body.get("reset"):
            funcheck.clear_catalog(which, user["email"])
        else:
            raw = body.get("entries")
            if not isinstance(raw, list):
                raise HTTPException(400, "entries must be a list of strings")
            funcheck.save_catalog(which, raw, user["email"])
        return funcheck.options()

    try:
        current = await asyncio.to_thread(write)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "functionality": current["functionality"],
            "category": current["category"]}


@app.post("/api/admin/share/folder")
async def create_share_folder(request: Request,
                              user: dict = Depends(auth.require_admin)):
    """Create the Drive reports folder through the app and store its ID.

    Exists because drive.file scope cannot see hand-made folders: the app must
    be the folder's creator for the folder to exist as far as it can tell.
    """
    body = await request.json() if await request.body() else {}
    try:
        folder = await asyncio.to_thread(
            gcp.create_drive_folder, (body or {}).get("name") or "")
    except gcp.DriveNotReady as e:
        raise HTTPException(503, str(e))
    except Exception as e:
        raise HTTPException(502, f"Folder creation failed: {str(e)[:300]}")
    vault.set_settings({"share_drive_folder_id": folder["id"]}, user["email"])
    vault.audit(user["email"], "share.folder.create",
                f"{folder['name']} ({folder['id']})")
    return {"ok": True, **folder,
            "status": await asyncio.to_thread(gcp.drive_status, folder["id"])}


@app.get("/api/funcheck/share/meta")
async def funcheck_share_meta(user: dict = Depends(auth.require_operator)):
    """What the share panel can do right now: channel default, taggable
    groups, and honest capability probes (Slack scopes, Drive access)."""
    return await share.meta()


@app.post("/api/funcheck/share")
async def funcheck_share(request: Request,
                         user: dict = Depends(auth.require_operator)):
    """Send the sender's filtered rows + custom message to Slack."""
    body = await request.json()
    month = str(body.get("month") or "")
    _require_month(month)
    try:
        result = await share.send(
            month, body.get("numbers") or [],
            body.get("message"), body.get("mentions") or [],
            body.get("channel"), body.get("format") or "xlsx",
            user["email"])
    except ValueError as e:
        raise HTTPException(400, str(e))
    except slack.NotTheDeployment as e:
        raise HTTPException(403, str(e))
    except (slack.SlackNotConfigured, gcp.DriveNotReady) as e:
        raise HTTPException(503, str(e))
    except Exception as e:
        raise HTTPException(502, f"Share failed: {str(e)[:300]}")
    vault.audit(user["email"], "funcheck.share",
                f"{month} rows={result['rows']} to={result['channel']} "
                f"format={result['format']}")
    return {"ok": True, **result}


@app.post("/api/funcheck/run")
async def run_funcheck(month: str, user: dict = Depends(auth.require_operator)):
    """Check the month's tagging. Operator-gated: it bills Vertex like QC."""
    _require_month(month)
    try:
        with db.advisory_lock(f"funcheck:{month}", user["email"],
                              ttl_seconds=3600):
            result = await asyncio.to_thread(funcheck.run, month, user["email"])
    except db.LockBusy as e:
        raise HTTPException(409, str(e))
    except qc_runner.VertexNotConfigured as e:
        raise HTTPException(503, str(e))
    except Exception as e:
        raise HTTPException(502, qc_runner.explain_vertex_error(e))
    vault.audit(user["email"], "funcheck.run",
                f"{month} checked={result.get('checked')}")
    return result


# ── monthly Product Signals reports ───────────────────────────────────────────

@app.get("/weekly", response_class=HTMLResponse)
async def weekly_page(user: dict = Depends(auth.require_user)):
    return _page("weekly.html")


# One CSAT sweep per period per process every 10 minutes: surveys trickle in
# over days, so fresher than that buys nothing and each sweep is a paginated
# Pylon crawl.
_CSAT_REFRESHED: dict[tuple, float] = {}
_CSAT_TTL_SECONDS = 600


def _csat_refresh_due(start: str, end: str) -> bool:
    now = time.monotonic()
    last = _CSAT_REFRESHED.get((start, end))
    if last is not None and now - last < _CSAT_TTL_SECONDS:
        return False
    _CSAT_REFRESHED[(start, end)] = now
    return True


@app.get("/api/weekly")
async def get_weekly(week: str | None = None,
                    start: str | None = None,
                    end: str | None = None,
                    user: dict = Depends(auth.require_user)):
    """Period-over-period support operations from the local ticket store.

    `start`+`end` select the current window (previous is the same length
    immediately before). `week` is the Monday fallback. CSAT is a
    separate `/api/weekly/csat` call so this stays fast.
    """
    if week:
        _require_date(week)
    if start:
        _require_date(start)
    if end:
        _require_date(end)
    try:
        return await asyncio.to_thread(
            weekly.build, week, start=start, end=end)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@app.get("/api/weekly/csat")
async def get_weekly_csat(week: str | None = None,
                         start: str | None = None,
                         end: str | None = None,
                         user: dict = Depends(auth.require_user)):
    """Pull Pylon CSAT for the weekly window, then return the CSAT slice.

    Kept off `/api/weekly` so Apply dates / This week stay a local SQLite
    read. Failures here leave ticket KPIs alone.
    """
    if week:
        _require_date(week)
    if start:
        _require_date(start)
    if end:
        _require_date(end)
    try:
        _curr_start, curr_end, prev_start, _prev_end = weekly.resolve_period(
            week, start, end)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e

    fetched = 0
    error = None
    # Operators refresh CSAT from Pylon, throttled so a busy morning of
    # reloads is one survey sweep. Members still read stored scores.
    if auth.can_run_qc(user) and _csat_refresh_due(prev_start, curr_end):
        try:
            rows = await pylon.fetch_csat_responses(prev_start, curr_end)
            fetched = await asyncio.to_thread(weekly.store_csat_responses, rows)
        except pylon.PylonNotConfigured:
            error = "pylon_not_configured"
        except Exception as e:
            _CSAT_REFRESHED.pop((prev_start, curr_end), None)
            logger.warning("Weekly CSAT fetch failed: %s", e)
            error = str(e)[:200]

    try:
        payload = await asyncio.to_thread(
            weekly.build, week, start=start, end=end)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e

    keys = ("agent", "pv_csat_avg", "cv_csat_avg",
            "pv_csat_total", "cv_csat_total",
            "pv_csat_award", "cv_csat_award")
    return {
        "csatPrev": payload["csatPrev"],
        "csatCurr": payload["csatCurr"],
        "agentTable": [{k: a.get(k) for k in keys} for a in payload["agentTable"]],
        "coverage": payload["coverage"],
        "fetched": fetched,
        "error": error,
    }


@app.get("/reports", response_class=HTMLResponse)
async def reports_page(user: dict = Depends(auth.require_user)):
    return _page("reports.html")


@app.get("/api/reports")
async def get_reports(user: dict = Depends(auth.require_user)):
    return {"reports": await asyncio.to_thread(report.list_reports),
            "available_months": await asyncio.to_thread(report.available_months)}


@app.get("/reports/{key}", response_class=HTMLResponse)
async def view_report(key: str, download: bool = False,
                      user: dict = Depends(auth.require_user)):
    """The stored page, verbatim — regeneration is the only way it changes.

    `key` is a month, or a comma list of months for a trend comparison.
    `download=1` serves the same bytes as a file, for sharing the report
    outside the app; “Save as PDF” is the page's own print button.
    """
    try:
        page = await asyncio.to_thread(report.get_html, key)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if page is None:
        raise HTTPException(404, "No report generated for this month yet")
    headers = {"Cache-Control": "no-store"}
    if download:
        safe = key.replace(",", "_")
        headers["Content-Disposition"] = \
            f'attachment; filename="product-signals-{safe}.html"'
    return HTMLResponse(page, headers=headers)


@app.post("/api/reports/compare")
async def generate_comparison(months: str,
                              user: dict = Depends(auth.require_operator)):
    """Generate a multi-month trend report — AI-narrated, stored like the
    monthly ones. `months` is a comma list, at least two."""
    try:
        month_list = report.require_key(months).split(",")
    except ValueError as e:
        raise HTTPException(400, str(e))
    # A comparison over unfetched months narrates gaps as trends — buy the
    # data for every chosen month first, exactly like the monthly report.
    fetch_infos = {}
    try:
        for m in month_list:
            fetch_infos[m] = await _ensure_month_fetched(m, user["email"])
    except pylon.PylonNotConfigured as e:
        raise HTTPException(503, f"Months have unfetched days and {e}")
    lock_key = f"report:{','.join(month_list)}"
    try:
        with db.advisory_lock(lock_key[:60], user["email"], ttl_seconds=1800):
            result = await asyncio.to_thread(
                report.generate_compare, month_list, user["email"])
    except db.LockBusy as e:
        raise HTTPException(409, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(502, qc_runner.explain_vertex_error(e))
    backfilled = sum(f["fetched"] for f in fetch_infos.values())
    vault.audit(user["email"], "report.compare",
                f"{result['key']}"
                + (f" backfilled={backfilled}d" if backfilled else "")
                + ("" if result["ai_narrative"] else " (no AI narrative)"))
    return {**result, "fetch": fetch_infos}


@app.post("/api/reports/{month}/chat")
async def report_chat(month: str, request: Request,
                      user: dict = Depends(auth.require_operator)):
    """One chat turn over the month's evidence. Operator-gated: every question
    is a Vertex call, and the response carries its own cost."""
    _require_month(month)
    body = await request.json()
    try:
        return await asyncio.to_thread(
            report.chat, month, body.get("question"),
            body.get("history") or [], user["email"])
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(502, qc_runner.explain_vertex_error(e))


@app.get("/api/reports/{month}/evidence.csv")
async def report_evidence_csv(month: str,
                              user: dict = Depends(auth.require_user)):
    """Every ticket behind the month's report, one row each — the answer to
    “where is the evidence?”. Tags and resolution fields are Pylon's values
    verbatim; the AI summary is the stored one-liner from generation."""
    _require_month(month)
    rows = await asyncio.to_thread(report.evidence_rows, month)

    def safe(v):
        """Neutralise spreadsheet formula injection — titles and resolution
        text are customer-controlled, and Excel executes leading = + - @."""
        s = "" if v is None else str(v)
        return "'" + s if s[:1] in ("=", "+", "-", "@") else s

    buf = io.StringIO()
    writer = csv.writer(buf)
    cols = ["ticket_id", "title", "account", "assignee", "status", "date",
            "functionality", "request_category", "ai_summary",
            "resolution_details", "resolution_category", "link"]
    writer.writerow(cols)
    for r in rows:
        writer.writerow([safe(r[c]) for c in cols])
    return StreamingResponse(
        iter([buf.getvalue()]), media_type="text/csv",
        headers={"Content-Disposition":
                 f'attachment; filename="product-signals-evidence-{month}.csv"'})


async def _ensure_month_fetched(month: str, user_email: str) -> dict:
    """Day-fetch every missing day of a month from Pylon, before a report.

    A report over an unfetched month narrates fetch gaps as demand, so
    generation now buys the data first, however long that takes. Each day goes
    through the same `fetch_and_store` the Dashboard uses — tickets, fetch_log
    and R-checks land in the shared database, so the calendar fills in as a
    side effect rather than by any extra sync.

    Days already in fetch_log are trusted and skipped; future days do not
    exist yet. A day that fails is recorded and skipped — except when Pylon
    itself is unconfigured, which aborts, because every day would fail the
    same way.
    """
    first = datetime.strptime(month, "%Y-%m").date()
    today = date.today()
    if first > today:
        return {"days": 0, "missing": 0, "fetched": 0, "failed": []}
    next_month = (first.replace(day=28) + timedelta(days=4)).replace(day=1)
    last = min(next_month - timedelta(days=1), today)

    def have() -> set:
        with db.get_conn() as conn:
            return {r["fetch_date"] for r in conn.execute(
                "SELECT fetch_date FROM fetch_log WHERE fetch_date LIKE ?",
                (f"{month}-%",))}

    covered = await asyncio.to_thread(have)
    days = [first + timedelta(days=i) for i in range((last - first).days + 1)]
    missing = [d for d in days if d.isoformat() not in covered]

    fetched = 0
    failed: list[str] = []
    for d in missing:
        try:
            with db.advisory_lock(f"fetch:{d.isoformat()}", user_email,
                                  ttl_seconds=1800):
                await fetch_and_store(d)
            fetched += 1
        except pylon.PylonNotConfigured:
            raise
        except db.LockBusy as e:
            failed.append(f"{d.isoformat()}: {e}")
        except Exception as e:
            logger.warning("Report backfill fetch failed for %s: %s", d, e)
            failed.append(f"{d.isoformat()}: {str(e)[:120]}")

    if fetched:
        vault.audit(user_email, "report.backfill",
                    f"{month} fetched={fetched} of {len(missing)} missing days")
    return {"days": len(days), "missing": len(missing),
            "fetched": fetched, "failed": failed}


@app.post("/api/reports/generate")
async def generate_report(month: str,
                          user: dict = Depends(auth.require_operator)):
    """Generate or regenerate one month's report — fetching the month first.

    Operator-gated: the backfill hits Pylon and R-scores every fetched day,
    and the narrative layer bills Vertex (the report still generates without
    the narrative)."""
    _require_month(month)
    try:
        fetch_info = await _ensure_month_fetched(month, user["email"])
    except pylon.PylonNotConfigured as e:
        raise HTTPException(503, f"Month has unfetched days and {e}")
    try:
        with db.advisory_lock(f"report:{month}", user["email"],
                              ttl_seconds=1800):
            result = await asyncio.to_thread(report.generate, month, user["email"])
    except db.LockBusy as e:
        raise HTTPException(409, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(502, qc_runner.explain_vertex_error(e))
    vault.audit(user["email"], "report.generate",
                f"{month} tickets={result['tickets']} cases={result['cases']}"
                + (f" backfilled={fetch_info['fetched']}d"
                   if fetch_info["fetched"] else "")
                + ("" if result["ai_narrative"] else " (no AI narrative)"))
    return {**result, "fetch": fetch_info}


# ── manual Slack reports ──────────────────────────────────────────────────────

def _slack_send_errors(e: Exception):
    """One place to translate a failed manual send into the right status."""
    if isinstance(e, slack.SlackNotConfigured):
        return HTTPException(503, str(e))
    if isinstance(e, slack.NotTheDeployment):
        return HTTPException(403, str(e))
    return HTTPException(502, f"Slack send failed: {e}")


@app.post("/api/slack/report/day/{date_str}")
async def send_day_report(date_str: str,
                          user: dict = Depends(auth.require_operator)):
    """Post the day's report to the configured channel, on demand.

    The same report the scheduler posts — same criteria, same mention mode —
    so a manual send can never say something different from the morning run.
    """
    _require_date(date_str)
    try:
        result = await slack.post_day_report(date_str)
    except Exception as e:
        raise _slack_send_errors(e)
    vault.audit(user["email"], "slack.report.day",
                f"{date_str} replies={result.get('thread_replies')}")
    return {"ok": True, "date": date_str,
            "thread_replies": result.get("thread_replies"),
            "mention_mode": result.get("mention_mode"),
            "unresolved_names": result.get("unresolved_names") or []}


@app.post("/api/open/report")
async def send_open_report(start: str | None = None, end: str | None = None,
                           states: str | None = None,
                           user: dict = Depends(auth.require_operator)):
    """Post the open-backlog report, scoped by the tab's date/status filters."""
    s, e, st = _open_args(start, end, states)
    try:
        result = await slack.post_open_report(s, e, st)
    except Exception as e2:
        raise _slack_send_errors(e2)
    vault.audit(user["email"], "slack.report.open",
                f"start={s} end={e} states={st} "
                f"replies={result.get('thread_replies')}")
    return {"ok": True,
            "thread_replies": result.get("thread_replies"),
            "mention_mode": result.get("mention_mode"),
            "unresolved_names": result.get("unresolved_names") or []}


# ── tickets for a day ─────────────────────────────────────────────────────────

@app.get("/api/day/{date_str}")
async def get_day(date_str: str, user: dict = Depends(auth.require_user)):
    def load():
        log = db.get_fetch_log(date_str)
        tickets = review.annotate_tickets(db.get_day_tickets(date_str), user)
        return (log, tickets, db.latest_qc_run(date_str),
                db.qc_spend_for_date(date_str))

    log, tickets, last_run, spend = await asyncio.to_thread(load)
    return {
        "date": date_str,
        "fetched": log is not None,
        "ticket_count": len(tickets),
        "tickets": tickets,
        "last_run": last_run,
        # Cumulative across every run for the date, so a rescore adds to the
        # figure rather than appearing to reset it.
        "spend": spend,
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
                       rc.r8, rc.r9,
                       ac.a1, ac.a2, ac.a3, ac.a4, ac.a5, ac.ai_notes,
                       ac.overall_result, ac.checked_at AS ai_checked_at
                FROM tickets t
                LEFT JOIN accounts    a  ON t.account_id = a.id
                LEFT JOIN rule_checks rc ON t.id = rc.ticket_id
                LEFT JOIN ai_checks   ac ON t.id = ac.ticket_id
                WHERE t.id = ? AND t.deleted_at IS NULL
            """, (ticket_id,)).fetchone()
            if not t:
                return None, [], {}
            msgs = conn.execute(
                "SELECT * FROM messages WHERE ticket_id = ? ORDER BY timestamp",
                (ticket_id,)
            ).fetchall()
            ticket = dict(t)
            messages = [dict(m) for m in msgs]

            # Why each R-check landed where it did. The stored verdict is only
            # Pass/Fail/N/A, so a reviewer looking at a pass previously saw the
            # rule's generic description and no evidence at all.
            try:
                why = evidence.for_ticket(ticket, messages)
            except Exception:
                logger.exception("Could not build evidence for %s", ticket_id)
                why = {}
            return ticket, messages, why

    ticket, messages, why = await asyncio.to_thread(query)
    if not ticket:
        raise HTTPException(404, "Ticket not found")
    review.annotate_tickets([ticket], user)
    return {"ticket": ticket, "messages": messages, "evidence": why}


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
    if record["decision"] == "Revert":
        return {"ok": True, "review": None}
    return {"ok": True, "review": {
        "decision": record["decision"],
        "kept_ai": bool(record["kept_ai"]),
        "reviewer_email": record["reviewer_email"],
        "reviewer_name": record["reviewer_name"],
        "reviewed_at": record["reviewed_at"],
        "note": record["note"],
    }}


# ── Report Card: the frozen grade of record ──────────────────────────────────

@app.get("/reportcard", response_class=HTMLResponse)
async def reportcard_page(user: dict = Depends(auth.require_user)):
    return _page("reportcard.html")


@app.get("/api/reportcard/dates")
async def reportcard_dates(user: dict = Depends(auth.require_user)):
    return {"dates": await asyncio.to_thread(reportcard.snapshot_dates)}


@app.get("/api/reportcard/day/{date_str}")
async def reportcard_day(date_str: str,
                         user: dict = Depends(auth.require_user)):
    _require_date(date_str)
    return await asyncio.to_thread(reportcard.day, date_str)


@app.get("/api/reportcard/leaderboard")
async def reportcard_leaderboard(start: str, end: str,
                                 user: dict = Depends(auth.require_user)):
    _require_date(start)
    _require_date(end)
    if start > end:
        raise HTTPException(400, "start must be on or before end")
    return await asyncio.to_thread(reportcard.leaderboard, start, end)


@app.get("/api/reportcard/rewards")
async def reportcard_rewards(weeks: int = 4,
                             user: dict = Depends(auth.require_user)):
    if not (1 <= weeks <= 26):
        raise HTTPException(400, "weeks must be between 1 and 26")
    return await asyncio.to_thread(reportcard.rewards, weeks)


@app.post("/api/reportcard/capture/{date_str}")
async def reportcard_capture(date_str: str,
                             user: dict = Depends(auth.require_admin)):
    """Admin backfill of one date. Insert-once: an existing snapshot wins.

    Freezes the grades as they stand NOW, labelled with the admin's email —
    honest seed data for the cold start, never disguised as the morning run.
    """
    _require_date(date_str)
    if date_str > date.today().isoformat():
        raise HTTPException(400, "Cannot snapshot a future date")
    with db.get_conn() as conn:
        fetched = conn.execute("SELECT 1 FROM fetch_log WHERE fetch_date = ?",
                               (date_str,)).fetchone()
    if not fetched:
        raise HTTPException(400, f"{date_str} has never been fetched — there "
                                 "is nothing to freeze. Fetch the day first.")
    res = await asyncio.to_thread(reportcard.capture, date_str, None,
                                  user["email"])
    if not res.get("captured"):
        raise HTTPException(409, "A snapshot already exists for this date — "
                                 "frozen records are never replaced")
    vault.audit(user["email"], "reportcard.capture",
                f"{date_str} tickets={res['tickets']}")
    return res


@app.post("/api/reportcard/backfill")
async def reportcard_backfill(request: Request,
                              user: dict = Depends(auth.require_admin)):
    """Admin backfill of a range: every fetched, unsnapshotted day, once each."""
    body = await request.json()
    start = _require_date(str(body.get("start") or "")).isoformat()
    end = _require_date(str(body.get("end") or "")).isoformat()
    if start > end:
        raise HTTPException(400, "start must be on or before end")
    if end > date.today().isoformat():
        raise HTTPException(400, "Cannot snapshot future dates")
    if (date.fromisoformat(end) - date.fromisoformat(start)).days > 120:
        raise HTTPException(400, "Backfill at most 120 days at a time")
    res = await asyncio.to_thread(reportcard.backfill, start, end,
                                  user["email"])
    vault.audit(user["email"], "reportcard.backfill",
                f"{start}..{end} captured={res['count']} "
                f"skipped={res['skipped_existing']}")
    return res


@app.get("/api/reportcard/export/{date_str}")
async def reportcard_csv(date_str: str,
                         user: dict = Depends(auth.require_user)):
    """The frozen day as raw CSV — the data the evaluation used."""
    _require_date(date_str)
    try:
        content = await asyncio.to_thread(reportcard.day_csv, date_str)
    except ValueError as e:
        raise HTTPException(404, str(e))
    return StreamingResponse(
        iter([content]), media_type="text/csv",
        headers={"Content-Disposition":
                 f'attachment; filename="report-card-{date_str}.csv"'})


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
    "r4": ("R4 — Response time", "No customer message may go unanswered longer than the SLA."),  # SLA filled in live
    "r5": ("R5 — Status ownership", "A ticket's state must match who owns the next action, proven by an @-mention of someone on the right team (or a Rootly/Jira link for engineering)."),
    "r7": ("R7 — Rootly/Jira link", "Engineering tickets must reference a Rootly incident or Jira issue. No parameters."),
    "r8": ("R8 — Oncall completeness", "When escalated to oncall, the required fields must be consistent."),  # conditions filled in live
    "r10": ("R10 — SpotAssist trigger (advisory)", "Slack tickets should engage SpotAssist."),  # bot name filled in live
    "r11": ("R11 — Follow-through", "Support must not go silent after taking the last word."),  # hours filled in live
    "a":  ("A1–A5 — AI grading", "Category accuracy, customer sentiment, response quality, status-vs-conversation, and premature closure — graded by Gemini against a fixed rubric with pinned generation."),
}


def live_rule_descriptions() -> dict:
    """RULE_DESCRIPTIONS with the configurable parts filled in from the rules.

    The prose used to state settings as facts — "all four fields must be
    consistent", "the 'functionalities' custom field" — which stops being true
    the moment an admin changes one. A description that contradicts the control
    next to it is worse than no description, so the two configurable ones are
    completed from the live document, and a switched-off check says so first.
    """
    import rules as qc_rules
    import scorer

    out = {}
    off = qc_rules.disabled_checks()
    conditions = qc_rules.r8_conditions()
    for key, (title, desc) in RULE_DESCRIPTIONS.items():
        if key == "r4":
            desc = (f"No customer message may go unanswered longer than "
                    f"{qc_rules.sla_hours():g} hours.")
        elif key == "r8":
            required = [scorer.R8_CONDITION_LABELS[c]
                        for c in scorer.R8_CONDITIONS if c in conditions]
            desc = ("When escalated to oncall, all of these must hold: "
                    + "; ".join(required) + ".")
        elif key == "r10":
            bot = qc_rules.spotassist_author()
            desc = (f"Advisory — never fails a ticket. On "
                    f"{', '.join(sorted(qc_rules.spotassist_sources()))} tickets, "
                    f"{bot} only engages when someone adds the ticket emoji; if a "
                    f"rep answers by hand without {bot} ever appearing in the "
                    f"thread, the emoji was skipped and this check flags it.")
        elif key == "r11":
            desc = (f"In {', '.join(sorted(qc_rules.r11_states()))}, support may "
                    f"hold the last public word for at most "
                    f"{qc_rules.r11_update_hours():g} working hours (weekends "
                    f"skipped) before the customer is owed an update. The mirror "
                    f"of R4: R4 times the first reply, this times the follow-up "
                    f"after \"I'm checking, will update you\".")
        if key in off:
            desc = ("Switched off — stored verdicts are kept but no longer "
                    "count towards any grade. " + desc)
        out[key] = (title, desc)
    return out


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
                "SELECT state, COUNT(*) n FROM tickets"
                " WHERE state IS NOT NULL AND deleted_at IS NULL"
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

    # How many tickets carry each status, so a row says what editing it affects.
    # Its own query: `states_seen` returns bare names for the scope chips.
    def status_counts():
        with db.get_conn() as conn:
            return {r["state"]: r["n"] for r in conn.execute(
                "SELECT state, COUNT(*) n FROM tickets"
                " WHERE state IS NOT NULL AND state != '' AND deleted_at IS NULL"
                " GROUP BY state").fetchall()}

    state_counts = await asyncio.to_thread(status_counts)

    return {
        "rules":        current,
        "defaults":     qc_rules.defaults(),
        "labels":       labels,
        "rules_hash":   qc_rules.rules_hash(),
        "descriptions": live_rule_descriptions(),
        # The toggle surface, served rather than hardcoded in the page so the
        # controls cannot offer a key validation would reject.
        "checks": [
            {
                "key":     key,
                "title":   live_rule_descriptions()[key][0],
                "help":    live_rule_descriptions()[key][1],
                "enabled": key not in qc_rules.disabled_checks(),
            }
            for key in __import__("scorer").TOGGLEABLE_CHECKS
        ],
        "r5_eng_sources": [
            {"key": s,
             "label": __import__("scorer").R5_ENG_SOURCE_LABELS[s],
             "accepted": s in qc_rules.r5_eng_sources()}
            for s in __import__("scorer").R5_ENG_SOURCES
        ],
        "r8_conditions": [
            {
                "key":      c,
                "label":    __import__("scorer").R8_CONDITION_LABELS[c],
                "required": c in qc_rules.r8_conditions(),
            }
            for c in __import__("scorer").R8_CONDITIONS
        ],
        "can_undo": qc_rules.has_previous(),
        # What each status means to the checks, plus the vocabulary the editor
        # offers, served so the UI cannot present a value validation rejects.
        "statuses": sorted(
            (
                {"state": state, **row,
                 "seen": state_counts.get(state, 0),
                 "is_default": row == {**__import__("scorer").STATUS_FALLBACK,
                                       **__import__("scorer")
                                       .DEFAULT_STATUS_POLICY.get(state, {})}}
                for state, row in qc_rules.all_status_policies().items()
            ),
            key=lambda r: (-r["seen"], r["state"]),
        ),
        "r5_expectations": [
            {"value": v, "label": __import__("scorer").R5_EXPECTATION_LABELS[v]}
            for v in __import__("scorer").R5_EXPECTATIONS
        ],
        # Which Pylon field each check reads, and what it is called, so the
        # editor can offer a picker rather than a slug to type from memory.
        "fields": [
            {
                "name":    name,
                "slug":    qc_rules.field(name),
                "default": default,
                "used_by": list(__import__("scorer").FIELD_USED_BY.get(name, ())),
            }
            for name, default in __import__("scorer").DEFAULT_FIELD_MAP.items()
        ],
        "states_seen":  await asyncio.to_thread(states_seen),
        "meta":         vault.get_setting_meta(qc_rules.RULES_KEY),
        "can_edit":     auth.can_edit_rules(user),
        # The prompt as it will actually be sent on the next run, not a
        # hardcoded literal — whoever reads this needs to see their own edits
        # reflected, or the read-only view is a lie.
        "rubric":         prompts.system_prompt(current),
        "prompt_sections": [
            {
                "key":     key,
                "title":   prompts.SECTION_LABELS[key][0],
                "help":    prompts.SECTION_LABELS[key][1],
                "value":   current.get(key) or "",
                "default": prompts.DEFAULT_SECTIONS[key],
                "grades":  list(prompts.GRADES.get(key[:2], ())),
            }
            for key in prompts.SECTION_KEYS
        ],
        # Shown read-only. These describe the wire contract, not grading policy:
        # editing them would not change a grade, it would break scoring.
        "prompt_fixed": [
            {"title": title, "text": text} for title, text in prompts.FIXED_BLOCKS
        ],
        "prompt_hash":  prompts.fingerprint(current),
        "prompt_limit": prompts.MAX_SECTION_CHARS,
    }


@app.get("/api/ticket-states")
async def ticket_states(user: dict = Depends(auth.require_user)):
    """Every Pylon status seen across all fetched tickets, with totals.

    The dashboard's status filter offers these rather than only the states
    present on the selected day: a filter that silently changes its own options
    as you move between days is not a filter you can rely on.
    """
    def query():
        with db.get_conn() as conn:
            rows = conn.execute(
                "SELECT state, COUNT(*) AS n FROM tickets"
                " WHERE state IS NOT NULL AND state != '' AND deleted_at IS NULL"
                " GROUP BY state ORDER BY n DESC"
            ).fetchall()
        return [dict(r) for r in rows]

    return {"states": await asyncio.to_thread(query)}


@app.get("/api/rules/suggestions")
async def rules_suggestions(days: int = 30,
                            user: dict = Depends(auth.require_user)):
    """Where humans have been overriding the AI — evidence, not actions.

    Read-only by design: the system may propose a rules change and show what it
    is based on, but never applies one. Auto-tuning a grading rubric from its own
    past disagreements is a feedback loop with no human in it, and the failure
    mode is silent drift in what "Pass" means with nobody able to say when it
    changed. Accepting a suggestion goes through the normal gated rules save.
    """
    if not 1 <= days <= 365:
        raise HTTPException(400, "days must be between 1 and 365")
    return {"ok": True, **await asyncio.to_thread(suggestions.build, days)}


@app.post("/api/rules/dry-run")
async def rules_dry_run(request: Request,
                        user: dict = Depends(auth.require_rules_editor)):
    """Grade a few real tickets with unsaved rubric text. Writes nothing.

    Gated to rubric editors because it spends money on the workspace's Vertex
    quota, not because it changes anything — it deliberately cannot. The draft
    never reaches `app_settings`, no `ai_checks` row is touched, and no run is
    recorded. What comes back is a side-by-side of the stored grade and the
    grade the draft produced, so an editor can see the effect of a rubric edit
    before making it everyone's grades.

    The response labels its own cost. Token counts come from the API for this
    specific call, but the figure is still an estimate of what the same edit
    would cost across a full run, and the UI says so.
    """
    import rules as qc_rules

    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(400, "expected a JSON object")

    # Only string fields are rubric text; `limit` and `date` are controls.
    draft = {k: v for k, v in body.items()
             if isinstance(v, str) and k not in ("date",)}
    errors = qc_rules.validate(draft)
    if errors:
        # A draft that could not be saved must not be billable either.
        raise HTTPException(400, "; ".join(errors[:4]))

    # Validated then re-serialised: fetch_date is stored as text, and handing
    # sqlite3 a date object relies on a deprecated adapter.
    day = body.get("date")
    day = _require_date(str(day)).isoformat() if day else None

    try:
        result = await asyncio.to_thread(
            dryrun.run, draft, body.get("limit", dryrun.DEFAULT_LIMIT), day
        )
    except qc_runner.VertexNotConfigured as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        logger.exception("rules dry-run failed")
        raise HTTPException(502, f"Dry-run could not complete: {str(e)[:300]}")

    return {"ok": True, **result}


@app.post("/api/rules/preview")
async def rules_preview(request: Request,
                        user: dict = Depends(auth.require_rules_editor)):
    """What a draft rules change would do to the R-checks. Writes nothing.

    Deterministic and local — no AI call and no Slack call — so unlike the
    A-rubric dry-run this costs nothing but CPU and can cover a whole range
    rather than a sample.
    """
    import rcheck_dryrun

    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(400, "expected a JSON object")

    draft = body.get("rules")
    if draft is not None and not isinstance(draft, dict):
        raise HTTPException(400, "rules must be an object")

    start, end = body.get("start"), body.get("end")
    if bool(start) != bool(end):
        raise HTTPException(400, "Provide both start and end, or neither")
    if start and end:
        if _require_date(start) > _require_date(end):
            raise HTTPException(400, "start must not be after end")

    try:
        result = await asyncio.to_thread(
            rcheck_dryrun.run, draft,
            body.get("limit", rcheck_dryrun.DEFAULT_LIMIT), start, end)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        logger.exception("rules preview failed")
        raise HTTPException(502, f"Preview could not complete: {str(e)[:300]}")

    return {"ok": True, **result}


@app.get("/api/pylon/fields")
async def pylon_fields(user: dict = Depends(auth.require_user)):
    """Pylon's live issue custom fields, plus which mappings no longer match one.

    The drift list is the point. A check whose field has been retired does not
    fail loudly — an absent custom field reads as an empty one, so the check
    simply fails every ticket, quietly, until somebody notices the failure
    counts. That is exactly what happened with `does_rootly_exist`.
    """
    import rules as qc_rules
    import scorer

    mapped = qc_rules.field_map()
    try:
        fields = await pylon.fetch_custom_fields()
    except pylon.PylonNotConfigured as e:
        return {"ok": False, "message": str(e), "fields": [], "drift": []}
    except Exception as e:
        logger.warning("Could not list Pylon custom fields: %s", e)
        return {"ok": False, "message": str(e)[:300], "fields": [], "drift": []}

    known = {f.get("slug"): f for f in fields if f.get("slug")}
    drift = [
        {
            "name":  name,
            "slug":  slug,
            "used_by": list(scorer.FIELD_USED_BY.get(name, ())),
            "message": (
                f"{' and '.join(scorer.FIELD_USED_BY.get(name, ())) or 'A check'} "
                f"reads '{slug}', which Pylon no longer defines. Until it is "
                f"repointed those checks read an empty value on every ticket."
            ),
        }
        for name, slug in sorted(mapped.items())
        if slug and slug not in known
    ]

    return {
        "ok": True,
        "fields": sorted(
            ({"slug": f["slug"], "label": f.get("label") or f["slug"],
              "type": f.get("type"), "read_only": bool(f.get("is_read_only"))}
             for f in fields if f.get("slug")),
            key=lambda f: f["label"].lower(),
        ),
        "mapped": mapped,
        "drift":  drift,
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
async def put_rules(request: Request,
                    user: dict = Depends(auth.require_rules_editor)):
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

    # Stored overalls are derived from stored R-verdicts through the enabled-key
    # mask, so a rules save can change a verdict without any ticket changing.
    # Resyncing here is what makes switching a check off take effect now rather
    # than at the next refetch — and it costs nothing: pure SQL, no AI calls.
    resync = await asyncio.to_thread(resync_overall.run)
    if resync["overall_updated"] or resync["notes_updated"]:
        vault.audit(
            user["email"], "rules.resync",
            f"hash={qc_rules.rules_hash()} "
            f"overall={resync['overall_updated']} notes={resync['notes_updated']} "
            f"({', '.join(f'{k} x{v}' for k, v in sorted(resync['changes'].items())) or '-'})",
        )

    return {"ok": True, "rules": qc_rules.current(), "rules_hash": qc_rules.rules_hash(),
            "meta": vault.get_setting_meta(qc_rules.RULES_KEY),
            # What the save actually moved, so the page can say so instead of
            # leaving the admin to guess whether it did anything.
            "resync": resync,
            "can_undo": qc_rules.has_previous()}


@app.post("/api/rules/undo")
async def undo_rules(user: dict = Depends(auth.require_rules_editor)):
    """Put the previous rules document back, and resync what it changes.

    One step, not a history. `set_raw_setting` is INSERT OR REPLACE and the
    audit log records only a hash, so without this a save that moved grades the
    wrong way could not be walked back.
    """
    import rules as qc_rules

    restored = await asyncio.to_thread(qc_rules.restore_previous, user["email"])
    if not restored:
        raise HTTPException(400, "There is no previous rules document to restore.")

    vault.audit(user["email"], "rules.undo", f"hash={qc_rules.rules_hash()}")
    resync = await asyncio.to_thread(resync_overall.run)
    return {"ok": True, "rules": qc_rules.current(),
            "rules_hash": qc_rules.rules_hash(),
            "meta": vault.get_setting_meta(qc_rules.RULES_KEY),
            "resync": resync,
            "can_undo": qc_rules.has_previous()}


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
        # Separate from can_edit: an operator may trigger a run but not change
        # when runs happen.
        "can_run":   auth.can_run_qc(user),
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
    _require_date(date_str)

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
            "r1", "r2", "r3", "r4", "r5", "r7", "r8", "r10",
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
            "r10": "R10 SpotAssist (advisory)",
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
    if start and end:
        _require_date(start)
        _require_date(end)
    if month:
        _require_month(month)

    def query():
        import rules as qc_rules

        # Every branch carries the soft-delete guard: a ticket removed at source
        # must not keep counting toward anyone's leaderboard.
        if start and end:
            where  = "WHERE t.deleted_at IS NULL AND t.fetch_date BETWEEN ? AND ?"
            params = [start, end]
        elif month:
            where  = "WHERE t.deleted_at IS NULL AND t.fetch_date LIKE ?"
            params = [f"{month}-%"]
        else:
            where  = "WHERE t.deleted_at IS NULL"
            params = []

        # Out-of-scope states, on the same terms as scoring and the leaderboard.
        # Without this, archived tickets counted toward every assignee's total
        # and sat as "pending" forever — waiting for a grade the scorer would
        # never give them, because it excluded them too.
        clause, extra = qc_rules.excluded_state_clause("t")
        if clause:
            where += f" AND {clause}"
            params += extra
        with db.get_conn() as conn:
            rows = conn.execute(f"""
                SELECT
                    COALESCE(t.assignee_name, 'Unassigned')            AS assignee,
                    COUNT(*)                                           AS total,
                    SUM(CASE WHEN {_EFFECTIVE_GRADE} = 'Pass'
                             THEN 1 ELSE 0 END)                        AS pass_count,
                    SUM(CASE WHEN {_EFFECTIVE_GRADE} = 'Fail'
                             THEN 1 ELSE 0 END)                        AS fail_count,
                    SUM(CASE WHEN {_EFFECTIVE_GRADE} = 'Needs Review'
                             THEN 1 ELSE 0 END)                        AS review_count,
                    SUM(CASE WHEN ac.ticket_id IS NULL
                             THEN 1 ELSE 0 END)                        AS pending_count,
                    COUNT(ac.ticket_id)                                AS ai_done
                FROM tickets t
                LEFT JOIN ai_checks ac ON ac.ticket_id = t.id
                LEFT JOIN ({_LATEST_REVIEW}) rev ON rev.ticket_id = t.id
                {where}
                GROUP BY t.assignee_name
                ORDER BY pass_count DESC, total DESC
            """, params).fetchall()
            return [dict(r) for r in rows]
    return {"month": month, "start": start, "end": end,
            "assignees": await asyncio.to_thread(query)}


# ── leaderboard ───────────────────────────────────────────────────────────────

@app.get("/leaderboard", response_class=HTMLResponse)
async def leaderboard_page(user: dict = Depends(auth.require_user)):
    return _page("leaderboard.html")


@app.get("/api/leaderboard")
async def get_leaderboard(start: str | None = None, end: str | None = None,
                          user: dict = Depends(auth.require_user)):
    """Team and individual standings. Both dates or neither."""
    if bool(start) != bool(end):
        raise HTTPException(400, "Provide both start and end, or neither")
    if start and end:
        if _require_date(start) > _require_date(end):
            raise HTTPException(400, "start must not be after end")
    return await asyncio.to_thread(leaderboard.build, start, end)


@app.get("/api/analytics/tickets")
async def get_failing_tickets(
    check: str,
    assignee: str | None = None,
    start: str | None = None,
    end: str | None = None,
    limit: int = 200,
    user: dict = Depends(auth.require_user),
):
    """Tickets where one named check failed — the analytics drill-down.

    `check` is validated against an allowlist inside `drilldown`; an unknown
    value is rejected rather than quietly returning everything.
    """
    if start:
        _require_date(start)
    if end:
        _require_date(end)
    if start and end and _require_date(start) > _require_date(end):
        raise HTTPException(400, "start must not be after end")
    try:
        return await asyncio.to_thread(
            drilldown.tickets_failing, check, assignee, start, end, limit)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/analytics/assignee")
async def get_assignee_breakdown(
    name: str,
    month: str | None = None,
    start: str | None = None,
    end: str | None = None,
    user: dict = Depends(auth.require_user),
):
    """Per-check pass/fail bifurcation for one assignee — what opens when a
    name on the analytics leaderboard is clicked.

    Takes the same range shapes as /api/analytics: start+end, or month, or
    neither for all time. A month is widened to its first/last day here so the
    query layer only ever sees one range shape.
    """
    if start and end:
        _require_date(start)
        _require_date(end)
        if _require_date(start) > _require_date(end):
            raise HTTPException(400, "start must not be after end")
    elif month:
        _require_month(month)
        # "-31" is a string bound, not a date: fetch_date is ISO text, so it
        # covers every real day of the month in every month.
        start, end = f"{month}-01", f"{month}-31"
    try:
        return await asyncio.to_thread(
            drilldown.assignee_breakdown, name, start, end)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/leaderboard/weekly")
async def get_weekly_leaderboard(weeks: int = 8,
                                 user: dict = Depends(auth.require_user)):
    """Week-over-week standings, most recent week first."""
    if not 1 <= weeks <= 26:
        raise HTTPException(400, "weeks must be between 1 and 26")
    return await asyncio.to_thread(leaderboard.build_weekly, weeks)


# ── stats ─────────────────────────────────────────────────────────────────────

@app.get("/api/stats")
async def get_stats(date: str | None = None, user: dict = Depends(auth.require_user)):
    if date:
        _require_date(date)
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
        # Served rather than hardcoded in the page, so adding a role is a change
        # in one place and the selector cannot fall out of step with validation.
        "roles":           [
            {"value": r, "label": auth.ROLE_LABELS[r],
             "description": auth.ROLE_DESCRIPTIONS[r]}
            for r in auth.ROLES
        ],
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


@app.get("/api/admin/surveys")
async def admin_surveys(user: dict = Depends(auth.require_user)):
    """Every Pylon survey, for the Admin CSAT picker.

    GET https://api.usepylon.com/surveys — the id saved here is what
    Weekly uses on GET /surveys/{id}/responses.
    """
    try:
        surveys = await pylon.fetch_surveys()
    except pylon.PylonNotConfigured as e:
        raise HTTPException(503, str(e))
    except Exception as e:
        raise HTTPException(502, f"Pylon surveys failed: {e}") from e
    return {
        "surveys": [
            {
                "id": s.get("id"),
                "name": s.get("name") or s.get("id"),
                "type": s.get("type") or "",
            }
            for s in surveys if s.get("id")
        ],
        "selected": vault.get_setting("csat_survey_id"),
    }


@app.put("/api/admin/settings")
async def update_settings(request: Request, user: dict = Depends(auth.require_admin)):
    body = await request.json()
    # An explicit clear is a deliberate act, so it needs saying. Without this
    # flag, blanking a protected setting is refused rather than obeyed.
    allow_clear = bool(body.pop("_allow_clear", False))
    refused = vault.set_settings(body, user["email"], allow_clear=allow_clear)
    qc_runner.invalidate_vertex_client()

    saved = sorted(set(body) - set(refused))
    if saved:
        vault.audit(user["email"], "settings.update", ", ".join(saved))
    if refused:
        vault.audit(user["email"], "settings.refused", ", ".join(sorted(refused)))
    return {
        "ok": True,
        "settings":        vault.get_settings(),
        "setting_sources": vault.get_setting_sources(),
        "schedule":        scheduler.next_run_description(),
        # Reported back rather than silently dropped: values the environment
        # owns, and protected values an empty form would have erased.
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


@app.get("/api/admin/slack/identities")
async def slack_identities(user: dict = Depends(auth.require_user)):
    """Which assignees can be @-mentioned, and which cannot.

    An unresolvable name is not an error — the report falls back to plain text —
    but it is invisible unless surfaced here, so admins can add a mapping.
    """
    names = await asyncio.to_thread(review.list_assignee_names)
    names = [n for n in names if n != "Unassigned"]
    try:
        resolved = await slack.resolve_assignee_ids(names)
    except slack.SlackNotConfigured:
        resolved = {}

    return {
        "mode": slack.mention_mode(),
        "modes": list(slack.MENTION_MODES),
        "mapped": slack.identity_map(),
        "resolved": {n: resolved.get(n) for n in names},
        "unresolved": sorted(n for n in names if not resolved.get(n)),
        "can_edit": user["role"] == "admin",
    }


@app.put("/api/admin/slack/identities")
async def put_slack_identities(request: Request,
                               user: dict = Depends(auth.require_admin)):
    """Replace the assignee-name → Slack-user-ID overrides."""
    body = await request.json()
    mapping = body.get("mapped")
    if not isinstance(mapping, dict):
        raise HTTPException(400, 'Body must be {"mapped": {"Name": "U…"}}')

    cleaned = {}
    for name, sid in mapping.items():
        name, sid = str(name).strip(), str(sid).strip()
        if not name or not sid:
            continue
        # DO_NOT_MENTION is the one non-ID value with a meaning: never tag
        # this name, even when the directory could resolve it.
        if sid != slack.DO_NOT_MENTION and not re.fullmatch(r"[UW][A-Z0-9]{4,}", sid):
            raise HTTPException(400, f"{sid!r} is not a Slack user ID")
        cleaned[name] = sid

    vault.set_raw_setting(slack.IDENTITY_MAP_KEY, json.dumps(cleaned), user["email"])
    vault.audit(user["email"], "slack.identities.save", f"{len(cleaned)} mapped")
    return {"ok": True, "mapped": cleaned}


@app.post("/api/admin/slack/test")
async def slack_test(user: dict = Depends(auth.require_admin)):
    try:
        await slack.post_test()
        return {"ok": True, "message": "Test message posted"}
    except Exception as e:
        return {"ok": False, "message": str(e)[:300]}


@app.post("/api/admin/run-now")
async def run_now(request: Request,
                  user: dict = Depends(auth.require_operator)):
    body = await request.json() if await request.body() else {}
    date_str = (body or {}).get("date")
    if date_str:
        target = _require_date(date_str)
        trigger = None  # an explicit date is a backfill, not today's run
    else:
        offset = 0 if vault.get_setting("schedule_target") == "today" else 1
        target = date.today() - timedelta(days=offset)
        # This IS today's scheduled work done early, so record it under
        # today's trigger date — otherwise the scheduler sees no run for
        # today and fetches, scores and Slack-posts the same date again.
        trigger = datetime.now(scheduler._tz()).date().isoformat()

    try:
        result = await scheduler.run_pipeline(target, user["email"],
                                              trigger_date=trigger)
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

    role = body.get("role", target["role"])
    if role not in auth.ROLES:
        raise HTTPException(
            400, "Role must be one of: " + ", ".join(f"'{r}'" for r in auth.ROLES))

    # `1 if is_active else 0` silently reactivated a revoked user whenever the
    # value arrived as a JSON string, because "0" and "false" are both truthy in
    # Python. Accept only what the API actually documents.
    raw_active = body.get("is_active", target["is_active"])
    if isinstance(raw_active, str):
        if raw_active.strip().lower() not in ("0", "1", "true", "false", "yes", "no"):
            raise HTTPException(400, "is_active must be true or false")
        is_active = raw_active.strip().lower() in ("1", "true", "yes")
    else:
        is_active = bool(raw_active)

    # Never let the last remaining admin be demoted or deactivated: the Admin UI
    # is itself behind sign-in, so there would be no way back in.
    losing_admin = target["role"] == "admin" and (role != "admin" or not is_active)
    if losing_admin:
        with db.get_conn() as conn:
            others = conn.execute(
                "SELECT COUNT(*) AS n FROM app_users"
                " WHERE role = 'admin' AND is_active = 1 AND email != ?",
                (email,),
            ).fetchone()["n"]
        if others == 0:
            raise HTTPException(
                400,
                "This is the only active administrator. Promote someone else first.",
            )

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
    if role not in auth.ROLES:
        raise HTTPException(400, "Role must be 'admin' or 'member'")
    # Inviting yourself was a self-demotion path: the UPDATE below used to apply
    # to existing rows, and with QC_ADMIN_EMAILS unset the last admin could
    # remove their own access with no way back short of database surgery.
    if email == user["email"]:
        raise HTTPException(400, "You cannot change your own role or access")

    with db.get_conn() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO app_users (email, name, role, is_active, created_at)"
            " VALUES (?, ?, ?, 1, ?)",
            (email, email.split("@")[0], role,
             datetime.now(timezone.utc).isoformat()),
        )
        created = cur.rowcount > 0

    # An invite pre-authorises someone who has never signed in. Changing an
    # existing person's role is a different decision and belongs to
    # PUT /api/admin/users/{email}, which has its own guards.
    if not created:
        raise HTTPException(
            409,
            f"{email} already has access. Change their role from the users list.",
        )

    vault.audit(user["email"], "user.invite", f"{email} role={role}")
    return {"ok": True, "users": auth.list_users()}


class RevalidatedStaticFiles(StaticFiles):
    """StaticFiles that forbids heuristic caching.

    Starlette sends ETag/Last-Modified but no Cache-Control, which lets a
    browser reuse a cached shell.js for days without asking — so every deploy
    left signed-in users on the old UI until a hard refresh. `no-cache` does
    not mean "don't cache": it means "revalidate before use", and the ETag
    makes that revalidation a cheap 304 rather than a re-download.
    """

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


app.mount("/static", RevalidatedStaticFiles(directory=STATIC_DIR), name="static")
