"""
The monthly Product Signals report: support-desk evidence for the product team,
generated inside the app, stored, and regenerable.

What the page is: case scenarios plus the ticket trails behind them — where
demand sits, what repeats, what support does by hand. What it is NOT, by
contract with its owner: a recommendations document. The generator never
writes "the product should…"; it surfaces scenario + evidence and stops.

Two layers, deliberately separate:

    numbers     computed here, deterministically, from the tickets table —
                demand mix, FAQ themes, hotspots, and four fixed evidence
                clusters (credential relays, console toggles, access asks,
                the month's loudest FAQ theme). Rerunning without new data
                reproduces them exactly.
    narrative   one Gemini call writes the short scenario paragraph on top of
                each cluster's evidence, under a schema and a no-prescriptions
                instruction. If the model is unavailable the report still
                generates, with a plain factual line instead of prose.

Each month's rendered HTML is stored in `product_reports`, one row per month;
regeneration replaces the row. The AI spend is recorded in qc_runs under
'report:<month>', next to everything else that bills Vertex.
"""

import html as html_mod
import json
import logging
import re
from datetime import datetime, timezone

import db
from funcheck import _require_month
from qc_runner import RunStats, _call_gemini, _cf_val, _html_text, _parse_response, _utc_now

logger = logging.getLogger(__name__)

RUN_LABEL_PREFIX = "report:"

NARRATIVE_SCHEMA = {
    "type": "ARRAY",
    "items": {
        "type": "OBJECT",
        "properties": {
            "key":      {"type": "STRING"},
            "title":    {"type": "STRING"},
            "scenario": {"type": "STRING"},
        },
        "required": ["key", "title", "scenario"],
    },
}

_NARRATIVE_SYSTEM = """You are a product analyst summarising support-ticket \
evidence for a product team. For each cluster you receive its pattern, stats, \
sample tickets and conversation excerpts. Return one object per cluster:
- title: at most 6 words, evocative but factual (e.g. "The re-authentication relay").
- scenario: ONE paragraph, at most 90 words, describing what happens and what \
it costs in human steps, grounded ONLY in the evidence given. Plain narrative \
present tense.
STRICT RULES: never recommend, propose, or imply what the product should do; \
never use words like should, could, opportunity, fix, automate, improve; never \
invent numbers or ticket details. Return ONLY a JSON array."""

# Legacy category slugs and the new curated names both map into these groups.
_DEMAND_GROUPS = [
    ("Questions & how-to (FAQ-shaped)", ("general",)),
    ("On-call / engineering escalations", ("oncall",)),
    ("Console ops done for customers", ("support_task", "support task",
                                        "workspace", "ws creation")),
    ("Alerts relayed onward", ("alerts_",)),
    ("Legal & template tasks", ("legal",)),
    ("Feature requests", ("feature",)),
    ("Outages", ("global",)),
]

_FAQ_THEMES = [
    ("User & access management", r"\buser\b|invite|remove|deactivat|access|permission|role\b|sso|okta|login"),
    ("Signature & signing flow", r"signat|sign[- ]|esign|docusign envelope|counter[- ]?sign"),
    ("Templates, workflows & intake", r"template|workflow|intake"),
    ("Metadata & fields", r"metadata|field(s)?\b|custom field"),
    ("Notifications & reminders", r"notif|reminder|alert email"),
    ("Reports & analytics", r"report|analytic|export|dashboard"),
    ("Integrations", r"integration|hubspot|slack|drive|sharepoint|dropbox|word|salesforce|sfdc"),
    ("Uploads, downloads & versions", r"upload|download|version|executed"),
]

_ESC = html_mod.escape


def _low(t: dict, f: str) -> str:
    return (t.get(f) or "").lower()


def _is_reauth(t):
    return bool(re.search(r"re-?auth|authenticat|authorization expir|credential",
                          _low(t, "title"))) \
        or _low(t, "category") == "alerts_automated_tray_sentry_slack"


def _is_toggle(t):
    return "feature_flag" in _low(t, "category") \
        or "feature flag" in _low(t, "category") \
        or _low(t, "functionality") in ("feature_flags", "others : feature flags")


def _is_access(t):
    return _low(t, "functionality") in (
        "invite_remove_users", "team_based_permissions",
        "access_control_contract_type_access_control_global",
        "email_domain_change") \
        or _low(t, "functionality").startswith("access control")


# The recurring patterns both the monthly report and the trend comparison
# count, defined once so the two can never disagree about what a pattern is.
CLUSTER_PATTERNS = [
    ("reauth", "Credential & re-authentication relays", _is_reauth),
    ("toggles", "Capabilities toggled by ticket", _is_toggle),
    ("access", "User & access changes by ticket", _is_access),
]


def _load(month: str) -> list[dict]:
    """The month's non-archived tickets with account names."""
    with db.get_conn() as conn:
        rows = conn.execute("""
            SELECT t.id, t.number, t.title, t.state, t.fetch_date, t.source,
                   t.customer_portal_visible, t.custom_fields, t.link,
                   t.assignee_name, a.name AS account
            FROM tickets t LEFT JOIN accounts a ON a.id = t.account_id
            WHERE t.fetch_date LIKE ? AND t.deleted_at IS NULL
              AND LOWER(COALESCE(t.state, '')) != 'archived'
            ORDER BY t.number
        """, (f"{_require_month(month)}-%",)).fetchall()
    out = []
    for r in rows:
        t = dict(r)
        cf = json.loads(t.pop("custom_fields") or "{}")
        t["category"] = (_cf_val(cf.get("request_category")) or "").strip()
        t["functionality"] = (_cf_val(cf.get("functionalities")) or "").strip()
        t["resolution_details"] = (_cf_val(cf.get("resolution_details")) or "").strip()
        t["resolution_category"] = (_cf_val(cf.get("resolution_category")) or "").strip()
        t["internal"] = (r["customer_portal_visible"] == 0) or (r["source"] == "manual")
        out.append(t)
    return out


def _excerpts(ticket_id: str, limit: int = 2) -> list[dict]:
    """Short conversation excerpts for narrative grounding and evidence."""
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT is_customer, is_private, author_name, message_html"
            " FROM messages WHERE ticket_id = ? ORDER BY timestamp LIMIT 6",
            (ticket_id,)).fetchall()
    out = []
    for m in rows:
        text = " ".join(_html_text(m["message_html"]).split())
        if not text or "has been assigned to this ticket" in text:
            continue
        who = "customer" if m["is_customer"] else "support"
        if m["is_private"]:
            who = "internal note"
        out.append({"who": who, "text": text[:220]})
        if len(out) >= limit:
            break
    return out


def _group_of(category: str) -> str:
    c = (category or "").lower()
    for name, prefixes in _DEMAND_GROUPS:
        if any(c.startswith(p) for p in prefixes):
            return name
    return "Other"


def _trail(tickets: list[dict], limit: int = 10) -> list[dict]:
    """Trail rows plus repeat markers for accounts that appear more than once."""
    seen: dict = {}
    for t in tickets:
        if t["account"]:
            seen[t["account"]] = seen.get(t["account"], 0) + 1
    rows = []
    nth: dict = {}
    for t in sorted(tickets, key=lambda t: t["fetch_date"])[:limit]:
        acct = t["account"]
        chip = ""
        if acct and seen[acct] > 1:
            nth[acct] = nth.get(acct, 0) + 1
            chip = f"{nth[acct]}× {acct.split()[0]}" if nth[acct] > 1 else ""
        rows.append({"number": t["number"], "date": t["fetch_date"][5:],
                     "title": (t["title"] or "(no title)")[:90],
                     "account": acct or "—", "chip": chip,
                     "tid": t["id"], "fdate": t["fetch_date"]})
    return rows


