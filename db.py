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


# Columns added after their table shipped. Every CREATE below also lists them,
# so a fresh database gets them directly; these ALTERs bring an existing volume
# up to date. Applied AFTER the CREATEs — running them first meant that on a
# fresh database the table did not exist yet and the ALTER was silently
# swallowed, leaving the column missing until the next boot.
_ADDED_COLUMNS = [
    "ALTER TABLE tickets ADD COLUMN external_issues TEXT",
    "ALTER TABLE rule_checks ADD COLUMN r8 TEXT",
    "ALTER TABLE rule_checks ADD COLUMN r9 TEXT",
    "ALTER TABLE tickets ADD COLUMN customer_portal_visible INTEGER",
    # Reasoning tokens bill at the output rate but are reported separately;
    # cached input bills at a discount. Both were missing from the estimate.
    "ALTER TABLE qc_runs ADD COLUMN cached_tokens INTEGER",
    "ALTER TABLE qc_runs ADD COLUMN thought_tokens INTEGER",
    "ALTER TABLE qc_runs ADD COLUMN cost_estimated INTEGER",
    # Hash of the content the AI actually grades, so a refetch that changes
    # nothing no longer rescores — and re-bills — the whole day.
    "ALTER TABLE ai_checks ADD COLUMN qc_fingerprint TEXT",
]


