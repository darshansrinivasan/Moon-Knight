"""
Editable scoring rules.

Every tunable the R-checks and AI grading depend on lives here: what counts as
an internal account, the response-time SLA, the Slack rosters that satisfy R5
handoffs, the oncall categories R8 accepts, which ticket states are excluded
from AI scoring, and workspace-specific grading guidance appended to the AI
rubric.

Defaults come from the constants in scorer.py, so behaviour is unchanged until
an admin edits something. Overrides are stored as one JSON document in
app_settings and validated before they are accepted. Each run's config snapshot
records the rules hash, so a grade change is attributable to a rules change.
"""

import hashlib
import json
import re
import threading

import db
import vault

_lock = threading.Lock()
_cache: dict | None = None

RULES_KEY = "qc_rules_json"

_USER_ID  = re.compile(r"^[UW][A-Z0-9]{4,}$")
_GROUP_ID = re.compile(r"^S[A-Z0-9]{4,}$")
_STATE    = re.compile(r"^[a-z0-9_]+$")


# ── defaults (sourced from scorer's constants — single origin, no drift) ──────

def _roster_lines(ids) -> list:
    """Persist `id  name` so scoring still reads the first token and the UI can show names."""
    if isinstance(ids, dict):
        return [f"{i}  {n}" for i, n in sorted(ids.items(), key=lambda kv: str(kv[1]).lower())]
    return sorted(ids)


def defaults() -> dict:
    import scorer   # deferred: scorer imports this module at top level
    import prompts  # deferred: prompts is the origin of the rubric text
    return {
        # AI scoring scope: tickets in these states are left un-scored.
        "excluded_states":       [],

        # R3 — what counts as an internal / invalid account
        "r3_internal_account_ids":   _roster_lines(scorer.INTERNAL_ACCOUNTS),
        "r3_invalid_name_fragments": list(scorer.INVALID_NAME_FRAGMENTS),

        # R4 — response-time SLA
        "r4_sla_hours": 24,

        # R5 — who satisfies a handoff, per team. One entry per line in the UI;
        # the first token is the Slack ID, anything after it is a label.
        "cs_user_ids":    _roster_lines(scorer.CS_SLACK_USERS),
        "impl_user_ids":  _roster_lines(scorer.IMPL_SLACK_USERS),
        "impl_group_ids": _roster_lines(scorer.IMPL_SLACK_GROUPS),
        "eng_user_ids":   _roster_lines(scorer.ENG_SLACK_USERS),
        "eng_group_ids":  _roster_lines(scorer.ENG_SLACK_GROUPS),
        "pt_user_ids":    _roster_lines(scorer.PT_SLACK_USERS),
        "pt_group_ids":   _roster_lines(scorer.PT_SLACK_GROUPS),

        # R5 — delegation states that require a literal tag in the thread
        "r5_group_states": {k: list(v) for k, v in scorer._R5_GROUP_STATES.items()},

        # R8 — request categories accepted as oncall
        "r8_oncall_categories": sorted(scorer.ONCALL_CATEGORIES),

        # A-checks — appended to the grading rubric; recorded per run
        "a_guidance": "",

        # The grading prompt itself, section by section. Defaults are the exact
        # text that used to be a string literal in qc_runner, so an admin who
        # never touches these gets byte-identical grading. Clearing a section
        # restores its default rather than sending the model an empty rubric —
        # see prompts._section. The fixed half of the prompt (the idx
        # correlation, the JSON envelope, the grade vocabularies) is not here
        # because editing it would not change grading, it would break scoring.
        **dict(prompts.DEFAULT_SECTIONS),
    }


# ── access ────────────────────────────────────────────────────────────────────

def _load() -> dict:
    base = defaults()
    raw = vault.get_raw_setting(RULES_KEY)
    if raw:
        try:
            stored = json.loads(raw)
            for k in base:
                if k in stored:
                    base[k] = stored[k]
        except json.JSONDecodeError:
            pass          # a corrupt document must never break scoring
    return base


def current() -> dict:
    global _cache
    with _lock:
        if _cache is None:
            _cache = _load()
        return dict(_cache)


def invalidate() -> None:
    global _cache
    with _lock:
        _cache = None


def rules_hash(r: dict | None = None) -> str:
    doc = json.dumps(r or current(), sort_keys=True)
    return hashlib.sha256(doc.encode()).hexdigest()[:10]


def parse_entry(line: str) -> tuple[str, str]:
    """Split a stored roster line into (id, optional label)."""
    text = str(line).strip()
    if not text:
        return "", ""
    tok, _, rest = text.partition(" ")
    return tok, rest.strip()


def labeled_entries(key: str, resolved: dict[str, str] | None = None,
                    fallback: dict[str, str] | None = None) -> list[dict]:
    """Roster lines as `{id, name}` for the UI. IDs stay the persisted fact."""
    resolved = resolved or {}
    fallback = fallback or {}
    out = []
    for line in current().get(key, []):
        sid, label = parse_entry(line)
        if not sid:
            continue
        name = resolved.get(sid) or label or fallback.get(sid) or sid
        out.append({"id": sid, "name": name})
    out.sort(key=lambda e: e["name"].lower())
    return out


def id_set(key: str) -> set:
    """Roster lines -> the set of Slack IDs (first token of each line)."""
    return {sid for sid, _ in (parse_entry(x) for x in current().get(key, [])) if sid}


