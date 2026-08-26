"""
AI scoring (A1–A5) on Gemini through **Vertex AI**.

Authentication is OAuth-based: either a service account stored in the admin
credential vault, or Application Default Credentials (`gcloud auth
application-default login`, or the attached service account in GCP). No API
keys. The GCP project and region are admin settings.
"""

import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

from google import genai
from google.genai import types
from bs4 import BeautifulSoup

import db
import scorer
import vault

logger = logging.getLogger(__name__)

MAX_WORKERS = 5
BATCH_SIZE  = 6

VERTEX_SCOPES = ["https://www.googleapis.com/auth/cloud-platform"]


class VertexNotConfigured(RuntimeError):
    """Raised when Vertex AI project/credentials have not been set up."""


_client_lock = threading.Lock()
_client_cache: tuple[str, object] | None = None   # (fingerprint, client)


def _vertex_credentials(quota_override: str | None = None):
    """Resolve a Vertex credential: service account → connected account → ADC.

    quota_override lets configuration-time calls bill the project being browsed,
    which is not yet saved in settings.
    """
    try:
        info = vault.service_account_info()
    except ValueError as e:
        raise VertexNotConfigured(str(e))

    if info:
        from google.oauth2 import service_account
        try:
            creds = service_account.Credentials.from_service_account_info(
                info, scopes=VERTEX_SCOPES
            )
        except Exception as e:
            raise VertexNotConfigured(f"Vertex service account is not usable: {e}")
        return creds, "sa:" + (info.get("client_email") or "?")

    # A Google account connected through the Admin UI's OAuth flow.
    import gcp
    oauth_creds = gcp.oauth_credentials()
    if oauth_creds:
        return _with_quota_project(oauth_creds, quota_override), \
            "oauth:" + (vault.get_setting("google_cloud_account") or "?")

    import google.auth
    try:
        creds, _ = google.auth.default(scopes=VERTEX_SCOPES)
    except Exception as e:
        raise VertexNotConfigured(
            "No Google Cloud credentials. Connect a Google account in Admin → AI, "
            "paste a service account, or run "
            f"`gcloud auth application-default login` locally. ({e})"
        )
    return _with_quota_project(creds, quota_override), "adc"


def quota_project() -> str:
    """Project billed for Vertex calls. Defaults to the model project."""
    return (vault.get_setting("vertex_quota_project").strip()
            or vault.get_setting("vertex_project").strip())


def _with_quota_project(creds, override: str | None = None):
    """Attach a billing project to *user* credentials.

    A user credential carries no project of its own, so Vertex has nothing to
    bill and rejects the call. Setting a quota project makes the client send
    X-Goog-User-Project. Service accounts belong to a project already, and
    attaching one there just adds a serviceusage permission they may not have.
    """
    from google.oauth2 import service_account
    if isinstance(creds, service_account.Credentials):
        return creds

    project = (override or "").strip() or quota_project()
    if project and hasattr(creds, "with_quota_project"):
        return creds.with_quota_project(project)
    return creds


def explain_vertex_error(exc: Exception) -> str:
    """Turn Google's near-identical 403s into the specific fix each one needs.

    They read alike and have entirely different causes: the API not enabled on
    the billed project, a user credential with no usable quota project, or an
    account missing the Vertex role.
    """
    msg = str(exc)
    low = msg.lower()
    proj  = vault.get_setting("vertex_project").strip() or "<project>"
    quota = quota_project() or proj

    import re
    m = re.search(r"project (\d{6,})", msg)
    named = m.group(1) if m else quota

    if "has not been used in project" in low or "it is disabled" in low:
        return (
            f"The Vertex AI API is not enabled on project {named} — the project being "
            f"billed, which is not always the one hosting the model. Enable it:\n"
            f"    gcloud services enable aiplatform.googleapis.com --project {named}\n"
            "Then wait a minute and retry. If the quota project differs from the model "
            "project, enable it on both."
        )
    if "quota project" in low or "serviceusage.services.use" in low:
        return (
            f"The signed-in account cannot bill project {quota}. Grant it "
            "roles/serviceusage.serviceUsageConsumer on that project, or set a "
            "different Quota project in Admin → AI."
        )
    if "aiplatform.endpoints.predict" in low:
        return (
            f"The API is enabled but this account lacks permission to generate. "
            f"Grant roles/aiplatform.user on project {proj}."
        )
    if "aiplatform.locations.list" in low:
        return (
            "This account cannot list Vertex regions. That only affects the region "
            "dropdown — generation still works with a region selected manually."
        )
    if "404" in low and "publisher" in low:
        return (
            f"That model is not available in the selected region. Pick another region "
            "or model in Admin → AI."
        )
    return msg[:400]