def build_data(month: str) -> dict:
    """Everything deterministic the report shows. No model involved."""
    month = _require_month(month)
    tickets = _load(month)

    demand = {}
    for t in tickets:
        demand[_group_of(t["category"])] = demand.get(_group_of(t["category"]), 0) + 1
    demand = sorted(demand.items(), key=lambda kv: -kv[1])

    faq = [t for t in tickets if (t["category"] or "").lower().startswith("general")]
    themes = []
    themed_ids = set()
    for name, pat in _FAQ_THEMES:
        hit = [t for t in faq if re.search(pat, (t["title"] or "").lower())
               and t["id"] not in themed_ids]
        themed_ids.update(t["id"] for t in hit)
        if hit:
            themes.append({"name": name, "n": len(hit),
                           "accounts": len({t["account"] for t in hit if t["account"]}),
                           "examples": [(t["number"], (t["title"] or "")[:70])
                                        for t in hit[:3]]})
    themes.sort(key=lambda t: -t["n"])
    untagged_faq = len(faq) - len(themed_ids)

    hot = {}
    for t in tickets:
        if t["functionality"]:
            hot[t["functionality"]] = hot.get(t["functionality"], 0) + 1
    hotspots = sorted(hot.items(), key=lambda kv: -kv[1])[:12]

    # The four fixed evidence clusters. Recipes, not discoveries: each is a
    # pattern the desk demonstrably produces, matched deterministically so two
    # generations of the same month agree.
    def cluster(key, name, pred):
        sel = [t for t in tickets if pred(t)]
        ex = []
        for t in sel[:3]:
            for e in _excerpts(t["id"]):
                e2 = {**e, "number": t["number"],
                      "tid": t["id"], "fdate": t["fetch_date"]}
                ex.append(e2)
        return {"key": key, "name": name, "tickets": len(sel),
                "accounts": len({t["account"] for t in sel if t["account"]}),
                "internal": sum(1 for t in sel if t["internal"]),
                "trail": _trail(sel), "excerpts": ex[:4],
                "_sel": sel}

    topfaq_pred = (lambda top: (lambda t: _low(t, "category").startswith("general")
                                and re.search(top, _low(t, "title"))))(
        dict(_FAQ_THEMES).get(themes[0]["name"], r"$^") if themes else r"$^")
    clusters = [c for c in (
        [cluster(key, name, pred) for key, name, pred in CLUSTER_PATTERNS]
        + [cluster("topfaq", f"Repeated questions: {themes[0]['name'].lower()}"
                   if themes else "Repeated questions", topfaq_pred)]
    ) if c["tickets"] >= 2]
    for c in clusters:
        del c["_sel"]

    return {
        "month": month,
        "total": len(tickets),
        "accounts": len({t["account"] for t in tickets if t["account"]}),
        "internal_pct": round(sum(t["internal"] for t in tickets) * 100 / len(tickets)) if tickets else 0,
        "console_ops": sum(1 for t in tickets
                           if _group_of(t["category"]) == "Console ops done for customers"),
        "demand": demand,
        "themes": themes,
        "untagged_faq": untagged_faq,
        "faq_total": len(faq),
        "hotspots": hotspots,
        "clusters": clusters,
    }


SUMMARY_SCHEMA = {
    "type": "ARRAY",
    "items": {
        "type": "OBJECT",
        "properties": {"idx": {"type": "INTEGER"}, "summary": {"type": "STRING"}},
        "required": ["idx", "summary"],
    },
}

_SUMMARY_SYSTEM = """You summarise support tickets for a product-analytics \
evidence file. For each ticket return one object with its idx and a summary: \
ONE sentence, at most 160 characters, plain product language, stating what the \
requester needed and how it ended (or that it is still open), based only on \
the given conversation. No names, no judgement, no recommendations. Return \
ONLY a JSON array, one object per ticket, in input order."""

_SUMMARY_BATCH = 10


def _summary_fingerprint(t: dict, excerpts: list[dict]) -> str:
    import hashlib
    basis = "\x1f".join([t.get("title") or ""]
                        + [e["text"] for e in excerpts])
    return hashlib.sha256(basis.encode()).hexdigest()


def _summarize(tickets: list[dict], stats: RunStats) -> dict:
    """ticket_id -> one-line summary, generated only where content changed.

    Stored in ticket_summaries with a content fingerprint, so regenerating a
    report re-bills nothing that did not change. A model failure leaves the
    affected summaries blank rather than failing the report — the CSV column
    is evidence support, not the report itself.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    from qc_runner import MAX_WORKERS

    material = []
    for t in tickets:
        ex = _excerpts(t["id"], limit=4)
        material.append((t, ex, _summary_fingerprint(t, ex)))

    with db.get_conn() as conn:
        stored = {r["ticket_id"]: dict(r) for r in conn.execute(
            "SELECT ticket_id, summary, fingerprint FROM ticket_summaries"
            " WHERE fetch_date LIKE ?",
            (f"{tickets[0]['fetch_date'][:7]}-%",)).fetchall()} if tickets else {}

    out = {t["id"]: stored[t["id"]]["summary"] for t, _, fp in material
           if t["id"] in stored and stored[t["id"]]["fingerprint"] == fp}
    todo = [(t, ex, fp) for t, ex, fp in material
            if out.get(t["id"]) is None]
    if not todo:
        return out

    def block(t, ex, i):
        lines = [f"=== TICKET idx:{i} ===", f"Title: {t.get('title') or '(no title)'}",
                 f"Status: {t.get('state') or '—'}"]
        lines += [f"[{e['who']}] {e['text']}" for e in ex] or ["(no messages)"]
        return "\n".join(lines)

    def run_batch(batch):
        prompt = "\n\n".join(block(t, ex, i) for i, (t, ex, _) in enumerate(batch))
        results = _parse_response(_call_gemini(
            prompt, stats, system=_SUMMARY_SYSTEM, schema=SUMMARY_SCHEMA))
        return [str(r.get("summary") or "")[:200] for r in results]

    batches = [todo[i:i + _SUMMARY_BATCH]
               for i in range(0, len(todo), _SUMMARY_BATCH)]
    now = _utc_now()
    with ThreadPoolExecutor(max_workers=min(len(batches), MAX_WORKERS)) as pool:
        futures = {pool.submit(run_batch, b): b for b in batches}
        for fut in as_completed(futures):
            batch = futures[fut]
            try:
                summaries = fut.result()
            except Exception as e:
                logger.warning("Summary batch failed (%d tickets): %s",
                               len(batch), e)
                continue
            with db.get_conn() as conn:
                for (t, _ex, fp), s in zip(batch, summaries):
                    if not s:
                        continue
                    conn.execute("""
                        INSERT OR REPLACE INTO ticket_summaries
                            (ticket_id, fetch_date, summary, fingerprint,
                             summarized_at)
                        VALUES (?, ?, ?, ?, ?)
                    """, (t["id"], t["fetch_date"], s, fp, now))
                    out[t["id"]] = s
    return out


def evidence_rows(month: str) -> list[dict]:
    """The evidence CSV behind a month's report, one row per ticket.

    Functionality, category and the resolution fields are Pylon's own values,
    verbatim (blank when nobody filled them — that blankness is itself data);
    the AI summary is the stored one-liner from the last generation.
    """
    tickets = _load(month)
    with db.get_conn() as conn:
        summaries = {r["ticket_id"]: r["summary"] for r in conn.execute(
            "SELECT ticket_id, summary FROM ticket_summaries"
            " WHERE fetch_date LIKE ?", (f"{month}-%",)).fetchall()}
    return [{
        "ticket_id": t["number"],
        "title": t["title"] or "",
        "account": t["account"] or "",
        "assignee": t["assignee_name"] or "",
        "status": t["state"] or "",
        "date": t["fetch_date"],
        "functionality": t["functionality"],
        "request_category": t["category"],
        "ai_summary": summaries.get(t["id"]) or "",
        "resolution_details": t["resolution_details"],
        "resolution_category": t["resolution_category"],
        "link": t["link"] or "",
    } for t in tickets]


SUGGEST_SCHEMA = {
    "type": "ARRAY",
    "items": {
        "type": "OBJECT",
        "properties": {
            "title":      {"type": "STRING"},
            "suggestion": {"type": "STRING"},
            "evidence":   {"type": "STRING"},
        },
        "required": ["title", "suggestion", "evidence"],
    },
}

_SUGGEST_SYSTEM = """You are a product analyst. From the support-desk evidence \
given (demand mix, FAQ themes, evidence clusters with ticket trails), propose \
3 to 5 product enhancements that would plausibly reduce support-ticket volume.
For each: title (max 8 words), suggestion (ONE paragraph, max 70 words, \
concrete about what changes for the user), evidence (one line citing the \
cluster names and ticket numbers it is grounded in, e.g. "Credential relays — \
#73964, #76121"). Ground every suggestion in the given evidence only; never \
invent tickets or numbers. Return ONLY a JSON array, strongest first."""


def _suggestions(data: dict, sug_stats: RunStats) -> tuple[list, str]:
    """(suggestions, model name) — the one deliberately opinionated section.

    Isolated in its own call with its own stats so the page can name exactly
    which model wrote the opinions. Everything else on the page stays
    descriptive; when the model is unavailable the section is simply absent.
    """
    payload = {"demand": data["demand"], "themes": data["themes"],
               "clusters": [{k: v for k, v in c.items() if k != "excerpts"}
                            for c in data["clusters"]]}
    try:
        raw = _call_gemini(json.dumps(payload), sug_stats,
                           system=_SUGGEST_SYSTEM, schema=SUGGEST_SCHEMA)
        out = []
        for r in _parse_response(raw)[:5]:
            if r.get("title") and r.get("suggestion"):
                out.append({"title": str(r["title"])[:80],
                            "suggestion": str(r["suggestion"])[:600],
                            "evidence": str(r.get("evidence") or "")[:200]})
        return out, ", ".join(sorted(sug_stats.models)) or "unknown model"
    except Exception as e:
        logger.warning("AI suggestions unavailable, section omitted: %s", e)
        return [], ""


def _narratives(clusters: list[dict], stats: RunStats) -> dict:
    """key -> {title, scenario} from one model call; {} when unavailable."""
    if not clusters:
        return {}
    payload = [{k: v for k, v in c.items() if k != "trail"} | {"trail": c["trail"][:6]}
               for c in clusters]
    try:
        raw = _call_gemini(json.dumps(payload), stats,
                           system=_NARRATIVE_SYSTEM, schema=NARRATIVE_SCHEMA)
        out = {}
        for r in _parse_response(raw):
            if r.get("key") and r.get("scenario"):
                out[r["key"]] = {"title": str(r.get("title") or "")[:60],
                                 "scenario": str(r["scenario"])[:700]}
        return out
    except Exception as e:
        logger.warning("Report narrative unavailable, using factual lines: %s", e)
        return {}


# ── chat with the month's evidence ────────────────────────────────────────────

CHAT_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "answer":         {"type": "STRING"},
        "ticket_numbers": {"type": "ARRAY", "items": {"type": "INTEGER"}},
        "breakdown": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "label":          {"type": "STRING"},
                    "ticket_numbers": {"type": "ARRAY",
                                       "items": {"type": "INTEGER"}},
                },
                "required": ["label", "ticket_numbers"],
            },
        },
    },
    "required": ["answer", "ticket_numbers"],
}

_CHAT_SYSTEM_PREFIX = """You answer a product manager's questions about one \
month of support-ticket evidence. The manager is reading the report BUILT FROM \
this data, so they will use the report's own labels and numbers: the `group` \
column below is exactly the report's "Where the demand sits" chart (e.g. \
"Questions & how-to (FAQ-shaped)"), and GROUP COUNTS restates that chart. When \
the manager quotes a number (say 113), it is almost always one of those counts.

