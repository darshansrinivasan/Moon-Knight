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

    low = lambda t, f: (t[f] or "").lower()
    clusters = [c for c in [
        cluster("reauth", "Credential and re-authentication relays",
                lambda t: re.search(r"re-?auth|authenticat|authorization expir|credential",
                                    low(t, "title"))
                or low(t, "category") == "alerts_automated_tray_sentry_slack"),
        cluster("toggles", "Capabilities toggled by ticket",
                lambda t: "feature_flag" in low(t, "category")
                or "feature flag" in low(t, "category")
                or low(t, "functionality") in ("feature_flags", "others : feature flags")),
        cluster("access", "User and access changes by ticket",
                lambda t: low(t, "functionality") in (
                    "invite_remove_users", "team_based_permissions",
                    "access_control_contract_type_access_control_global",
                    "email_domain_change")
                or low(t, "functionality").startswith("access control")),
        cluster("topfaq", f"Repeated questions: {themes[0]['name'].lower()}"
                if themes else "Repeated questions",
                (lambda top: (lambda t: (t["category"] or "").lower().startswith("general")
                              and re.search(top, low(t, "title"))))(
                    dict(_FAQ_THEMES).get(themes[0]["name"], r"$^") if themes else r"$^")),
    ] if c["tickets"] >= 2]
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

  <div class="caveats"><b>Method &amp; caveats</b> · Generated {now} by
  {_ESC(generated_by)} from the Pylon QC database. Counts are tickets, not
  customers or revenue. Groupings trust the tags; the tag-accuracy audit runs
  in the QC app's Functionality Check tab. Narrative paragraphs are
  model-written from the cited evidence and constrained to describe, never to
  recommend; numbers and trails are computed directly from the data.</div>
</div></body></html>"""


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


def get_html(month: str) -> str | None:
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT html FROM product_reports WHERE month = ?",
            (_require_month(month),)).fetchone()
    return row["html"] if row else None
