"""
Background scheduler for the daily QC run.

Ticks once every 30 s. When the configured local time for the day has passed
and no successful run exists for that trigger date, it runs the full pipeline:

    fetch from Pylon → AI scoring → post summary to Slack

Because the guard is "no successful run for this trigger date" rather than an
exact-minute match, a run missed while the server was down is picked up on the
next tick instead of being skipped.
"""

import asyncio
import logging
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import db
import qc_runner
import slack
import vault

logger = logging.getLogger(__name__)

TICK_SECONDS = 30
LOCK_NAME    = "daily_run"

# A failing run should retry (transient Pylon/Vertex blips recover), but a
# misconfiguration must not re-fire every tick forever.
MAX_ATTEMPTS   = 3
RETRY_BACKOFF  = timedelta(minutes=15)

# A 'running' row older than this belongs to a process that died mid-run. Must
# exceed the advisory lock TTL below, or a run still holding the lock would be
# judged abandoned and its retry would immediately hit LockBusy.
STALE_RUN_AFTER = timedelta(minutes=db.STALE_RUN_MINUTES)
# 30 min. A real run takes about a minute, so this is generous; the old 2 hours
# meant a lock whose holder died blocked every run for the rest of the morning.
# Startup also deletes stale locks outright, since their holder cannot be alive.
LOCK_TTL_SECONDS = 1800

_task: asyncio.Task | None = None


# ── helpers ───────────────────────────────────────────────────────────────────

def _tz() -> ZoneInfo:
    name = vault.get_setting("schedule_tz") or "Asia/Kolkata"
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        logger.warning("Unknown timezone %r, falling back to UTC", name)
        return ZoneInfo("UTC")


def _target_date(trigger_day: date) -> date:
    if vault.get_setting("schedule_target") == "today":
        return trigger_day
    return trigger_day - timedelta(days=1)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_utc(value: str | None) -> datetime | None:
    """Parse a stored timestamp as UTC-aware, or None if unusable.

    Rows written before timestamps were tz-aware parse as naive, and subtracting
    those from an aware `now` raises TypeError — which used to abort the tick.
    """
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


def _is_abandoned(row) -> bool:
    """True for a 'running' row whose process died without finishing.

    Treating every `running` row as still-live meant one killed container (a
    deploy, an OOM) blocked that trigger date forever: the guard saw 'running',
    returned True, and the day was silently never scored again.
    """
    if row["status"] != "running" or row["finished_at"]:
        return False
    started = _parse_utc(row["started_at"])
    if started is None:
        return True
    return datetime.now(timezone.utc) - started > STALE_RUN_AFTER


def _already_ran(trigger_date: str) -> bool:
    """True when today's scheduled run is done — or has failed too often to retry."""
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT status, started_at, finished_at FROM scheduled_runs"
            " WHERE trigger_date = ? AND triggered_by = 'scheduler'"
            " ORDER BY id DESC",
            (trigger_date,),
        ).fetchall()

    if not rows:
        return False

    live = [r for r in rows if not _is_abandoned(r)]

    # Succeeded, or genuinely still running — nothing more to do today.
    if any(r["status"] in ("success", "partial", "running") for r in live):
        return True

    # Only failures so far: retry a bounded number of times, spaced out, so a
    # misconfigured integration doesn't re-fire on every 30-second tick.
    attempts = [r for r in live if r["status"] == "error"]
    if len(attempts) >= MAX_ATTEMPTS:
        return True
    if not attempts:
        return False
    last = _parse_utc(attempts[0]["started_at"])
    if last is None:
        return False
    return datetime.now(timezone.utc) - last < RETRY_BACKOFF


# Set once when a non-deployed process declines to run, so the explanation
# appears in the log a single time rather than on every tick.
_warned_not_deployment = False