Rules:
- Use ONLY the data below. If it cannot answer the question, say so plainly \
and name the nearest thing it CAN answer.
- For ANY question that filters, lists or counts tickets: put EVERY matching \
ticket number in ticket_numbers — all matches, never a sample — and write the \
count in `answer` as the literal placeholder {count}; it is replaced with the \
verified count. Never write a numeric count yourself.
- For a question that asks to classify, break down, or segment tickets \
("deep classify the FAQs", "split by functionality"): ALSO fill `breakdown` — \
one entry per segment, label + every ticket number in that segment. Segments \
must partition the QUESTION'S set exactly: every member satisfies the \
question's filter, each ticket appears in exactly one segment, and nothing \
outside the filter sneaks in. Each segment's count is computed from its \
numbers, so never write per-segment numbers in prose. Keep `answer` to one or \
two framing sentences.
- Keep answers short and in plain product language.
- functionality/category values mix legacy slugs (salesforce_sfdc) with \
catalog names ('Integrations : Salesforce (SFDC)'); treat matching ones as \
the same when filtering. A catalog group is the text before ' : '.
- When the question names a functionality area ('Contract Creation', \
'Integrations', 'Workflow Manager'): match STRICTLY by the functionality \
column — the group prefix before ' : ', or a legacy slug that plainly means \
an item of that group. Topical similarity is NOT a match: a signature or \
Salesforce ticket does not "relate to Contract Creation" unless its tag says \
so. When strict matching yields few or none, say so and note that many \
tickets carry legacy or empty functionality tags rather than inventing \
matches. A subset can never exceed the set it is drawn from.

Columns: number|title|account|assignee|status|date|group|functionality|\
category|resolution_category|summary
"""

# Serialised evidence per month, cached in-process: the table is the expensive
# part of every question, and it only changes when the month's data does.
# Keeping it byte-identical across calls also lets Vertex prefix-cache it, so
# repeat questions bill mostly cached tokens.
_chat_ctx_cache: dict = {}


def _chat_context(month: str) -> tuple[str, dict]:
    rows = evidence_rows(month)
    cached = _chat_ctx_cache.get(month)
    if cached and cached[0] == len(rows):
        return cached[1], cached[2]

    def clean(value, n):
        return str(value or "").replace("|", "/").replace("\n", " ")[:n]

    lines = []
    by_number: dict = {}
    groups: dict = {}
    for r in rows:
        by_number[r["ticket_id"]] = r
        # The report's own grouping rides on every row, so the manager's
        # on-page vocabulary ("Questions & how-to (FAQ-shaped)") is directly
        # filterable rather than something the model must reverse-engineer.
        group = _group_of(r["request_category"])
        groups[group] = groups.get(group, 0) + 1
        lines.append("|".join([
            str(r["ticket_id"]), clean(r["title"], 70),
            clean(r["account"], 30), clean(r["assignee"], 25),
            clean(r["status"], 20), r["date"], group,
            clean(r["functionality"], 60), clean(r["request_category"], 50),
            clean(r["resolution_category"], 40), clean(r["ai_summary"], 170),
        ]))
    header = ("GROUP COUNTS (the report's demand chart):\n"
              + "\n".join(f"  {g}: {n}" for g, n in
                          sorted(groups.items(), key=lambda kv: -kv[1]))
              + f"\n  TOTAL: {len(rows)}\n\nTICKETS:\n")
    text = header + "\n".join(lines)
    _chat_ctx_cache[month] = (len(rows), text, by_number)
    return text, by_number


def chat(month: str, question: str, history: list | None = None,
         triggered_by: str = "chat") -> dict:
    """One question over the month's evidence table.

    Counts are computed here, never trusted from the model: it must return the
    matching ticket numbers, invalid or hallucinated ones are dropped against
    the real table, and `{count}` in its prose is substituted with the length
    of what survived. Every call's spend is returned to the caller and filed
    in qc_runs under 'chat:<month>'.
    """
    month = _require_month(month)
    question = str(question or "").strip()[:2000]
    if not question:
        raise ValueError("Ask a question")
    ctx, by_number = _chat_context(month)
    if not by_number:
        raise ValueError("No tickets for this month — generate its report first")

    parts = []
    for h in (history or [])[-6:]:
        q = str((h or {}).get("q") or "").strip()[:500]
        a = str((h or {}).get("a") or "").strip()[:700]
        if q:
            parts.append(f"Q: {q}\nA: {a}")
    convo = ("Previous conversation:\n" + "\n\n".join(parts) + "\n\n"
             if parts else "")
    prompt = f"{convo}Current question: {question}"

    stats = RunStats()
    # 32k output: 2.5's thinking tokens bill against this budget, and a
    # breakdown citing a hundred-odd ticket numbers needs the headroom — the
    # 8k default truncated mid-JSON.
    raw = _call_gemini(prompt, stats,
                       system=_CHAT_SYSTEM_PREFIX + ctx, schema=CHAT_SCHEMA,
                       max_output=32768)
    try:
        res = _parse_response(raw)[0]
    except ValueError as e:
        raise RuntimeError(
            "The model's answer came back malformed — ask a narrower "
            f"question, or ask again. ({e})")

    def valid_nums(raw) -> list:
        out, seen = [], set()
        for n in raw or []:
            try:
                n = int(n)
            except (TypeError, ValueError):
                continue
            if n in by_number and n not in seen:
                seen.add(n)
                out.append(n)
        return out

    nums = valid_nums(res.get("ticket_numbers"))
    # A breakdown's segments are verified the same way; the total is the union
    # of everything cited anywhere, so {count} can never disagree with the sum
    # of what is shown.
    breakdown = []
    union = dict.fromkeys(nums)
    for seg in (res.get("breakdown") or [])[:12]:
        seg_nums = valid_nums((seg or {}).get("ticket_numbers"))
        label = str((seg or {}).get("label") or "").strip()[:80]
        if not label or not seg_nums:
            continue
        union.update(dict.fromkeys(seg_nums))
        breakdown.append({"label": label, "count": len(seg_nums),
                          "numbers": seg_nums[:15],
                          "truncated": len(seg_nums) > 15})
    nums = list(union)
    count = len(nums)
    answer = str(res.get("answer") or "").replace("{count}", str(count)).strip()
    tickets = [{"number": n, "title": (by_number[n]["title"] or "")[:80],
                "link": by_number[n]["link"] or None,
                "date": by_number[n]["date"]}
               for n in nums[:50]]

    with db.get_conn() as conn:
        conn.execute("""
            INSERT INTO qc_runs (date, triggered_by, started_at, finished_at,
                                 status, total, scored, skipped, model_used,
                                 prompt_tokens, output_tokens, cost_usd,
                                 cached_tokens, thought_tokens, cost_estimated,
                                 config_json)
            VALUES (?, ?, ?, ?, 'success', 1, 1, 0, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (f"chat:{month}", triggered_by, _utc_now(), _utc_now(),
              stats.model_summary(), stats.prompt_tokens, stats.output_tokens,
              stats.cost_usd(), stats.cached_tokens, stats.thought_tokens,
              1 if stats.cost_is_estimated() else 0,
              json.dumps({"report_chat": True, "month": month,
                          "question": question[:200], "matches": count})))

    return {"answer": answer, "count": count, "tickets": tickets,
            "breakdown": breakdown,
            "truncated": count > len(tickets),
            "cost_usd": stats.cost_usd(),
            "model": ", ".join(sorted(stats.models)) or "unknown model",
            "tokens": {"prompt": stats.prompt_tokens,
                       "cached": stats.cached_tokens,
                       "output": stats.output_tokens}}


