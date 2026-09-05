import asyncio
import logging
import os
import random
from dataclasses import dataclass, field
from datetime import date, timedelta, timezone
from typing import NamedTuple

import httpx

logger = logging.getLogger(__name__)

BASE_URL = os.getenv("PYLON_BASE_URL", "https://api.usepylon.com")

# Per-resource retries. Pylon rate-limits under the concurrency we drive, and a
# single dropped response used to be recorded as fact by the scorer.
MAX_TRIES   = 3
BASE_BACKOFF = 0.5


@dataclass
class FetchedDay:
    """One day pulled from Pylon, plus what could NOT be pulled.

    The failure sets are the point: a ticket whose messages or account did not
    load must be excluded from rule scoring rather than graded against the gap.

    `issues_complete` is a stronger claim than "no exception was raised": it
    means the issue list is the authoritative set for this date. Only that
    justifies inferring a deletion from absence — see `_fetch_issues_page`.
    """

    issues: list
    messages_by_id: dict
    accounts_by_id: dict
    failed_messages: set = field(default_factory=set)
    failed_accounts: set = field(default_factory=set)
    issues_complete: bool = True

    def is_complete(self, issue: dict) -> bool:
        """True when everything the R-checks need for this ticket was fetched."""
        if issue["id"] in self.failed_messages:
            return False
        account_id = (issue.get("account") or {}).get("id")
        return not (account_id and account_id in self.failed_accounts)

    def may_infer_deletions(self) -> bool:
        """Whether absence from `issues` can be read as "deleted at source".

        Deliberately conservative. Pylon has no tombstone for a deleted ticket,
        so absence is the only signal — which means an incomplete fetch looks
        exactly like a mass deletion. Getting this wrong destroys tickets,
        messages, grades and human sign-offs, and sign-offs cannot be recovered
        by refetching. When in doubt, infer nothing.
        """
        return self.issues_complete


def _is_retryable(exc: Exception) -> bool:
    if isinstance(exc, (httpx.TimeoutException, httpx.TransportError)):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in (429, 500, 502, 503, 504)
    return False


async def _with_retry(call):
    """Await `call()`, retrying transient Pylon failures with jittered backoff."""
    last: Exception | None = None
    for attempt in range(1, MAX_TRIES + 1):
        try:
            return await call()
        except Exception as e:                      # noqa: BLE001 - re-raised below
            last = e
            if attempt == MAX_TRIES or not _is_retryable(e):
                raise
            delay = BASE_BACKOFF * (2 ** (attempt - 1))
            await asyncio.sleep(delay + random.uniform(0, delay / 2))
    raise last            # unreachable; keeps the contract explicit

# Pylon stores times in UTC; we fetch a calendar date in IST (UTC+5:30).
# IST midnight = 18:30 UTC previous day, IST end-of-day = 18:29:59 UTC same day.
_IST = timezone(timedelta(hours=5, minutes=30))


def _ist_window(target: date) -> tuple[str, str]:
    """Return (start, end) as UTC ISO strings for the full IST calendar day."""
    from datetime import datetime
    start_ist = datetime(target.year, target.month, target.day, 0,  0,  0,  tzinfo=_IST)
    end_ist   = datetime(target.year, target.month, target.day, 23, 59, 59, tzinfo=_IST)
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    return (
        start_ist.astimezone(timezone.utc).strftime(fmt),
        end_ist.astimezone(timezone.utc).strftime(fmt),
    )


class PylonNotConfigured(RuntimeError):
    """Raised when no Pylon API token has been configured by an admin."""


def _headers() -> dict:
    """Auth header built from the admin-managed credential vault."""
    import vault
    token = vault.get_credential("pylon_api_token")
    if not token:
        raise PylonNotConfigured(
            "No Pylon API token configured — an admin must add it in Admin → Credentials"
        )
    return {"Authorization": f"Bearer {token}"}


# ── issue list (paginated) ──────────────────────────────────────────────────

class IssuePage(NamedTuple):
    """One page of issues, and whether it can be trusted as data.

    `ok` is False when Pylon answered with something other than a data page —
    an error envelope for an invalid range, say. That used to be flattened into
    an empty list, which is indistinguishable from "this date has no tickets"
    and would let a deletion sweep read an outage as a mass deletion.
    """

    issues: list
    cursor: str | None
    has_next: bool
    ok: bool


async def _fetch_issues_page(
    client: httpx.AsyncClient,
    start: str,
    end: str,
    cursor: str | None,
) -> IssuePage:
    params: dict = {"start_time": start, "end_time": end, "limit": 100}
    if cursor:
        params["cursor"] = cursor
    r = await client.get(f"{BASE_URL}/issues", headers=_headers(), params=params)
    r.raise_for_status()
    body = r.json()
    if "data" not in body:
        # Pylon returns {"errors": [...]} for invalid ranges (e.g. future dates).
        # Report it as not-ok rather than as an empty page.
        logger.warning(
            "Pylon returned no data page for %s..%s: %s",
            start, end, str(body)[:200],
        )
        return IssuePage([], None, False, ok=False)
    pag = body.get("pagination", {})
    return IssuePage(
        body["data"], pag.get("cursor"), pag.get("has_next_page", False), ok=True
    )


