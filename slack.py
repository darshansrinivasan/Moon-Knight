"""
Slack delivery for QC results.

Posts a Block Kit summary of a day's QC run to a configured channel using a
bot token from the admin credential vault.
"""

import asyncio
import json
import logging
import re
import time

import httpx

import db
import vault

logger = logging.getLogger(__name__)

SLACK_API = "https://slack.com/api"

RULE_LABELS = {
    "r1": "Functionality missing",
    "r2": "Category missing",
    "r3": "Invalid account",
    "r4": "Response >24h",
    "r5": "Status/owner mismatch",
    "r7": "Rootly/Jira missing",
    "r8": "Oncall incomplete",
}


class SlackNotConfigured(RuntimeError):
    pass


def _token() -> str:
    tok = vault.get_credential("slack_bot_token")
    if not tok:
        raise SlackNotConfigured(
            "No Slack bot token configured — add it in Admin → Credentials"
        )
    return tok


async def _post(method: str, payload: dict) -> dict:
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.post(
            f"{SLACK_API}/{method}",
            json=payload,
            headers={
                "Authorization": f"Bearer {_token()}",
                "Content-Type":  "application/json; charset=utf-8",
            },
        )
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(f"Slack {method} failed: {data.get('error', 'unknown error')}")
    return data


async def test_auth() -> dict:
    """Verify the bot token works. Returns the workspace/bot identity."""
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(
            f"{SLACK_API}/auth.test",
            headers={"Authorization": f"Bearer {_token()}"},
        )
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(f"Slack auth failed: {data.get('error', 'unknown error')}")
    return {"team": data.get("team"), "bot": data.get("user")}


# ── report building ───────────────────────────────────────────────────────────

# Slack caps a message at 50 blocks and a section at 3000 characters. Stay under
# both, and split across as many thread replies as the day needs rather than
# truncating the report.
MAX_BLOCKS_PER_MESSAGE = 45
MAX_SECTION_CHARS      = 2800
MAX_REASON_CHARS       = 260
MAX_PAYLOAD_CHARS      = 28000   # Slack rejects oversized block payloads outright

# Reasons arrive two ways: R-check notes joined with " | ", and AI notes run
# together as "A1 Fail: ... A3 Poor: ...". Splitting on the check marker gives
# one bullet per finding either way.
_CHECK_BOUNDARY = re.compile(r"(?=(?:R[1-9]|A[1-5])\b[^:]{0,40}:)")


def _esc(text) -> str:
    """Slack mrkdwn requires these three escaped; nothing else."""
    return (str(text or "").replace("&", "&amp;")
                           .replace("<", "&lt;")
                           .replace(">", "&gt;"))


def ticket_reasons(ai_notes) -> list:
    """One bullet per finding, in the order the checks produced them."""
    out = []
    for chunk in (ai_notes or "").split(" | "):
        for piece in _CHECK_BOUNDARY.split(chunk):
            piece = piece.strip(" .|\n")
            if piece:
                out.append(piece[:MAX_REASON_CHARS])
    return out


