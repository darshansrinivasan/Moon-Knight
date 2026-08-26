import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

# On a container platform the app directory is wiped on every deploy, so the
# database must live on a mounted volume — point QC_DB_PATH at it
# (e.g. /data/qc.db with a Railway volume mounted at /data).
DB_PATH = Path(os.getenv("QC_DB_PATH") or (Path(__file__).parent / "qc.db"))
DB_PATH.parent.mkdir(parents=True, exist_ok=True)


@contextmanager
def get_conn():
    """Yield a connection that commits on success, rolls back on error,
    and is always closed (so WAL checkpoints can run)."""
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    with get_conn() as conn:
        # migrate existing DBs that predate optional columns
        for stmt in [
            "ALTER TABLE tickets ADD COLUMN external_issues TEXT",
            "ALTER TABLE rule_checks ADD COLUMN r8 TEXT",
            "ALTER TABLE rule_checks ADD COLUMN r9 TEXT",
            "ALTER TABLE tickets ADD COLUMN customer_portal_visible INTEGER",
        ]:
            try:
                conn.execute(stmt)
            except Exception:
                pass
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS tickets (
            id            TEXT PRIMARY KEY,
            number        INTEGER,
            fetch_date    TEXT,
            title         TEXT,
            link          TEXT,
            state         TEXT,
            source        TEXT,
            type          TEXT,
            priority      TEXT,
            assignee_id   TEXT,
            assignee_name TEXT,
            account_id    TEXT,
            custom_fields   TEXT,
            external_issues TEXT,
            body_html       TEXT,
            created_at      TEXT,
            updated_at    TEXT,
            latest_message_time TEXT,
            customer_portal_visible INTEGER,
            fetched_at    TEXT
        );

        CREATE TABLE IF NOT EXISTS messages (
            id           TEXT PRIMARY KEY,
            ticket_id    TEXT,
            message_html TEXT,
            timestamp    TEXT,
            source       TEXT,
            author_name  TEXT,
            author_email TEXT,
            is_customer  INTEGER,
            is_private   INTEGER
        );

        CREATE TABLE IF NOT EXISTS accounts (
            id            TEXT PRIMARY KEY,
            name          TEXT,
            domain        TEXT,
            type          TEXT,
            custom_fields TEXT,
            fetched_at    TEXT
        );

        CREATE TABLE IF NOT EXISTS users (
            id    TEXT PRIMARY KEY,
            name  TEXT,
            email TEXT
        );

        CREATE TABLE IF NOT EXISTS rule_checks (
            ticket_id  TEXT PRIMARY KEY,
            fetch_date TEXT,
            r1 TEXT, r2 TEXT, r3 TEXT, r4 TEXT,
            r5 TEXT, r6 TEXT, r7 TEXT, r8 TEXT, r9 TEXT,
            checked_at TEXT
        );

        CREATE TABLE IF NOT EXISTS ai_checks (
            ticket_id      TEXT PRIMARY KEY,
            fetch_date     TEXT,
            a1 TEXT, a2 TEXT, a3 TEXT, a4 TEXT, a5 TEXT,
            ai_notes       TEXT,
            overall_result TEXT,
            checked_at     TEXT
        );

        CREATE TABLE IF NOT EXISTS fetch_log (
            fetch_date   TEXT PRIMARY KEY,
            ticket_count INTEGER,
            fetched_at   TEXT
        );

        -- migrate: add external_issues column if upgrading from older schema
        -- (safe to run repeatedly — ALTER TABLE ignores existing columns via try/except in init_db)

        -- ── platform tables (multi-user, credentials, scheduling) ────────────

        CREATE TABLE IF NOT EXISTS app_users (
            email         TEXT PRIMARY KEY,
            name          TEXT,
            picture       TEXT,
            role          TEXT NOT NULL DEFAULT 'member',   -- 'admin' | 'member'
            is_active     INTEGER NOT NULL DEFAULT 1,
            created_at    TEXT,
            last_login_at TEXT
        );

        -- Secrets, encrypted at rest with Fernet. Plaintext never leaves the server.
        CREATE TABLE IF NOT EXISTS credentials (
            key        TEXT PRIMARY KEY,
            value_enc  BLOB NOT NULL,
            updated_by TEXT,
            updated_at TEXT
        );

        -- Non-secret configuration (Vertex project, schedule, Slack channel…).
        CREATE TABLE IF NOT EXISTS app_settings (
            key        TEXT PRIMARY KEY,
            value      TEXT,
            updated_by TEXT,
            updated_at TEXT
        );

        CREATE TABLE IF NOT EXISTS audit_log (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            ts         TEXT,
            user_email TEXT,
            action     TEXT,
            detail     TEXT
        );

        CREATE TABLE IF NOT EXISTS scheduled_runs (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            run_date     TEXT,     -- the QC target date
            trigger_date TEXT,     -- local date the schedule fired on
            triggered_by TEXT,     -- 'scheduler' or a user email
            started_at   TEXT,
            finished_at  TEXT,
            status       TEXT,     -- running | success | partial | error
            fetched      INTEGER,
            scored       INTEGER,
            skipped      INTEGER,
            error        TEXT,
            slack_ok     INTEGER
        );

        -- Advisory locks so a scheduled run and a human can never collide.
        CREATE TABLE IF NOT EXISTS run_locks (
            name        TEXT PRIMARY KEY,
            holder      TEXT,
            acquired_at TEXT,
            expires_at  TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_tickets_fetch_date ON tickets(fetch_date);
        CREATE INDEX IF NOT EXISTS idx_messages_ticket    ON messages(ticket_id);
        CREATE INDEX IF NOT EXISTS idx_rule_fetch_date   ON rule_checks(fetch_date);
        CREATE INDEX IF NOT EXISTS idx_ai_fetch_date     ON ai_checks(fetch_date);
        CREATE INDEX IF NOT EXISTS idx_audit_ts          ON audit_log(ts);
        CREATE INDEX IF NOT EXISTS idx_sched_trigger     ON scheduled_runs(trigger_date);
        """)


# ── advisory locks ────────────────────────────────────────────────────────────
# Keeps a scheduled run and a human clicking "Run QC" from working on the same
# date at once. DB-backed so it stays correct across processes/workers.

class LockBusy(RuntimeError):
    def __init__(self, holder: str):
        self.holder = holder
        super().__init__(f"Already running (started by {holder})")


@contextmanager
def advisory_lock(name: str, holder: str, ttl_seconds: int = 3600):
    """Acquire a named lock or raise LockBusy. Stale locks expire after ttl."""
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    expires = (now + timedelta(seconds=ttl_seconds)).isoformat()

    with get_conn() as conn:
        conn.execute(
            "DELETE FROM run_locks WHERE name = ? AND expires_at < ?",
            (name, now.isoformat()),
        )
        row = conn.execute(
            "SELECT holder FROM run_locks WHERE name = ?", (name,)
        ).fetchone()
        if row:
            raise LockBusy(row["holder"])
        conn.execute(
            "INSERT INTO run_locks (name, holder, acquired_at, expires_at)"
            " VALUES (?, ?, ?, ?)",
            (name, holder, now.isoformat(), expires),
        )

    try:
        yield
    finally:
        with get_conn() as conn:
            conn.execute("DELETE FROM run_locks WHERE name = ?", (name,))


def get_fetch_log(date_str: str):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM fetch_log WHERE fetch_date = ?", (date_str,)
        ).fetchone()
        return dict(row) if row else None


def get_day_tickets(date_str: str):
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT
                t.*,
                a.name  AS account_name,
                a.type  AS account_type,
                a.domain AS account_domain,
                rc.r1, rc.r2, rc.r3, rc.r4, rc.r5, rc.r6, rc.r7, rc.r8, rc.r9,
                ac.a1, ac.a2, ac.a3, ac.a4, ac.a5,
                ac.ai_notes, ac.overall_result, ac.checked_at AS ai_checked_at
            FROM tickets t
            LEFT JOIN accounts    a  ON t.account_id = a.id
            LEFT JOIN rule_checks rc ON t.id = rc.ticket_id
            LEFT JOIN ai_checks   ac ON t.id = ac.ticket_id
            WHERE t.fetch_date = ?
            ORDER BY t.number
        """, (date_str,)).fetchall()
        return [dict(r) for r in rows]