# ── multi-month comparison ────────────────────────────────────────────────────

def _days_fetched(month: str) -> int:
    """How many of the month's days the DAY pipeline actually fetched.

    The single most important number when comparing months: the database only
    holds what was fetched, so a barely-fetched month reads as a quiet month
    unless coverage is put next to the counts.
    """
    with db.get_conn() as conn:
        return conn.execute(
            "SELECT COUNT(*) AS n FROM fetch_log WHERE fetch_date LIKE ?",
            (f"{month}-%",)).fetchone()["n"]


def snapshot(month: str) -> dict:
    """One month's aggregates for trend comparison — no excerpts, no model."""
    tickets = _load(month)
    demand = {}
    funcs = {}
    for t in tickets:
        demand[_group_of(t["category"])] = demand.get(_group_of(t["category"]), 0) + 1
        if t["functionality"]:
            funcs[t["functionality"]] = funcs.get(t["functionality"], 0) + 1
    faq = [t for t in tickets if _low(t, "category").startswith("general")]
    themes = {}
    seen = set()
    for name, pat in _FAQ_THEMES:
        hit = [t for t in faq if re.search(pat, _low(t, "title"))
               and t["id"] not in seen]
        seen.update(t["id"] for t in hit)
        if hit:
            themes[name] = len(hit)
    patterns = {name: sum(1 for t in tickets if pred(t))
                for _key, name, pred in CLUSTER_PATTERNS}
    return {
        "month": month,
        "total": len(tickets),
        "days_fetched": _days_fetched(month),
        "accounts": len({t["account"] for t in tickets if t["account"]}),
        "internal_pct": round(sum(t["internal"] for t in tickets) * 100
                              / len(tickets)) if tickets else 0,
        "console_ops": sum(1 for t in tickets
                           if _group_of(t["category"]) == "Console ops done for customers"),
        "demand": demand,
        "functionalities": funcs,
        "themes": themes,
        "patterns": patterns,
    }


def _series(snaps: list[dict], field: str, top: int | None = None) -> list[dict]:
    """[{name, values[], delta}] across months, ordered by total volume."""
    names: dict = {}
    for s in snaps:
        for name, n in s[field].items():
            names[name] = names.get(name, 0) + n
    ordered = sorted(names, key=lambda n: -names[n])
    if top:
        ordered = ordered[:top]
    out = []
    for name in ordered:
        values = [s[field].get(name, 0) for s in snaps]
        out.append({"name": name, "values": values,
                    "delta": values[-1] - values[0]})
    return out


def compare_data(months: list[str]) -> dict:
    """The deterministic half of a trend comparison across 2+ months."""
    months = sorted({_require_month(m) for m in months})
    if len(months) < 2:
        raise ValueError("Pick at least two different months to compare")
    snaps = [snapshot(m) for m in months]

    kpis = [{"name": label, "values": [s[key] for s in snaps],
             "delta": snaps[-1][key] - snaps[0][key]}
            for label, key in (("Days day-fetched", "days_fetched"),
                               ("Tickets in scope", "total"),
                               ("Customer accounts", "accounts"),
                               ("Internal-origin %", "internal_pct"),
                               ("Console ops", "console_ops"))]

    # Coverage guard: a month the day pipeline barely fetched will read as a
    # quiet month. Named loudly rather than left to a footnote.
    fetched = {s["month"]: s["days_fetched"] for s in snaps}
    best = max(fetched.values(), default=0)
    sparse = sorted(m for m, n in fetched.items()
                    if n == 0 or (best and n < best * 0.6))
    coverage_warning = ""
    if best == 0:
        coverage_warning = ("None of these months was day-fetched — every "
                            "count comes from backlog backfills, so absolute "
                            "volumes are not comparable to a fetched month.")
    elif sparse:
        parts = ", ".join(f"{m} ({fetched[m]} day{'s' if fetched[m] != 1 else ''} fetched)"
                          for m in sparse)
        coverage_warning = (f"Uneven fetch coverage: {parts} vs "
                            f"{best} days for the best-covered month. Their "
                            "lower counts reflect missing fetches, not lower "
                            "demand — read shares and mixes, not absolutes.")

    funcs = _series(snaps, "functionalities", top=15)
    first, last = snaps[0]["functionalities"], snaps[-1]["functionalities"]
    all_funcs = _series(snaps, "functionalities")
    appeared = [f["name"] for f in all_funcs
                if last.get(f["name"]) and not any(
                    s["functionalities"].get(f["name"]) for s in snaps[:-1])]
    vanished = [f["name"] for f in all_funcs
                if first.get(f["name"], 0) >= 2 and not last.get(f["name"])]
    movers = [f for f in all_funcs if sum(f["values"]) >= 3]
    risers = sorted((f for f in movers if f["delta"] > 0),
                    key=lambda f: -f["delta"])[:5]
    fallers = sorted((f for f in movers if f["delta"] < 0),
                     key=lambda f: f["delta"])[:5]

    return {
        "months": months,
        "coverage_warning": coverage_warning,
        "kpis": kpis,
        "demand": _series(snaps, "demand"),
        "functionalities": funcs,
        "themes": _series(snaps, "themes"),
        "patterns": _series(snaps, "patterns"),
        "appeared": appeared[:8],
        "vanished": vanished[:8],
        "risers": [(f["name"], f["delta"]) for f in risers],
        "fallers": [(f["name"], f["delta"]) for f in fallers],
    }


TREND_SCHEMA = {
    "type": "ARRAY",
    "items": {
        "type": "OBJECT",
        "properties": {"section": {"type": "STRING"},
                       "insight": {"type": "STRING"}},
        "required": ["section", "insight"],
    },
}

_TREND_SYSTEM = """You are a product analyst reading month-over-month \
support-desk series. You receive JSON: per-month KPIs, demand groups, \
functionality counts, FAQ themes, recurring patterns, plus risers/fallers and \
appeared/vanished lists. Return a JSON array with EXACTLY these objects:
- {"section":"summary","insight": one paragraph, max 120 words, the overall \
trend story across the months}
- one object each for sections "demand", "functionalities", "themes", \
"patterns": insight max 40 words naming the most meaningful movement, with \
numbers.
STRICT RULES: describe trends only — never recommend or imply what the product \
should do; never use should/could/opportunity/fix/improve; only numbers that \
appear in the data; name months explicitly. CRITICAL: check the "Days \
day-fetched" KPI and coverage_warning first — when months were fetched \
unevenly, attribute volume differences to fetch coverage, compare mixes and \
shares instead of absolute counts, and say so."""


def _trend_narratives(cdata: dict, stats: RunStats) -> tuple[dict, str]:
    """section -> insight text, plus the model that wrote them."""
    try:
        raw = _call_gemini(json.dumps(cdata), stats,
                           system=_TREND_SYSTEM, schema=TREND_SCHEMA)
        out = {}
        for r in _parse_response(raw):
            if r.get("section") and r.get("insight"):
                out[str(r["section"]).strip().lower()] = str(r["insight"])[:900]
        return out, ", ".join(sorted(stats.models)) or "unknown model"
    except Exception as e:
        logger.warning("Trend narrative unavailable: %s", e)
        return {}, ""


# ── rendering ─────────────────────────────────────────────────────────────────