def build_summary(date_str: str) -> dict:
    """Aggregate a day's results into the numbers the report needs.

    Grades come from `review.effective_grades`, so a ticket a human has signed
    off reads the same here as it does on the dashboard. Tickets whose state is
    excluded from AI scoring are reported separately from genuinely pending
    ones: counting them as "not scored" made a completely healthy run look like
    it had silently dropped a third of the day.

    Scope is decided by the query, not here. `get_day_tickets` returns only
    tickets in scope, so every count below is over in-scope tickets and the
    excluded ones are counted separately for the one line that names them. This
    function used to re-derive the exclusion per ticket in three places, which is
    three chances to disagree with the scorer about what the day contained.
    """
    import review

    tickets  = review.apply_effective_grades(db.get_day_tickets(date_str))
    excluded = db.excluded_ticket_count(date_str)

    counts   = {"Pass": 0, "Fail": 0, "Needs Review": 0}
    pending  = 0
    for t in tickets:
        result = t.get("overall_result")
        if result in counts:
            counts[result] += 1
        else:
            pending += 1

    total = len(tickets)

    # Only checks that are switched on. A disabled check must not appear in the
    # morning report as a reason anybody failed.
    import rules as qc_rules
    live_rules = qc_rules.enabled_rule_keys(tuple(RULE_LABELS))

    rule_fails = {}
    for t in tickets:
        for key in live_rules:
            if t.get(key) == "Fail":
                rule_fails[key] = rule_fails.get(key, 0) + 1

    # Anything that is not a clean pass deserves an explanation.
    needs_attention = [t for t in tickets
                       if t.get("overall_result") in ("Fail", "Needs Review")]

    by_assignee = {}
    for t in needs_attention:
        by_assignee.setdefault(t.get("assignee_name") or "Unassigned", []).append(t)
    for group in by_assignee.values():
        group.sort(key=lambda t: (t.get("overall_result") != "Fail", t.get("number") or 0))

    # Worst first, so the thread reads in order of who needs attention most.
    groups = sorted(by_assignee.items(), key=lambda kv: (-len(kv[1]), kv[0].lower()))

    scored = total - pending
    return {
        "date":       date_str,
        "total":      total,
        "pass":       counts["Pass"],
        "fail":       counts["Fail"],
        "review":     counts["Needs Review"],
        "pending":    pending,
        "excluded":   excluded,
        "pass_rate":  round(counts["Pass"] / scored * 100) if scored else None,
        "rule_fails": sorted(rule_fails.items(), key=lambda kv: -kv[1]),
        "groups":     groups,
        "attention":  len(needs_attention),
    }


def _summary_blocks(s: dict, base_url: str) -> list:
    """The parent message: what happened, and what stands out."""
    day_link = f"{base_url.rstrip('/')}/?date={s['date']}"
    rate = f"{s['pass_rate']}%" if s["pass_rate"] is not None else "\u2014"

    blocks = [
        {"type": "header",
         "text": {"type": "plain_text", "text": f"Support QC run \u2014 {s['date']}",
                  "emoji": True}},
        {"type": "section", "fields": [
            {"type": "mrkdwn", "text": f"*In scope*\n{s['total']}"},
            {"type": "mrkdwn", "text": f"*Pass rate*\n{rate}"},
            {"type": "mrkdwn", "text": f"*\u2705 Pass*\n{s['pass']}"},
            {"type": "mrkdwn", "text": f"*\u274c Fail*\n{s['fail']}"},
            {"type": "mrkdwn", "text": f"*\u26a0\ufe0f Needs review*\n{s['review']}"},
            {"type": "mrkdwn", "text": f"*\u23f3 Not scored*\n{s['pending']}"},
        ]},
    ]

    # Excluded tickets are not a problem to fix, so they sit outside the counts
    # above \u2014 but staying silent about them makes the total look wrong.
    if s.get("excluded"):
        blocks.append({"type": "context", "elements": [
            {"type": "mrkdwn",
             "text": f"{s['excluded']} archived or out-of-scope "
                     f"ticket{'s' if s['excluded'] != 1 else ''} excluded from scoring"}
        ]})

    lines = []
    # A ticket that is in scope but ungraded means scoring dropped it. That is a
    # run failure, not a statistic, so say so before anything else.
    if s["pending"]:
        lines.append(
            f"\u2022 :rotating_light: {s['pending']} in-scope "
            f"ticket{'s' if s['pending'] != 1 else ''} could not be graded \u2014 "
            "re-run scoring for this day"
        )
    if s["rule_fails"]:
        top = ", ".join(f"{RULE_LABELS[k]} ({n})" for k, n in s["rule_fails"][:3])
        lines.append(f"\u2022 Most common rule failures: {top}")
    if s["groups"]:
        worst, tickets = s["groups"][0]
        plural = "s" if len(tickets) != 1 else ""
        lines.append(f"\u2022 {_esc(worst)} has the most to address \u2014 "
                     f"{len(tickets)} ticket{plural}")
        people = "people" if len(s["groups"]) != 1 else "person"
        aplural = "s" if s["attention"] != 1 else ""
        lines.append(f"\u2022 {s['attention']} ticket{aplural} need attention across "
                     f"{len(s['groups'])} {people} \u2014 details in the thread")
    else:
        lines.append("\u2022 Nothing needs attention today \u2014 every scored ticket passed")

    blocks.append({"type": "section",
                   "text": {"type": "mrkdwn", "text": "*Analysis*\n" + "\n".join(lines)}})
    blocks.append({"type": "context", "elements": [
        {"type": "mrkdwn", "text": f"<{day_link}|Open the QC dashboard for this day>"}
    ]})
    return blocks


