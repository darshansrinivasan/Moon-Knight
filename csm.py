"""
CSM page: what is open right now on the accounts a CSM owns.

Not QC. This page carries no grade, no check and no sign-off — its reader is a
customer-facing owner about to walk into a call, and "your ticket scored Fail"
is not theirs to relay. It answers one question: what is still open on my
accounts, who last touched it, and where do I go to read it.

Three things this module exists to get right:

  ownership   A CSM owns ACCOUNTS, not tickets. The link is the account's
              "Company owner" field, which stores a Pylon user UUID; the name
              beside it comes from the synced `users` table. Nothing here keys
              on assignee — the assignee is support, the owner is the CSM, and
              conflating them puts a CSM's name on tickets for accounts they
              have never heard of.

  raw values  The page covers one lifecycle bucket, keyed on the STORED value
              and shown as its label. Pylon's raw value for the bucket
              labelled "Current Customer" is the string
              'Customer/ Churned Customer'; filtering on the words a person
              sees returns zero rows, and a separate 'Churned Customer' bucket
              sits one sloppy match away from being swept in. Translation goes
              through `funcheck.canon`, the app's one translator.

  roster      The accounts come from a filtered Pylon sweep, not from the
              accounts the ticket fetches happened to touch. A CSM whose
              accounts are quiet was otherwise absent from the store entirely,
              so their page showed nothing at all instead of the "0 open
              tickets" that is the real answer. The sweep is filtered because
              Pylon auto-creates an account per email domain — the unfiltered
              list is five figures of one-contact shells.
"""

import asyncio
import json
import logging
from datetime import datetime, timezone

import db
import funcheck

logger = logging.getLogger(__name__)

OWNER_SETTING = "csm_owner_field"
BUCKET_SETTING = "csm_bucket_field"
BUCKET_VALUE_SETTING = "csm_bucket_value"
SYNCED_AT_SETTING = "accounts_synced_at"

# Private notes are INCLUDED, and marked. Excluding them looked like the
# careful choice and was the wrong one: a ticket a CSM raised in an internal
# Slack channel has is_private=1 on every message in the thread — 7 of Seetha
# Preetha's 22 open tickets — so hiding them blanked "Created by" and "Last
# replied by" on exactly the tickets that CSM had opened themselves. The rest
# of the app (qc_runner, funcheck, report) reads private notes and labels the
# role "(private)"; this follows that.


def _setting(key: str) -> str:
    import vault
    return (vault.get_setting(key) or "").strip()


def _path(slug: str) -> str:
    """The json_extract path for one account custom field's stored value.

    Passed as a bound parameter, never interpolated into SQL: the slug is an
    admin-editable setting, and a setting that reaches a query as text is a
    setting that can rewrite the query.
    """
    return f'$."{slug}".value'


def _owner_path() -> str:
    return _path(_setting(OWNER_SETTING))


def _bucket_path() -> str:
    return _path(_setting(BUCKET_SETTING))


def bucket_value() -> str:
    return _setting(BUCKET_VALUE_SETTING)


def bucket_label() -> str:
    """What the covered bucket is CALLED, for the page to say out loud."""
    return funcheck.canon("tam_bucket", bucket_value()) or bucket_value()


_CF = "json_extract(a.custom_fields, ?)"


def _open_where() -> tuple[str, list]:
    """The open-ticket predicate, borrowed rather than restated.

    openqc owns what "open" means (terminal states, soft deletes, the Admin
    exclusions). A second definition here is how the CSM page and the Open
    Tickets tab end up disagreeing about the same account on the same morning.
    """
    import openqc
    return openqc._where(None, None, None)


# ── sync ──────────────────────────────────────────────────────────────────────

