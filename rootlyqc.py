"""Rootly QC — evaluate open incidents the way qc_runner evaluates tickets.

Same platform, second source. The shape mirrors the Pylon side deliberately:
one rules config an admin edits (rootly_rules_json, read through
`rules_config()` only), deterministic IR checks that cost nothing, AI IA
checks behind a content fingerprint so an unchanged incident never re-bills,
and every run recorded in qc_runs (date label 'rootly') so cost shows in Runs.

What is deliberately DIFFERENT from tickets: an incident is one evolving
record keyed by its Rootly id — there is no per-day keying and no frozen
snapshot in V1. QC here is a live judgement of "now", re-taken each morning.
"""

import asyncio
import hashlib
import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import db

logger = logging.getLogger(__name__)

RUN_LABEL = "rootly"
MAX_WORKERS = 4
SLACK_MSG_LIMIT = 100
SLACK_TEXT_BUDGET = 15000

# One vocabulary for every surface (list page, CSV, Slack line, Rules page).
CHECKS = {
    "ir1": "Severity set",
    "ir2": "Commander assigned",
    "ir3": "Jira linked",
    "ir4": "Pylon ticket linked",
    "ir5": "Update cadence",
    "ir6": "Status freshness",
    "ir7": "Summary present",
    "ia1": "Summary quality",
    "ia2": "Comms quality",
    "ia3": "Pending on",          # informational — never flips the grade
    "ia4": "Status consistency",
}
RULE_KEYS = ("ir1", "ir2", "ir3", "ir4", "ir5", "ir6", "ir7")
AI_KEYS = ("ia1", "ia2", "ia3", "ia4")
TOGGLEABLE = RULE_KEYS + AI_KEYS

DEFAULT_RULES = {
    "disabled_checks": [],
    # On top of rootly.TERMINAL_STATUSES; e.g. "in_triage" if triage noise
    # should not be graded.
    "excluded_statuses": [],
    # IR5: hours without an update before the check fails, by severity slug.
    "cadence_hours": {"sev1": 4, "sev2": 12, "default": 24},
    # IR6: hours since the incident last moved lifecycle stage.
    "stuck_hours": 72,
}


def rules_config() -> dict:
    """The Rootly rules doc with defaults filled — the ONE config surface."""
    import vault
    raw = vault.get_setting("rootly_rules_json") or ""
    try:
        doc = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        doc = {}
    out = {**DEFAULT_RULES, **{k: v for k, v in doc.items() if v is not None}}
    out["cadence_hours"] = {**DEFAULT_RULES["cadence_hours"],
                            **(doc.get("cadence_hours") or {})}
    out["disabled_checks"] = [k for k in (out.get("disabled_checks") or [])
                              if k in TOGGLEABLE]
    return out


def enabled(key: str, cfg: dict | None = None) -> bool:
    return key not in (cfg or rules_config())["disabled_checks"]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_ts(value) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


# ── fetch ─────────────────────────────────────────────────────────────────────

_PYLON_NUM = re.compile(r"(?:issueNumber=|#)(\d{3,})")
_ANY_NUM = re.compile(r"\b(\d{4,})\b")


def _pylon_from_field(value: str | None) -> int | None:
    """A ticket number out of whatever a human typed into the custom field —
    a Pylon URL, '#76707', or a bare number."""
    if not value:
        return None
    m = _PYLON_NUM.search(value) or _ANY_NUM.search(value)
    return int(m.group(1)) if m else None


def _pylon_from_jira(jira_key: str | None, jira_id: str | None) -> int | None:
    """The Pylon ticket whose external_issues reference the same Jira issue.

    Keys on the browse link (SPD-1234) first, then the numeric Jira id —
    both shapes exist in stored external_issues.
    """
    probes = [p for p in (jira_key, jira_id) if p]
    if not probes:
        return None
    with db.get_conn() as conn:
        for probe in probes:
            row = conn.execute(
                "SELECT number FROM tickets WHERE deleted_at IS NULL"
                " AND external_issues LIKE ? ORDER BY number DESC LIMIT 1",
                (f"%{probe}%",)).fetchone()
            if row:
                return row["number"]
    return None