def get_vertex_client():
    """Build (and cache) a Vertex-backed genai client for the configured project."""
    global _client_cache

    project  = vault.get_setting("vertex_project").strip()
    location = vault.get_setting("vertex_location").strip() or "us-central1"
    if not project:
        raise VertexNotConfigured(
            "No Google Cloud project configured — set it in Admin → AI (Vertex)"
        )

    creds, cred_id = _vertex_credentials()
    fingerprint = f"{project}|{location}|{quota_project()}|{cred_id}"

    with _client_lock:
        if _client_cache and _client_cache[0] == fingerprint:
            return _client_cache[1]
        client = genai.Client(
            vertexai=True, project=project, location=location, credentials=creds
        )
        _client_cache = (fingerprint, client)
        return client


def invalidate_vertex_client() -> None:
    """Drop the cached client so the next call picks up changed settings."""
    global _client_cache
    with _client_lock:
        _client_cache = None
    try:
        import gcp
        gcp.reset_probe_cache()
    except ImportError:
        pass


def vertex_models() -> list[str]:
    raw = vault.get_setting("vertex_models")
    models = [m.strip() for m in raw.split(",") if m.strip()]
    return models or ["gemini-2.5-flash"]

# Single-ticket prompt uses "idx:0" so the model never has to echo a UUID.
SYSTEM_PROMPT = """You are a support quality-control analyst for SpotDraft, a contract-management SaaS. Evaluate support tickets and return ONLY a JSON array — no prose, no markdown fences.

Each ticket in the input is prefixed with === TICKET #<number> idx:<index> ===
Return one result object per ticket in the SAME ORDER, using the "idx" field to identify each.

CHECKS:

A1 — Category accuracy  →  Pass / Fail / Needs Review
  Compare functionalities and request_category against what the customer actually asked. Fail if clearly mismatched. Needs Review if multiple categories reasonably apply.

A2 — Customer sentiment  →  Positive / Neutral / Concerned / Frustrated / Urgent
  Base on customer language, escalation cues, time-in-queue.

A3 — Response quality  →  Good / Needs Improvement / Poor
  Good: clear, accurate, empathetic, assigns ownership, includes next steps.
  Needs Improvement: vague or incomplete but serviceable.
  Poor: wrong guidance, missed ask, no next step, confusing handoff.
  IMPORTANT — Internal tickets: if "Internal ticket: Yes" appears in the ticket block,
  the Pylon thread IS the communication channel (it mirrors a Slack thread). The requester
  is a colleague, not an external customer. A brief but clear confirmation of the action
  taken in the thread is fully adequate — rate A3=Good. Do NOT penalize for absence of a
  formal email reply or elaborate closure message.

A4 — Status vs conversation  →  Pass / Fail / Needs Review
  Does the current ticket state match who actually owns the next action?

A5 — Not closed prematurely  →  Pass / Fail / Needs Review / N/A
  N/A for open tickets. Pass if closed with resolution evidence or documented no-response follow-up. Fail if customer ask still open at closure.

OVERALL RESULT (you must compute this):
  Fail         — any R-check is Fail, OR A3 = Poor, OR A5 = Fail
  Needs Review — no Fails, but ≥1 check is Needs Review
  Pass         — everything Pass or N/A

AI NOTES: one concise string. For every Fail or Needs Review check, write a specific sentence that names:
  (1) what exactly went wrong (quote the customer's missed ask, the incorrect category, the unanswered message, etc.)
  (2) what the support agent should do to fix it.
  Format: "A<n> <grade>: <specific finding> — <specific fix>."
  Be concrete — never write generic phrases like "fix needed" or "review required" without explaining what to fix or review.
  If all AI checks pass, write a one-sentence summary of what was handled well.

Message roles: is_customer=1 → message visible to requester; is_private=1 → internal note (exclude from customer-response logic).
For internal tickets the "requester" is a colleague — treat is_customer=1 messages as internal thread replies, not external customer communication.

Return format (idx matches the input idx value, NOT the ticket number):
[
  {
    "idx": 0,
    "a1": "Pass|Fail|Needs Review",
    "a2": "Positive|Neutral|Concerned|Frustrated|Urgent",
    "a3": "Good|Needs Improvement|Poor",
    "a4": "Pass|Fail|Needs Review",
    "a5": "Pass|Fail|Needs Review|N/A",
    "ai_notes": "...",
    "overall_result": "Pass|Fail|Needs Review"
  }
]"""


