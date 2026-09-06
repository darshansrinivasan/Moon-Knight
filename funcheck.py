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


def options() -> dict:
    """{'functionality': [...], 'category': [...]} — the canonical vocabulary.

    From `funcheck_catalog`, the product owner's curated lists mirrored in
    Pylon's dropdowns — not the values observed on tickets. Observed values
    are what people DID select; the catalog is what they MAY select, and a
    suggestion must come from the latter. Older tickets carry legacy slugs
    ('salesforce_sfdc') for what the catalog now names in full; the prompt
    tells the model to treat an obvious legacy spelling as the same tag.
    """
    import funcheck_catalog
    return {"functionality": list(funcheck_catalog.FUNCTIONALITIES),
            "category": list(funcheck_catalog.REQUEST_CATEGORIES)}


def _tagged(t: dict) -> tuple[str, str]:
    cf = json.loads(t.get("custom_fields") or "{}")
    return ((_cf_val(cf.get(_field_slug("functionality"))) or "").strip(),
            (_cf_val(cf.get(_field_slug("request_category"))) or "").strip())


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