def get_calendar_month(year: int, month: int):
    prefix = f"{year:04d}-{month:02d}-"
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT
                fl.fetch_date,
                -- Count the tickets actually stored for the day. fl.ticket_count
                -- records only the non-archived count at fetch time, so it drifts
                -- below what the day panel lists once tickets are archived.
                COUNT(DISTINCT t.id) AS ticket_count,
                SUM(CASE WHEN rc.r1='Fail' OR rc.r2='Fail' OR rc.r3='Fail'
                              OR rc.r4='Fail' OR rc.r5='Fail' OR rc.r7='Fail'
                              OR rc.r8='Fail' OR rc.r9='Fail' THEN 1 ELSE 0 END) AS rule_fails,
                SUM(CASE WHEN ac.overall_result = 'Fail'        THEN 1 ELSE 0 END) AS ai_fails,
                SUM(CASE WHEN ac.overall_result = 'Needs Review' THEN 1 ELSE 0 END) AS needs_review,
                COUNT(ac.ticket_id) AS ai_done_count
            FROM fetch_log fl
            LEFT JOIN tickets      t  ON fl.fetch_date = t.fetch_date
            LEFT JOIN rule_checks  rc ON t.id = rc.ticket_id
            LEFT JOIN ai_checks    ac ON t.id = ac.ticket_id
            WHERE fl.fetch_date LIKE ?
            GROUP BY fl.fetch_date
        """, (prefix + "%",)).fetchall()
        return [dict(r) for r in rows]


def get_ticket_messages(ticket_id: str):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM messages WHERE ticket_id = ? ORDER BY timestamp",
            (ticket_id,)
        ).fetchall()
        return [dict(r) for r in rows]
