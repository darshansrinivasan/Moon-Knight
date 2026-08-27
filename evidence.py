"""
Why THIS ticket passed: the evidence behind one stored R-check verdict.

`rule_checks` keeps a single word per deterministic check — 'Pass', 'Fail' or
'N/A'. A reviewer opening a ticket therefore reads the rule's generic
description ("R1: functionalities custom field is filled") and still cannot see
what on this particular ticket satisfied it; confirming it means opening Pylon
and re-reading the custom fields and the whole thread. That gap is what this
module closes: for each check it re-walks the same inputs `scorer` walked and
states the actual deciding value —

    r1  functionalities = 'Contract Lifecycle'
    r4  customer's last message has gone 41h unanswered against a 24h SLA
    r8  no oncall evidence on the ticket

Two rules keep the strings trustworthy rather than merely helpful:

    the stored verdict wins   The verdict in `rule_checks` is the fact; this is
                              only its explanation. When the reconstruction
                              disagrees — a custom field filled in after
                              scoring, an SLA clock that has since run out, an
                              edited rule — the string reports the verdict as
                              recorded and what the ticket shows NOW. It never
                              restates the verdict as something else.

    silence over invention    R5 can pass on a Slack thread fetched at scoring
                              time and never stored. That case returns "passed
                              on evidence not recorded at scoring time". A
                              confident wrong explanation is worse than an
                              admission of ignorance.

Inputs are a row from `db.get_day_tickets()` and its `db.get_ticket_messages()`
rows, so `custom_fields` and `external_issues` arrive as JSON *strings* and the
message author payload scoring saw is already flattened into `is_customer`.
Where scoring consults `rules`, so does this module, so the evidence reflects
the configuration in force now rather than a copy of it.

Total and side-effect free: nothing is written, no ticket data is fetched, and
no input — unparseable JSON, a missing key, a malformed timestamp — raises. This
runs while a UI panel renders. Values are quoted verbatim and truncated; the
caller escapes for HTML.
"""

import json
import logging
import re
from datetime import datetime, timedelta, timezone

import rules as qc_rules
import scorer

logger = logging.getLogger(__name__)

# Long custom-field values (a pasted URL, a paragraph in a text field) would
# push the real evidence off the panel, so a quoted value is cut here.
MAX_VALUE_CHARS = 60

# What a reviewer sees when the explanation, not the verdict, is unavailable.
UNRECONSTRUCTABLE = "evidence could not be reconstructed for this ticket"
NOT_SCORED = "not scored yet"
_CF_UNREADABLE = "the ticket's stored custom fields could not be read"
_ONCALL_THREAD_ONLY = (
    "the deciding check was a fetch of the linked oncall Slack thread, "
    "whose contents are not stored"
)

# Verdict -> how the fallback sentence starts.
_VERB = {"Pass": "passed", "Fail": "failed", "N/A": "was marked N/A"}

# Roster ids are the persisted fact; a name is only a label, and one may be
# missing from the rules document. scorer's defaults fill that in.
_FALLBACK_NAMES = scorer.default_display_names()

_WS = re.compile(r"\s+")


# ── quoting and phrasing ──────────────────────────────────────────────────────

def _quote(value: object) -> str:
    """A real value, collapsed to one line and cut to `MAX_VALUE_CHARS`."""
    text = _WS.sub(" ", str(value if value is not None else "")).strip()
    if len(text) > MAX_VALUE_CHARS:
        text = text[:MAX_VALUE_CHARS - 1].rstrip() + "…"
    return f"'{text}'"


def _hours(hours: float) -> str:
    """`24.0` -> "24h". The SLA is configurable and may be fractional."""
    return f"{hours:g}h"