def _assignee_sections(s: dict, base_url: str, mentions: dict | None = None) -> list:
    """One or more section blocks per person, each listing their tickets.

    `mentions` maps assignee name -> Slack user ID for names that resolved
    unambiguously. Anyone absent is rendered as plain text: see
    resolve_assignee_ids for why guessing is not an option.
    """
    day_link = f"{base_url.rstrip('/')}/?date={s['date']}"
    blocks = []

    for name, tickets in s["groups"]:
        plural = "s" if len(tickets) != 1 else ""
        who = mention_or_name(name, mentions) if mentions else _esc(name)
        current = (f"*{who}* \u2014 {len(tickets)} "
                   f"ticket{plural} needing attention")

        for t in tickets:
            mark  = "\u274c" if t.get("overall_result") == "Fail" else "\u26a0\ufe0f"
            link  = t.get("link") or day_link
            title = _esc((t.get("title") or "(no title)")[:90])
            entry = f"\n\n{mark} <{link}|#{t.get('number')}> *{title}*"
            for reason in ticket_reasons(t.get("ai_notes")):
                entry += f"\n     \u2022 {_esc(reason)}"

            # Keep a person's tickets contiguous, continuing under a
            # "(continued)" heading rather than dropping any.
            if len(current) + len(entry) > MAX_SECTION_CHARS:
                blocks.append({"type": "section",
                               "text": {"type": "mrkdwn", "text": current}})
                current = f"*{_esc(name)}* _(continued)_" + entry
            else:
                current += entry

        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": current}})
        blocks.append({"type": "divider"})

    return blocks


def _chunk(blocks: list, size: int = MAX_BLOCKS_PER_MESSAGE) -> list:
    """Split into messages Slack will accept.

    Block count alone is not enough: a busy day stays under 50 blocks while the
    serialised payload sails past Slack's size ceiling, which fails the whole
    message rather than trimming it. Chunk on both.
    """
    out, current, current_size = [], [], 0
    for block in blocks:
        block_size = len(json.dumps(block))
        too_many = len(current) >= size
        too_big  = current and current_size + block_size > MAX_PAYLOAD_CHARS
        if too_many or too_big:
            out.append(current)
            current, current_size = [], 0
        current.append(block)
        current_size += block_size
    if current:
        out.append(current)
    return out


