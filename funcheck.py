"""
Functionality-tagging review: does each ticket's functionality and
request-category dropdown match what the conversation actually shows?

This is NOT part of QC and touches no grade. Its consumer is a product
analysis built on what people *selected*, which is only as good as the
selections — so the check reads each conversation and answers one question per
field: is the tag right, and if not, what should it be?

Ground rules the model is held to, and this module enforces after it answers:

    vocabulary   Suggestions must come from the existing dropdown options
                 whenever one fits. The option lists are the values observed
                 across every stored ticket (Pylon's field-definition API is
                 not always available; what people could select, they did
                 select). A suggestion outside the list is allowed but flagged
                 `*_suggestion_new` — a proposal to CREATE an option, which is
                 a product decision, never something to silently invent.
    one line     The note is one sentence saying why the tag looks wrong.
    frugality    Results are fingerprinted on the tag values, the conversation
                 and the vocabulary, so re-running a month re-bills only what
                 changed — the same discipline as QC scoring.

Runs are recorded in qc_runs under the label 'func:<month>' so their cost sits
on the Runs page next to everything else that spends money.
"""

import hashlib
import json
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

import db
from qc_runner import (
    BATCH_SIZE,
    MAX_MESSAGE_CHARS,
    MAX_TICKET_CHARS,
    MAX_WORKERS,
    RunStats,
    _call_gemini,
    _cf_val,
    _html_text,
    _parse_response,
    _utc_now,
    get_vertex_client,
)

logger = logging.getLogger(__name__)

RUN_LABEL_PREFIX = "func:"

FIELD_SLUGS = {"functionality": "functionalities",
               "category": "request_category"}

RESPONSE_SCHEMA = {
    "type": "ARRAY",
    "items": {
        "type": "OBJECT",
        "properties": {
            "idx":                     {"type": "INTEGER"},
            "functionality_ok":        {"type": "BOOLEAN"},
            "category_ok":             {"type": "BOOLEAN"},
            "note":                    {"type": "STRING"},
            "suggested_functionality": {"type": "STRING"},
            "suggested_category":      {"type": "STRING"},
        },
        "required": ["idx", "functionality_ok", "category_ok", "note",
                     "suggested_functionality", "suggested_category"],
    },
}


def _field_slug(name: str) -> str:
    """The Pylon slug for a field, honouring the Admin mapping like R1/R2 do."""
    import rules as qc_rules
    return qc_rules.field(name) or FIELD_SLUGS[
        "functionality" if name == "functionality" else "category"]


CATALOG_SETTINGS = {"functionality": "funcheck_functionalities_json",
                    "category": "funcheck_categories_json"}

# Pylon's API stores an option's VALUE (a slug like 'general_question') on the
# ticket, while its UI shows the LABEL ('General FAQ - How to questions').
# This map — synced from Pylon's own field definitions — is the bridge, and
# every surface that shows a tag translates through it so people read the
# words they chose in the dropdown, never the machine name behind it.
LABELS_SETTING = "pylon_option_labels_json"


def _json_setting(key: str, what: str):
    """A JSON vault setting, or None when absent or unparseable (logged).

    One definition of "load a JSON setting defensively" — options() and the
    label map used to each carry their own copy of this dance.
    """
    import vault
    raw = vault.get_raw_setting(key)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("Stored %s is not valid JSON — ignoring", what)
        return None


# The label map is on the hottest paths in the app (twice per ticket per load,
# six times per ticket per check run), so it is memoized like rules.current()
# rather than re-read from SQLite per call — that cost was ~12,000 connection
# cycles for one month's run. Writers must call invalidate_labels().
_labels_cache: dict | None = None
_labels_lock = __import__("threading").Lock()


def invalidate_labels() -> None:
    global _labels_cache
    with _labels_lock:
        _labels_cache = None