async def fetch(triggered_by: str) -> dict:
    """Pull open incidents, resolve their Pylon link, upsert, refresh leftovers.

    A stored incident absent from a COMPLETE open-incident list has left the
    open set at Rootly — refresh it by id so its terminal status lands here
    (mirrors openqc's leftover discipline; absence from an INCOMPLETE list
    proves nothing and refreshes nothing).
    """
    import rootly
    import vault

    cfg = rules_config()
    incidents, complete = await rootly.list_open_incidents(
        cfg["excluded_statuses"])

    field_id = vault.get_setting("rootly_pylon_field") or ""
    sem = asyncio.Semaphore(5)

    async def resolve_pylon(inc: dict) -> None:
        num, source = None, None
        if field_id:
            async with sem:
                try:
                    sels = await rootly.field_selections(inc["id"])
                except Exception:
                    logger.exception("field_selections failed for %s", inc["id"])
                    sels = []
            for s in sels:
                if str(s.get("form_field_id")) == str(field_id):
                    num = _pylon_from_field(s.get("value"))
                    break
            if num:
                source = "field"
        if num is None:
            raw = json.loads(inc.get("raw_json") or "{}")
            num = _pylon_from_jira(inc.get("jira_key"), raw.get("jira_issue_id"))
            source = "jira" if num else None
        inc["pylon_ticket_number"] = num
        inc["pylon_ticket_source"] = source

    await asyncio.gather(*(resolve_pylon(i) for i in incidents))

    now = _utc_now()
    seen = {i["id"] for i in incidents}

    def upsert(rows):
        with db.get_conn() as conn:
            for i in rows:
                conn.execute("""
                    INSERT OR REPLACE INTO incidents
                        (id, sequential_id, title, url, status, kind, summary,
                         severity, severity_name, started_at, detected_at,
                         mitigated_at, resolved_at, created_at, updated_at,
                         slack_channel_id, jira_key, jira_url,
                         pylon_ticket_number, pylon_ticket_source,
                         commander_name, created_by_name, functionality,
                         raw_json, fetched_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """, (i["id"], i.get("sequential_id"), i.get("title"),
                      i.get("url"), i.get("status"), i.get("kind"),
                      i.get("summary"), i.get("severity"),
                      i.get("severity_name"), i.get("started_at"),
                      i.get("detected_at"), i.get("mitigated_at"),
                      i.get("resolved_at"), i.get("created_at"),
                      i.get("updated_at"), i.get("slack_channel_id"),
                      i.get("jira_key"), i.get("jira_url"),
                      i.get("pylon_ticket_number"),
                      i.get("pylon_ticket_source"),
                      i.get("commander_name"), i.get("created_by_name"),
                      i.get("functionality"), i.get("raw_json"), now))

    await asyncio.to_thread(upsert, incidents)

    refreshed = 0
    if complete:
        import rootly as _r
        terminal = [*_r.TERMINAL_STATUSES, *cfg["excluded_statuses"]]

        def leftovers():
            with db.get_conn() as conn:
                marks = ",".join("?" for _ in terminal)
                return [r["id"] for r in conn.execute(
                    f"SELECT id FROM incidents WHERE LOWER(COALESCE(status,''))"
                    f" NOT IN ({marks})", [t.lower() for t in terminal])
                    if r["id"] not in seen]

        for iid in await asyncio.to_thread(leftovers):
            inc = await rootly.get_incident(iid)
            if inc is None:
                continue        # deleted at source; the stored row stays as-is
            await resolve_pylon(inc)
            await asyncio.to_thread(upsert, [inc])
            refreshed += 1

    return {"found": len(incidents), "complete": complete,
            "leftovers_refreshed": refreshed}


# ── open set ──────────────────────────────────────────────────────────────────

