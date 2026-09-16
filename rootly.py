"""Rootly API client — the ONLY module that talks to api.rootly.com.

Same discipline as pylon.py: retries with jittered backoff on transient
failures, and every network call lives here so the rest of Rootly QC works on
plain dicts. Rootly speaks JSON:API (application/vnd.api+json): each resource
is {id, type, attributes}, and some relationships (severity) arrive embedded
INSIDE attributes as {"data": {..., "attributes": {...}}} — `_normalize`
flattens both shapes, because which one you get has varied across instances.
"""

import asyncio
import json
import logging
import os
import random

import httpx

logger = logging.getLogger(__name__)

BASE_URL = os.getenv("ROOTLY_BASE_URL", "https://api.rootly.com")

MAX_TRIES = 3
BASE_BACKOFF = 0.5
PAGE_SIZE = 100
# Hard stop for pagination: a wedged cursor must not loop forever.
MAX_PAGES = 50

# Rootly's lifecycle tail. Everything else counts as open for QC; the Admin
# page can exclude further statuses via rootly_rules.excluded_statuses.
TERMINAL_STATUSES = ("resolved", "closed", "cancelled")


class RootlyNotConfigured(RuntimeError):
    pass


def _headers() -> dict:
    import vault
    token = vault.get_credential("rootly_api_token")
    if not token:
        raise RootlyNotConfigured(
            "No Rootly API token configured — add it in Rootly QC → Admin.")
    return {"Authorization": f"Bearer {token}",
            "Content-Type": "application/vnd.api+json"}


def _is_retryable(exc: Exception) -> bool:
    if isinstance(exc, (httpx.TimeoutException, httpx.TransportError)):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in (429, 500, 502, 503, 504)
    return False


async def _get(path: str, params: dict | None = None) -> dict:
    """One GET with retry; returns the parsed JSON:API document."""
    last: Exception | None = None
    for attempt in range(1, MAX_TRIES + 1):
        try:
            async with httpx.AsyncClient(timeout=30, headers=_headers()) as c:
                r = await c.get(f"{BASE_URL}{path}", params=params or {})
                r.raise_for_status()
                return r.json()
        except Exception as e:                       # noqa: BLE001 - re-raised
            last = e
            if attempt == MAX_TRIES or not _is_retryable(e):
                raise
            delay = BASE_BACKOFF * (2 ** (attempt - 1))
            await asyncio.sleep(delay + random.uniform(0, delay / 2))
    raise last


def _rel_attrs(value) -> dict:
    """Flatten a JSON:API relationship value to its attributes dict."""
    if not isinstance(value, dict):
        return {}
    data = value.get("data", value)
    if not isinstance(data, dict):
        return {}
    attrs = data.get("attributes")
    return attrs if isinstance(attrs, dict) else data


def _normalize(item: dict) -> dict:
    """One incident as a flat dict of exactly what Rootly QC stores.

    raw_json keeps the full attributes payload: the custom-field reader and
    any future check can mine it without another fetch.
    """
    a = item.get("attributes") or {}
    sev = _rel_attrs(a.get("severity"))
    return {
        "id": item.get("id"),
        "sequential_id": a.get("sequential_id"),
        "title": a.get("title"),
        "url": a.get("url") or a.get("short_url"),
        "status": a.get("status"),
        "kind": a.get("kind"),
        "summary": a.get("summary"),
        "severity": (sev.get("slug") or sev.get("severity") or "") or None,
        "severity_name": sev.get("name"),
        "started_at": a.get("started_at"),
        "detected_at": a.get("detected_at"),
        "mitigated_at": a.get("mitigated_at"),
        "resolved_at": a.get("resolved_at"),
        "created_at": a.get("created_at"),
        "updated_at": a.get("updated_at"),
        "slack_channel_id": a.get("slack_channel_id"),
        "jira_key": a.get("jira_issue_key"),
        "jira_url": a.get("jira_issue_url"),
        "raw_json": json.dumps(a),
    }


async def list_open_incidents(exclude_statuses: list[str] | None = None) -> tuple:
    """Every incident NOT in a terminal/excluded status.

    Returns (incidents, complete). `complete` is the deletion-inference guard
    from pylon.FetchedDay, kept for the same reason: an incomplete page walk
    must never be read as "everything else closed".
    """
    exclude = list(dict.fromkeys(
        [*TERMINAL_STATUSES, *(exclude_statuses or [])]))
    out, complete = [], True
    page = 1
    while page <= MAX_PAGES:
        doc = await _get("/v1/incidents", {
            "filter[status][not_in]": ",".join(exclude),
            "page[size]": str(PAGE_SIZE),
            "page[number]": str(page),
            "sort": "-created_at",
        })
        data = doc.get("data") or []
        out.extend(_normalize(i) for i in data)
        meta = doc.get("meta") or {}
        if not data or not meta.get("next_page"):
            break
        page += 1
    else:
        complete = False
        logger.warning("Rootly pagination hit MAX_PAGES; incident list truncated")
    return out, complete


async def get_incident(incident_id: str) -> dict | None:
    """One incident by id, normalized; None on 404 (deleted at source)."""
    try:
        doc = await _get(f"/v1/incidents/{incident_id}")
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            return None
        raise
    data = doc.get("data")
    return _normalize(data) if data else None


async def form_fields() -> list[dict]:
    """Custom/form fields, for the Admin "which field holds the Pylon ticket"
    picker. Names and slugs only — values come per incident."""
    doc = await _get("/v1/form_fields", {"page[size]": "100"})
    out = []
    for item in doc.get("data") or []:
        a = item.get("attributes") or {}
        out.append({"id": item.get("id"), "name": a.get("name"),
                    "slug": a.get("slug"), "kind": a.get("kind"),
                    "input_kind": a.get("input_kind")})
    return out


async def field_selections(incident_id: str) -> list[dict]:
    """The incident's custom-field values (text `value` per form_field_id)."""
    doc = await _get(f"/v1/incidents/{incident_id}/form_field_selections",
                     {"page[size]": "100"})
    out = []
    for item in doc.get("data") or []:
        a = item.get("attributes") or {}
        out.append({"form_field_id": a.get("form_field_id"),
                    "value": a.get("value")})
    return out


async def incident_events(incident_id: str, limit: int = 100) -> list[dict]:
    """The Rootly timeline for one incident, newest last."""
    doc = await _get(f"/v1/incidents/{incident_id}/events",
                     {"page[size]": str(min(limit, 100))})
    out = []
    for item in doc.get("data") or []:
        a = item.get("attributes") or {}
        out.append({"event": a.get("event"),
                    "occurred_at": a.get("occurred_at") or a.get("created_at"),
                    "kind": a.get("kind")})
    return out


async def test_token() -> dict:
    """One-page probe for the Admin credential test."""
    doc = await _get("/v1/incidents", {"page[size]": "1"})
    total = ((doc.get("meta") or {}).get("total_count"))
    return {"ok": True,
            "message": "Rootly token works"
                       + (f" — {total} incidents visible" if total is not None
                          else "")}