async def post_day_report(date_str: str, channel: str | None = None) -> dict:
    """Post the day's QC report: a summary, then per-person detail in a thread."""
    channel = channel or vault.get_setting("slack_channel").strip()
    if not channel:
        raise SlackNotConfigured("No Slack channel configured \u2014 set it in Admin \u2192 Slack")

    summary  = build_summary(date_str)
    base_url = vault.get_setting("dashboard_base_url")
    rate = f"{summary['pass_rate']}%" if summary["pass_rate"] is not None else "\u2014"

    mode = mention_mode()
    mentions: dict = {}
    unresolved: list = []
    if mode == MENTION_ALL:
        names = [name for name, _ in summary["groups"]]
        mentions = await resolve_assignee_ids(names)
        unresolved = sorted(n for n in names
                            if n != "Unassigned" and not mentions.get(n))
        if unresolved:
            logger.info(
                "No Slack id for %d assignee(s), posting their names as plain "
                "text: %s", len(unresolved), ", ".join(unresolved),
            )

    lead_blocks = await _lead_mention_blocks(summary) if mode != MENTION_OFF else []

    parent = await _post("chat.postMessage", {
        "channel": channel,
        "text": (f"Support QC {date_str}: {summary['total']} tickets, "
                 f"{summary['pass']} pass / {summary['fail']} fail ({rate})"),
        "blocks": _summary_blocks(summary, base_url) + lead_blocks,
    })

    # Detail lives in the thread so the channel stays readable, split across as
    # many replies as the day needs rather than being cut short.
    thread_ts = parent.get("ts")
    replies = 0
    for chunk in _chunk(_assignee_sections(summary, base_url, mentions)):
        await _post("chat.postMessage", {
            "channel": channel,
            "thread_ts": thread_ts,
            "text": f"QC detail for {date_str}",
            "blocks": chunk,
        })
        replies += 1

    return {**parent, "thread_replies": replies,
            "mention_mode": mode, "unresolved_names": unresolved}


async def _lead_mention_blocks(summary: dict) -> list:
    """A context block tagging the lead of each team that has work to address.

    Leads are coverage reviewers, identified by email \u2014 which Slack can resolve
    directly, so this needs none of the name-matching caution above.
    """
    import leaderboard

    owners = {name for name, _ in summary["groups"]}
    if not owners:
        return []

    membership, leads, _ = leaderboard._team_membership()
    affected = sorted({
        team for assignee in owners for team in membership.get(assignee, [])
    })
    if not affected:
        return []

    try:
        await directory()
        async with _dir_lock:
            by_email = {
                (p.get("email") or "").lower(): p["id"]
                for p in _dir_people.values() if p.get("email")
            }
    except Exception as e:
        logger.warning("Could not resolve lead mentions: %s", e)
        by_email = {}

    parts = []
    for team in affected:
        lead = leads.get(team) or {}
        email = (lead.get("lead_email") or "").lower()
        sid = by_email.get(email)
        who = f"<@{sid}>" if sid else _esc(lead.get("lead_name") or email or "?")
        parts.append(f"{_esc(team)}: {who}")

    return [{"type": "context", "elements": [
        {"type": "mrkdwn", "text": "Leads to review \u2014 " + " \u00b7 ".join(parts)}
    ]}]


async def post_failure(date_str: str, error: str, channel: str | None = None) -> dict:
    """Tell the channel a scheduled run failed, rather than failing silently.

    The alarm names the instance that raised it. Any deployment holding the same
    bot token posts to the same channel, so an unconfigured second instance —
    a stale service, a review environment, someone's laptop — can alarm about a
    failure that never happened to production, while production's own Runs page
    shows a clean success. That is not hypothetical: it cost a morning here,
    with a "No Google Cloud project configured" alarm arriving nine minutes
    after a run that had fetched and scored 44 tickets without incident. One
    line of provenance turns that from an investigation into a glance.
    """
    channel = channel or vault.get_setting("slack_channel").strip()
    if not channel:
        raise SlackNotConfigured("No Slack channel configured")
    body = f"*⚠️ Scheduled QC run failed — {date_str}*\n```{error[:500]}```"
    origin = vault.get_setting("dashboard_base_url").strip()
    if origin:
        body += f"\n_Reported by_ {origin}"
    return await _post("chat.postMessage", {
        "channel": channel,
        "text": f"⚠️ Scheduled QC run for {date_str} failed: {error}",
        "blocks": [{
            "type": "section",
            "text": {"type": "mrkdwn", "text": body},
        }],
    })


