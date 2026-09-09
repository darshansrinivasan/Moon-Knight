"""
Internal vs external tickets, decided by Slack channel of ORIGIN.

CSMs raise internal tickets in a known set of Slack channels; those tickets
never trigger CSAT and are operationally a different population. The catch,
proven against live data (#76226): a ticket's own Pylon record usually does
not confess its channel — most internal-channel tickets read source='manual'
with slack=null. The ONLY authority is Pylon's channel-filtered search
(POST /issues/search, slack_channel_id equals <id>), which is exactly the
filter Pylon support recommended.

So classification is membership-based:

* The TAGGER runs that search per configured channel and mirrors the result
  into `channel_index` (ticket_id → channel_id). Full sweep at ship/admin-add;
  incremental daily (newest-first + early stop) from the scheduler.
* Classification is DERIVED at read time: internal = ticket_id in
  channel_index with a channel currently on the Admin list. Editing the list
  reclassifies instantly; unknown tickets default to external — the correct
  reading, since the tagger enumerates the internal side exhaustively.
* `channel_scope_clause` is the one SQL opinion, in the style of
  rules.excluded_state_clause, so every future surface (QC dashboard, Report
  Card) scopes with the same predicate instead of re-deciding what "internal"
  means.

No Slack API, no AI: one Pylon endpoint, remembered locally.
"""

import json
import logging
from datetime import datetime, timezone

import db

logger = logging.getLogger(__name__)

SETTING_KEY = "internal_slack_channel_ids"
TAG_STATE_KEY = "channel_tag_state_json"

SCOPES = ("all", "external", "internal")


def internal_channel_ids() -> list[str]:
    """The configured internal channel IDs, one per line in Admin."""
    import vault
    raw = vault.get_setting(SETTING_KEY) or ""
    out, seen = [], set()
    for line in raw.replace(",", "\n").splitlines():
        cid = line.strip()
        if cid and cid not in seen:
            seen.add(cid)
            out.append(cid)
    return out


def channel_scope_clause(scope: str, alias: str = "t") -> tuple:
    """SQL predicate for a channel scope, plus its params. ("", []) = no-op.

    external: NOT in the internal membership set (unknown = external);
    internal: in it. With no channels configured, internal matches nothing
    rather than everything — an empty list must not invert the filter.
    """
    if scope in ("", "all", None):
        return "", []
    ids = internal_channel_ids()
    if scope == "internal" and not ids:
        return "0 = 1", []
    if scope == "external" and not ids:
        return "", []
    marks = ",".join("?" for _ in ids)
    member = (f"{alias}.id IN (SELECT ticket_id FROM channel_index"
              f" WHERE channel_id IN ({marks}))")
    if scope == "internal":
        return member, list(ids)
    if scope == "external":
        return f"NOT ({member})", list(ids)
    raise ValueError(f"scope must be one of {SCOPES}, got {scope!r}")


def internal_ticket_ids() -> set:
    """Every ticket id the index places in a currently-internal channel.

    For classifying data that never touches SQL — e.g. the resolved-in-range
    search results behind the CSAT response-rate denominator.
    """
    ids = internal_channel_ids()
    if not ids:
        return set()
    marks = ",".join("?" for _ in ids)
    with db.get_conn() as conn:
        return {r["ticket_id"] for r in conn.execute(
            f"SELECT ticket_id FROM channel_index WHERE channel_id IN ({marks})",
            ids).fetchall()}


def note_payload_channel(issue: dict, conn) -> None:
    """Fast-path writer: an issue payload that DOES carry its channel.

    Takes the CALLER's connection — the fetch loop already holds the day's
    write transaction, and WAL has one writer, so opening a second connection
    here would deadlock against the very transaction that called us.
    """
    chan = (issue.get("slack") or {}).get("channel_id")
    if not chan or not issue.get("id"):
        return
    conn.execute(
        "INSERT OR REPLACE INTO channel_index (ticket_id, channel_id,"
        " tagged_at) VALUES (?, ?, ?)",
        (issue["id"], chan, datetime.now(timezone.utc).isoformat()))


# ── the tagger ────────────────────────────────────────────────────────────────

def _known_ids(channel_id: str) -> set:
    with db.get_conn() as conn:
        return {r["ticket_id"] for r in conn.execute(
            "SELECT ticket_id FROM channel_index WHERE channel_id = ?",
            (channel_id,)).fetchall()}


async def tag_channel(channel_id: str, full: bool = False) -> dict:
    """Mirror one channel's Pylon search into channel_index.

    `full` reads every page (ship-time backfill, admin re-run); otherwise the
    newest-first early stop makes the daily run ~one page per channel.
    """
    import pylon
    known = None if full else _known_ids(channel_id)
    refs, complete = await pylon.search_issue_refs(
        {"field": "slack_channel_id", "operator": "equals",
         "value": channel_id},
        known_ids=known)
    now = datetime.now(timezone.utc).isoformat()
    with db.get_conn() as conn:
        conn.executemany(
            "INSERT OR REPLACE INTO channel_index (ticket_id, channel_id,"
            " tagged_at) VALUES (?, ?, ?)",
            [(r["id"], channel_id, now) for r in refs if r.get("id")])
    return {"channel": channel_id, "seen": len(refs), "complete": complete}


async def tag_all(full: bool = False, only: list[str] | None = None) -> dict:
    """Tag every configured channel (or `only` those), recording state.

    The per-channel state (last run, completeness, count) is what Admin shows —
    an incomplete sweep must be a visible warning with a re-run button, never a
    silently short index.
    """
    import vault
    channels = only if only is not None else internal_channel_ids()
    state = tag_state()
    results = []
    for cid in channels:
        try:
            res = await tag_channel(cid, full=full)
        except Exception as e:
            logger.exception("Channel tagging failed for %s", cid)
            res = {"channel": cid, "seen": 0, "complete": False,
                   "error": str(e)[:200]}
        results.append(res)
        state[cid] = {"at": datetime.now(timezone.utc).isoformat(),
                      "complete": res["complete"], "seen": res["seen"],
                      **({"error": res["error"]} if res.get("error") else {})}
    vault.set_raw_setting(TAG_STATE_KEY, json.dumps(state), "channel-tagger")
    return {"channels": results,
            "complete": all(r["complete"] for r in results) if results else True}


def tag_state() -> dict:
    import vault
    raw = vault.get_raw_setting(TAG_STATE_KEY)
    if not raw:
        return {}
    try:
        loaded = json.loads(raw)
        return loaded if isinstance(loaded, dict) else {}
    except json.JSONDecodeError:
        return {}