async def sync_accounts() -> dict:
    """Refresh the account roster, the user directory and the bucket labels.

    Costs no Vertex tokens — three Pylon listings — so it rides the scheduler
    next to channel tagging rather than being something an operator has to
    remember. Upsert-only: an account that stops coming back is left alone,
    because absence in one sweep is not deletion.
    """
    import pylon

    bucket_slug, value = _setting(BUCKET_SETTING), bucket_value()
    if not (bucket_slug and value):
        raise RuntimeError(
            "The CSM bucket field and value must both be set (Admin → Settings)")

    accounts, users, fields = await asyncio.gather(
        pylon.search_accounts({"field": bucket_slug, "operator": "equals",
                               "value": value}),
        pylon.fetch_users(),
        pylon.fetch_custom_fields("account"),
    )

    labels: dict = {}
    for f in fields:
        if f.get("slug") == bucket_slug:
            labels = {
                o["slug"]: ((o.get("label") or "").strip() or o["slug"])
                for o in (f.get("select_metadata") or {}).get("options") or []
                if o.get("slug")
            }
            break
    if value not in labels:
        # Loud, because this is the failure the whole module is shaped around:
        # a bucket value that no longer exists returns an empty roster, and an
        # empty roster is indistinguishable from "this CSM has no accounts".
        logger.warning("Pylon no longer offers %r on %s — the CSM roster will "
                       "be empty until the setting is repointed",
                       value, bucket_slug)

    now = datetime.now(timezone.utc).isoformat()
    with db.get_conn() as conn:
        for acc in accounts:
            conn.execute(
                "INSERT OR REPLACE INTO accounts"
                " (id, name, domain, type, custom_fields, fetched_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (acc["id"], acc.get("name"), acc.get("domain"),
                 acc.get("type"),
                 json.dumps(acc.get("custom_fields") or {}), now))
        for u in users:
            if not u.get("id"):
                continue
            # REPLACE, not IGNORE: the message-author cache seeds this table
            # too, and a rename must land. The Pylon directory is the
            # authority on a team member's name.
            conn.execute(
                "INSERT OR REPLACE INTO users (id, name, email)"
                " VALUES (?, ?, ?)",
                (u["id"], u.get("name") or "", u.get("email") or ""))

    def persist():
        import vault
        # One transaction: a stamp claiming a sync happened while the labels it
        # was supposed to write are missing is worse than no stamp at all.
        vault.set_raw_settings({
            funcheck.ACCOUNT_LABELS_SETTING: json.dumps({"tam_bucket": labels}),
            SYNCED_AT_SETTING: now,
        }, "pylon-sync")

    await asyncio.to_thread(persist)
    funcheck.invalidate_labels()
    return {"accounts": len(accounts), "users": len(users),
            "bucket": value, "bucket_label": bucket_label(), "at": now}


AUTO_SETTING = "csm_auto_refresh"
AUTO_MINUTES_SETTING = "csm_auto_refresh_minutes"
MIN_AUTO_MINUTES = 5


def auto_refresh_minutes() -> int | None:
    """The auto-refresh interval, or None when it is off.

    Floored at MIN_AUTO_MINUTES. The full refetch takes ~105s against a
    few-hundred-ticket backlog, so an interval near that duration would have a
    run starting as the last one finishes — a permanent fetch, not a schedule.
    """
    import vault
    if vault.get_setting(AUTO_SETTING) != "1":
        return None
    raw = (vault.get_setting(AUTO_MINUTES_SETTING) or "").strip()
    try:
        minutes = int(float(raw))
    except (TypeError, ValueError):
        logger.warning("Unreadable %s=%r — falling back to 10 minutes",
                       AUTO_MINUTES_SETTING, raw)
        return 10
    return max(MIN_AUTO_MINUTES, minutes)


async def auto_refresh_once() -> dict:
    """One scheduled refresh: the same work the page's button does.

    Two things this must get right, because it runs unattended:

      * it takes `fetch:open`, the SAME lock the 09:30 pipeline and the page's
        button hold. A collision SKIPS this tick rather than waiting — a queued
        refresh that lands twenty minutes later serves nobody, and blocking
        here would hold the loop past its next tick.
      * a local copy must not fetch alongside production. Same guard as every
        other scheduled job.
    """
    import openqc
    import vault

    if not vault.may_act_outward():
        return {"skipped": "not the deployed instance"}
    try:
        with db.advisory_lock("accounts:sync", "csm-auto", ttl_seconds=900), \
             db.advisory_lock("fetch:open", "csm-auto", ttl_seconds=1800):
            accounts = await sync_accounts()
            tickets = await openqc.refetch_open()
    except db.LockBusy as e:
        logger.info("CSM auto-refresh skipped, a fetch is already running: %s", e)
        return {"skipped": str(e)}
    return {"accounts": accounts, "tickets": tickets}


def synced_at() -> str | None:
    import vault
    return vault.get_setting(SYNCED_AT_SETTING) or None


def freshness() -> dict:
    """A cheap "has the store moved?" probe for the open page to poll.

    Deliberately not the ticket list: a page checking whether to offer a reload
    must not cost what the reload costs. The newest `fetched_at` across open
    tickets changes on every refetch and on nothing else, so one indexed MAX
    answers it.
    """
    with db.get_conn() as conn:
        # Unfiltered on purpose. Every refetch stamps fetched_at on the tickets
        # it stored, so the table maximum moves exactly when the store does —
        # and on an indexed column that is one lookup, where the same MAX
        # behind the open-set predicate was a 14k-row scan at 0.38s a poll.
        at = conn.execute("SELECT MAX(fetched_at) FROM tickets").fetchone()[0]
    return {"tickets_at": at, "accounts_at": synced_at(),
            "auto_minutes": auto_refresh_minutes()}