async def fetch_issues_for_date(
    target: date, client: httpx.AsyncClient
) -> tuple[list[dict], bool]:
    """(issues, complete) for one date.

    `complete` is False if any page came back as a non-data body, so callers
    can tell "this date genuinely has no tickets" from "we could not read it".
    """
    start, end = _ist_window(target)
    page = await _with_retry(lambda: _fetch_issues_page(client, start, end, None))
    if not page.ok:
        return [], False

    issues = list(page.issues)
    cursor, has_next = page.cursor, page.has_next
    while has_next:
        nxt = await _with_retry(
            lambda c=cursor: _fetch_issues_page(client, start, end, c)
        )
        if not nxt.ok:
            # Partial list: keep what we have for scoring, but never let a
            # truncated page be read as the authoritative set for the date.
            return issues, False
        issues.extend(nxt.issues)
        cursor, has_next = nxt.cursor, nxt.has_next
    return issues, True


# ── messages ────────────────────────────────────────────────────────────────

async def fetch_messages(
    issue_id: str, client: httpx.AsyncClient
) -> list[dict]:
    r = await client.get(
        f"{BASE_URL}/issues/{issue_id}/messages", headers=_headers()
    )
    r.raise_for_status()
    return r.json()["data"]


# ── account ─────────────────────────────────────────────────────────────────

async def fetch_account(
    account_id: str, client: httpx.AsyncClient
) -> dict | None:
    r = await client.get(
        f"{BASE_URL}/accounts/{account_id}", headers=_headers()
    )
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.json()["data"]


# ── fetch everything for one day ────────────────────────────────────────────

async def fetch_custom_fields() -> list[dict]:
    """Every custom field Pylon currently defines on issues.

    Used to populate the field-mapping pickers and, more usefully, to notice
    when a field a check reads has stopped existing. A check whose field is
    gone does not error — an absent field reads as an empty one, so R1 would
    simply fail every ticket, quietly and forever. Being told is the difference
    between a setting to change and a week of wrong grades.
    """
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(f"{BASE_URL}/custom_fields", headers=_headers(),
                             params={"object_type": "issue"})
        r.raise_for_status()
        body = r.json()
    if "data" not in body:
        raise RuntimeError(
            f"Pylon returned no custom-field list: {str(body)[:200]}")
    return body["data"]


async def fetch_day(target: date) -> FetchedDay:
    """Fetch one day's issues with their messages and accounts.

    All network calls run concurrently (semaphore-limited to 10). Anything that
    could not be fetched after retries is reported on the result rather than
    silently returned as empty — see `FetchedDay`.
    """
    sem = asyncio.Semaphore(10)

    # Resolve the token once per run so a mid-run credential change can't
    # produce a half-authenticated fetch.
    async with httpx.AsyncClient(timeout=30, headers=_headers()) as client:
        issues, issues_complete = await fetch_issues_for_date(target, client)

        # A swallowed failure is indistinguishable from real emptiness, and the
        # scorer reads both as evidence: no messages looks like an unanswered
        # thread, and a missing account looks like an invalid one. So retry,
        # then report what could not be fetched instead of guessing.
        failed_messages: set[str] = set()
        failed_accounts: set[str] = set()

        async def safe_messages(issue_id: str) -> tuple[str, list]:
            async with sem:
                try:
                    return issue_id, await _with_retry(
                        lambda: fetch_messages(issue_id, client)
                    )
                except Exception as e:
                    logger.warning("Messages unavailable for %s: %s", issue_id, e)
                    failed_messages.add(issue_id)
                    return issue_id, []

        async def safe_account(account_id: str) -> tuple[str, dict | None]:
            async with sem:
                try:
                    return account_id, await _with_retry(
                        lambda: fetch_account(account_id, client)
                    )
                except Exception as e:
                    logger.warning("Account unavailable for %s: %s", account_id, e)
                    failed_accounts.add(account_id)
                    return account_id, None

        # concurrent messages
        msg_results = await asyncio.gather(
            *[safe_messages(i["id"]) for i in issues]
        )
        messages_by_id = dict(msg_results)

        # unique accounts (deduplicated)
        account_ids = {
            i["account"]["id"]
            for i in issues
            if i.get("account") and i["account"].get("id")
        }
        acc_results = await asyncio.gather(
            *[safe_account(aid) for aid in account_ids]
        )
        accounts_by_id = {aid: acc for aid, acc in acc_results if acc}

    return FetchedDay(
        issues=issues,
        messages_by_id=messages_by_id,
        accounts_by_id=accounts_by_id,
        failed_messages=failed_messages,
        failed_accounts=failed_accounts,
        issues_complete=issues_complete,
    )