_CSS = """
:root{--bg:#F7F8F6;--surface:#FFFFFF;--surface2:#EFF2EF;--ink:#1E2428;--ink2:#45535A;
--muted:#6B7A7E;--line:#D9DFDC;--accent:#0F766E;--accent-ink:#0B5D57;--repeat:#B45309;
--repeat-soft:#F6EBDD;--quote:#F1F4F1;--bar:#23968C}
@media (prefers-color-scheme: dark){:root:not([data-theme="light"]){--bg:#14181B;
--surface:#1B2125;--surface2:#20272C;--ink:#E8ECEA;--ink2:#B9C4C4;--muted:#8C9B9D;
--line:#2C353A;--accent:#41BDB0;--accent-ink:#5ACCC0;--repeat:#E2A15E;
--repeat-soft:#33271A;--quote:#1F262A;--bar:#35A99E}}
:root[data-theme="dark"]{--bg:#14181B;--surface:#1B2125;--surface2:#20272C;--ink:#E8ECEA;
--ink2:#B9C4C4;--muted:#8C9B9D;--line:#2C353A;--accent:#41BDB0;--accent-ink:#5ACCC0;
--repeat:#E2A15E;--repeat-soft:#33271A;--quote:#1F262A;--bar:#35A99E}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
font-family:"Source Sans 3","Segoe UI",system-ui,sans-serif;font-size:16.5px;line-height:1.55}
.wrap{max-width:940px;margin:0 auto;padding:44px 26px 80px}
.kicker{font-family:"IBM Plex Mono",monospace;font-size:12px;letter-spacing:.14em;
text-transform:uppercase;color:var(--accent-ink);font-weight:600}
h1{font-family:"Spectral",Georgia,serif;font-size:clamp(32px,5vw,44px);font-weight:700;
line-height:1.08;margin:10px 0 12px}
.lede{font-size:18px;color:var(--ink2);max-width:64ch;margin:0}
.method{font-size:14px;color:var(--muted);max-width:72ch;border-left:3px solid var(--accent);
padding-left:12px;margin:16px 0 0}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px;margin:30px 0 6px}
.tile{background:var(--surface);border:1px solid var(--line);border-radius:6px;padding:14px 16px}
.tile b{display:block;font-family:"IBM Plex Mono",monospace;font-size:28px;font-weight:600}
.tile span{font-size:12.5px;color:var(--muted);display:block;margin-top:2px;line-height:1.35}
h2{font-family:"Spectral",Georgia,serif;font-weight:600;font-size:26px;margin:50px 0 6px}
.sub{color:var(--muted);font-size:15px;margin:0 0 18px;max-width:70ch}
.bars{display:grid;gap:9px;margin:18px 0 6px}
.brow{display:grid;grid-template-columns:240px 1fr;gap:14px;align-items:center}
.brow .lab{font-size:14px;color:var(--ink2);text-align:right;line-height:1.25}
.brow .track{display:flex;align-items:center;gap:9px;min-height:20px}
.brow .fill{height:17px;background:var(--bar);border-radius:0 3px 3px 0;min-width:2px}
.brow .val{font-family:"IBM Plex Mono",monospace;font-size:13px}
.brow .pct{color:var(--muted);font-size:12px}
@media(max-width:620px){.brow{grid-template-columns:1fr;gap:3px}.brow .lab{text-align:left}}
.case{background:var(--surface);border:1px solid var(--line);border-radius:8px;
padding:26px 28px 22px;margin:24px 0}
.case-head{display:flex;align-items:baseline;gap:14px;flex-wrap:wrap}
.case-id{font-family:"IBM Plex Mono",monospace;font-size:12px;font-weight:600;
letter-spacing:.12em;color:var(--accent-ink);border:1px solid var(--accent);
border-radius:3px;padding:2px 8px;white-space:nowrap}
.case h3{font-family:"Spectral",Georgia,serif;font-size:22px;font-weight:600;margin:0}
.cstats{display:flex;gap:8px;flex-wrap:wrap;margin:12px 0 4px}
.cstat{font-family:"IBM Plex Mono",monospace;font-size:12px;background:var(--surface2);
border:1px solid var(--line);border-radius:999px;padding:3px 11px;color:var(--ink2)}
.case p{font-size:15.5px;max-width:72ch}
.evlab{font-family:"IBM Plex Mono",monospace;font-size:11px;font-weight:600;
letter-spacing:.14em;text-transform:uppercase;color:var(--muted);margin:18px 0 8px}
blockquote{margin:10px 0;padding:11px 15px;background:var(--quote);
border-left:3px solid var(--accent);border-radius:0 6px 6px 0;font-size:14px;color:var(--ink2)}
blockquote .who{display:block;font-family:"IBM Plex Mono",monospace;font-size:11px;
color:var(--muted);margin-bottom:3px}
.trail{overflow-x:auto;margin:6px 0 2px}
.trail table{border-collapse:collapse;width:100%;font-size:13.5px;min-width:520px}
.trail th{font-family:"IBM Plex Mono",monospace;font-size:10.5px;font-weight:600;
text-transform:uppercase;letter-spacing:.1em;color:var(--muted);text-align:left;
padding:6px 10px;border-bottom:1px solid var(--line)}
.trail td{padding:6px 10px;border-bottom:1px solid var(--line);vertical-align:top}
.trail tr:last-child td{border-bottom:0}
.trail .num{font-family:"IBM Plex Mono",monospace;font-size:12.5px;white-space:nowrap;
color:var(--accent-ink);font-weight:500}
.trail .num a,blockquote .who a{color:var(--accent-ink);text-decoration:none;
border-bottom:1px dotted var(--accent)}
.trail .num a:hover,blockquote .who a:hover{border-bottom-style:solid}
.trail .dt{font-family:"IBM Plex Mono",monospace;font-size:12px;color:var(--muted);white-space:nowrap}
.chip{font-family:"IBM Plex Mono",monospace;font-size:10.5px;font-weight:600;
color:var(--repeat);background:var(--repeat-soft);border-radius:3px;padding:1px 6px;white-space:nowrap}
.ftable{overflow-x:auto;background:var(--surface);border:1px solid var(--line);border-radius:8px}
.ftable table{border-collapse:collapse;width:100%;font-size:14px;min-width:600px}
.ftable th{font-family:"IBM Plex Mono",monospace;font-size:10.5px;font-weight:600;
text-transform:uppercase;letter-spacing:.1em;color:var(--muted);text-align:left;
padding:10px 15px;border-bottom:1px solid var(--line);background:var(--surface2)}
.ftable td{padding:9px 15px;border-bottom:1px solid var(--line);vertical-align:top}
.ftable tr:last-child td{border-bottom:0}
.ftable .n{font-family:"IBM Plex Mono",monospace;white-space:nowrap}
.ftable .ex{color:var(--muted);font-size:13px}
.caveats{margin-top:50px;border-top:1px solid var(--line);padding-top:20px;
font-size:13.5px;color:var(--muted);max-width:76ch}
.caveats b{color:var(--ink2)}
.d{font-family:"IBM Plex Mono",monospace;font-size:11.5px;font-weight:600;white-space:nowrap}
.d-up{color:var(--repeat)}
.d-down{color:var(--accent-ink)}
.d-flat{color:var(--muted)}
.insight{border-left:3px solid var(--repeat);background:var(--quote);
border-radius:0 6px 6px 0;padding:10px 14px;font-size:14px;color:var(--ink2);
max-width:74ch;margin:10px 0 14px}
.insight .tag{font-family:"IBM Plex Mono",monospace;font-size:10px;font-weight:700;
letter-spacing:.12em;text-transform:uppercase;color:var(--repeat);display:block;margin-bottom:3px}
.chatbox{background:var(--surface);border:1px solid var(--line);border-radius:8px;
padding:20px 22px;margin:20px 0 0}
.chat-log{display:flex;flex-direction:column;gap:12px;margin-bottom:14px}
.chat-q{align-self:flex-end;background:var(--accent-soft);border:1px solid var(--accent);
border-radius:10px 10px 2px 10px;padding:9px 13px;font-size:14.5px;max-width:60ch}
.chat-a{align-self:flex-start;background:var(--quote);border:1px solid var(--line);
border-radius:10px 10px 10px 2px;padding:11px 14px;font-size:14.5px;max-width:70ch}
.chat-a .bd{margin-top:9px;border-top:1px solid var(--line);padding-top:8px;
display:grid;gap:6px}
.chat-a .bd-row{display:flex;align-items:baseline;gap:8px;flex-wrap:wrap;font-size:13.5px}
.chat-a .bd-row b{font-family:"IBM Plex Mono",monospace;font-size:12.5px;min-width:2ch}
.chat-a .tickets{display:flex;flex-wrap:wrap;gap:5px;margin-top:8px}
.chat-a .tickets a{font-family:"IBM Plex Mono",monospace;font-size:11px;
color:var(--accent-ink);border:1px solid var(--line);border-radius:3px;
padding:1px 6px;text-decoration:none;background:var(--surface)}
.chat-a .tickets a:hover{border-color:var(--accent)}
.chat-cost{font-family:"IBM Plex Mono",monospace;font-size:10.5px;color:var(--muted);
border-top:1px solid var(--line);margin-top:9px;padding-top:6px}
.chat-row{display:flex;gap:8px}
.chat-row input{flex:1;background:var(--bg);border:1px solid var(--line);
border-radius:8px;padding:9px 12px;font-size:14px;color:var(--ink);font-family:inherit}
.chat-row input:focus{outline:none;border-color:var(--accent)}
.chat-row button{background:var(--accent);border:1px solid var(--accent);
color:var(--bg);border-radius:8px;padding:9px 18px;font-size:13px;font-weight:700;
cursor:pointer;font-family:inherit}
.chat-row button:disabled{opacity:.5;cursor:default}
.chat-note{font-size:13px;color:var(--muted);margin-top:8px}
@media print{.chatbox,#chat-section{display:none}}
.covwarn{border:1.5px solid var(--repeat);background:var(--repeat-soft);
border-radius:8px;padding:12px 16px;font-size:14px;color:var(--ink2);
max-width:76ch;margin:20px 0 0}
.covwarn b{color:var(--repeat)}
.movers{display:flex;flex-wrap:wrap;gap:8px;margin:12px 0 4px}
.mover{font-family:"IBM Plex Mono",monospace;font-size:12px;background:var(--surface2);
border:1px solid var(--line);border-radius:999px;padding:3px 11px;color:var(--ink2)}
.toolbar{position:fixed;top:14px;right:16px;display:flex;gap:8px;z-index:50}
.toolbar a,.toolbar button{font-family:"IBM Plex Mono",monospace;font-size:11.5px;
font-weight:600;color:var(--accent-ink);background:var(--surface);
border:1px solid var(--accent);border-radius:999px;padding:5px 13px;
text-decoration:none;cursor:pointer}
.toolbar a:hover,.toolbar button:hover{background:var(--accent);color:var(--bg)}
.ai-sug{border:1.5px dashed var(--repeat);border-radius:8px;background:var(--surface);
padding:24px 28px;margin:26px 0 0}
.ai-banner{display:inline-block;font-family:"IBM Plex Mono",monospace;font-size:10.5px;
font-weight:700;letter-spacing:.14em;text-transform:uppercase;color:var(--repeat);
background:var(--repeat-soft);border-radius:3px;padding:3px 10px;margin-bottom:10px}
.ai-sug .disclaimer{font-size:13px;color:var(--muted);max-width:74ch;margin:0 0 16px}
.sug{border-top:1px solid var(--line);padding:14px 0 4px}
.sug h4{font-family:"Spectral",Georgia,serif;font-size:17px;font-weight:600;margin:0 0 6px}
.sug p{font-size:14.5px;color:var(--ink2);margin:0 0 6px;max-width:74ch}
.sug .ev{font-family:"IBM Plex Mono",monospace;font-size:11.5px;color:var(--muted)}
@media print{
  *{-webkit-print-color-adjust:exact;print-color-adjust:exact}
  .toolbar{display:none}
  :root,:root[data-theme="dark"]{--bg:#fff;--surface:#fff;--surface2:#F2F4F2;
  --ink:#1E2428;--ink2:#45535A;--muted:#6B7A7E;--line:#D9DFDC;--accent:#0F766E;
  --accent-ink:#0B5D57;--repeat:#B45309;--repeat-soft:#F6EBDD;--quote:#F1F4F1;
  --bar:#23968C}
  body{font-size:13px}
  .case,.ftable,.tiles,.sug{break-inside:avoid}
}
"""