def _span(age: timedelta) -> str:
    whole = int(age.total_seconds() // 3600)
    return f"{whole}h" if whole >= 1 else "under an hour"


def _ago(age: timedelta) -> str:
    whole = int(age.total_seconds() // 3600)
    return f"{whole}h ago" if whole >= 1 else "less than an hour ago"


# ── ticket facts, derived once ────────────────────────────────────────────────

def _parse_json(raw: object, want: type) -> tuple[object, bool]:
    """(value, readable). `readable` False means the stored JSON is unusable.

    The flag exists because "unreadable" and "empty" must not read the same: a
    corrupt custom_fields document would otherwise be reported as "functionalities
    is empty", which is an assertion about the ticket that nobody checked.
    """
    if isinstance(raw, want):
        return raw, True
    if raw is None or raw == "":
        return want(), True
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return want(), False
    return (parsed, True) if isinstance(parsed, want) else (want(), False)


class _Context:
    """The inputs every handler reads, walked once per ticket.

    Mentions and thread text cost a BeautifulSoup pass each, and four of the
    seven checks want them, so they are built here rather than per check.
    """

    def __init__(self, ticket: dict | None, messages: list[dict] | None):
        self.ticket: dict = ticket or {}
        self.messages: list[dict] = [
            m for m in (messages or []) if isinstance(m, dict)
        ]

        fields, self.fields_ok = _parse_json(self.ticket.get("custom_fields"), dict)
        self.fields: dict = fields
        external, _ = _parse_json(self.ticket.get("external_issues"), list)
        self.external: list = external

        self.state = str(self.ticket.get("state") or "")
        self.user_mentions = scorer._mentioned_slack_ids(self.messages)
        self.group_mentions = scorer._mentioned_group_ids(self.messages)
        self.thread_text = " ".join(
            scorer._html_text(m.get("message_html")) for m in self.messages
        )
        # R7/R8 search the body and the thread together, exactly as scoring does.
        self.all_text = (
            scorer._html_text(self.ticket.get("body_html")) + " " + self.thread_text
        )
        self.oncall_link = _cf_text(self.field("oncall_slack_chat_link"))

    def field(self, name: str) -> dict | None:
        """One custom field, or None. A non-dict value is treated as absent."""
        value = self.fields.get(name)
        return value if isinstance(value, dict) else None


def _cf_text(field: dict | None) -> str:
    """The value scoring reads off a custom field, as text."""
    if not field:
        return ""
    return str(scorer._cf_val(field) or field.get("value") or "")


def _is_customer(msg: dict) -> bool:
    """Whether a customer wrote a message.

    `scorer._is_customer_msg` looks for an `author.contact` payload, which only
    exists on the live Pylon issue. `messages` rows keep that decision in
    `is_customer` (app.py writes `1 if "contact" in author else 0`), so read the
    column when it is there and fall back for a caller passing raw API messages.
    """
    if "is_customer" in msg:
        return bool(msg.get("is_customer"))
    return scorer._is_customer_msg(msg)


def _roster_label(roster_key: str, slack_id: str) -> str:
    """Configured display name for a roster id, or the id itself."""
    for entry in qc_rules.labeled_entries(roster_key, fallback=_FALLBACK_NAMES):
        if entry["id"] == slack_id:
            return entry["name"]
    return slack_id


def _mentioned_member(ctx: _Context, roster_key: str,
                      *, groups: bool = False) -> str | None:
    """Label of the lowest-sorting roster member @-mentioned in the thread.

    Sorted rather than set-order so the same ticket always names the same person.
    """
    seen = ctx.group_mentions if groups else ctx.user_mentions
    hits = sorted(seen & qc_rules.id_set(roster_key))
    return _roster_label(roster_key, hits[0]) if hits else None


# ── R1 / R2: one custom field must be filled ─────────────────────────────────

def _filled_field(ctx: _Context, name: str) -> tuple[str | None, str]:
    if not ctx.fields_ok:
        return None, _CF_UNREADABLE
    field = ctx.field(name)
    if not scorer._cf_filled(field):
        return "Fail", f"{name} is empty"
    shown = scorer._cf_val(field)
    if not shown:
        # `_cf_filled` also accepts a multi-select's `values` list, which has no
        # single interpreted value to quote.
        values = [str(v) for v in (field.get("values") or []) if v]
        shown = ", ".join(values)
    if not shown:
        return "Pass", f"{name} is filled"
    return "Pass", f"{name} = {_quote(shown)}"


def _r1(ctx: _Context, stored: str) -> tuple[str | None, str]:
    return _filled_field(ctx, "functionalities")


def _r2(ctx: _Context, stored: str) -> tuple[str | None, str]:
    return _filled_field(ctx, "request_category")


# ── R3: the ticket belongs to a real external customer ───────────────────────

def _r3(ctx: _Context, stored: str) -> tuple[str | None, str]:
    account_id = ctx.ticket.get("account_id") or (
        ctx.ticket.get("account") or {}
    ).get("id")
    name = ctx.ticket.get("account_name")
    acc_type = ctx.ticket.get("account_type")

    if not account_id:
        return "Fail", "the ticket has no account"
    if name is None and acc_type is None:
        # The accounts join produced nothing, which is the `not account` branch
        # of scorer.r3 — the account was never fetched alongside the ticket.
        return "Fail", f"no account record is stored for account id {_quote(account_id)}"

    shown = _quote(name) if name else f"account id {_quote(account_id)}"
    if account_id in qc_rules.internal_account_ids():
        return "Fail", f"account {shown} matches the internal-account list"
    if acc_type == "internal":
        return "Fail", f"account {shown} is typed internal"
    lowered = str(name or "").lower()
    for fragment in qc_rules.invalid_name_fragments():
        if fragment in lowered:
            return "Fail", (
                f"account {shown} contains the invalid-name fragment "
                f"{_quote(fragment)}"
            )
    return "Pass", f"account {shown} is an external customer account"


# ── R4: the customer's last message was answered inside the SLA ──────────────

def _r4(ctx: _Context, stored: str) -> tuple[str | None, str]:
    if ctx.state in scorer.TERMINAL_STATES | scorer.WAITING_CUSTOMER:
        return "Pass", f"state {_quote(ctx.state)} leaves no reply owed by support"

    dated = [
        (when, m)
        for m in scorer._public_substantive(ctx.messages)
        if (when := scorer._parse_ts(m.get("timestamp"))) is not None
    ]
    if not dated:
        return "N/A", "no dated public messages to measure against"

    when, latest = max(dated, key=lambda pair: pair[0])
    age = datetime.now(timezone.utc) - when
    if not _is_customer(latest):
        return "Pass", f"last public message was from support, {_ago(age)}"

    sla = qc_rules.sla_hours()
    if age > timedelta(hours=sla):
        return "Fail", (
            f"customer's last message has gone {_span(age)} unanswered "
            f"against a {_hours(sla)} SLA"
        )
    return "Pass", (
        f"customer's last message is {_span(age)} old, inside the "
        f"{_hours(sla)} SLA"
    )


# ── R5: the state matches who actually holds the ticket ──────────────────────

def _rootly_or_jira(ctx: _Context) -> str | None:
    """The Rootly/Jira evidence on the ticket, named, or None if there is none.

    The ladder mirrors scorer's: the two custom fields, then `external_issues`,
    then the body and thread text.
    """
    field = ctx.field("does_rootly_exist")
    if field and field.get("value") == "Yes":
        return "does_rootly_exist = 'Yes'"

    ref = ctx.field("rootly.incident_reference")
    if ref and ref.get("value"):
        return f"rootly.incident_reference = {_quote(ref['value'])}"

    for ext in ctx.external:
        if not isinstance(ext, dict):
            continue
        source = (ext.get("source") or "").lower()
        link = ext.get("link") or ""
        if source in ("jira", "rootly") or scorer.JIRA_RE.search(link) \
                or scorer.ROOTLY_RE.search(link):
            return f"a linked {source or 'external'} issue {_quote(link)}"

    match = scorer.ROOTLY_RE.search(ctx.all_text) or scorer.JIRA_RE.search(ctx.all_text)
    if match:
        return f"the thread references {_quote(match.group(0))}"
    return None


def _handoff_unresolved(ctx: _Context, stored: str,
                        fail_phrase: str) -> tuple[str | None, str]:
    """Everything reconstructable is ruled out; only the Slack fetch is left.

    The stored verdict is consulted here, and only here, because it is itself
    evidence about the un-recorded fetch: a Fail means the thread supplied
    nothing either, so the local absence is the whole story. A Pass may have
    come from the thread, and that reason is gone.
    """
    if not ctx.oncall_link:
        return "Fail", fail_phrase
    if stored == "Fail":
        return "Fail", f"{fail_phrase}, and the linked oncall Slack thread supplied none"
    return None, _ONCALL_THREAD_ONLY


def _r5_csm(ctx: _Context, stored: str) -> tuple[str | None, str]:
    where = f"state {_quote(ctx.state)} and"
    who = _mentioned_member(ctx, "cs_user_ids")
    if who:
        return "Pass", f"{where} CS roster member {who} is @-mentioned in the thread"
    who = _mentioned_member(ctx, "impl_user_ids")
    if who:
        return "Pass", (
            f"{where} Implementation roster member {who} is @-mentioned in the thread"
        )
    group = _mentioned_member(ctx, "impl_group_ids", groups=True)
    if group:
        return "Pass", f"{where} the {group} group is @-mentioned in the thread"
    for tag in ("@cs", "@implementation"):
        if tag in ctx.thread_text:
            return "Pass", f"{where} the thread text contains {_quote(tag)}"
    return _handoff_unresolved(
        ctx, stored,
        f"{where} no CS or Implementation member is @-mentioned in the thread",
    )


def _r5_product(ctx: _Context, stored: str) -> tuple[str | None, str]:
    where = f"state {_quote(ctx.state)} and"
    who = _mentioned_member(ctx, "pt_user_ids")
    if who:
        return "Pass", f"{where} product member {who} is @-mentioned in the thread"
    group = _mentioned_member(ctx, "pt_group_ids", groups=True)
    if group:
        return "Pass", f"{where} the {group} group is @-mentioned in the thread"
    if "@pt" in ctx.thread_text:
        return "Pass", f"{where} the thread text contains '@pt'"
    return _handoff_unresolved(
        ctx, stored, f"{where} no product member is @-mentioned in the thread",
    )


def _r5_engg(ctx: _Context, stored: str) -> tuple[str | None, str]:
    where = f"state {_quote(ctx.state)} and"
    who = _mentioned_member(ctx, "eng_user_ids")
    if who:
        return "Pass", f"{where} engineer {who} is @-mentioned in the thread"
    group = _mentioned_member(ctx, "eng_group_ids", groups=True)
    if group:
        return "Pass", f"{where} the {group} group is @-mentioned in the thread"

    # The Slack fetch sits between the mentions and the Rootly/Jira fallback, so
    # a Pass may have short-circuited there. Naming the fallback is still true:
    # its presence alone satisfies the rule.
    reference = _rootly_or_jira(ctx)
    if reference:
        return "Pass", f"{where} {reference}"

    return _handoff_unresolved(
        ctx, stored,
        f"{where} no engineer or eng group is @-mentioned and the ticket carries "
        "no Rootly or Jira reference",
    )


def _r5(ctx: _Context, stored: str) -> tuple[str | None, str]:
    state = ctx.state
    quoted = _quote(state)

    if state in scorer._R5_NA_STATES:
        return "N/A", (
            f"state {quoted} is exempt from the ownership check, "
            "so the rule does not apply"
        )
    if state in scorer.WAITING_CUSTOMER:
        return "Pass", f"state {quoted} — support replied and the ball is with the customer"
    if state == "new":
        owner = ctx.ticket.get("assignee_name") or ctx.ticket.get("assignee_id")
        if owner:
            return "Pass", f"state {quoted} and the ticket is assigned to {_quote(owner)}"
        return "Fail", f"state {quoted} with no assignee"
    if state == "waiting_on_you":
        return "Fail", f"state {quoted} — support has not answered the customer yet"
    if state == "waiting_on_csm":
        return _r5_csm(ctx, stored)
    if state == "waiting_on_product":
        return _r5_product(ctx, stored)
    if state in scorer.ENGG_STATES:
        return _r5_engg(ctx, stored)

    group_states = qc_rules.group_states()
    if state in group_states:
        tags = [str(t) for t in (group_states.get(state) or [])]
        present = [t for t in tags if t in ctx.thread_text]
        if present:
            return "Pass", f"state {quoted} and the thread contains {_quote(present[0])}"
        expected = ", ".join(tags) or "a group tag"
        return "Fail", f"state {quoted} and none of {expected} appears in the thread"

    return "N/A", f"state {quoted} is not one the ownership rule covers"


# ── R7: an engineering ticket carries a Rootly or Jira reference ─────────────

def _r7(ctx: _Context, stored: str) -> tuple[str | None, str]:
    if ctx.state not in scorer.ENGG_STATES:
        return "N/A", (
            f"state {_quote(ctx.state)} is not an engineering state, "
            "so the rule does not apply"
        )
    if not ctx.fields_ok:
        return None, _CF_UNREADABLE

    field = ctx.field("does_rootly_exist")
    if field and field.get("value") == "No":
        return "Fail", "does_rootly_exist = 'No'"

    reference = _rootly_or_jira(ctx)
    if reference:
        return "Pass", reference
    return "Fail", "no Rootly or Jira reference on the ticket or in the thread"


# ── R8: an oncall escalation is complete in all four places ──────────────────

def _r8(ctx: _Context, stored: str) -> tuple[str | None, str]:
    if not ctx.fields_ok:
        return None, _CF_UNREADABLE

    resolution = (scorer._cf_val(ctx.field("resolution_category")) or "").strip()
    ref_field = ctx.field("rootly.incident_reference")
    ref = (ref_field or {}).get("value") or ""

    escalated = resolution.lower() == "escalated to oncall"
    if not escalated and not ref:
        return "N/A", "no oncall evidence on the ticket"

    trigger = (
        f"resolution_category = {_quote(resolution)}" if escalated
        else f"rootly.incident_reference = {_quote(ref)}"
    )
    rootly_yes = (ctx.field("does_rootly_exist") or {}).get("value") == "Yes"
    has_jira = scorer._has_jira(
        {"body_html": ctx.ticket.get("body_html")}, ctx.messages, ctx.external
    )
    category = (scorer._cf_val(ctx.field("request_category")) or "").strip()

    missing = []
    if not rootly_yes:
        missing.append("does_rootly_exist is not 'Yes'")
    if not ref:
        missing.append("rootly.incident_reference is empty")
    if not has_jira:
        missing.append("no Jira link on the ticket or in the thread")
    if category.lower() not in qc_rules.oncall_categories():
        missing.append(
            "request_category is empty" if not category
            else f"request_category {_quote(category)} is not an oncall category"
        )

    if missing:
        return "Fail", f"{trigger} but {', '.join(missing)}"
    return "Pass", (
        f"{trigger} with does_rootly_exist = 'Yes', a Jira link, and "
        f"request_category {_quote(category)}"
    )


# ── entry point ───────────────────────────────────────────────────────────────

# check -> handler. A dict rather than a branch tree so the set of explained
# checks is one readable line and mirrors how `scorer` is organised. The keys are
# the deterministic checks the leaderboard counts (leaderboard.RULE_KEYS); R6 and
# R9 are retired and always N/A, so there is nothing to explain.
_HANDLERS = {
    "r1": _r1,
    "r2": _r2,
    "r3": _r3,
    "r4": _r4,
    "r5": _r5,
    "r7": _r7,
    "r8": _r8,
}

CHECK_KEYS = tuple(_HANDLERS)


def _reconcile(stored: str, computed: str | None, phrase: str) -> str:
    """One sentence that explains `stored` without ever restating it.

    `computed is None` means the handler could not reconstruct the decision at
    all. A `computed` that differs from `stored` means the ticket has moved since
    it was scored, so the sentence separates the recorded verdict from what the
    ticket shows now instead of asserting the newer reading.
    """
    if not stored:
        return NOT_SCORED
    if computed is None:
        opening = f"{_VERB.get(stored, stored)} on evidence not recorded at scoring time"
        return f"{opening}: {phrase}" if phrase else opening
    if computed == stored:
        return phrase
    return f"recorded as {stored} at scoring time; the ticket now shows {phrase}"


def for_ticket(ticket: dict, messages: list[dict]) -> dict[str, str]:
    """`{check_key: one-sentence evidence}` for the deterministic R-checks.

    `ticket` is a `db.get_day_tickets()` row — `custom_fields` and
    `external_issues` as JSON strings, `account_name`/`account_type` joined in,
    and the stored `r1`..`r9` verdicts. `messages` is `db.get_ticket_messages()`
    rows for the same ticket.

    Every key in `CHECK_KEYS` is always present with a non-empty string. The
    string explains the STORED verdict: it is never a re-score, and where the
    reason cannot be recovered — a Slack thread read at scoring time, an
    unreadable custom_fields document — it says so rather than guessing.

    Never raises. Reads `rules` for current configuration; touches nothing else.
    """
    try:
        ctx = _Context(ticket, messages)
    except Exception:
        logger.exception("Evidence context failed for ticket %s",
                         (ticket or {}).get("id"))
        return {key: UNRECONSTRUCTABLE for key in CHECK_KEYS}

    out: dict[str, str] = {}
    for key, handler in _HANDLERS.items():
        stored = str((ticket or {}).get(key) or "").strip()
        try:
            computed, phrase = handler(ctx, stored)
            out[key] = _reconcile(stored, computed, phrase)
        except Exception:
            # A panel renders seven checks; one unexplainable check must not cost
            # the other six, and must never cost the page.
            logger.exception("Evidence failed for %s on ticket %s",
                             key, ctx.ticket.get("id"))
            out[key] = UNRECONSTRUCTABLE
    return out
