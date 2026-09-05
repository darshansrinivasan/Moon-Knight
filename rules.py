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

# The document as it was before the last save, kept for a single undo. Not a
# version history: one step back, because `set_raw_setting` is INSERT OR REPLACE
# and the audit log stores only a hash, so before this there was no way to
# recover from a bad save at all. That was tolerable while edits were rosters
# and prose; once one save can change what "Pass" means across months of
# tickets, "nothing breaks" has to include "a bad save is recoverable".
PREV_KEY = "qc_rules_json_prev"

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

        # Checks switched off. This is a read-time mask, not a scoring change:
        # stored verdicts are never rewritten, every consumer filters through
        # `enabled_rule_keys()`, and saving resyncs the stored overalls with no
        # AI calls. That makes turning a check off immediate, free and
        # reversible — see SPEC_v5 decision D1 for why writing N/A at fetch time
        # was rejected (it moves qc_fingerprint and rebills identical prompts).
        "disabled_checks":      [],

        # What each Pylon status means to the checks — see
        # scorer.DEFAULT_STATUS_POLICY. Replaces five overlapping hardcoded
        # sets, and gives a status Pylon adds later a row to decide about
        # rather than a silent default.
        "status_policy": {k: dict(v)
                          for k, v in scorer.DEFAULT_STATUS_POLICY.items()},

        # Which Pylon custom field each check reads. Slugs were literals in
        # five files, so a field Pylon renames or retires could only be
        # followed with a deploy — and the check meanwhile fails every ticket,
        # because an absent field reads as an empty one. Editable here, and
        # the Rules page warns when a mapped slug is no longer one Pylon
        # defines.
        **{f"field_{k}": v for k, v in scorer.DEFAULT_FIELD_MAP.items()},

        # R8's conditions, each required only while it is listed here. The
        # `does_rootly_exist` field is still defined in Pylon but has stopped
        # being filled, so dropping `rootly_yes` is how an admin says that
        # without waiting for a deploy.
        "r8_conditions":        list(scorer.R8_CONDITIONS),

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


def disabled_checks() -> set:
    """Checks the admin has switched off. Only toggleable keys count."""
    import scorer
    allowed = set(scorer.TOGGLEABLE_CHECKS)
    return {str(k).strip().lower() for k in current().get("disabled_checks", [])
            if str(k).strip().lower() in allowed}


def enabled_rule_keys(keys=None) -> tuple:
    """`keys` minus whatever is switched off, order preserved.

    The single gate every consumer reads: the overall verdict, the leaderboard
    sums, the Slack counts, the calendar's failure column, the drill-down and
    the dashboard matrix. A disabled check has to disappear from all of them at
    once or it keeps failing tickets somewhere nobody thought to look.

    Defaults to the toggleable set. Pass the caller's own tuple to preserve
    keys this does not govern (r9 still appears in stored rows).
    """
    import scorer
    if keys is None:
        keys = scorer.TOGGLEABLE_CHECKS
    off = disabled_checks()
    return tuple(k for k in keys if k not in off)


def check_enabled(key: str) -> bool:
    return str(key).strip().lower() not in disabled_checks()


def field(name: str) -> str:
    """The Pylon slug this check should read for `name`.

    Falls back to the shipped default rather than returning empty: a blank
    mapping would make the check read a field called "", which fails every
    ticket silently. A mapping an admin has actually cleared is treated as
    "use the default", the same convention the prompt sections use.
    """
    import scorer
    stored = str(current().get(f"field_{name}") or "").strip()
    return stored or scorer.DEFAULT_FIELD_MAP.get(name, "")


def field_map() -> dict:
    """Every logical field name to the slug currently configured for it."""
    import scorer
    return {name: field(name) for name in scorer.DEFAULT_FIELD_MAP}


def status_policy(state: str) -> dict:
    """How the checks should treat this status.

    Unknown statuses get `scorer.STATUS_FALLBACK`, which is deliberately the
    lenient reading — scored, but never failed by R5 or R7 for a rule nobody
    has written yet. `migration` reached production before anyone configured
    it and landed on that reading by accident; now it is a decision.
    """
    import scorer
    stored = current().get("status_policy") or {}
    row = stored.get(str(state or "").strip().lower())
    if not isinstance(row, dict):
        return dict(scorer.STATUS_FALLBACK)
    return {**scorer.STATUS_FALLBACK, **row}


def all_status_policies() -> dict:
    import scorer
    stored = current().get("status_policy") or {}
    return {k: {**scorer.STATUS_FALLBACK, **v}
            for k, v in stored.items() if isinstance(v, dict)}