# ── the picker ────────────────────────────────────────────────────────────────

def owners() -> list[dict]:
    """Every CSM owning an account in the covered bucket, and how many.

    Named from the synced user directory; a UUID with no directory entry is
    still listed, under its id, rather than dropped — an owner the page cannot
    name is a sync to fix, not a CSM to hide.
    """
    op, bp, val = _owner_path(), _bucket_path(), bucket_value()
    with db.get_conn() as conn:
        rows = conn.execute(
            f"""SELECT {_CF} AS owner_id, COUNT(*) AS accounts
                FROM accounts a
                WHERE {_CF} = ?
                  AND {_CF} IS NOT NULL AND TRIM({_CF}) != ''
                GROUP BY owner_id""",
            (op, bp, val, op, op)).fetchall()
        names = {r["id"]: r for r in conn.execute(
            "SELECT id, name, email FROM users").fetchall()}
    out = []
    for r in rows:
        u = names.get(r["owner_id"])
        out.append({
            "id": r["owner_id"],
            "name": (u["name"] if u and u["name"] else None) or r["owner_id"],
            "email": (u["email"] if u else None) or None,
            "named": bool(u and u["name"]),
            "accounts": r["accounts"],
        })
    out.sort(key=lambda o: o["name"].lower())
    return out


# ── the page's two views ──────────────────────────────────────────────────────

def _owner_list(owners) -> list[str]:
    """Normalise the owner argument to a de-duplicated list of ids.

    Takes a single id or a list, because every caller used to pass one string
    and the page now sends several — accepting both keeps one code path rather
    than a `_for_one` and a `_for_many` that drift.
    """
    if isinstance(owners, str):
        owners = [owners]
    seen, out = set(), []
    for o in owners or []:
        o = (o or "").strip()
        if o and o not in seen:
            seen.add(o)
            out.append(o)
    return out


def _account_filter(owners, *, everyone: bool = False) -> tuple[str, list]:
    """Accounts belonging to `owners`, or to anyone when `everyone`.

    `everyone` is a separate argument rather than an empty list meaning "all",
    because those two are opposite intentions that look identical: a caller
    that asked for nobody must get nothing, and only a caller that said so by
    name gets the whole company.
    """
    if everyone:
        return (f"{_CF} = ? AND {_CF} IS NOT NULL AND TRIM({_CF}) != ''",
                [_bucket_path(), bucket_value(), _owner_path(), _owner_path()])
    ids = _owner_list(owners)
    if not ids:
        # An empty selection matches nothing. Returning a bare bucket filter
        # would hand the reader every account in the company under the heading
        # of whoever they last had selected.
        return "0 = 1", []
    marks = ",".join("?" * len(ids))
    return (f"{_CF} IN ({marks}) AND {_CF} = ?",
            [_owner_path(), *ids, _bucket_path(), bucket_value()])


def _owner_names() -> dict:
    """{pylon user id: display name} for naming an account's owner."""
    with db.get_conn() as conn:
        return {r["id"]: (r["name"] or r["id"]) for r in conn.execute(
            "SELECT id, name FROM users").fetchall()}


def accounts_for(owners) -> list[dict]:
    """The selected CSMs' accounts and how many tickets are open on each.

    Every account is returned, including the ones at zero. A roster that drops
    quiet accounts cannot be read as a roster — the reader has no way to tell
    "nothing open" from "not mine". Each row carries its owner, because with
    two CSMs selected an account name alone no longer says whose it is.
    """
    acc_where, acc_params = _account_filter(owners)
    open_where, open_params = _open_where()
    with db.get_conn() as conn:
        # Same shape as standings(): aggregate once, join, rather than a
        # per-account correlated count.
        rows = conn.execute(
            f"""SELECT a.id, a.name, a.domain,
                       json_extract(a.custom_fields, ?) AS owner_id,
                       COALESCE(t.n, 0) AS open_tickets
                FROM accounts a
                LEFT JOIN (SELECT t.account_id AS aid, COUNT(*) AS n
                             FROM tickets t WHERE {open_where}
                            GROUP BY t.account_id) t ON t.aid = a.id
                WHERE {acc_where}""",
            [_owner_path(), *open_params, *acc_params]).fetchall()
    names = _owner_names()
    out = [{"id": r["id"], "name": r["name"] or r["id"],
            "domain": r["domain"], "open_tickets": r["open_tickets"],
            "owner_id": r["owner_id"],
            "owner_name": names.get(r["owner_id"]) or r["owner_id"]}
           for r in rows]
    out.sort(key=lambda a: (-a["open_tickets"], a["name"].lower()))
    return out