def option_labels() -> dict:
    """{'functionality': {value: label}, 'category': {...}} — cached."""
    global _labels_cache
    with _labels_lock:
        if _labels_cache is not None:
            return _labels_cache
    out = {"functionality": {}, "category": {}}
    data = _json_setting(LABELS_SETTING, "Pylon label map")
    if isinstance(data, dict):
        for key in out:
            m = data.get(key)
            if isinstance(m, dict):
                out[key] = {str(k): str(v) for k, v in m.items() if k and v}
    with _labels_lock:
        _labels_cache = out
    return out


def canon(kind: str, value: str | None) -> str:
    """A tag as its Pylon LABEL when the map knows it, verbatim otherwise."""
    value = (value or "").strip()
    if not value:
        return value
    return option_labels()[kind].get(value, value)


def clean_entries(entries: list) -> list:
    """The one definition of a valid catalog list, shared by the hand editor
    and the Pylon sync — two definitions is how a synced entry stops
    round-tripping through the Admin textarea."""
    cleaned, seen = [], set()
    for e in entries or []:
        e = " ".join(str(e or "").split())[:120]
        if not e or e.lower() in seen:
            continue
        seen.add(e.lower())
        cleaned.append(e)
    return cleaned[:1000]


def save_catalog(kind: str, entries: list, actor: str) -> list:
    """Clean, persist and audit one catalog list. Raises ValueError on empty."""
    import vault
    if kind not in CATALOG_SETTINGS:
        raise ValueError("list must be 'functionality' or 'category'")
    cleaned = clean_entries(entries)
    if not cleaned:
        raise ValueError("The list cannot be emptied — reset it to the "
                         "shipped catalog instead")
    vault.set_raw_setting(CATALOG_SETTINGS[kind], json.dumps(cleaned), actor)
    vault.audit(actor, "catalog.save", f"{kind} n={len(cleaned)}")
    return cleaned


def clear_catalog(kind: str, actor: str) -> None:
    """Reset one list to the shipped catalog AND drop its slice of the label
    map — otherwise a reset restores the vocabulary while tickets keep being
    translated to labels that vocabulary no longer contains, and there is no
    way back from a bad sync without editing the database."""
    import vault
    if kind not in CATALOG_SETTINGS:
        raise ValueError("list must be 'functionality' or 'category'")
    data = _json_setting(LABELS_SETTING, "Pylon label map")
    if isinstance(data, dict) and data.get(kind):
        data[kind] = {}
        vault.set_raw_settings({CATALOG_SETTINGS[kind]: "",
                                LABELS_SETTING: json.dumps(data)}, actor)
    else:
        vault.set_raw_setting(CATALOG_SETTINGS[kind], "", actor)
    invalidate_labels()
    vault.audit(actor, "catalog.reset", kind)


_SYNC_FIELDS = {"functionality": "functionality",
                "category": "request_category"}


async def sync_catalog_from_pylon() -> dict:
    """Pull the two fields' options from Pylon: labels become the catalog,
    and the value→label map becomes the translation every page reads through.

    Pylon is the source of truth for what the dropdowns offer — a catalog
    maintained by hand drifts the day someone edits an option in Pylon only.
    Field slugs resolve through the same Admin mapping `_tagged` reads with,
    so repointing a field moves the sync and the reader together. All four
    settings land in one transaction: a torn write here would translate tags
    into a vocabulary the catalog no longer contains.
    """
    import pylon
    import vault

    fields = {f["slug"]: f for f in await pylon.fetch_custom_fields()
              if f.get("slug")}
    labels: dict = {}
    lists: dict = {}
    for kind, mapping_name in _SYNC_FIELDS.items():
        slug = _field_slug(mapping_name)
        field = fields.get(slug)
        if not field:
            raise RuntimeError(f"Pylon no longer defines the '{slug}' field "
                               "(check Rules → Pylon fields)")
        opts = (field.get("select_metadata") or {}).get("options") or []
        labels[kind] = {
            o["slug"]: ((o.get("label") or "").strip() or o["slug"])
            for o in opts if o.get("slug")
        }
        lists[kind] = clean_entries(labels[kind].values())
        if not lists[kind]:
            raise RuntimeError(f"Pylon returned no options for '{slug}'")

    def persist():
        vault.set_raw_settings({
            LABELS_SETTING: json.dumps(labels),
            CATALOG_SETTINGS["functionality"]: json.dumps(lists["functionality"]),
            CATALOG_SETTINGS["category"]: json.dumps(lists["category"]),
        }, "pylon-sync")

    await asyncio.to_thread(persist)
    invalidate_labels()
    # Labels change what the report's evidence rows say, at the same row
    # count — the chat context must not serve the pre-sync spellings.
    import report
    report._chat_ctx_cache.clear()
    return {"functionality": len(lists["functionality"]),
            "category": len(lists["category"])}