def value(key: str):
    return current().get(key)


def sla_hours() -> float:
    try:
        return float(current().get("r4_sla_hours", 24))
    except (TypeError, ValueError):
        return 24.0


def excluded_states() -> list:
    return [s for s in current().get("excluded_states", []) if s]


def excluded_state_clause(alias: str = "t") -> tuple:
    """SQL predicate dropping out-of-scope states, plus its params.

    Returns ("", []) when nothing is excluded, so callers can skip the clause
    entirely rather than emitting a tautology.

    This exists because four queries needed the same predicate and three of them
    wrote it out themselves. The fourth — /api/analytics — simply did not, so
    with `archived` excluded from scoring, 814 archived tickets still counted
    toward every assignee's total and appeared as a permanent "pending" backlog:
    tickets awaiting a grade that the scorer would never give them, because the
    scorer agreed they were out of scope. For August that read as 1,691 tickets
    with 860 pending on the Analytics page against 877 and 83 on the
    leaderboard, for the same month and the same data.

    Reporting and scoring must not hold separate opinions about what a day
    contains, so the opinion lives in one function.
    """
    states = excluded_states()
    if not states:
        return "", []
    return (f"{alias}.state NOT IN ({','.join('?' * len(states))})",
            list(states))


def oncall_categories() -> set:
    return {str(c).strip().lower() for c in current().get("r8_oncall_categories", [])}


def internal_account_ids() -> set:
    return {sid for sid, _ in (parse_entry(x) for x in current().get("r3_internal_account_ids", [])) if sid}


def invalid_name_fragments() -> list:
    return [str(x).strip().lower() for x in current().get("r3_invalid_name_fragments", []) if str(x).strip()]


def group_states() -> dict:
    return {k: list(v) for k, v in (current().get("r5_group_states") or {}).items()}


def guidance() -> str:
    return str(current().get("a_guidance") or "").strip()


# ── validation: edits are accepted by the system or rejected with reasons ─────

def validate(candidate: dict) -> list:
    errors: list = []

    def check_lines(key, pattern, kind):
        for line in candidate.get(key, []):
            tok, _ = parse_entry(line)
            if not tok:
                continue
            if not pattern.match(tok):
                errors.append(f"{key}: '{tok}' does not look like a Slack {kind} ID")

    try:
        hours = float(candidate.get("r4_sla_hours", 24))
        if not (1 <= hours <= 336):
            errors.append("r4_sla_hours must be between 1 and 336 hours")
    except (TypeError, ValueError):
        errors.append("r4_sla_hours must be a number")

    for s in candidate.get("excluded_states", []):
        if not _STATE.match(str(s)):
            errors.append(f"excluded_states: '{s}' is not a valid state name")

    for x in candidate.get("r3_internal_account_ids", []):
        tok, _ = parse_entry(x)
        if not tok:
            continue
        if not re.match(r"^[0-9a-fA-F-]{8,40}$", tok):
            errors.append(f"r3_internal_account_ids: '{tok}' is not an account id")

    for x in candidate.get("r3_invalid_name_fragments", []):
        if not str(x).strip():
            errors.append("r3_invalid_name_fragments: empty fragment")

    for key in ("cs_user_ids", "impl_user_ids", "eng_user_ids", "pt_user_ids"):
        check_lines(key, _USER_ID, "user")
    for key in ("impl_group_ids", "eng_group_ids", "pt_group_ids"):
        check_lines(key, _GROUP_ID, "group")

    gs = candidate.get("r5_group_states", {})
    if not isinstance(gs, dict):
        errors.append("r5_group_states must map state -> list of tags")
    else:
        for state, tags in gs.items():
            if not _STATE.match(str(state)):
                errors.append(f"r5_group_states: '{state}' is not a valid state name")
            for t in (tags or []):
                if not str(t).startswith("@"):
                    errors.append(f"r5_group_states: tag '{t}' must start with @")

    if len(str(candidate.get("a_guidance", ""))) > 4000:
        errors.append("a_guidance must be 4000 characters or fewer")

    import prompts
    for key in prompts.SECTION_KEYS:
        if key in candidate:
            errors.extend(prompts.validate_section(key, candidate[key]))

    return errors


def save(candidate: dict, updated_by: str) -> list:
    """Validate and persist. Returns [] on success, else the error list.

    The candidate is merged over what is already stored, not written in its
    place. This used to be a straight replace, which meant any key absent from
    the payload silently reverted to its default — no error, no trace, and the
    only thing standing between an admin's rubric and a reset was every client
    remembering to send all of it back. That is an invariant living in the
    wrong place. Now a save can only change the keys it actually carries.

    Absent and empty are deliberately different: a key that is present with an
    empty value is a real edit (clearing `excluded_states` must clear it), while
    a key that is missing means "not part of this change".
    """
    known = set(defaults().keys())
    cleaned = {k: v for k, v in candidate.items() if k in known}

    merged = _load()
    merged.update(cleaned)

    # Validate the whole merged document rather than the fragment: what gets
    # stored is what has to be coherent.
    errors = validate(merged)
    if errors:
        return errors
    vault.set_raw_setting(RULES_KEY, json.dumps(merged), updated_by)
    invalidate()
    return []