def _html_text(html: str | None) -> str:
    if not html:
        return ""
    return BeautifulSoup(html, "html.parser").get_text(" ", strip=True)


def _cf_val(field: dict | None) -> str | None:
    if not field:
        return None
    return field.get("interpreted_value") or field.get("value") or None


def _strip_r_notes(note: str) -> str:
    """Remove all previously prepended R-check lines from an ai_notes string."""
    parts = (note or "").split(" | ")
    ai_parts = [
        p for p in parts
        if not any(
            p.startswith(f"R{n}:") or p.startswith(f"R{n} Fail:")
            for n in ["1","2","3","4","5","7","8","9"]
        )
    ]
    return " | ".join(ai_parts).strip(" |")


def _r_check_notes(r_checks: dict, cf: dict | None = None,
                   state: str = "", account_name: str = "") -> str:
    """Return specific, actionable failure reasons for failing R-checks."""
    cf = cf or {}
    parts = []

    if r_checks.get("r1") == "Fail":
        parts.append("R1 Fail: 'Functionalities' field is empty — fill in the product area affected")

    if r_checks.get("r2") == "Fail":
        parts.append("R2 Fail: 'Request Category' field is empty — categorise the request type")

    if r_checks.get("r3") == "Fail":
        acct = f" (account: '{account_name}')" if account_name else ""
        parts.append(f"R3 Fail: linked account{acct} is internal or invalid — re-link to the correct customer account")

    if r_checks.get("r4") == "Fail":
        parts.append("R4 Fail: customer's last message has gone unanswered for >24 hours — reply or update the customer immediately")

    if r_checks.get("r5") == "Fail":
        state_lower = state.lower()
        if state_lower == "waiting_on_csm":
            parts.append(
                "R5 Fail: ticket is 'waiting_on_csm' but no CS or Implementation team member is tagged in the Pylon thread — "
                "@ mention the assigned CSM, IM, @cs, or @implementation to formally hand off"
            )
        elif state_lower in ("waiting_on_engg", "waiting_on_engineering"):
            parts.append(
                "R5 Fail: ticket is 'waiting_on_engg' but no engineer or engineering group (@eng-be, @eng-fe, @eng--oncall, "
                "@pod-sidebar, @pod-ai-infra) is tagged in the thread, and no Rootly/Jira link exists — "
                "tag the relevant engineer or add a Jira/Rootly link"
            )
        elif state_lower == "waiting_on_product":
            parts.append(
                "R5 Fail: ticket is 'waiting_on_product' but no product team member or @pt group is tagged — "
                "@ mention the relevant PM or @pt to hand off"
            )
        elif state_lower == "waiting_on_legal":
            parts.append(
                "R5 Fail: ticket is 'waiting_on_legal' but @legal-ops is not mentioned in the thread — "
                "tag @legal-ops to formalise the handoff"
            )
        elif state_lower == "waiting_on_you":
            parts.append(
                "R5 Fail: ticket is 'waiting_on_you' — support team has not replied to the customer yet"
            )
        elif state_lower == "new":
            parts.append(
                "R5 Fail: ticket is 'new' with no assignee — assign it to an agent"
            )
        else:
            parts.append(
                f"R5 Fail: ticket state '{state}' lacks the required handoff evidence in the thread"
            )

    if r_checks.get("r7") == "Fail":
        parts.append(
            "R7 Fail: engineering ticket has no Rootly incident or Jira link — "
            "add a Rootly incident reference or link the Jira ticket"
        )

    if r_checks.get("r8") == "Fail":
        missing = []
        if (cf.get("does_rootly_exist") or {}).get("value") != "Yes":
            missing.append("set 'does_rootly_exist' to Yes")
        if not (cf.get("rootly.incident_reference") or {}).get("value"):
            missing.append("fill in the Rootly incident reference (e.g. ROOT-1234)")
        req_cat = ((cf.get("request_category") or {}).get("value") or "").lower()
        if req_cat not in scorer.ONCALL_CATEGORIES:
            missing.append(f"change request_category from '{req_cat}' to an oncall category")
        if not any("jira" in (e.get("source") or "").lower() or "atlassian" in (e.get("link") or "").lower()
                   for e in []):  # Jira check simplified here; actual check is in scorer
            pass  # avoid false positives — R8 already confirmed Jira missing
        action = "; ".join(missing) if missing else "add a Jira link to the ticket"
        parts.append(f"R8 Fail: oncall completeness incomplete — {action}")

    return " | ".join(parts)