def options() -> dict:
    """{'functionality': [...], 'category': [...]} — the canonical vocabulary.

    The curated lists mirrored in Pylon's dropdowns — not the values observed
    on tickets. Observed values are what people DID select; the catalog is
    what they MAY select, and a suggestion must come from the latter. Older
    tickets carry legacy slugs ('salesforce_sfdc') for what the catalog names
    in full; the prompt tells the model to treat an obvious legacy spelling
    as the same tag.

    `funcheck_catalog.py` ships the defaults; Admin → Tagging catalog stores
    an override per list in the vault, because tending the vocabulary is the
    product team's recurring job and must not wait for a deploy. A stored
    override that fails to parse is logged and ignored — a broken edit must
    degrade to the shipped list, never to an empty vocabulary.
    """
    import funcheck_catalog
    out = {"functionality": list(funcheck_catalog.FUNCTIONALITIES),
           "category": list(funcheck_catalog.REQUEST_CATEGORIES)}
    for key, setting in CATALOG_SETTINGS.items():
        data = _json_setting(setting, f"{key} catalog override")
        if (isinstance(data, list) and data
                and all(isinstance(x, str) and x.strip() for x in data)):
            out[key] = [x.strip() for x in data]
    return out


def _tagged(t: dict) -> tuple[str, str]:
    """The ticket's tags as their Pylon LABELS, once the label map is synced.

    Before a sync the raw values pass through verbatim, so nothing breaks —
    they just read like machine names until Admin syncs the catalog.
    """
    cf = json.loads(t.get("custom_fields") or "{}")
    return (canon("functionality",
                  _cf_val(cf.get(_field_slug("functionality")))),
            canon("category",
                  _cf_val(cf.get(_field_slug("request_category")))))


def _conversation(t: dict) -> str:
    lines = []
    for m in t.get("messages", []):
        role = "Customer" if m["is_customer"] else "Support"
        if m["is_private"]:
            role += " (private)"
        text = _html_text(m.get("message_html"))
        if text:
            if len(text) > MAX_MESSAGE_CHARS:
                text = text[:MAX_MESSAGE_CHARS] + " …[truncated]"
            lines.append(f"  [{role}] {text}")
    return "\n".join(lines) or "  (no messages)"


def _fingerprint(t: dict, vocab_hash: str) -> str:
    """What the verdict depends on: the tags, the conversation, the vocabulary.

    A new dropdown option can change the right suggestion, so the vocabulary
    hash is part of the print — adding an option re-opens old verdicts rather
    than freezing them against a list that no longer exists.
    """
    func, cat = _tagged(t)
    basis = "\x1f".join([func, cat, t.get("title") or "", _conversation(t),
                         vocab_hash])
    return hashlib.sha256(basis.encode()).hexdigest()


def _vocab_hash(vocab: dict) -> str:
    return hashlib.sha256(json.dumps(vocab, sort_keys=True).encode()).hexdigest()