_FONTS = ('<link rel="stylesheet" href="https://fonts.googleapis.com/css2?'
          'family=Spectral:wght@600;700&family=Source+Sans+3:wght@400;600;700&'
          'family=IBM+Plex+Mono:wght@400;500;600&display=swap">')


def _bars_html(data, total=None):
    if not data:
        return '<p class="sub">Nothing tagged this month.</p>'
    mx = max(n for _, n in data)
    rows = []
    for label, n in data:
        pct = f'<span class="pct">{round(n / total * 100)}%</span>' if total else ""
        rows.append(
            f'<div class="brow"><div class="lab">{_ESC(str(label))}</div>'
            f'<div class="track"><div class="fill" style="width:{n / mx * 100:.1f}%"></div>'
            f'<span class="val">{n}</span>{pct}</div></div>')
    return f'<div class="bars">{"".join(rows)}</div>'


def _ticket_href(base: str, tid: str, fdate: str) -> str:
    """Deep link into the dashboard's ticket sheet — conversation, checks and
    the Pylon link in one place, same as clicking a row on the Open tab.

    `base` is the configured application URL so a downloaded copy of the
    report still points somewhere real; relative when nothing is configured.
    """
    return f"{base}/?date={fdate}&ticket={tid}"


def _case_html(i, c, narrative, base=""):
    title = (narrative or {}).get("title") or c["name"]
    scenario = (narrative or {}).get("scenario") or (
        f"{c['tickets']} tickets from {c['accounts']} accounts this month match "
        f"this pattern; {c['internal']} of them started internally.")
    quotes = "".join(
        f'<blockquote><span class="who">{_ESC(e["who"])} · '
        f'<a href="{_ESC(_ticket_href(base, e["tid"], e["fdate"]))}"'
        f' target="_blank" rel="noopener">#{e["number"]}</a></span>'
        f'“{_ESC(e["text"])}”</blockquote>' for e in c["excerpts"])
    trail = "".join(
        f'<tr><td class="num">'
        f'<a href="{_ESC(_ticket_href(base, r["tid"], r["fdate"]))}"'
        f' target="_blank" rel="noopener" title="Open the QC ticket sheet">'
        f'#{r["number"]}</a></td>'
        f'<td class="dt">{_ESC(r["date"])}</td>'
        f'<td>{_ESC(r["title"])}</td><td>{_ESC(r["account"])}'
        + (f' <span class="chip">{_ESC(r["chip"])}</span>' if r["chip"] else "")
        + "</td></tr>"
        for r in c["trail"])
    return f"""
  <div class="case">
    <div class="case-head"><span class="case-id">CASE FILE {i:02d}</span>
      <h3>{_ESC(title)}</h3></div>
    <div class="cstats"><span class="cstat">{c["tickets"]} tickets</span>
      <span class="cstat">{c["accounts"]} accounts</span>
      <span class="cstat">{c["internal"]} internal-origin</span></div>
    <p>{_ESC(scenario)}</p>
    {f'<div class="evlab">Evidence — verbatim excerpts</div>{quotes}' if quotes else ""}
    <div class="evlab">Ticket trail</div>
    <div class="trail"><table>
      <tr><th>Ticket</th><th>Date</th><th>Title</th><th>Account</th></tr>{trail}
    </table></div>
  </div>"""


def _suggestions_html(suggestions: list, model: str) -> str:
    if not suggestions:
        return ""
    items = "".join(
        f'<div class="sug"><h4>{_ESC(s["title"])}</h4>'
        f'<p>{_ESC(s["suggestion"])}</p>'
        + (f'<div class="ev">Grounded in: {_ESC(s["evidence"])}</div>'
           if s["evidence"] else "")
        + "</div>"
        for s in suggestions)
    return f"""
  <h2>AI-suggested feature enhancements</h2>
  <div class="ai-sug">
    <span class="ai-banner">Purely AI-generated · {_ESC(model)}</span>
    <p class="disclaimer">Unlike everything above, this section is opinion:
    written entirely by <b>{_ESC(model)}</b> from the evidence on this page,
    not by the support team. Treat each item as a hypothesis to verify against
    the cited tickets, never as a committed ask.</p>
    {items}
  </div>"""


def render(data: dict, narratives: dict, generated_by: str,
           suggestions: list | None = None, suggestion_model: str = "") -> str:
    import vault

    base = (vault.get_setting("dashboard_base_url") or "").rstrip("/")
    month_name = datetime.strptime(data["month"], "%Y-%m").strftime("%B %Y")
    themes = "".join(
        f'<tr><td>{_ESC(t["name"])}</td><td class="n">{t["n"]}</td>'
        f'<td class="n">{t["accounts"]}</td>'
        f'<td class="ex">{" · ".join(f"#{n} {_ESC(x)}" for n, x in t["examples"])}</td></tr>'
        for t in data["themes"])
    if data["untagged_faq"]:
        themes += (f'<tr><td>Unclustered long tail</td>'
                   f'<td class="n">{data["untagged_faq"]}</td><td class="n">—</td>'
                   f'<td class="ex">one-off configuration and product questions</td></tr>')
    cases = "".join(_case_html(i + 1, c, narratives.get(c["key"]), base)
                    for i, c in enumerate(data["clusters"]))
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return f"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Product Signals — {_ESC(month_name)}</title>{_FONTS}
<style>{_CSS}</style></head><body>
<div class="toolbar">
  <a href="/api/reports/{data["month"]}/evidence.csv"
     title="Every ticket behind these numbers: tags, AI summary, resolution fields">↓ Evidence CSV</a>
  <a href="/reports/{data["month"]}?download=1" title="This page as a standalone HTML file">↓ HTML</a>
  <button type="button" onclick="window.print()"
          title="Print dialog — choose 'Save as PDF'">Save as PDF</button>