def _compute_overall(r_checks: dict, a: dict) -> str:
    r_keys = ["r1", "r2", "r3", "r4", "r5", "r7", "r8", "r9"]
    if any(r_checks.get(k) == "Fail" for k in r_keys):
        return "Fail"
    if a.get("a3") == "Poor" or a.get("a5") == "Fail":
        return "Fail"
    if any(r_checks.get(k) == "Needs Review" for k in r_keys):
        return "Needs Review"
    if any(a.get(k) == "Needs Review" for k in ["a1", "a3", "a4", "a5"]):
        return "Needs Review"
    return "Pass"


def _is_retryable_error(exc: Exception) -> bool:
    """True for transient API errors that warrant trying the next model."""
    msg = str(exc).lower()
    return any(kw in msg for kw in [
        "quota", "exhausted", "429", "rate limit", "resource_exhausted",
        "too many requests", "503", "unavailable", "service unavailable",
        "overloaded", "high demand", "try again",
    ])


def _build_ticket_block(t: dict, idx: int) -> str:
    cf = json.loads(t.get("custom_fields") or "{}")
    cpv = t.get("customer_portal_visible")
    src = t.get("source") or ""
    is_internal = (cpv == 0) or (src == "manual")
    lines = [
        f"=== TICKET #{t['number']} idx:{idx} ===",
        f"Title      : {t['title']}",
        f"State      : {t['state']}",
        f"Assignee   : {t.get('assignee_name') or 'Unassigned'}",
        f"Account    : {t.get('account_name') or '—'} ({t.get('account_type') or '—'})",
        f"Source     : {src or '—'}",
        f"Internal ticket: {'Yes — requester is a colleague; Pylon thread = Slack thread' if is_internal else 'No — external customer'}",
        f"Functionality : {_cf_val(cf.get('functionalities')) or '—'}",
        f"Category      : {_cf_val(cf.get('request_category')) or '—'}",
        f"R-checks   : R1={t.get('r1')} R2={t.get('r2')} R3={t.get('r3')} "
        f"R4={t.get('r4')} R5={t.get('r5')} R7={t.get('r7')}",
        "",
        "Messages:",
    ]
    for m in t.get("messages", []):
        role = "Customer" if m["is_customer"] else "Support"
        if m["is_private"]:
            role += " (private)"
        text = _html_text(m.get("message_html"))
        if text:
            lines.append(f"  [{role}] {text}")
    if not any(m.get("message_html") for m in t.get("messages", [])):
        lines.append("  (no messages)")
    return "\n".join(lines)


def _call_gemini(prompt: str) -> str:
    """Call Gemini on Vertex, cascading through models on quota errors."""
    client = get_vertex_client()
    last_err: Exception | None = None

    for model_name in vertex_models():
        try:
            response = client.models.generate_content(
                model=model_name,
                contents=prompt,
                config=types.GenerateContentConfig(
                    system_instruction=SYSTEM_PROMPT,
                    temperature=0.2,
                ),
            )
            return response.text
        except Exception as e:
            last_err = e
            logger.warning("Error on %s (%s), trying next model", model_name, e)
            continue

    raise RuntimeError(f"All Vertex models failed. Last error: {last_err}")


def _parse_response(text: str) -> list[dict]:
    """Strip markdown fences and parse JSON."""
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
        text = text.rsplit("```", 1)[0].strip()
    return json.loads(text)


def _score_single(ticket: dict, idx: int = 0) -> dict:
    """Score one ticket solo — used as a fallback when a batch fails."""
    prompt = _build_ticket_block(ticket, idx)
    results = _parse_response(_call_gemini(prompt))
    return results[0]


def _score_batch(batch: list[dict]) -> list[dict]:
    """
    Score a batch of tickets.
    Results are matched back to tickets by idx (0-based position in the batch),
    not by UUID, so model transcription errors can't cause silent skips.
    Falls back to scoring each ticket individually if batch scoring fails.
    """
    prompt = "\n\n".join(_build_ticket_block(t, i) for i, t in enumerate(batch))
    try:
        results = _parse_response(_call_gemini(prompt))
        # Validate we got the right number of results; fall back if not.
        if len(results) != len(batch):
            raise ValueError(
                f"Expected {len(batch)} results, got {len(results)}"
            )
        return results
    except VertexNotConfigured:
        raise  # configuration problem — retrying per ticket would just repeat it
    except Exception as e:
        logger.warning(
            "Batch of %d tickets failed (%s), retrying individually",
            len(batch), e,
        )
        # Retry each ticket solo so one bad ticket can't sink the whole batch.
        individual = []
        for i, t in enumerate(batch):
            try:
                individual.append(_score_single(t, idx=i))
            except Exception as ie:
                logger.error("Solo score failed for ticket #%s: %s", t.get("number"), ie)
                individual.append(None)  # placeholder — skipped in write loop
        return individual