def _system_prompt(vocab: dict) -> str:
    return f"""You are auditing support-ticket field tagging for SpotDraft, a \
contract-management SaaS. For each ticket you get its tagged Functionality and \
Request category plus the conversation. Judge ONLY whether each tag matches \
what the customer actually asked about and how the ticket was handled — you \
are not grading support quality.

Existing Functionality options (the ONLY valid suggestions unless nothing fits):
{json.dumps(vocab["functionality"])}

Existing Request-category options (same rule):
{json.dumps(vocab["category"])}

For each ticket return one object:
- functionality_ok / category_ok: true when the tag is right. An empty tag on \
a ticket whose conversation clearly belongs to some option is NOT ok. Tags on \
older tickets may be legacy machine slugs (e.g. 'salesforce_sfdc', \
'support_task_enable_disable_feature_flag'); when a slug plainly denotes the \
same thing as a listed option, the tag is RIGHT — do not flag spelling.
- note: at most ONE short sentence (max 140 chars) saying why a tag is wrong \
or missing. Empty string when both are ok. Never restate the conversation.
- suggested_functionality / suggested_category: when the tag is not ok, the \
option it should be — copied EXACTLY from the lists above whenever any \
existing option fits, even loosely. Only when genuinely nothing fits, propose \
a short new option name. Empty string when the tag is ok.

Be conservative: if the tagged value is defensible, mark it ok. Return ONLY a \
JSON array, one object per ticket, in input order."""


def _ticket_block(t: dict, idx: int) -> str:
    func, cat = _tagged(t)
    block = "\n".join([
        f"=== TICKET #{t['number']} idx:{idx} ===",
        f"Title                : {t.get('title') or '(no title)'}",
        f"Tagged Functionality : {func or '(empty)'}",
        f"Tagged Category      : {cat or '(empty)'}",
        "Conversation:",
        _conversation(t),
    ])
    if len(block) > MAX_TICKET_CHARS:
        keep = MAX_TICKET_CHARS // 2
        block = (block[:keep] + "\n  …[middle of thread truncated]…\n"
                 + block[-keep:])
    return block


def _require_month(month: str) -> str:
    from datetime import datetime

    try:
        datetime.strptime(str(month), "%Y-%m")
    except (TypeError, ValueError):
        raise ValueError(f"month must be YYYY-MM, got {month!r}")
    return str(month)


def _load_month(month: str) -> list[dict]:
    """The month's tickets with messages — everything except Archived.

    Deliberately NOT the QC scope: rules.excluded_states() narrows what gets
    graded, and it is a setting — the product analysis wants every real ticket
    however QC treats it, and must not shrink because an admin tuned scoring.
    Archived is the one fixed exclusion: an archived ticket is noise here.
    """
    with db.get_conn() as conn:
        rows = conn.execute("""
            SELECT t.id, t.number, t.title, t.link, t.state, t.fetch_date,
                   t.assignee_name, t.custom_fields
            FROM tickets t
            WHERE t.fetch_date LIKE ? AND t.deleted_at IS NULL
              AND LOWER(COALESCE(t.state, '')) != 'archived'
            ORDER BY t.number
        """, (f"{_require_month(month)}-%",)).fetchall()
        tickets = [dict(r) for r in rows]
        for t in tickets:
            msgs = conn.execute(
                "SELECT author_name, is_customer, is_private, message_html "
                "FROM messages WHERE ticket_id = ? ORDER BY timestamp",
                (t["id"],)).fetchall()
            t["messages"] = [dict(m) for m in msgs]
    return tickets


def _stored(month: str) -> dict:
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM func_checks WHERE fetch_date LIKE ?",
            (f"{month}-%",)).fetchall()
    return {r["ticket_id"]: dict(r) for r in rows}


def preview(month: str) -> dict:
    """What a run would do — the run uses this same eligibility."""
    vocab = options()
    vh = _vocab_hash(vocab)
    tickets = _load_month(month)
    stored = _stored(month)
    eligible = [t for t in tickets
                if (stored.get(t["id"]) or {}).get("fingerprint")
                != _fingerprint(t, vh)]
    if not tickets:
        reason = "No tickets in scope for this month."
    elif not eligible:
        reason = "Every ticket this month is already checked and unchanged."
    else:
        reason = (f"{len(eligible)} of {len(tickets)} tickets are unchecked "
                  "or changed since their last check.")
    return {"month": month, "in_scope": len(tickets),
            "eligible": len(eligible), "checked": len(stored),
            "reason": reason,
            "spend": db.qc_spend_for_date(f"{RUN_LABEL_PREFIX}{month}")}