def tickets_for(owners) -> dict:
    """Open tickets across the selected CSMs' accounts, one row per ticket.

    `created_by` and the last reply are derived from the message thread rather
    than read off the ticket: Pylon's `requester` is a contact id that would
    cost a fetch per ticket to name, while the thread already holds the name of
    whoever opened it and whoever answered last.
    """
    acc_where, acc_params = _account_filter(owners)
    open_where, open_params = _open_where()
    with db.get_conn() as conn:
        rows = conn.execute(
            f"""SELECT t.id, t.number, t.title, t.link, t.state, t.source,
                       t.created_at, t.assignee_name, t.slack_url,
                       t.latest_message_time,
                       a.name AS account_name, a.domain AS account_domain,
                       json_extract(a.custom_fields, ?) AS owner_id,
                       (SELECT m.author_name FROM messages m
                         WHERE m.ticket_id = t.id
                         ORDER BY m.timestamp ASC  LIMIT 1) AS created_by,
                       (SELECT m.author_name FROM messages m
                         WHERE m.ticket_id = t.id
                         ORDER BY m.timestamp DESC LIMIT 1) AS last_reply_by,
                       (SELECT m.timestamp FROM messages m
                         WHERE m.ticket_id = t.id
                         ORDER BY m.timestamp DESC LIMIT 1) AS last_reply_at
                FROM tickets t
                JOIN accounts a ON a.id = t.account_id
                WHERE {acc_where} AND {open_where}""",
            [_owner_path(), *acc_params, *open_params]).fetchall()

    # The coverage rosters are read ONCE for the whole listing, not per row:
    # they are a handful of rows that every ticket asks the same question of.
    import review
    groups = review.groups_by_assignee()
    names = _owner_names()

    out = []
    for r in rows:
        d = dict(r)
        # A ticket whose thread was never stored still has a last-touch time on
        # the ticket itself; falling back keeps the column from reading as
        # "never answered" when the truth is "messages not fetched".
        d["last_reply_at"] = d["last_reply_at"] or d["latest_message_time"]
        # Group follows the ASSIGNEE, the same rule the Open tab's group filter
        # and the Leaderboard's teams use — a coverage owns people, not
        # accounts, so an unassigned ticket has no group to belong to.
        d["groups"] = groups.get(d["assignee_name"] or "", [])
        # Whose account this is. Constant when one CSM is selected, which is
        # why the page only shows the column once more than one is.
        d["owner_name"] = names.get(d["owner_id"]) or d["owner_id"]
        out.append(d)
    out.sort(key=lambda t: (t["account_name"] or "").lower())
    return {"tickets": out, "accounts": accounts_for(owners),
            "owners": _owner_list(owners),
            "bucket_label": bucket_label(), "synced_at": synced_at()}


# ── analytics ─────────────────────────────────────────────────────────────────

TREND_MONTHS = 6
TREND_ACCOUNTS = 5   # AGC() offers seven categorical hues; five plus "Other"
                     # plus the two volume series stays inside that, and a hue
                     # invented for an eighth account is not a colour anyone
                     # can read.


def _month_axis(n: int = TREND_MONTHS) -> list[str]:
    """The last `n` calendar months as 'YYYY-MM', oldest first.

    Built here rather than taken from the rows the query happens to return: a
    month with no tickets must be a zero on the axis, not a missing point. A
    trend line that silently closes over a quiet month reads as continuous
    volume across a gap that was actually empty.
    """
    today = datetime.now(timezone.utc).date()
    y, m = today.year, today.month
    out = []
    for _ in range(n):
        out.append(f"{y:04d}-{m:02d}")
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    return list(reversed(out))