def r8_conditions() -> set:
    """Which of R8's four conditions are still required.

    Falls back to all of them when the stored list is empty or unrecognised:
    an empty requirement set would make R8 pass every oncall ticket, which
    looks like a working check and is not one. Validation rejects the empty
    list on save; this is the second line of defence for a document written
    before the key existed.
    """
    import scorer
    allowed = set(scorer.R8_CONDITIONS)
    chosen = {str(c).strip() for c in current().get("r8_conditions", [])}
    chosen &= allowed
    return chosen or set(scorer.R8_CONDITIONS)


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

    import scorer
    if "disabled_checks" in candidate:
        raw = candidate.get("disabled_checks")
        if not isinstance(raw, list):
            errors.append("disabled_checks must be a list of check keys")
        else:
            for key in raw:
                if str(key).strip().lower() not in scorer.TOGGLEABLE_CHECKS:
                    errors.append(
                        f"disabled_checks: '{key}' is not a check that can be "
                        f"switched off (choose from "
                        f"{', '.join(scorer.TOGGLEABLE_CHECKS)})")

    if "r8_conditions" in candidate:
        raw = candidate.get("r8_conditions")
        if not isinstance(raw, list):
            errors.append("r8_conditions must be a list")
        else:
            unknown = [c for c in raw
                       if str(c).strip() not in scorer.R8_CONDITIONS]
            for c in unknown:
                errors.append(f"r8_conditions: '{c}' is not one of "
                              f"{', '.join(scorer.R8_CONDITIONS)}")
            # An R8 with no conditions passes every oncall ticket, which reads
            # as a working check and is not one. Switching R8 off is the honest
            # way to say "stop checking this".
            if not unknown and not [c for c in raw if str(c).strip()]:
                errors.append(
                    "r8_conditions cannot be empty — an R8 with no conditions "
                    "would pass every oncall ticket. Switch R8 off instead.")

    # A slug is Pylon's own identifier shape: lowercase, digits, underscore,
    # and a dot for namespaced fields like rootly.incident_reference.
    _SLUG = re.compile(r"^[a-z0-9_]+(\.[a-z0-9_]+)*$")
    for name in scorer.DEFAULT_FIELD_MAP:
        key = f"field_{name}"
        if key not in candidate:
            continue
        value = str(candidate.get(key) or "").strip()
        if value and not _SLUG.match(value):
            errors.append(
                f"{key}: '{value}' is not a Pylon field slug — expected "
                f"lowercase letters, digits and underscores, e.g. "
                f"'{scorer.DEFAULT_FIELD_MAP[name]}'")

    if "status_policy" in candidate:
        policy = candidate.get("status_policy")
        if not isinstance(policy, dict):
            errors.append("status_policy must map a status to its settings")
        else:
            for state, row in policy.items():
                if not _STATE.match(str(state)):
                    errors.append(f"status_policy: '{state}' is not a status name")
                    continue
                if not isinstance(row, dict):
                    errors.append(f"status_policy: '{state}' must be an object")
                    continue
                exp = row.get("r5", "none")
                if exp not in scorer.R5_EXPECTATIONS:
                    errors.append(
                        f"status_policy: '{state}' expects r5 to be one of "
                        f"{', '.join(scorer.R5_EXPECTATIONS)}, not '{exp}'")
                if exp == "tags" and not [x for x in (row.get("tags") or []) if x]:
                    # "require a tag" with no tag listed fails every ticket in
                    # that status and reads as a broken check.
                    errors.append(
                        f"status_policy: '{state}' expects a literal tag but "
                        f"lists none — add a tag, or choose another expectation")
                for flag in ("in_scope", "r4_reply_owed", "r7_engineering"):
                    if flag in row and not isinstance(row[flag], bool):
                        errors.append(
                            f"status_policy: '{state}'.{flag} must be true or false")

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
    # Keep the outgoing document before overwriting it, so the save can be
    # undone in one step. Stored only on a save that is actually accepted.
    #
    # An empty document when nothing was stored yet, rather than skipping: the
    # state before the first-ever save is "everything at its default", and
    # `_load` treats an empty document exactly that way. Skipping it left the
    # first save — often the one an admin most wants to take back — as the only
    # save with no way out.
    existing = vault.get_raw_setting(RULES_KEY) or "{}"
    vault.set_raw_setting(PREV_KEY, existing, updated_by)

    vault.set_raw_setting(RULES_KEY, json.dumps(merged), updated_by)
    invalidate()
    return []


def has_previous() -> bool:
    return bool(vault.get_raw_setting(PREV_KEY))


def restore_previous(updated_by: str) -> bool:
    """Put the previous document back. Returns False when there is none.

    The document being replaced becomes the new undo target, so undo is
    reversible too — press it twice and you are back where you started, which
    is what someone comparing two settings actually wants.
    """
    prev = vault.get_raw_setting(PREV_KEY)
    if not prev:
        return False
    current_doc = vault.get_raw_setting(RULES_KEY) or ""
    vault.set_raw_setting(RULES_KEY, prev, updated_by)
    vault.set_raw_setting(PREV_KEY, current_doc, updated_by)
    invalidate()
    return True