def _clean_result(r: dict, t: dict, vocab: dict) -> dict:
    """Validate one model answer against the ticket and the vocabulary.

    The model's job was judgement; vocabulary membership is checked here, in
    code, so a hallucinated 'existing' option is caught rather than trusted.
    """
    func, cat = _tagged(t)
    func_ok = bool(r.get("functionality_ok"))
    cat_ok = bool(r.get("category_ok"))
    s_func = ("" if func_ok else str(r.get("suggested_functionality") or "").strip())
    s_cat = ("" if cat_ok else str(r.get("suggested_category") or "").strip())
    # A suggestion identical to the tag is a contradiction; treat it as ok.
    if s_func and s_func == func:
        func_ok, s_func = True, ""
    if s_cat and s_cat == cat:
        cat_ok, s_cat = True, ""
    note = " ".join(str(r.get("note") or "").split())[:140]
    if func_ok and cat_ok:
        note = ""
    return {
        "tagged_functionality": func,
        "tagged_category": cat,
        "func_ok": 1 if func_ok else 0,
        "cat_ok": 1 if cat_ok else 0,
        "note": note,
        "suggested_functionality": s_func,
        "suggested_category": s_cat,
        "func_suggestion_new": 1 if s_func and s_func not in vocab["functionality"] else 0,
        "cat_suggestion_new": 1 if s_cat and s_cat not in vocab["category"] else 0,
    }


def _check_batch(batch: list[dict], system: str, stats: RunStats) -> list[dict]:
    prompt = "\n\n".join(_ticket_block(t, i) for i, t in enumerate(batch))
    results = _parse_response(
        _call_gemini(prompt, stats, system=system, schema=RESPONSE_SCHEMA))
    if len(results) != len(batch):
        raise ValueError(
            f"expected {len(batch)} results, got {len(results)}")
    return results