def analytics(owners=None) -> dict:
    """Ticket volume over time for the selection, and standings for everyone.

    `owners=None` means every CSM in the bucket — the Analytics tab opens on
    the whole picture and the picker narrows it, because its standings table is
    company-wide regardless and a blank page was the alternative.

    Both trends count tickets by the month they were CREATED, and both are
    counts of tickets — one y-axis, one unit. "Still open" is drawn as a subset
    of "created" rather than on a second scale, because two scales in one frame
    invite a comparison the numbers do not support.
    """
    months = _month_axis()
    floor = months[0]
    everyone = owners is None
    acc_where, acc_params = _account_filter(owners, everyone=everyone)
    open_where, open_params = _open_where()

    with db.get_conn() as conn:
        rows = conn.execute(
            f"""SELECT substr(t.created_at, 1, 7) AS ym,
                       a.name AS account,
                       COUNT(*) AS created,
                       SUM(CASE WHEN {open_where} THEN 1 ELSE 0 END) AS still_open
                FROM tickets t
                JOIN accounts a ON a.id = t.account_id
                WHERE {acc_where}
                  AND t.deleted_at IS NULL
                  AND substr(t.created_at, 1, 7) >= ?
                GROUP BY ym, account""",
            [*open_params, *acc_params, floor]).fetchall()

    idx = {ym: i for i, ym in enumerate(months)}
    created = [0] * len(months)
    still_open = [0] * len(months)
    by_account: dict[str, list] = {}
    for r in rows:
        i = idx.get(r["ym"])
        if i is None:                     # a month past the axis floor
            continue
        created[i] += r["created"]
        still_open[i] += r["still_open"] or 0
        series = by_account.setdefault(r["account"], [0] * len(months))
        series[i] += r["created"]

    ranked = sorted(by_account.items(), key=lambda kv: -sum(kv[1]))
    accounts = [{"name": n, "counts": c} for n, c in ranked[:TREND_ACCOUNTS]]
    rest = ranked[TREND_ACCOUNTS:]
    if rest:
        # Folded, not dropped and not given a made-up hue: the reader can still
        # see the total, and the named lines stay the ones worth naming.
        merged = [sum(c[i] for _, c in rest) for i in range(len(months))]
        accounts.append({"name": f"Other ({len(rest)} accounts)", "counts": merged})

    return {"months": months, "created": created, "still_open": still_open,
            "accounts": accounts, "standings": standings(),
            "owners": [] if everyone else _owner_list(owners),
            "everyone": everyone}


def standings() -> list[dict]:
    """Every CSM in the covered bucket: accounts owned, tickets open.

    The whole table in one query rather than `owners()` plus a count each — at
    29 CSMs the per-owner version was 29 round trips for a table nobody would
    wait for.
    """
    op, bp, val = _owner_path(), _bucket_path(), bucket_value()
    open_where, open_params = _open_where()
    with db.get_conn() as conn:
        # One pass over the open tickets, joined to their account — not a
        # correlated COUNT per account. The subquery form re-ran an open-ticket
        # count for each of ~1,100 accounts and took three seconds on its own,
        # on every visit to the Analytics tab.
        rows = conn.execute(
            f"""SELECT {_CF} AS owner_id,
                       COUNT(*) AS accounts,
                       COALESCE(SUM(t.n), 0) AS open_tickets
                FROM accounts a
                LEFT JOIN (SELECT t.account_id AS aid, COUNT(*) AS n
                             FROM tickets t WHERE {open_where}
                            GROUP BY t.account_id) t ON t.aid = a.id
                WHERE {_CF} = ?
                  AND {_CF} IS NOT NULL AND TRIM({_CF}) != ''
                GROUP BY owner_id""",
            (op, *open_params, bp, val, op, op)).fetchall()
        names = {r["id"]: r["name"] for r in conn.execute(
            "SELECT id, name FROM users").fetchall()}
    out = [{"id": r["owner_id"],
            "name": names.get(r["owner_id"]) or r["owner_id"],
            "accounts": r["accounts"],
            "open_tickets": r["open_tickets"] or 0} for r in rows]
    out.sort(key=lambda o: (-o["open_tickets"], o["name"].lower()))
    return out


def conversation(ticket_id: str) -> dict:
    """One ticket's thread, for the expanded row. Private notes marked."""
    with db.get_conn() as conn:
        t = conn.execute(
            "SELECT t.id, t.number, t.title, t.link, t.state, t.slack_url,"
            "       a.name AS account_name"
            "  FROM tickets t LEFT JOIN accounts a ON a.id = t.account_id"
            " WHERE t.id = ?", (ticket_id,)).fetchone()
        if not t:
            return {"ticket": None, "messages": []}
        msgs = conn.execute(
            "SELECT author_name, author_email, is_customer, is_private,"
            "       timestamp, message_html"
            "  FROM messages m WHERE m.ticket_id = ?"
            " ORDER BY m.timestamp ASC", (ticket_id,)).fetchall()
    return {"ticket": dict(t),
            "messages": [{**dict(m), "is_customer": bool(m["is_customer"]),
                          "is_private": bool(m["is_private"])}
                         for m in msgs]}