def _write_results(batch: list[dict], results: list[dict], date_str: str, now: str) -> tuple[int, int]:
    """Write scored results to DB. Returns (scored, skipped)."""
    scored = skipped = 0
    with db.get_conn() as conn:
        for i, (t, r) in enumerate(zip(batch, results)):
            if r is None:
                skipped += 1
                continue
            # Accept idx match OR fall back to positional (model may omit idx).
            r_idx = r.get("idx")
            if r_idx is not None and r_idx != i:
                logger.warning(
                    "idx mismatch: expected %d got %d for ticket #%s — using positional match",
                    i, r_idx, t.get("number"),
                )
            r_checks  = {k: t.get(k) for k in ["r1","r2","r3","r4","r5","r7","r8","r9"]}
            overall   = _compute_overall(r_checks, r)
            cf        = json.loads(t.get("custom_fields") or "{}")
            r_note    = _r_check_notes(r_checks, cf,
                                       state=t.get("state", ""),
                                       account_name=t.get("account_name", ""))
            ai_note   = r.get("ai_notes") or ""
            full_note = f"{r_note} | {ai_note}".strip(" |") if r_note else ai_note
            conn.execute("""
                INSERT OR REPLACE INTO ai_checks
                    (ticket_id, fetch_date, a1, a2, a3, a4, a5, ai_notes, overall_result, checked_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                t["id"], date_str,
                r.get("a1"), r.get("a2"), r.get("a3"), r.get("a4"), r.get("a5"),
                full_note, overall, now,
            ))
            scored += 1
    return scored, skipped


def run_qc_date(date_str: str) -> dict:
    """Score pending or stale tickets for date_str.
    Stale = ticket was refetched after the AI check was last run.
    Returns counts.
    """
    with db.get_conn() as conn:
        rows = conn.execute("""
            SELECT t.id, t.number, t.title, t.state, t.assignee_name,
                   t.account_id, t.custom_fields, t.source, t.customer_portal_visible,
                   a.name AS account_name, a.type AS account_type,
                   rc.r1, rc.r2, rc.r3, rc.r4, rc.r5, rc.r7, rc.r8, rc.r9
            FROM tickets t
            LEFT JOIN accounts    a  ON t.account_id = a.id
            LEFT JOIN rule_checks rc ON t.id = rc.ticket_id
            LEFT JOIN ai_checks   ac ON t.id = ac.ticket_id
            WHERE t.fetch_date = ?
              AND (ac.ticket_id IS NULL OR t.fetched_at > ac.checked_at)
            ORDER BY t.number
        """, (date_str,)).fetchall()

    tickets = [dict(r) for r in rows]
    if not tickets:
        return {"scored": 0, "skipped": 0, "already_done": True}

    # Fail fast on misconfiguration rather than burning a call per ticket.
    get_vertex_client()

    with db.get_conn() as conn:
        for t in tickets:
            msgs = conn.execute(
                "SELECT author_name, is_customer, is_private, message_html "
                "FROM messages WHERE ticket_id = ? ORDER BY timestamp",
                (t["id"],),
            ).fetchall()
            t["messages"] = [dict(m) for m in msgs]

    batches = [tickets[i : i + BATCH_SIZE] for i in range(0, len(tickets), BATCH_SIZE)]
    now     = datetime.now(timezone.utc).isoformat()
    scored  = 0
    skipped = 0
    errors: list[str] = []

    with ThreadPoolExecutor(max_workers=min(len(batches), MAX_WORKERS)) as pool:
        future_to_batch = {pool.submit(_score_batch, batch): batch for batch in batches}

        for future in as_completed(future_to_batch):
            batch = future_to_batch[future]
            try:
                results = future.result()
            except Exception as e:
                msg = f"Batch #{batches.index(batch)} (tickets {[t['number'] for t in batch]}): {e}"
                logger.error(msg)
                errors.append(msg)
                skipped += len(batch)
                continue

            s, sk = _write_results(batch, results, date_str, now)
            scored  += s
            skipped += sk

    result: dict = {"scored": scored, "skipped": skipped, "already_done": False}
    if errors:
        result["errors"] = errors
    return result