def init_db():
    with get_conn() as conn:
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
            checked_at     TEXT,
            -- Hash of the content that produced this grade. Rescoring compares
            -- it rather than trusting fetched_at, so a refetch that changes
            -- nothing costs nothing. NULL means "graded before fingerprints
            -- existed" and forces exactly one regrade.
            qc_fingerprint TEXT
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

        -- One row per AI scoring run. The config that produced the scores is
        -- snapshotted here so results are explainable after settings change.
        CREATE TABLE IF NOT EXISTS qc_runs (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            date          TEXT,      -- the QC target date
            triggered_by  TEXT,      -- user email or 'scheduler'
            started_at    TEXT,
            finished_at   TEXT,
            status        TEXT,      -- running | success | partial | error
            total         INTEGER,   -- tickets eligible this run
            scored        INTEGER,
            skipped       INTEGER,
            model_used    TEXT,      -- models that actually graded, with call counts
            config_json   TEXT,      -- full effective config snapshot
            prompt_tokens INTEGER,
            output_tokens INTEGER,
            cost_usd      REAL,      -- estimate from the price table
            compared_to   INTEGER,   -- previous run id for the same date, if any
            stability     REAL,      -- % of tickets whose overall matched that run
            changed       INTEGER,   -- tickets whose overall changed vs that run
            error         TEXT
        );

        -- Full end-of-run grade snapshot per ticket, so any two runs of the
        -- same day can be diffed exactly instead of guessing from memory.
        CREATE TABLE IF NOT EXISTS qc_run_results (
            run_id    INTEGER,
            ticket_id TEXT,
            number    INTEGER,
            a1 TEXT, a2 TEXT, a3 TEXT, a4 TEXT, a5 TEXT,
            r_fails   TEXT,    -- comma list of failing R-checks at snapshot time
            overall_result TEXT,
            PRIMARY KEY (run_id, ticket_id)
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
        CREATE INDEX IF NOT EXISTS idx_qcruns_date       ON qc_runs(date);

        -- Human sign-off of a ticket's QC. Append-only; latest row wins.
        -- AI grades in ai_checks are never overwritten.
        CREATE TABLE IF NOT EXISTS ticket_reviews (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            ticket_id       TEXT NOT NULL,
            decision        TEXT NOT NULL,  -- Pass | Fail
            kept_ai         INTEGER NOT NULL DEFAULT 0,
            reviewer_email  TEXT NOT NULL,
            reviewer_name   TEXT,
            note            TEXT,
            reviewed_at     TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_reviews_ticket ON ticket_reviews(ticket_id, id);

        -- Region / group coverage: one reviewer owns a named set of assignees.
        -- App admins bypass this and can review every ticket.
        CREATE TABLE IF NOT EXISTS review_coverages (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            name            TEXT NOT NULL,
            reviewer_email  TEXT NOT NULL,
            reviewer_name   TEXT,
            updated_by      TEXT,
            updated_at      TEXT
        );
        CREATE TABLE IF NOT EXISTS review_coverage_assignees (
            coverage_id   INTEGER NOT NULL,
            assignee_name TEXT NOT NULL,
            PRIMARY KEY (coverage_id, assignee_name)
        );
        """)

        # Bring an existing volume up to date. "duplicate column name" is the
        # expected outcome on an already-migrated database, so only that is
        # swallowed — anything else is a real schema problem and should surface.
        for stmt in _ADDED_COLUMNS:
            try:
                conn.execute(stmt)
            except sqlite3.OperationalError as e:
                if "duplicate column name" not in str(e).lower():
                    raise


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


# One definition of a calendar cell, used for both a month and a single day, so
# the two can never disagree. Driven from `tickets` rather than `fetch_log`: a
# day whose fetch partly failed has tickets but no log row, and keying off the
# log made it invisible on the calendar even though the data was there.
_CALENDAR_CELL_SQL = """
    SELECT
        t.fetch_date,
        -- Count tickets actually stored. fetch_log.ticket_count records only the
        -- non-archived count at fetch time, so it drifts below what the day
        -- panel lists once tickets are archived.
        COUNT(DISTINCT t.id) AS ticket_count,
        SUM(CASE WHEN rc.r1='Fail' OR rc.r2='Fail' OR rc.r3='Fail'
                      OR rc.r4='Fail' OR rc.r5='Fail' OR rc.r7='Fail'
                      OR rc.r8='Fail' OR rc.r9='Fail' THEN 1 ELSE 0 END) AS rule_fails,
        SUM(CASE WHEN ac.overall_result = 'Fail'         THEN 1 ELSE 0 END) AS ai_fails,
        SUM(CASE WHEN ac.overall_result = 'Needs Review' THEN 1 ELSE 0 END) AS needs_review,
        COUNT(ac.ticket_id) AS ai_done_count,
        MAX(CASE WHEN fl.fetch_date IS NOT NULL THEN 1 ELSE 0 END) AS logged
    FROM tickets t
    LEFT JOIN rule_checks rc ON t.id = rc.ticket_id
    LEFT JOIN ai_checks   ac ON t.id = ac.ticket_id
    LEFT JOIN fetch_log   fl ON fl.fetch_date = t.fetch_date
    WHERE {where}
    GROUP BY t.fetch_date
"""


def get_calendar_month(year: int, month: int):
    prefix = f"{year:04d}-{month:02d}-"
    sql = _CALENDAR_CELL_SQL.format(where="t.fetch_date LIKE ?")
    with get_conn() as conn:
        rows = conn.execute(sql, (prefix + "%",)).fetchall()
    return [dict(r) for r in rows]


def get_calendar_day(date_str: str) -> dict | None:
    """One day's calendar cell, for patching a single square after a fetch.

    Refetching the whole month to update one square meant overlapping requests
    could land out of order and leave a stale count on screen.
    """
    sql = _CALENDAR_CELL_SQL.format(where="t.fetch_date = ?")
    with get_conn() as conn:
        row = conn.execute(sql, (date_str,)).fetchone()
    return dict(row) if row else None


def account_names(ids: list[str]) -> dict[str, str]:
    """Account id → name for ids we have already fetched from Pylon."""
    ids = [i for i in ids if i]
    if not ids:
        return {}
    placeholders = ",".join("?" * len(ids))
    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT id, name FROM accounts WHERE id IN ({placeholders})", ids
        ).fetchall()
    return {r["id"]: r["name"] for r in rows if r["name"]}


def search_accounts(q: str, limit: int = 25) -> list[dict]:
    """Name search over accounts seen on fetched tickets."""
    q = (q or "").strip()
    with get_conn() as conn:
        if q:
            rows = conn.execute(
                "SELECT id, name, domain FROM accounts"
                " WHERE name LIKE ? COLLATE NOCASE"
                " ORDER BY name LIMIT ?",
                (f"%{q}%", limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, name, domain FROM accounts"
                " WHERE name IS NOT NULL AND name != ''"
                " ORDER BY name LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]


def latest_qc_run(date_str: str) -> dict | None:
    """Most recent scoring run for a day — used to show cost on the dashboard."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id, status, prompt_tokens, output_tokens, cost_usd,"
            " cached_tokens, thought_tokens, cost_estimated, model_used, finished_at"
            " FROM qc_runs WHERE date = ? ORDER BY id DESC LIMIT 1",
            (date_str,),
        ).fetchone()
    return dict(row) if row else None


def qc_spend_for_date(date_str: str) -> dict:
    """Cumulative scoring spend for a date, across every run.

    Showing only the newest run made the cost look like it reset each time a
    day was rescored. Spend accumulates, so report the total and let the caller
    show the latest run's share separately.
    """
    with get_conn() as conn:
        row = conn.execute("""
            SELECT COUNT(*)                       AS runs,
                   COALESCE(SUM(cost_usd), 0)     AS total_cost_usd,
                   COALESCE(SUM(prompt_tokens),0) AS total_prompt_tokens,
                   COALESCE(SUM(output_tokens),0) AS total_output_tokens,
                   COALESCE(SUM(thought_tokens),0) AS total_thought_tokens,
                   MAX(COALESCE(cost_estimated, 0)) AS any_estimated
            FROM qc_runs
            WHERE date = ? AND status IN ('success', 'partial')
        """, (date_str,)).fetchone()

    out = dict(row)
    out["total_cost_usd"] = round(out["total_cost_usd"] or 0, 6)
    out["any_estimated"] = bool(out["any_estimated"])
    return out


def ticket_stats(date: str | None = None) -> dict:
    """Pass/Fail/NR counts using the latest human review when present, else the AI grade."""
    where = "WHERE t.fetch_date = ?" if date else ""
    params = (date,) if date else ()
    with get_conn() as conn:
        row = conn.execute(f"""
            SELECT
                COUNT(*) AS total_tickets,
                SUM(CASE WHEN COALESCE(
                         CASE WHEN rev.decision IN ('Pass','Fail') THEN rev.decision END,
                         ac.overall_result) = 'Pass' THEN 1 ELSE 0 END) AS pass_count,
                SUM(CASE WHEN COALESCE(
                         CASE WHEN rev.decision IN ('Pass','Fail') THEN rev.decision END,
                         ac.overall_result) = 'Fail' THEN 1 ELSE 0 END) AS fail_count,
                SUM(CASE WHEN COALESCE(
                         CASE WHEN rev.decision IN ('Pass','Fail') THEN rev.decision END,
                         ac.overall_result) = 'Needs Review' THEN 1 ELSE 0 END) AS review_count,
                COUNT(ac.ticket_id) AS ai_done,
                SUM(CASE WHEN ac.ticket_id IS NOT NULL
                          AND (rev.id IS NULL OR rev.decision NOT IN ('Pass','Fail'))
                         THEN 1 ELSE 0 END) AS unreviewed
            FROM tickets t
            LEFT JOIN ai_checks ac ON ac.ticket_id = t.id
            LEFT JOIN (
                SELECT r.* FROM ticket_reviews r
                JOIN (
                    SELECT ticket_id, MAX(id) AS max_id
                    FROM ticket_reviews GROUP BY ticket_id
                ) x ON x.max_id = r.id
            ) rev ON rev.ticket_id = t.id
            {where}
        """, params).fetchone()
    out = dict(row)
    for k, v in out.items():
        if v is None:
            out[k] = 0
    return out


def get_ticket_messages(ticket_id: str):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM messages WHERE ticket_id = ? ORDER BY timestamp",
            (ticket_id,)
        ).fetchall()
        return [dict(r) for r in rows]
