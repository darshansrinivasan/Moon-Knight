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


def _already_ran(trigger_date: str) -> bool:
    """True when today's scheduled run is done — or has failed too often to retry."""
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT status, started_at FROM scheduled_runs"
            " WHERE trigger_date = ? AND triggered_by = 'scheduler'"
            " ORDER BY id DESC",
            (trigger_date,),
        ).fetchall()

    if not rows:
        return False
    # Succeeded (or currently running) — nothing more to do today.
    if any(r["status"] in ("success", "partial", "running") for r in rows):
        return True
    # Only failures so far: retry a bounded number of times, spaced out, so a
    # misconfigured integration doesn't re-fire on every 30-second tick.
    if len(rows) >= MAX_ATTEMPTS:
        return True
    try:
        last = datetime.fromisoformat(rows[0]["started_at"])
    except (TypeError, ValueError):
        return False
    return datetime.now(timezone.utc) - last < RETRY_BACKOFF


def recent_runs(limit: int = 20) -> list[dict]:
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM scheduled_runs ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


def next_run_description() -> dict:
    """What the Admin UI shows: whether enabled, and when it next fires."""
    settings = vault.get_settings()
    if settings["schedule_enabled"] != "1":
        return {"enabled": False, "next_run": None}

    tz = _tz()
    now = datetime.now(tz)
    try:
        hh, mm = (int(x) for x in settings["schedule_time"].split(":"))
    except ValueError:
        return {"enabled": True, "next_run": None, "error": "Invalid schedule time"}

    today_at = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    nxt = today_at if today_at > now and not _already_ran(now.date().isoformat()) \
        else today_at + timedelta(days=1)
    return {
        "enabled": True,
        "next_run": nxt.isoformat(),
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
                "UPDATE scheduled_runs SET finished_at = ?, status = ?, fetched = ?,"
                " scored = ?, skipped = ?, error = ?, slack_ok = ? WHERE id = ?",
                (_now_iso(), status, fetched, scored, skipped,
                 error, slack_ok, run_id),
            )

    try:
        with db.advisory_lock(LOCK_NAME, triggered_by, ttl_seconds=7200):
            fetched = await app.fetch_and_store(target)
            qc = await asyncio.to_thread(qc_runner.run_qc_date, date_str)

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

    except db.LockBusy as e:
        _finish("error", error=str(e))
        raise
    except Exception as e:
        logger.exception("Scheduled run failed for %s", date_str)
        _finish("error", error=str(e)[:1000])
        if notify_slack:
            try:
                await slack.post_failure(date_str, str(e))
            except Exception:
                pass
        raise


# ── loop ──────────────────────────────────────────────────────────────────────

async def _tick() -> None:
    settings = vault.get_settings()
    if settings["schedule_enabled"] != "1":
        return

    try:
        hh, mm = (int(x) for x in settings["schedule_time"].split(":"))
    except ValueError:
        logger.warning("Invalid schedule_time %r", settings["schedule_time"])
        return

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