def open_incidents() -> list[dict]:
    """The evaluation set: stored incidents still in a non-terminal,
    non-excluded status. One opinion, used by checks, AI, the API and CSV."""
    import rootly
    cfg = rules_config()
    excluded = [s.lower() for s in
                (*rootly.TERMINAL_STATUSES, *cfg["excluded_statuses"])]
    marks = ",".join("?" for _ in excluded)
    with db.get_conn() as conn:
        rows = conn.execute(f"""
            SELECT i.*, c.ir1, c.ir2, c.ir3, c.ir4, c.ir5, c.ir6, c.ir7,
                   c.reasons, c.checked_at AS rules_checked_at,
                   ai.ia1, ai.ia2, ai.ia4, ai.pending_on, ai.pending_party,
                   ai.ai_notes, ai.checked_at AS ai_checked_at,
                   -- The linked ticket's status as of OUR last Pylon sync —
                   -- NULL both when no ticket is linked and when the linked
                   -- ticket was never fetched; the UI separates those two.
                   pt.state AS pylon_state, pt.link AS pylon_link,
                   pt.custom_fields AS pylon_custom_fields,
                   -- Slack channel identity, mined from the stored payload so
                   -- the row can link straight into the incident's channel.
                   json_extract(i.raw_json, '$.slack_channel_name')
                       AS slack_channel_name,
                   json_extract(i.raw_json, '$.slack_channel_url')
                       AS slack_channel_url
            FROM incidents i
            LEFT JOIN incident_checks c ON c.incident_id = i.id
            LEFT JOIN incident_ai    ai ON ai.incident_id = i.id
            LEFT JOIN tickets pt ON pt.number = i.pylon_ticket_number
                                 AND pt.deleted_at IS NULL
            WHERE LOWER(COALESCE(i.status,'')) NOT IN ({marks})
            ORDER BY i.sequential_id DESC
        """, excluded).fetchall()
        return [dict(r) for r in rows]


# ── deterministic IR checks ───────────────────────────────────────────────────

def _severity_bucket(slug: str | None) -> str:
    s = (slug or "").lower()
    for bucket in ("sev1", "sev2"):
        if s.startswith(bucket):
            return bucket
    return "default"


def _last_activity(inc: dict) -> datetime | None:
    """Best available "someone touched this": the Slack channel's last message
    (Rootly mirrors it as slack_last_message_ts), else updated_at."""
    raw = {}
    try:
        raw = json.loads(inc.get("raw_json") or "{}")
    except json.JSONDecodeError:
        pass
    ts = raw.get("slack_last_message_ts")
    if ts:
        try:
            return datetime.fromtimestamp(float(str(ts).split(".")[0] + "."
                                          + (str(ts).split(".") + ["0"])[1]),
                                          tz=timezone.utc)
        except (ValueError, OSError):
            pass
    return _parse_ts(inc.get("updated_at"))


def _stage_entered(inc: dict) -> datetime | None:
    """When the incident last moved lifecycle stage — the freshest of its
    stage timestamps. An approximation of "entered current status"; the
    reason string says so."""
    stamps = [_parse_ts(inc.get(k)) for k in
              ("mitigated_at", "started_at", "detected_at", "created_at")]
    stamps = [s for s in stamps if s]
    return max(stamps) if stamps else None