def _claim_alarm(trigger_date: str) -> bool:
    """Claim the right to post one failure alarm for this trigger date.

    Stored in app_settings rather than memory so a container restart — which
    Railway performs on failure and on every deploy — cannot re-alarm a failure
    the channel has already been told about. Returns True at most once per date.
    """
    key = "scheduler_alarmed_date"
    try:
        if vault.get_raw_setting(key) == trigger_date:
            return False
        vault.set_raw_setting(key, trigger_date, "scheduler")
        return True
    except Exception:
        # Never let alarm bookkeeping mask the failure being reported.
        logger.warning("Could not record alarm state for %s", trigger_date)
        return True


def recent_runs(limit: int = 20) -> list[dict]:
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM scheduled_runs ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


def _parse_schedule_time(raw: str) -> tuple[int, int] | None:
    """Parse 'HH:MM' into validated (hour, minute), or None if unusable."""
    parts = (raw or "").strip().split(":")
    if len(parts) != 2:
        return None
    try:
        hh, mm = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        return None
    return hh, mm


def next_run_description() -> dict:
    """What the Admin UI shows: whether enabled, and when it next fires."""
    settings = vault.get_settings()
    if settings["schedule_enabled"] != "1":
        return {"enabled": False, "next_run": None}

    tz = _tz()
    now = datetime.now(tz)
    parsed = _parse_schedule_time(settings["schedule_time"])
    if parsed is None:
        return {"enabled": True, "next_run": None, "error": "Invalid schedule time"}
    hh, mm = parsed

    today_at = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    pending_today = not _already_ran(now.date().isoformat())
    if today_at > now and pending_today:
        nxt, due = today_at, False
    elif pending_today:
        # The window has passed but the run has not happened — the next tick
        # will fire within TICK_SECONDS. Reporting tomorrow here made the Admin
        # UI contradict what the scheduler was about to do.
        nxt, due = now, True
    else:
        nxt, due = today_at + timedelta(days=1), False

    return {
        "enabled": True,
        "next_run": nxt.isoformat(),
        "due_now": due,
        "timezone": settings["schedule_tz"],
        "target": settings["schedule_target"],
    }


# ── the pipeline ──────────────────────────────────────────────────────────────

async def run_pipeline(target: date, triggered_by: str,
                       trigger_date: str | None = None,
                       notify_slack: bool | None = None) -> dict:
    """Fetch → score → report for one date. Serialised by an advisory lock."""
    import app  # imported lazily: app owns the fetch-and-store logic

    date_str = target.isoformat()
    trigger_date = trigger_date or date_str
    if notify_slack is None:
        notify_slack = vault.get_setting("slack_enabled") == "1"

    # The lock is taken BEFORE the run row exists. Inserting first meant a
    # collision with a manual run recorded a spurious 'error' row, and three of
    # those consumed MAX_ATTEMPTS and disabled the day's scheduled run. LockBusy
    # now propagates without leaving any trace, which is what it should be.
    with db.advisory_lock(LOCK_NAME, triggered_by, ttl_seconds=LOCK_TTL_SECONDS):
        with db.get_conn() as conn:
            cur = conn.execute(
                "INSERT INTO scheduled_runs"
                " (run_date, trigger_date, triggered_by, started_at, status)"
                " VALUES (?, ?, ?, ?, 'running')",
                (date_str, trigger_date, triggered_by, _now_iso()),
            )
            run_id = cur.lastrowid

        def _finish(status, *, fetched=None, scored=None, skipped=None,
                    error=None, slack_ok=None):
            with db.get_conn() as conn:
                conn.execute(
                    "UPDATE scheduled_runs SET finished_at = ?, status = ?,"
                    " fetched = ?, scored = ?, skipped = ?, error = ?,"
                    " slack_ok = ? WHERE id = ?",
                    (_now_iso(), status, fetched, scored, skipped,
                     error, slack_ok, run_id),
                )

        try:
            fetch = await app.fetch_and_store(target)
            fetched = fetch.count
            qc = await asyncio.to_thread(
                qc_runner.run_qc_date, date_str, triggered_by
            )

            slack_ok = None
            if notify_slack:
                try:
                    await slack.post_day_report(date_str)
                    slack_ok = 1
                except Exception as e:
                    logger.error("Slack post failed for %s: %s", date_str, e)
                    slack_ok = 0

            status = "partial" if qc.get("errors") else "success"
            _finish(status,
                    fetched=fetched,
                    scored=qc.get("scored", 0),
                    skipped=qc.get("skipped", 0),
                    error="; ".join(qc.get("errors", []))[:1000] or None,
                    slack_ok=slack_ok)
            return {"run_id": run_id, "status": status, "date": date_str,
                    "fetched": fetched, **qc, "slack_ok": slack_ok}

        except Exception as e:
            logger.exception("Scheduled run failed for %s", date_str)
            _finish("error", error=str(e)[:1000])
            # Alarm once per trigger date, not once per attempt: three retries
            # plus a container restart posted the same failure four times.
            if notify_slack and _claim_alarm(trigger_date):
                try:
                    await slack.post_failure(date_str, str(e))
                except Exception:
                    logger.warning(
                        "Could not post failure notice for %s", date_str
                    )
            raise