</div>
<div class="wrap">
  <div class="kicker">Support desk · evidence file · {_ESC(month_name)}</div>
  <h1>Product Signals</h1>
  <p class="lede">What {data["total"]} support tickets say about where the
  product carries support weight in {_ESC(month_name)} — repeated scenarios,
  with the ticket trails behind them.</p>
  <p class="method"><b>How to read this page:</b> case scenarios and evidence
  only — it deliberately makes no feature recommendations. Every claim cites
  ticket numbers you can open in Pylon. Archived tickets are excluded.</p>

  <div class="tiles">
    <div class="tile"><b>{data["total"]}</b><span>tickets in scope</span></div>
    <div class="tile"><b>{data["accounts"]}</b><span>distinct customer accounts</span></div>
    <div class="tile"><b>{data["internal_pct"]}%</b><span>originated internally — alerts, on-call relays, staff asks</span></div>
    <div class="tile"><b>{data["console_ops"]}</b><span>tickets where support performed a console action</span></div>
  </div>

  <h2>Where the demand sits</h2>
  <p class="sub">Every ticket, grouped by what the request actually was.</p>
  {_bars_html(data["demand"], data["total"])}

  {cases}

  <h2>What keeps being asked — the FAQ shape</h2>
  <p class="sub">{data["faq_total"]} tickets are tagged as general questions or
  FAQ queries. Clustered by what the title is about:</p>
  <div class="ftable"><table>
    <tr><th>Theme</th><th>Tickets</th><th>Accounts</th><th>Example asks</th></tr>
    {themes}
  </table></div>

  <h2>Functionality hotspots</h2>
  <p class="sub">Top tagged functionalities this month. Read with the tagging
  caveat below.</p>
  {_bars_html(data["hotspots"])}

  {_suggestions_html(suggestions or [], suggestion_model)}

  <div id="chat-section" hidden>
    <h2>Ask the data</h2>
    <p class="sub">Question the {data["total"]} tickets behind this report —
    "how many general FAQs are about Contract Creation?". Counts are computed
    from the tickets the model cites, never taken on its word.</p>
    <div class="chatbox">
      <div class="chat-log" id="chat-log"></div>
      <div class="chat-row">
        <input id="chat-input" type="text" maxlength="500"
               placeholder="Ask about this month's tickets…">
        <button id="chat-send" type="button">Ask</button>
      </div>
      <div class="chat-note">Each question is one model call over this month's
      evidence; its cost is shown under the answer and filed on the Runs page.
      The conversation is kept in this browser —
      <a href="#" id="chat-clear" style="color:var(--accent-ink)">clear it</a>.</div>
    </div>
  </div>
  <script>
  (function () {{
    var MONTH = "{data["month"]}";
    var STORE = "qc.reportchat." + MONTH;
    var hist = [];
    function el(id) {{ return document.getElementById(id); }}
    function esc(s) {{
      return String(s == null ? "" : s).replace(/&/g, "&amp;")
        .replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
    }}
    function push(cls, html) {{
      var d = document.createElement("div");
      d.className = cls;
      d.innerHTML = html;
      el("chat-log").appendChild(d);
      d.scrollIntoView({{ block: "nearest" }});
      return d;
    }}
    function fmtTokens(t) {{
      var k = function (n) {{ return n >= 1000 ? (n / 1000).toFixed(1) + "k" : n; }};
      return k(t.prompt) + " in" + (t.cached ? " (" + k(t.cached) + " cached)" : "")
        + " / " + k(t.output) + " out";
    }}
    function answerHtml(d) {{
      var chipHtml = (d.tickets || []).slice(0, 12).map(function (t) {{
        return '<a href="' + esc(t.link || "#") + '" target="_blank" rel="noopener" ' +
          'title="' + esc(t.title) + '">#' + t.number + "</a>";
      }}).join("");
      if (d.count > 12) chipHtml += '<span style="font-size:11px;color:var(--muted)">+' + (d.count - 12) + " more</span>";
      var bdHtml = "";
      if (d.breakdown && d.breakdown.length) {{
        bdHtml = '<div class="bd">' + d.breakdown.map(function (s) {{
          var nums = (s.numbers || []).slice(0, 6).map(function (n) {{
            return "#" + n;
          }}).join(" ");
          return '<div class="bd-row"><b>' + s.count + "</b>" + esc(s.label)
            + ' <span style="font-family:IBM Plex Mono,monospace;font-size:10.5px;color:var(--muted)">'
            + esc(nums) + (s.truncated ? " …" : "") + "</span></div>";
        }}).join("") + "</div>";
        chipHtml = "";  // the breakdown already cites; skip the flat chip row
      }}
      return esc(d.answer)
        + bdHtml
        + (chipHtml ? '<div class="tickets">' + chipHtml + "</div>" : "")
        + '<div class="chat-cost">≈ $' + Number(d.cost_usd).toFixed(4)
        + " · " + esc(d.model) + " · " + esc(fmtTokens(d.tokens)) + "</div>";
    }}
    // The conversation survives refresh and leave-and-return, per viewer, per
    // month. Storage can be unavailable (private windows) — every touch is
    // wrapped, and the chat simply doesn't persist there.
    function saveExchange(q, d) {{
      hist.push({{ q: q, a: d.answer }});
      try {{
        var saved = JSON.parse(localStorage.getItem(STORE) || "[]");
        saved.push({{ q: q, d: d }});
        localStorage.setItem(STORE, JSON.stringify(saved.slice(-15)));
      }} catch (e) {{ /* no storage — no persistence */ }}
    }}
    function restore() {{
      var saved = [];
      try {{ saved = JSON.parse(localStorage.getItem(STORE) || "[]"); }}
      catch (e) {{ return; }}
      saved.forEach(function (x) {{
        if (!x || !x.q || !x.d) return;
        push("chat-q", esc(x.q));
        push("chat-a", answerHtml(x.d));
        hist.push({{ q: x.q, a: x.d.answer }});
      }});
    }}
    fetch("/api/me").then(function (r) {{ return r.ok ? r.json() : null; }})
      .then(function (me) {{
        if (me && me.can_run_qc) {{
          el("chat-section").hidden = false;
          restore();
        }}
      }})
      .catch(function () {{ /* downloaded copy — no app, no chat */ }});
    async function send() {{
      var q = el("chat-input").value.trim();
      if (!q) return;
      el("chat-input").value = "";
      el("chat-send").disabled = true;
      push("chat-q", esc(q));
      var thinking = push("chat-a", "…");
      try {{
        var r = await fetch("/api/reports/" + MONTH + "/chat", {{
          method: "POST",
          headers: {{ "Content-Type": "application/json" }},
          body: JSON.stringify({{ question: q, history: hist.slice(-6) }}),
        }});
        var d = await r.json();
        if (!r.ok) throw new Error(d.detail || r.statusText);
        thinking.innerHTML = answerHtml(d);
        saveExchange(q, d);
      }} catch (e) {{
        thinking.innerHTML = '<span style="color:var(--repeat)">' + esc(e.message) + "</span>";
      }} finally {{
        el("chat-send").disabled = false;
        el("chat-input").focus();
      }}
    }}
    el("chat-send").addEventListener("click", send);
    el("chat-input").addEventListener("keydown", function (e) {{
      if (e.key === "Enter") send();
    }});
    el("chat-clear").addEventListener("click", function (e) {{
      e.preventDefault();
      try {{ localStorage.removeItem(STORE); }} catch (err) {{ }}
      el("chat-log").innerHTML = "";
      hist = [];
    }});
  }})();
  </script>

  <div class="caveats"><b>Method &amp; caveats</b> · Generated {now} by
  {_ESC(generated_by)} from the Pylon QC database. Counts are tickets, not
  customers or revenue. Groupings trust the tags; the tag-accuracy audit runs
  in the QC app's Functionality Check tab. Narrative paragraphs are
  model-written from the cited evidence and constrained to describe, never to
  recommend; numbers and trails are computed directly from the data.</div>