def evaluate_incident(inc: dict, cfg: dict, now: datetime) -> tuple[dict, dict]:
    """(verdicts, reasons) for one incident. Pure — no I/O, easy to pin."""
    v: dict = {}
    why: dict = {}

    if enabled("ir1", cfg):
        v["ir1"] = "Pass" if inc.get("severity") else "Fail"
        why["ir1"] = (f"Severity is {inc.get('severity_name') or inc.get('severity')}"
                      if inc.get("severity") else "No severity assigned")

    if enabled("ir2", cfg):
        # Role data is only judged when the payload exposes it — an instance
        # whose API omits assignments must read N/A, never Fail.
        raw = {}
        try:
            raw = json.loads(inc.get("raw_json") or "{}")
        except json.JSONDecodeError:
            pass
        roles = raw.get("incident_role_assignments")
        if inc.get("commander_name"):
            v["ir2"], why["ir2"] = "Pass", f"Commander: {inc['commander_name']}"
        elif roles is None:
            v["ir2"] = "N/A"
            why["ir2"] = "Role assignments not exposed by the Rootly API payload"
        else:
            data = roles.get("data") if isinstance(roles, dict) else roles
            filled = bool(data)
            v["ir2"] = "Pass" if filled else "Fail"
            why["ir2"] = ("Roles assigned" if filled
                          else "No roles assigned on the incident")

    if enabled("ir3", cfg):
        v["ir3"] = "Pass" if inc.get("jira_key") else "Fail"
        why["ir3"] = (f"Jira {inc['jira_key']}" if inc.get("jira_key")
                      else "No Jira issue linked")

    if enabled("ir4", cfg):
        num = inc.get("pylon_ticket_number")
        v["ir4"] = "Pass" if num else "Fail"
        why["ir4"] = (f"Pylon #{num} (via {inc.get('pylon_ticket_source')})"
                      if num else
                      "No Pylon ticket found — neither the custom field nor a "
                      "shared Jira issue names one")

    if enabled("ir5", cfg):
        # Cadence is an expectation on ACTIVE response only. A mitigated or
        # triaged incident needs no update rhythm — going quiet there is fine;
        # lingering there too long is IR6's finding, not a comms failure.
        status = (inc.get("status") or "").lower()
        hours = cfg["cadence_hours"].get(_severity_bucket(inc.get("severity")),
                                         cfg["cadence_hours"]["default"])
        last = _last_activity(inc)
        if status != "started":
            v["ir5"] = "N/A"
            why["ir5"] = (f"Cadence applies while actively worked (started) — "
                          f"'{status or 'unknown'}' needs no update rhythm")
        elif last is None:
            v["ir5"], why["ir5"] = "N/A", "No activity timestamp available"
        else:
            age = (now - last).total_seconds() / 3600
            v["ir5"] = "Pass" if age <= hours else "Fail"
            why["ir5"] = (f"Last activity {age:.1f}h ago "
                          f"(limit {hours}h for {_severity_bucket(inc.get('severity'))})")

    if enabled("ir6", cfg):
        entered = _stage_entered(inc)
        if entered is None:
            v["ir6"], why["ir6"] = "N/A", "No lifecycle timestamps available"
        else:
            age = (now - entered).total_seconds() / 3600
            v["ir6"] = "Pass" if age <= cfg["stuck_hours"] else "Fail"
            why["ir6"] = (f"~{age:.0f}h since the last lifecycle stage change "
                          f"(limit {cfg['stuck_hours']}h; approximated from "
                          f"stage timestamps)")

    if enabled("ir7", cfg):
        has = bool((inc.get("summary") or "").strip())
        v["ir7"] = "Pass" if has else "Fail"
        why["ir7"] = "Summary present" if has else "Summary is empty"

    return v, why


def run_rule_checks() -> dict:
    """Evaluate IR checks over the open set. Free — no lock, no run row."""
    cfg = rules_config()
    now = datetime.now(timezone.utc)
    ts = _utc_now()
    rows = open_incidents()
    with db.get_conn() as conn:
        for inc in rows:
            v, why = evaluate_incident(inc, cfg, now)
            conn.execute("""
                INSERT OR REPLACE INTO incident_checks
                    (incident_id, ir1, ir2, ir3, ir4, ir5, ir6, ir7,
                     reasons, checked_at)
                VALUES (?,?,?,?,?,?,?,?,?,?)
            """, (inc["id"], v.get("ir1"), v.get("ir2"), v.get("ir3"),
                  v.get("ir4"), v.get("ir5"), v.get("ir6"), v.get("ir7"),
                  json.dumps(why), ts))
    return {"checked": len(rows)}


# ── AI IA checks ──────────────────────────────────────────────────────────────

AI_SYSTEM = """You are a quality reviewer for incident management.
You are given one incident's structured record from Rootly and (when readable)
its Slack channel conversation — the working narrative of the response.
Judge only what the record shows; do not invent facts.

- ia1 summary_quality: does the incident summary genuinely describe what
  happened and its impact? "Good" needs substance, not a restated title.
  "N/A" only when there is no summary to judge.
- ia2 comms_quality: are the updates (Slack messages / timeline) substantive
  and regular for an incident of this severity? "N/A" when no conversation
  was readable.
- ia4 status_consistency: does the narrative contradict the Rootly status —
  e.g. the channel reads resolved while the incident is still in progress?
- pending_on: one short sentence naming who or what the incident is currently
  waiting on (a person, a team, a customer, a vendor, or "nothing — actively
  worked"). pending_party classifies it.
- notes: 2-3 sentences a team lead can act on. Plain text."""

AI_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "ia1":           {"type": "STRING", "enum": ["Good", "Poor", "N/A"]},
        "ia2":           {"type": "STRING", "enum": ["Good", "Poor", "N/A"]},
        "ia4":           {"type": "STRING",
                          "enum": ["Consistent", "Inconsistent", "N/A"]},
        "pending_on":    {"type": "STRING"},
        "pending_party": {"type": "STRING",
                          "enum": ["us", "customer", "engineering", "vendor",
                                   "none", "unclear"]},
        "notes":         {"type": "STRING"},
    },
    "required": ["ia1", "ia2", "ia4", "pending_on", "pending_party", "notes"],
}


