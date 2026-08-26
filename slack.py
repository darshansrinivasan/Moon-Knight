"""
Slack delivery for QC results.

Posts a Block Kit summary of a day's QC run to a configured channel using a
bot token from the admin credential vault.
"""

import logging

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

def build_summary(date_str: str) -> dict:
    """Aggregate a day's results into the numbers the report needs."""
    tickets = db.get_day_tickets(date_str)
    total   = len(tickets)

    counts = {"Pass": 0, "Fail": 0, "Needs Review": 0}
    pending = 0
    for t in tickets:
        result = t.get("overall_result")
        if result in counts:
            counts[result] += 1
        else:
            pending += 1

    rule_fails: dict[str, int] = {}
    for t in tickets:
        for key in RULE_LABELS:
            if t.get(key) == "Fail":
                rule_fails[key] = rule_fails.get(key, 0) + 1

    failing = [t for t in tickets if t.get("overall_result") == "Fail"]
    failing.sort(key=lambda t: t.get("number") or 0)

    scored = total - pending
    return {
        "date":       date_str,
        "total":      total,
        "pass":       counts["Pass"],
        "fail":       counts["Fail"],
        "review":     counts["Needs Review"],
        "pending":    pending,
        "pass_rate":  round(counts["Pass"] / scored * 100) if scored else None,
        "rule_fails": sorted(rule_fails.items(), key=lambda kv: -kv[1]),
        "failing":    failing[:5],
    }


def _blocks(s: dict, base_url: str) -> list[dict]:
    day_link = f"{base_url.rstrip('/')}/?date={s['date']}"

    rate = f"{s['pass_rate']}%" if s["pass_rate"] is not None else "—"
    fields = [
        {"type": "mrkdwn", "text": f"*Tickets*\n{s['total']}"},
        {"type": "mrkdwn", "text": f"*Pass rate*\n{rate}"},
        {"type": "mrkdwn", "text": f"*✅ Pass*\n{s['pass']}"},
        {"type": "mrkdwn", "text": f"*❌ Fail*\n{s['fail']}"},
        {"type": "mrkdwn", "text": f"*⚠️ Needs review*\n{s['review']}"},
        {"type": "mrkdwn", "text": f"*⏳ Not scored*\n{s['pending']}"},
    ]

    blocks: list[dict] = [
        {"type": "header",
         "text": {"type": "plain_text", "text": f"Support QC — {s['date']}", "emoji": True}},
        {"type": "section", "fields": fields},
    ]

    if s["rule_fails"]:
        lines = "\n".join(
            f"• {RULE_LABELS[k]} — *{n}*" for k, n in s["rule_fails"][:5]
        )
        blocks.append({"type": "section",
                       "text": {"type": "mrkdwn", "text": f"*Most common rule failures*\n{lines}"}})

    if s["failing"]:
        lines = []
        for t in s["failing"]:
            title = (t.get("title") or "(no title)")[:70]
            who   = t.get("assignee_name") or "Unassigned"
            link  = t.get("link") or day_link
            lines.append(f"• <{link}|#{t.get('number')}> {title} — _{who}_")
        more = s["fail"] - len(s["failing"])
        if more > 0:
            lines.append(f"• …and {more} more")
        blocks.append({"type": "section",
                       "text": {"type": "mrkdwn", "text": "*Failing tickets*\n" + "\n".join(lines)}})

    blocks.append({"type": "context", "elements": [
        {"type": "mrkdwn", "text": f"<{day_link}|Open the QC dashboard for this day>"}
    ]})
    return blocks


async def post_day_report(date_str: str, channel: str | None = None) -> dict:
    """Post the QC summary for a day. Returns Slack's response."""
    channel = channel or vault.get_setting("slack_channel").strip()
    if not channel:
        raise SlackNotConfigured("No Slack channel configured — set it in Admin → Slack")

    summary  = build_summary(date_str)
    base_url = vault.get_setting("dashboard_base_url")
    rate = f"{summary['pass_rate']}%" if summary["pass_rate"] is not None else "—"

    return await _post("chat.postMessage", {
        "channel": channel,
        "text": (f"Support QC {date_str}: {summary['total']} tickets, "
                 f"{summary['pass']} pass / {summary['fail']} fail ({rate})"),
        "blocks": _blocks(summary, base_url),
    })


async def post_failure(date_str: str, error: str, channel: str | None = None) -> dict:
    """Tell the channel a scheduled run failed, rather than failing silently."""
    channel = channel or vault.get_setting("slack_channel").strip()
    if not channel:
        raise SlackNotConfigured("No Slack channel configured")
    return await _post("chat.postMessage", {
        "channel": channel,
        "text": f"⚠️ Scheduled QC run for {date_str} failed: {error}",
        "blocks": [{
            "type": "section",
            "text": {"type": "mrkdwn",
                     "text": f"*⚠️ Scheduled QC run failed — {date_str}*\n```{error[:500]}```"},
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