async def post_test(channel: str | None = None) -> dict:
    channel = channel or vault.get_setting("slack_channel").strip()
    if not channel:
        raise SlackNotConfigured("No Slack channel configured — set it in Admin → Slack")
    return await _post("chat.postMessage", {
        "channel": channel,
        "text": "✅ Pylon QC is connected. Scheduled reports will arrive here.",
    })


# ── directory (id → name for the Rules UI) ────────────────────────────────────

_dir_lock = asyncio.Lock()
_dir_users: dict[str, str] = {}
_dir_groups: dict[str, str] = {}
_dir_people: dict[str, dict] = {}   # id -> {id, name, email}
_dir_at: float = 0
_DIR_TTL = 600


async def _api_get(method: str, params: dict | None = None) -> dict:
    async with httpx.AsyncClient(timeout=25) as client:
        r = await client.get(
            f"{SLACK_API}/{method}",
            params=params or {},
            headers={"Authorization": f"Bearer {_token()}"},
        )
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(f"Slack {method} failed: {data.get('error', 'unknown error')}")
    return data


async def _refresh_directory() -> None:
    """Load workspace users and user-groups. Failure leaves the previous cache."""
    global _dir_users, _dir_groups, _dir_people, _dir_at
    users: dict[str, str] = {}
    people: dict[str, dict] = {}
    cursor = None
    while True:
        params: dict = {"limit": 200}
        if cursor:
            params["cursor"] = cursor
        data = await _api_get("users.list", params)
        for u in data.get("members") or []:
            if u.get("deleted") or u.get("is_bot"):
                continue
            uid = u.get("id")
            if not uid:
                continue
            profile = u.get("profile") or {}
            name = (profile.get("real_name") or profile.get("display_name")
                    or u.get("name") or uid)
            email = (profile.get("email") or "").strip().lower()
            users[uid] = name
            people[uid] = {"id": uid, "name": name, "email": email}
        cursor = (data.get("response_metadata") or {}).get("next_cursor")
        if not cursor:
            break

    groups: dict[str, str] = {}
    try:
        gdata = await _api_get("usergroups.list")
        for g in gdata.get("usergroups") or []:
            gid = g.get("id")
            handle = g.get("handle") or g.get("name") or gid
            if gid:
                groups[gid] = handle if str(handle).startswith("@") else f"@{handle}"
    except Exception as e:
        logger.warning("Slack usergroups.list failed: %s", e)

    _dir_users, _dir_groups, _dir_people, _dir_at = users, groups, people, time.time()


async def directory(force: bool = False) -> tuple[dict[str, str], dict[str, str]]:
    """Return (users, groups) maps, refreshing when stale. Empty if Slack isn't set up."""
    try:
        _token()
    except SlackNotConfigured:
        return {}, {}
    async with _dir_lock:
        stale = time.time() - _dir_at > _DIR_TTL
        if force or stale or not _dir_users:
            try:
                await _refresh_directory()
            except Exception as e:
                logger.warning("Slack directory refresh failed: %s", e)
        return dict(_dir_users), dict(_dir_groups)


async def resolve_ids(ids: list[str]) -> dict[str, str]:
    """Map Slack user/group ids to display names. Missing ids are omitted."""
    wanted = {i for i in ids if i}
    if not wanted:
        return {}
    users, groups = await directory()
    out = {}
    for sid in wanted:
        name = users.get(sid) or groups.get(sid)
        if name:
            out[sid] = name
    return out


async def search_directory(q: str, kind: str = "user", limit: int = 25) -> list[dict]:
    """Name search over the cached Slack directory. `kind` is user or group."""
    q = (q or "").strip().lower()
    users, groups = await directory()
    if kind == "group":
        items = [{"id": i, "name": n} for i, n in groups.items()]
    else:
        async with _dir_lock:
            items = [
                {"id": p["id"], "name": p["name"], "email": p.get("email") or ""}
                for p in _dir_people.values()
            ]
        if not items:
            items = [{"id": i, "name": n, "email": ""} for i, n in users.items()]
    if q:
        items = [e for e in items if q in e["name"].lower() or q in (e.get("email") or "")]
    items.sort(key=lambda e: e["name"].lower())
    return items[:limit]