def _slack_digest(messages: list[dict], names: dict,
                  groups: dict | None = None) -> str:
    """The channel narrative as plain text, oldest first, budget-capped from
    the END — the most recent messages are the ones that decide pending-on.

    Every Slack mention form is resolved to a human-readable name BEFORE the
    model reads it — raw ids otherwise surface verbatim in the coaching notes
    ("pending validation from S0B0X5BP3BK"): user (<@U…>/<@W…>, with or
    without a |label), usergroup (<!subteam^S…>), the broadcast keywords, and
    link markup. An unresolvable id keeps its id — wrong beats invented.
    """
    groups = groups or {}

    def _user(m):
        return "@" + (m.group(2) or names.get(m.group(1), m.group(1)))

    def _team(m):
        return m.group(2) or groups.get(m.group(1), m.group(1))

    def clean(text):
        text = re.sub(r"<@([UW][A-Z0-9]+)(?:\|([^>]+))?>", _user, text)
        text = re.sub(r"<!subteam\^(S[A-Z0-9]+)(?:\|([^>]+))?>", _team, text)
        text = re.sub(r"<!(here|channel|everyone)(?:\|[^>]*)?>", r"@\1", text)
        text = re.sub(r"<(https?://[^|>]+)\|([^>]+)>", r"\2", text)
        text = re.sub(r"<(https?://[^>]+)>", r"\1", text)
        return text

    lines = []
    for m in messages:
        text = (m.get("text") or "").strip()
        if not text:
            continue
        text = clean(text)
        uid = m.get("user") or ""
        who = (names.get(uid) or m.get("username")
               or (m.get("bot_profile") or {}).get("name") or uid or "bot")
        ts = m.get("ts") or ""
        try:
            when = datetime.fromtimestamp(float(ts), tz=timezone.utc)\
                .strftime("%m-%d %H:%M")
        except (TypeError, ValueError):
            when = ""
        lines.append(f"[{when}] {who}: {text}")
    out = "\n".join(lines)
    return out[-SLACK_TEXT_BUDGET:] if len(out) > SLACK_TEXT_BUDGET else out


def _fingerprint(inc: dict, slack_text: str) -> str:
    """Content hash gating the Gemini spend — unchanged content, no re-bill.
    Includes the prompt vocabulary version so a prompt change regrades once."""
    basis = json.dumps({
        "v": 1,
        "title": inc.get("title"), "status": inc.get("status"),
        "summary": inc.get("summary"), "severity": inc.get("severity"),
        "updated_at": inc.get("updated_at"),
        "jira": inc.get("jira_key"), "pylon": inc.get("pylon_ticket_number"),
        "slack": hashlib.sha256(slack_text.encode()).hexdigest(),
    }, sort_keys=True)
    return hashlib.sha256(basis.encode()).hexdigest()


def _ai_prompt(inc: dict, slack_text: str, slack_note: str) -> str:
    record = {k: inc.get(k) for k in
              ("sequential_id", "title", "status", "kind", "summary",
               "severity", "severity_name", "started_at", "detected_at",
               "mitigated_at", "created_at", "updated_at", "jira_key",
               "pylon_ticket_number")}
    return (f"INCIDENT RECORD (Rootly):\n{json.dumps(record, indent=1)}\n\n"
            f"SLACK CHANNEL CONVERSATION ({slack_note}):\n"
            f"{slack_text or '(none readable)'}")


async def _gather_slack(rows: list[dict]) -> dict:
    """channel_id-keyed {text, note}; unreadable channels degrade, never fail."""
    import slack
    names, groups = {}, {}
    try:
        names, groups = await slack.directory()
    except Exception:
        logger.exception("Slack directory unavailable; ids will show raw")
    out = {}
    sem = asyncio.Semaphore(4)

    async def one(inc):
        chan = inc.get("slack_channel_id")
        if not chan:
            out[inc["id"]] = {"text": "", "note": "no Slack channel on the incident"}
            return
        async with sem:
            try:
                msgs = await slack.channel_history(chan, limit=SLACK_MSG_LIMIT)
                out[inc["id"]] = {"text": _slack_digest(msgs, names, groups),
                                  "note": f"last {len(msgs)} messages"}
            except Exception as e:
                out[inc["id"]] = {"text": "",
                                  "note": f"channel unreadable: {str(e)[:120]}"}

    await asyncio.gather(*(one(i) for i in rows))
    return out