</div></body></html>"""


def _mn(month: str) -> str:
    return datetime.strptime(month, "%Y-%m").strftime("%b %Y")


def _delta_chip(delta) -> str:
    if delta > 0:
        return f'<span class="d d-up">▲ {delta}</span>'
    if delta < 0:
        return f'<span class="d d-down">▼ {abs(delta)}</span>'
    return '<span class="d d-flat">＝</span>'


def _trend_table(series: list[dict], months: list[str]) -> str:
    if not series:
        return '<p class="sub">Nothing tagged in these months.</p>'
    head = "".join(f'<th style="text-align:right">{_ESC(_mn(m))}</th>'
                   for m in months)
    rows = "".join(
        f'<tr><td>{_ESC(s["name"])}</td>'
        + "".join(f'<td class="n" style="text-align:right">{v or "·"}</td>'
                  for v in s["values"])
        + f'<td style="text-align:right">{_delta_chip(s["delta"])}</td></tr>'
        for s in series)
    return (f'<div class="ftable"><table><tr><th>&nbsp;</th>{head}'
            f'<th style="text-align:right">Δ</th></tr>{rows}</table></div>')


def _insight_html(narratives: dict, section: str) -> str:
    text = narratives.get(section)
    if not text:
        return ""
    return (f'<div class="insight"><span class="tag">AI trend read</span>'
            f'{_ESC(text)}</div>')


def render_compare(cdata: dict, narratives: dict, model: str,
                   generated_by: str) -> str:
    months = cdata["months"]
    label = " · ".join(_mn(m) for m in months)
    csv_links = " · ".join(
        f'<a href="/api/reports/{m}/evidence.csv">{_ESC(_mn(m))}</a>'
        for m in months)
    movers = ""
    if cdata["risers"] or cdata["fallers"] or cdata["appeared"] or cdata["vanished"]:
        chips = []
        chips += [f'<span class="mover">▲ {_ESC(n)} +{d}</span>'
                  for n, d in cdata["risers"]]
        chips += [f'<span class="mover">▼ {_ESC(n)} {d}</span>'
                  for n, d in cdata["fallers"]]
        chips += [f'<span class="mover">new: {_ESC(n)}</span>'
                  for n in cdata["appeared"]]
        chips += [f'<span class="mover">gone quiet: {_ESC(n)}</span>'
                  for n in cdata["vanished"]]
        movers = f'<div class="movers">{"".join(chips)}</div>'

    summary_block = ""
    if narratives.get("summary"):
        summary_block = f"""
  <h2>AI trend analysis</h2>
  <div class="ai-sug">
    <span class="ai-banner">AI-written · {_ESC(model)}</span>
    <p class="disclaimer">The narrative on this page is written entirely by
    <b>{_ESC(model)}</b> from the month-over-month numbers below — descriptive
    only, never a recommendation. The numbers themselves are computed directly
    from the database.</p>
    <p style="font-size:15.5px;max-width:74ch">{_ESC(narratives["summary"])}</p>
  </div>"""

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    key = ",".join(months)
    return f"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Product Signals Trends — {_ESC(label)}</title>{_FONTS}
<style>{_CSS}</style></head><body>
<div class="toolbar">
  <a href="/reports/{key}?download=1" title="This page as a standalone HTML file">↓ HTML</a>
  <button type="button" onclick="window.print()"
          title="Print dialog — choose 'Save as PDF'">Save as PDF</button>
</div>
<div class="wrap">
  <div class="kicker">Support desk · trend comparison · {_ESC(label)}</div>
  <h1>Product Signals — Trends</h1>
  <p class="lede">How support demand moved across {len(months)} months —
  functionalities, categories and recurring patterns, side by side.</p>
  <p class="method"><b>How to read this page:</b> every table is computed from
  the database; Δ compares the last month against the first. Per-month
  evidence CSVs: {csv_links}.</p>

  {f'<div class="covwarn"><b>Coverage warning ·</b> {_ESC(cdata["coverage_warning"])}</div>'
   if cdata.get("coverage_warning") else ""}

  {summary_block}

  <h2>The month at a glance, side by side</h2>
  {_insight_html(narratives, "demand")}
  {_trend_table(cdata["kpis"], months)}

  <h2>Where the demand sits</h2>
  {_trend_table(cdata["demand"], months)}

  <h2>Functionality trends</h2>
  {_insight_html(narratives, "functionalities")}
  {_trend_table(cdata["functionalities"], months)}
  {movers}

  <h2>FAQ theme trends</h2>
  {_insight_html(narratives, "themes")}
  {_trend_table(cdata["themes"], months)}

  <h2>Recurring pattern trends</h2>
  {_insight_html(narratives, "patterns")}
  {_trend_table(cdata["patterns"], months)}

  <div class="caveats"><b>Method &amp; caveats</b> · Generated {now} by
  {_ESC(generated_by)} from the Pylon QC database. Counts are tickets, not
  customers or revenue; archived tickets excluded; groupings trust the tags
  (audited in the Functionality Check tab). “·” means zero that month. A month
  fetched less completely than another will read as a false decline — compare
  the tickets-in-scope row first.</div>
</div></body></html>"""


def generate_compare(months: list[str], triggered_by: str = "manual") -> dict:
    """Build, AI-narrate, render and store a multi-month trend report."""
    cdata = compare_data(months)
    key = ",".join(cdata["months"])
    stats = RunStats()
    narratives, model = _trend_narratives(cdata, stats)
    page = render_compare(cdata, narratives, model or "model unavailable",
                          triggered_by)
    now = _utc_now()
    with db.get_conn() as conn:
        conn.execute("""
            INSERT OR REPLACE INTO product_reports
                (month, html, generated_at, generated_by, tickets, cases)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (key, page, now, triggered_by,
              sum(k["values"][-1] for k in cdata["kpis"][:1]),
              len(cdata["months"])))
        conn.execute("""
            INSERT INTO qc_runs (date, triggered_by, started_at, finished_at,
                                 status, total, scored, skipped, model_used,
                                 prompt_tokens, output_tokens, cost_usd,
                                 cached_tokens, thought_tokens, cost_estimated,
                                 config_json)
            VALUES (?, ?, ?, ?, 'success', ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (f"{RUN_LABEL_PREFIX}{key}"[:80], triggered_by, now, _utc_now(),
              len(cdata["months"]), len(narratives), stats.model_summary(),
              stats.prompt_tokens, stats.output_tokens, stats.cost_usd(),
              stats.cached_tokens, stats.thought_tokens,
              1 if stats.cost_is_estimated() else 0,
              json.dumps({"trend_comparison": True, "months": cdata["months"],
                          "model": model})))
    return {"key": key, "months": cdata["months"],
            "ai_narrative": bool(narratives), "model": model,
            "cost_usd": stats.cost_usd(), "generated_at": now}


def available_months() -> list[dict]:
    """Months that have data, newest first — what the compare picker offers."""
    with db.get_conn() as conn:
        rows = conn.execute("""
            SELECT SUBSTR(fetch_date, 1, 7) AS month, COUNT(*) AS n
            FROM tickets
            WHERE deleted_at IS NULL
              AND LOWER(COALESCE(state, '')) != 'archived'
            GROUP BY SUBSTR(fetch_date, 1, 7) ORDER BY month DESC
        """).fetchall()
    return [{**dict(r), "days_fetched": _days_fetched(r["month"])}
            for r in rows]


# ── generate / store / list ───────────────────────────────────────────────────

def generate(month: str, triggered_by: str = "manual") -> dict:
    """Build, render and store the month's report, replacing any previous one."""
    month = _require_month(month)
    label = f"{RUN_LABEL_PREFIX}{month}"
    data = build_data(month)

    stats = RunStats()
    tickets = _load(month)
    summaries = _summarize(tickets, stats) if tickets else {}
    narratives = _narratives(data["clusters"], stats) if data["total"] else {}
    # Own stats object so the page can name exactly which model wrote the
    # opinion section; its tokens are folded into the run record below.
    sug_stats = RunStats()
    suggestions, sug_model = (_suggestions(data, sug_stats)
                              if data["total"] else ([], ""))

    page = render(data, narratives, triggered_by, suggestions, sug_model)
    now = _utc_now()
    with db.get_conn() as conn:
        conn.execute("""
            INSERT OR REPLACE INTO product_reports
                (month, html, generated_at, generated_by, tickets, cases)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (month, page, now, triggered_by, data["total"],
              len(data["clusters"])))
        conn.execute("""
            INSERT INTO qc_runs (date, triggered_by, started_at, finished_at,
                                 status, total, scored, skipped, model_used,
                                 prompt_tokens, output_tokens, cost_usd,
                                 cached_tokens, thought_tokens, cost_estimated,
                                 config_json)
            VALUES (?, ?, ?, ?, 'success', ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (label, triggered_by, now, _utc_now(), data["total"],
              len(narratives),
              ", ".join(filter(None, [stats.model_summary(),
                                      sug_stats.model_summary()])),
              stats.prompt_tokens + sug_stats.prompt_tokens,
              stats.output_tokens + sug_stats.output_tokens,
              round(stats.cost_usd() + sug_stats.cost_usd(), 6),
              stats.cached_tokens + sug_stats.cached_tokens,
              stats.thought_tokens + sug_stats.thought_tokens,
              1 if (stats.cost_is_estimated()
                    or sug_stats.cost_is_estimated()) else 0,
              json.dumps({"product_report": True, "month": month,
                          "narratives": len(narratives),
                          "summaries": len(summaries),
                          "suggestions": len(suggestions),
                          "suggestion_model": sug_model,
                          "clusters": [c["key"] for c in data["clusters"]]})))
    # The month's evidence just changed (fetches, summaries) — the chat
    # context must not serve a stale table whose row count happens to match.
    _chat_ctx_cache.pop(month, None)
    return {"month": month, "tickets": data["total"],
            "cases": len(data["clusters"]),
            "narratives": len(narratives),
            "summaries": len(summaries),
            "suggestions": len(suggestions),
            "suggestion_model": sug_model,
            "ai_narrative": bool(narratives),
            "cost_usd": round(stats.cost_usd() + sug_stats.cost_usd(), 6),
            "generated_at": now}


def list_reports() -> list[dict]:
    with db.get_conn() as conn:
        rows = conn.execute("""
            SELECT month, generated_at, generated_by, tickets, cases,
                   LENGTH(html) AS size
            FROM product_reports ORDER BY month DESC
        """).fetchall()
    return [dict(r) for r in rows]


def require_key(key: str) -> str:
    """A report key: one month, or a comma list of months (a comparison)."""
    parts = sorted({p.strip() for p in str(key).split(",") if p.strip()})
    for p in parts:
        _require_month(p)
    if not parts:
        raise ValueError("Empty report key")
    return ",".join(parts)


def get_html(key: str) -> str | None:
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT html FROM product_reports WHERE month = ?",
            (require_key(key),)).fetchone()
    return row["html"] if row else None