# ── @-mention identity resolution ─────────────────────────────────────────────
# tickets.assignee_name is a display string; Slack needs a user ID. Fuzzy
# matching is not acceptable here: searching the live directory for "Deepak"
# returns both "Aditya Deepak" and "Deepak Kayala", and @-mentioning the wrong
# colleague in a shared channel about someone else's failed ticket is the worst
# thing this feature can do. So: an explicit map first, then an EXACT match that
# must be unique, then plain text. Never a guess.

IDENTITY_MAP_KEY = "qc_slack_identity_map"

MENTION_OFF, MENTION_LEADS, MENTION_ALL = "off", "leads", "all"
MENTION_MODES = (MENTION_OFF, MENTION_LEADS, MENTION_ALL)


def identity_map() -> dict:
    """Admin-maintained assignee name -> Slack user ID overrides."""
    raw = vault.get_raw_setting(IDENTITY_MAP_KEY)
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("Slack identity map is not valid JSON — ignoring it")
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k).strip(): str(v).strip() for k, v in data.items() if k and v}


def mention_mode() -> str:
    """off | leads | all. Defaults to leads.

    `all` @-mentions every assignee with failures. It is available but not the
    default on purpose: a daily automated mention about someone's own failed
    tickets, in a group channel, is a performance conversation happening in
    public. Making it a setting means it can be turned down without a deploy.
    """
    mode = (vault.get_setting("slack_mention_mode") or MENTION_LEADS).strip()
    return mode if mode in MENTION_MODES else MENTION_LEADS


def _exact_matches(name: str, people: list[dict]) -> list[dict]:
    target = (name or "").strip().casefold()
    if not target:
        return []
    return [p for p in people if (p.get("name") or "").strip().casefold() == target]


async def resolve_assignee_ids(names) -> dict:
    """Map assignee display names to Slack user IDs, or None when unsure.

    Resolution order, stopping at the first hit:
      1. the admin identity map
      2. a case-insensitive full-name match that is UNIQUE in the directory
      3. None — the caller must render plain text rather than guess
    """
    wanted = {n for n in (names or []) if n and n != "Unassigned"}
    if not wanted:
        return {}

    overrides = identity_map()
    out = {n: overrides.get(n) for n in wanted}
    unresolved = [n for n, v in out.items() if not v]
    if not unresolved:
        return out

    try:
        await directory()
        async with _dir_lock:
            people = [dict(p) for p in _dir_people.values()]
    except SlackNotConfigured:
        return out
    except Exception as e:
        logger.warning("Slack directory unavailable for mentions: %s", e)
        return out

    for name in unresolved:
        matches = _exact_matches(name, people)
        if len(matches) == 1:
            out[name] = matches[0]["id"]
        elif len(matches) > 1:
            logger.info(
                "Not mentioning %r — %d Slack users share that name",
                name, len(matches),
            )
    return out


def mention_or_name(name: str, resolved: dict) -> str:
    """`<@ID>` when confidently resolved, otherwise the escaped plain name.

    The only place raw <@…> may appear in a payload; everything else goes
    through _esc.
    """
    sid = (resolved or {}).get(name)
    return f"<@{sid}>" if sid else _esc(name)


async def search_reviewers(q: str, domain: str, limit: int = 25) -> list[dict]:
    """Slack people who have an email on the login domain — they can be reviewers."""
    domain = (domain or "").lower().lstrip("@")
    suffix = f"@{domain}"
    q = (q or "").strip().lower()
    await directory()
    async with _dir_lock:
        people = [dict(p) for p in _dir_people.values()]
    items = [p for p in people if (p.get("email") or "").endswith(suffix)]
    if q:
        items = [
            p for p in items
            if q in p["name"].lower() or q in (p.get("email") or "")
        ]
    items.sort(key=lambda e: e["name"].lower())
    return items[:limit]