def _score_incidents(todo: list, stats) -> tuple[int, int, list]:
    """Gemini over each (incident, slack) pair; returns (scored, failed, errors)."""
    from qc_runner import _call_gemini
    now = _utc_now()
    scored, errors = 0, []

    def one(item):
        inc, s = item
        raw = _call_gemini(_ai_prompt(inc, s["text"], s["note"]), stats,
                           system=AI_SYSTEM, schema=AI_SCHEMA)
        return inc, s, json.loads(raw)

    with ThreadPoolExecutor(max_workers=min(len(todo), MAX_WORKERS)) as pool:
        futures = {pool.submit(one, item): item for item in todo}
        for fut in as_completed(futures):
            inc = futures[fut][0]
            try:
                inc, s, r = fut.result()
            except Exception as e:
                errors.append(f"#{inc.get('sequential_id')}: {str(e)[:160]}")
                continue
            with db.get_conn() as conn:
                conn.execute("""
                    INSERT OR REPLACE INTO incident_ai
                        (incident_id, ia1, ia2, ia4, pending_on, pending_party,
                         ai_notes, fingerprint, checked_at)
                    VALUES (?,?,?,?,?,?,?,?,?)
                """, (inc["id"], r.get("ia1"), r.get("ia2"), r.get("ia4"),
                      (r.get("pending_on") or "").strip()[:300],
                      r.get("pending_party"),
                      (r.get("notes") or "").strip()[:2000],
                      _fingerprint(inc, s["text"]), now))
            scored += 1
    return scored, len(errors), errors


async def run_qc(triggered_by: str, only_ids: list[str] | None = None,
                 force: bool = False) -> dict:
    """Rules + AI over the open set, recorded as one qc_runs row ('rootly').

    `only_ids` scopes the AI spend to a filtered subset (rules still sweep the
    whole open set — they are free). `force` bypasses the fingerprint gate for
    that subset: a scoped rerun exists to re-judge incidents someone doubts,
    and "skipped, unchanged" is exactly the answer they came to overrule.
    """
    from qc_runner import RunStats, get_vertex_client
    cfg = rules_config()

    await asyncio.to_thread(run_rule_checks)
    rows = open_incidents()
    if only_ids is not None:
        wanted = set(only_ids)
        rows = [r for r in rows if r["id"] in wanted]

    slack_by_id = await _gather_slack(rows)

    def stored_fps():
        with db.get_conn() as conn:
            return {r["incident_id"]: r["fingerprint"] for r in
                    conn.execute("SELECT incident_id, fingerprint"
                                 " FROM incident_ai")}
    fps = await asyncio.to_thread(stored_fps)

    ai_on = any(enabled(k, cfg) for k in ("ia1", "ia2", "ia3", "ia4"))
    todo = [(inc, slack_by_id[inc["id"]]) for inc in rows
            if ai_on and (force or fps.get(inc["id"])
                          != _fingerprint(inc, slack_by_id[inc["id"]]["text"]))]

    config = {"rootly_qc": True, "triggered_by": triggered_by,
              "open": len(rows), "ai_enabled": ai_on,
              "disabled_checks": cfg["disabled_checks"],
              **({"scoped_to": len(only_ids), "forced": force}
                 if only_ids is not None else {})}
    if not todo:
        def record_empty():
            with db.get_conn() as conn:
                cur = conn.execute("""
                    INSERT INTO qc_runs (date, triggered_by, started_at,
                        finished_at, status, total, scored, skipped, config_json)
                    VALUES (?,?,?,?, 'success', ?, 0, ?, ?)
                """, (RUN_LABEL, triggered_by, _utc_now(), _utc_now(),
                      len(rows), len(rows), json.dumps(config)))
                return cur.lastrowid
        run_id = await asyncio.to_thread(record_empty)
        return {"open": len(rows), "scored": 0, "skipped": len(rows),
                "run_id": run_id, "status": "success"}

    await asyncio.to_thread(get_vertex_client)   # fail fast on misconfiguration

    def record_start():
        with db.get_conn() as conn:
            cur = conn.execute("""
                INSERT INTO qc_runs (date, triggered_by, started_at, status,
                                     total, config_json)
                VALUES (?,?,?, 'running', ?, ?)
            """, (RUN_LABEL, triggered_by, _utc_now(), len(rows),
                  json.dumps(config)))
            return cur.lastrowid
    run_id = await asyncio.to_thread(record_start)

    stats = RunStats()
    scored, failed, errors = await asyncio.to_thread(
        _score_incidents, todo, stats)

    status = ("error" if scored == 0 and failed
              else "partial" if failed else "success")

    def record_end():
        with db.get_conn() as conn:
            conn.execute("""
                UPDATE qc_runs SET finished_at=?, status=?, scored=?, skipped=?,
                    model_used=?, prompt_tokens=?, output_tokens=?, cost_usd=?,
                    cached_tokens=?, thought_tokens=?, cost_estimated=?, error=?
                WHERE id=?
            """, (_utc_now(), status, scored, len(rows) - len(todo),
                  stats.model_summary(), stats.prompt_tokens,
                  stats.output_tokens, stats.cost_usd(), stats.cached_tokens,
                  stats.thought_tokens, 1 if stats.cost_is_estimated() else 0,
                  "; ".join(errors)[:1000] or None, run_id))
    await asyncio.to_thread(record_end)

    return {"open": len(rows), "scored": scored,
            "skipped": len(rows) - len(todo), "failed": failed,
            "run_id": run_id, "status": status, "cost_usd": stats.cost_usd()}


