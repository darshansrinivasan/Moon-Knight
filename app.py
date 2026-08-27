import asyncio
import csv
import html
import io
import json
import logging
import os
import re
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
import gcp
import leaderboard
import prompts
import pylon
import qc_runner
import resync_overall
import review
import scheduler
import scorer
import slack
import suggestions
import vault

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
async def fetch_day(date_str: str, user: dict = Depends(auth.require_user)):
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
                 user: dict = Depends(auth.require_user)):
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

    return {
        "rules":        current,
        "defaults":     qc_rules.defaults(),
        "labels":       labels,
        "rules_hash":   qc_rules.rules_hash(),
        "descriptions": RULE_DESCRIPTIONS,
        "states_seen":  await asyncio.to_thread(states_seen),
        "meta":         vault.get_setting_meta(qc_rules.RULES_KEY),
        "can_edit":     user["role"] == "admin",
        # The prompt as it will actually be sent on the next run, not a
        # hardcoded literal — an admin reading this needs to see their own edits
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
    changed. Accepting a suggestion goes through the normal admin-gated rules
    save.
    """
    if not 1 <= days <= 365:
        raise HTTPException(400, "days must be between 1 and 365")
    return {"ok": True, **await asyncio.to_thread(suggestions.build, days)}


@app.post("/api/rules/dry-run")
async def rules_dry_run(request: Request,
                        user: dict = Depends(auth.require_admin)):
    """Grade a few real tickets with unsaved rubric text. Writes nothing.

    Admin-gated because it spends money on the workspace's Vertex quota, not
    because it changes anything — it deliberately cannot. The draft never
    reaches `app_settings`, no `ai_checks` row is touched, and no run is
    recorded. What comes back is a side-by-side of the stored grade and the
    grade the draft produced, so an admin can see the effect of a rubric edit
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
        if not re.fullmatch(r"[UW][A-Z0-9]{4,}", sid):
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
async def run_now(request: Request, user: dict = Depends(auth.require_admin)):
    body = await request.json() if await request.body() else {}
    date_str = (body or {}).get("date")
    if date_str:
        target = _require_date(date_str)
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

    role = body.get("role", target["role"])
    if role not in ("admin", "member"):
        raise HTTPException(400, "Role must be 'admin' or 'member'")

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
    if role not in ("admin", "member"):
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


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