# ── loop ──────────────────────────────────────────────────────────────────────

async def _tick() -> None:
    # Second line of defence against a hung run: startup clears runs orphaned by
    # a restart, this clears one that stalls inside the current process. Cheap
    # (one UPDATE touching only 'running' rows) and it runs regardless of
    # whether the schedule is enabled, since a manual run can hang too.
    try:
        reaped = await asyncio.to_thread(db.reap_interrupted_runs)
        if reaped["scheduled"] or reaped["qc"]:
            logger.warning(
                "Timed out %d scheduled and %d scoring run(s) with no progress "
                "for over %d minutes",
                reaped["scheduled"], reaped["qc"], db.STALE_RUN_MINUTES,
            )
    except Exception:
        logger.exception("Could not reap interrupted runs")

    settings = vault.get_settings()
    if settings["schedule_enabled"] != "1":
        return

    # `schedule_enabled` already defaults to "0", so a fresh database schedules
    # nothing. The leak is its legacy_env fallback: a copied `.env` carrying
    # SCHEDULE_ENABLED=1 turns a laptop into a second scheduler, fetching and
    # posting daily against the shared Pylon and Slack tokens. Logged once per
    # process rather than per tick, so the reason is visible without filling
    # the log minute by minute.
    if not vault.may_act_outward():
        global _warned_not_deployment
        if not _warned_not_deployment:
            _warned_not_deployment = True
            logger.warning(
                "Scheduler is enabled but this is not the deployed instance "
                "(no RAILWAY_PUBLIC_DOMAIN) — not running. A local copy would "
                "fetch and post alongside production. Set "
                "allow_local_side_effects in Admin to override."
            )
        return

    parsed = _parse_schedule_time(settings["schedule_time"])
    if parsed is None:
        logger.warning("Invalid schedule_time %r", settings["schedule_time"])
        return
    hh, mm = parsed

    now = datetime.now(_tz())
    trigger_date = now.date().isoformat()

    if (now.hour, now.minute) < (hh, mm):
        return
    if _already_ran(trigger_date):
        return

    target = _target_date(now.date())
    logger.info("Scheduler firing for target date %s", target)
    try:
        await run_pipeline(target, "scheduler", trigger_date=trigger_date)
    except Exception as e:
        logger.error("Scheduled run error: %s", e)


async def _loop() -> None:
    logger.info("QC scheduler started")
    while True:
        try:
            await _tick()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Scheduler tick failed")
        await asyncio.sleep(TICK_SECONDS)


def start() -> None:
    global _task
    if _task is None or _task.done():
        _task = asyncio.create_task(_loop())


async def stop() -> None:
    global _task
    if _task and not _task.done():
        _task.cancel()
        try:
            await _task
        except asyncio.CancelledError:
            pass
    _task = None