# ── overall grade ─────────────────────────────────────────────────────────────

def overall(inc: dict, cfg: dict | None = None) -> str | None:
    """Pass / Fail / Needs Review / None (pending) — same vocabulary as Pylon.

    Rules decide Fail; AI quality problems surface as Needs Review, never an
    automatic Fail — a poor summary deserves a human eye, not a red mark that
    argues with the responder mid-incident. ia3 (pending-on) is informational.
    """
    cfg = cfg or rules_config()
    rule_vals = [inc.get(k) for k in RULE_KEYS if enabled(k, cfg)]
    if not inc.get("rules_checked_at"):
        return None
    if any(val == "Fail" for val in rule_vals):
        return "Fail"
    ai_bad = ((enabled("ia1", cfg) and inc.get("ia1") == "Poor")
              or (enabled("ia2", cfg) and inc.get("ia2") == "Poor")
              or (enabled("ia4", cfg) and inc.get("ia4") == "Inconsistent"))
    return "Needs Review" if ai_bad else "Pass"


def _pylon_functionality_raw(custom_fields_json) -> str | None:
    """The linked ticket's functionality VALUE (raw slug). The field slug comes
    from the Rules mapping, never hardcoded; display goes through
    funcheck.canon — logic stays on the raw value, per the tags discipline."""
    import rules as qc_rules
    slug = qc_rules.field("functionality") or "functionalities"
    try:
        cf = json.loads(custom_fields_json or "{}")
    except (json.JSONDecodeError, TypeError):
        return None
    v = cf.get(slug)
    if isinstance(v, dict):
        v = v.get("value")
    return v or None


def annotate(rows: list[dict]) -> list[dict]:
    import funcheck
    cfg = rules_config()
    for inc in rows:
        inc["overall_result"] = overall(inc, cfg)
        try:
            inc["reasons"] = json.loads(inc.get("reasons") or "{}")
        except json.JSONDecodeError:
            inc["reasons"] = {}
        raw = _pylon_functionality_raw(inc.pop("pylon_custom_fields", None))
        inc["pylon_functionality_raw"] = raw
        inc["pylon_functionality"] = funcheck.canon("functionality", raw) \
            if raw else None
        # Rows fetched before the creator column existed still carry the
        # payload — read it rather than showing a blank until the next fetch.
        if not inc.get("created_by_name"):
            try:
                user = (json.loads(inc.get("raw_json") or "{}")
                        .get("user") or {}).get("data") or {}
                attrs = user.get("attributes") or {}
                inc["created_by_name"] = (attrs.get("full_name")
                                          or attrs.get("name"))
            except (json.JSONDecodeError, AttributeError):
                pass
        inc.pop("raw_json", None)
    return rows