def run(month: str, triggered_by: str = "manual") -> dict:
    """Check the month's eligible tickets; store verdicts; record the run."""
    month = _require_month(month)
    label = f"{RUN_LABEL_PREFIX}{month}"
    vocab = options()
    vh = _vocab_hash(vocab)
    tickets = _load_month(month)
    stored = _stored(month)
    todo = [t for t in tickets
            if (stored.get(t["id"]) or {}).get("fingerprint")
            != _fingerprint(t, vh)]

    if not todo:
        with db.get_conn() as conn:
            cur = conn.execute("""
                INSERT INTO qc_runs (date, triggered_by, started_at, finished_at,
                                     status, total, scored, skipped, config_json)
                VALUES (?, ?, ?, ?, 'success', 0, 0, 0, ?)
            """, (label, triggered_by, _utc_now(), _utc_now(),
                  json.dumps({"functionality_check": True, "month": month})))
            run_id = cur.lastrowid
        return {"checked": 0, "skipped": 0, "already_done": True,
                "run_id": run_id, "status": "success"}

    get_vertex_client()   # fail fast on misconfiguration

    config = {"functionality_check": True, "month": month,
              "triggered_by": triggered_by,
              "options": {k: len(v) for k, v in vocab.items()},
              "vocab_hash": vh, "batch_size": BATCH_SIZE}
    with db.get_conn() as conn:
        cur = conn.execute("""
            INSERT INTO qc_runs (date, triggered_by, started_at, status, total, config_json)
            VALUES (?, ?, ?, 'running', ?, ?)
        """, (label, triggered_by, _utc_now(), len(todo), json.dumps(config)))
        run_id = cur.lastrowid

    system = _system_prompt(vocab)
    stats = RunStats()
    batches = [todo[i:i + BATCH_SIZE] for i in range(0, len(todo), BATCH_SIZE)]
    now = _utc_now()
    checked = skipped = 0
    errors: list[str] = []

    with ThreadPoolExecutor(max_workers=min(len(batches), MAX_WORKERS)) as pool:
        future_to_index = {pool.submit(_check_batch, batch, system, stats): i
                           for i, batch in enumerate(batches)}
        for future in as_completed(future_to_index):
            index = future_to_index[future]
            batch = batches[index]
            try:
                results = future.result()
            except Exception as e:
                msg = f"Batch #{index} (tickets {[t['number'] for t in batch]}): {e}"
                logger.error(msg)
                errors.append(msg)
                skipped += len(batch)
                continue
            with db.get_conn() as conn:
                for t, r in zip(batch, results):
                    row = _clean_result(r, t, vocab)
                    conn.execute("""
                        INSERT OR REPLACE INTO func_checks
                            (ticket_id, fetch_date, tagged_functionality,
                             tagged_category, func_ok, cat_ok, note,
                             suggested_functionality, suggested_category,
                             func_suggestion_new, cat_suggestion_new,
                             fingerprint, checked_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (t["id"], t["fetch_date"],
                          row["tagged_functionality"], row["tagged_category"],
                          row["func_ok"], row["cat_ok"], row["note"],
                          row["suggested_functionality"],
                          row["suggested_category"],
                          row["func_suggestion_new"], row["cat_suggestion_new"],
                          _fingerprint(t, vh), now))
                    checked += 1

    if checked == 0 and (errors or skipped):
        status = "error"
    elif errors or skipped:
        status = "partial"
    else:
        status = "success"
    if skipped and not errors:
        errors.append(f"{skipped} ticket(s) could not be checked")

    with db.get_conn() as conn:
        conn.execute("""
            UPDATE qc_runs SET finished_at = ?, status = ?, scored = ?, skipped = ?,
                   model_used = ?, prompt_tokens = ?, output_tokens = ?, cost_usd = ?,
                   cached_tokens = ?, thought_tokens = ?, cost_estimated = ?, error = ?
            WHERE id = ?
        """, (_utc_now(), status, checked, skipped,
              stats.model_summary(), stats.prompt_tokens, stats.output_tokens,
              stats.cost_usd(), stats.cached_tokens, stats.thought_tokens,
              1 if stats.cost_is_estimated() else 0,
              "; ".join(errors)[:1000] or None, run_id))

    result = {"checked": checked, "skipped": skipped, "already_done": False,
              "run_id": run_id, "status": status,
              "cost_usd": stats.cost_usd(), "model_used": stats.model_summary()}
    if errors:
        result["errors"] = errors
    return result


def results(month: str) -> dict:
    """The month's verdicts for the page, issues first."""
    from drilldown import safe_link

    month = _require_month(month)
    tickets = _load_month(month)
    stored = _stored(month)

    rows = []
    summary = {"in_scope": len(tickets), "checked": 0, "ok": 0,
               "issues": 0, "new_options": 0}
    for t in tickets:
        fc = stored.get(t["id"])
        func, cat = _tagged(t)
        row = {
            "ticket_id": t["id"],
            "number": t["number"],
            "title": t["title"],
            "link": safe_link(t.get("link")),
            "assignee_name": t.get("assignee_name") or "Unassigned",
            "state": t.get("state") or "—",
            "fetch_date": t["fetch_date"],
            "tagged_functionality": func,
            "tagged_category": cat,
            "checked": bool(fc),
        }
        if fc:
            summary["checked"] += 1
            issue = not (fc["func_ok"] and fc["cat_ok"])
            summary["issues" if issue else "ok"] += 1
            if fc["func_suggestion_new"] or fc["cat_suggestion_new"]:
                summary["new_options"] += 1
            row.update({
                "func_ok": bool(fc["func_ok"]),
                "cat_ok": bool(fc["cat_ok"]),
                "note": fc["note"] or "",
                "suggested_functionality": fc["suggested_functionality"] or "",
                "suggested_category": fc["suggested_category"] or "",
                "func_suggestion_new": bool(fc["func_suggestion_new"]),
                "cat_suggestion_new": bool(fc["cat_suggestion_new"]),
            })
        rows.append(row)

    # Issues first, unchecked next, clean tail — the reader's priority order.
    rows.sort(key=lambda r: (
        0 if r["checked"] and not (r.get("func_ok") and r.get("cat_ok"))
        else 2 if r["checked"] else 1,
        -(r["number"] or 0)))

    return {"month": month, "summary": summary, "tickets": rows,
            "spend": db.qc_spend_for_date(f"{RUN_LABEL_PREFIX}{month}")}
