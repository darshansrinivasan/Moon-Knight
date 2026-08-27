"""The AI grading prompt: the parts admins own, and the parts the wire owns.

The rubric used to be one 60-line string literal in `qc_runner`, which made the
grading policy a code change. It is not a code change — it is the product. An
admin who decides that internal tickets should not be penalised for a missing
formal reply is editing policy, and should not need a deploy to do it.

But a prompt is not uniformly editable. Two kinds of text were tangled in that
literal:

  * **Policy** — what "Good" means, when to grade Needs Review, how strict A5
    is. Admins own this. Editing it changes grades, which is the point.
  * **Wire contract** — the `idx:` correlation mechanism, the JSON envelope, the
    grade vocabularies. Nothing about these expresses an opinion about support
    quality; they exist so `_parse_response` can match results back to tickets
    and so the DB gets values `_compute_overall` understands. An admin who edits
    these does not change grading, they break scoring.

So the editable sections live in `rules` (validated, versioned, hashed into every
run's config snapshot) and the fixed blocks live here as code. The UI shows both,
and shows which is which.

The grade vocabularies are declared once, in `GRADES`, and everything else is
derived from them: the `→ Pass / Fail / Needs Review` header on each check, the
`"a1": "Pass|Fail|Needs Review"` line in the return format, and the enum-
constrained `RESPONSE_SCHEMA` the API enforces. That is deliberate. When those
three drifted apart by hand, the prompt could promise a grade the schema
rejected, and the failure surfaced three frames away as an empty response.
Deriving them means an admin editing a check's prose cannot desynchronise them,
because the prose is not where the vocabulary lives.
"""

import hashlib

# ── the grade vocabulary: one origin for prompt, return format, and schema ────

GRADES: dict[str, tuple[str, ...]] = {
    "a1": ("Pass", "Fail", "Needs Review"),
    "a2": ("Positive", "Neutral", "Concerned", "Frustrated", "Urgent"),
    "a3": ("Good", "Needs Improvement", "Poor"),
    "a4": ("Pass", "Fail", "Needs Review"),
    "a5": ("Pass", "Fail", "Needs Review", "N/A"),
}

TITLES: dict[str, str] = {
    "a1": "Category accuracy",
    "a2": "Customer sentiment",
    "a3": "Response quality",
    "a4": "Status vs conversation",
    "a5": "Not closed prematurely",
}

A_CHECK_KEYS = tuple(GRADES)

RESPONSE_SCHEMA = {
    "type": "ARRAY",
    "items": {
        "type": "OBJECT",
        "properties": {
            "idx": {"type": "INTEGER"},
            **{k: {"type": "STRING", "enum": list(v)} for k, v in GRADES.items()},
            "ai_notes": {"type": "STRING"},
        },
        "required": ["idx", *A_CHECK_KEYS, "ai_notes"],
    },
}


# ── editable sections: defaults are the text the literal shipped with ─────────

SECTION_KEYS = ("a_preamble", *(f"{k}_rubric" for k in A_CHECK_KEYS),
                "a_notes_rubric", "a_consistency")

MAX_SECTION_CHARS = 2000

DEFAULT_SECTIONS: dict[str, str] = {
    "a_preamble":
        "You are a support quality-control analyst for SpotDraft, a "
        "contract-management SaaS. Evaluate support tickets and return ONLY a "
        "JSON array — no prose, no markdown fences.",

    "a1_rubric":
        "Compare functionalities and request_category against what the customer "
        "actually asked. Fail if clearly mismatched. Needs Review if multiple "
        "categories reasonably apply.",

    "a2_rubric":
        "Base on customer language, escalation cues, time-in-queue.",

    "a3_rubric":
        "Good: clear, accurate, empathetic, assigns ownership, includes next steps.\n"
        "Needs Improvement: vague or incomplete but serviceable.\n"
        "Poor: wrong guidance, missed ask, no next step, confusing handoff.\n"
        "IMPORTANT — Internal tickets: if \"Internal ticket: Yes\" appears in the ticket block,\n"
        "the Pylon thread IS the communication channel (it mirrors a Slack thread). The requester\n"
        "is a colleague, not an external customer. A brief but clear confirmation of the action\n"
        "taken in the thread is fully adequate — rate A3=Good. Do NOT penalize for absence of a\n"
        "formal email reply or elaborate closure message.",

    "a4_rubric":
        "Does the current ticket state match who actually owns the next action?",

    "a5_rubric":
        "N/A for open tickets. Pass if closed with resolution evidence or "
        "documented no-response follow-up. Fail if customer ask still open at "
        "closure.",

    "a_notes_rubric":
        "one concise string. For every Fail or Needs Review check, write a specific sentence that names:\n"
        "  (1) what exactly went wrong (quote the customer's missed ask, the incorrect category, the unanswered message, etc.)\n"
        "  (2) what the support agent should do to fix it.\n"
        "  Format: \"A<n> <grade>: <specific finding> — <specific fix>.\"\n"
        "  Be concrete — never write generic phrases like \"fix needed\" or \"review required\" without explaining what to fix or review.\n"
        "  If all AI checks pass, write a one-sentence summary of what was handled well.",

    "a_consistency":
        "Grade strictly from the evidence in the ticket block. Identical input must\n"
        "produce identical grades.\n"
        "When evidence is genuinely ambiguous between Pass and Fail, grade Needs\n"
        "Review — never guess. Reserve Fail for cases the rubric clearly covers.\n"
        "Do not let one ticket's grade influence another's; each is independent.",
}

# What each section is for, shown beside its editor. An admin editing grading
# policy deserves to know what the field does before they change grades with it.
SECTION_LABELS: dict[str, tuple[str, str]] = {
    "a_preamble": (
        "Role and context",
        "Opens the prompt. Tells the model who it is and what the company does.",
    ),
    "a1_rubric": (
        "A1 — Category accuracy",
        "When the picked functionality and request category count as right.",
    ),
    "a2_rubric": (
        "A2 — Customer sentiment",
        "What to read sentiment from. Sentiment never affects the overall verdict.",
    ),
    "a3_rubric": (
        "A3 — Response quality",
        "The scoring bar for the reply itself. 'Poor' fails the ticket outright.",
    ),
    "a4_rubric": (
        "A4 — Status vs conversation",
        "Whether the Pylon state matches who really owes the next action.",
    ),
    "a5_rubric": (
        "A5 — Not closed prematurely",
        "When a closure is legitimate. 'Fail' fails the ticket outright.",
    ),
    "a_notes_rubric": (
        "AI notes",
        "How the model must write the note reviewers read. Shape, not verdict.",
    ),
    "a_consistency": (
        "Consistency rules",
        "The tie-breaking policy for ambiguous evidence. Applies to every check.",
    ),
}

# ── fixed blocks: mechanism, not policy ──────────────────────────────────────

_INPUT_FORMAT = (
    "Each ticket in the input is prefixed with === TICKET #<number> idx:<index> ===\n"
    "Return one result object per ticket in the SAME ORDER, using the \"idx\" "
    "field to identify each."
)

_NO_OVERALL = (
    "Do NOT return an overall verdict. The overall result is computed\n"
    "deterministically from your grades plus the R-checks, so that identical grades\n"
    "always produce the same verdict."
)

_MESSAGE_ROLES = (
    "Message roles: is_customer=1 → message visible to requester; is_private=1 → "
    "internal note (exclude from customer-response logic).\n"
    "For internal tickets the \"requester\" is a colleague — treat is_customer=1 "
    "messages as internal thread replies, not external customer communication."
)


def _return_format() -> str:
    """The JSON envelope, spelled from GRADES so it cannot contradict the schema."""
    fields = ",\n".join(
        f'    "{k}": "{"|".join(GRADES[k])}"' for k in A_CHECK_KEYS
    )
    return (
        "Return format (idx matches the input idx value, NOT the ticket number):\n"
        "[\n  {\n"
        '    "idx": 0,\n'
        f"{fields},\n"
        '    "ai_notes": "..."\n'
        "  }\n]"
    )


FIXED_BLOCKS: tuple[tuple[str, str], ...] = (
    ("Input format", _INPUT_FORMAT),
    ("Overall verdict", _NO_OVERALL),
    ("Message roles", _MESSAGE_ROLES),
    ("Return format", _return_format()),
)


# ── assembly ─────────────────────────────────────────────────────────────────

def _section(overrides: dict | None, key: str) -> str:
    """An override wins only if it has content — a blank field is not a rubric."""
    if overrides:
        text = str(overrides.get(key) or "").strip()
        if text:
            return text
    return DEFAULT_SECTIONS[key]


def check_header(key: str) -> str:
    return f"A{key[1:]} — {TITLES[key]}  →  {' / '.join(GRADES[key])}"


def system_prompt(overrides: dict | None = None) -> str:
    """The full system instruction: editable policy inside a fixed envelope."""
    parts = [
        _section(overrides, "a_preamble"),
        "",
        _INPUT_FORMAT,
        "",
        "CHECKS:",
    ]
    for key in A_CHECK_KEYS:
        parts += ["", check_header(key),
                  _indent(_section(overrides, f"{key}_rubric"))]

    parts += [
        "",
        _NO_OVERALL,
        "",
        "AI NOTES: " + _section(overrides, "a_notes_rubric"),
        "",
        "CONSISTENCY RULES:",
        _indent(_section(overrides, "a_consistency")),
        "",
        _MESSAGE_ROLES,
        "",
        _return_format(),
    ]

    guidance = str((overrides or {}).get("a_guidance") or "").strip()
    if guidance:
        parts += [
            "",
            "WORKSPACE-SPECIFIC GUIDANCE (set by admins — apply alongside the rubric):",
            guidance,
        ]
    return "\n".join(parts)


def _indent(text: str) -> str:
    return "\n".join("  " + line if line.strip() else line
                     for line in text.split("\n"))


def fingerprint(overrides: dict | None = None) -> str:
    """Hash of the assembled prompt.

    Folded into each ticket's `qc_fingerprint` so that editing the rubric marks
    grades as stale. Without it, an admin could rewrite what "Poor" means, re-run
    the date, and get the old grades back from the skip path — the edit would
    appear to do nothing. Rescoring is per-date and admin-triggered, so this
    invalidates nothing until someone deliberately re-runs a day.
    """
    return hashlib.sha256(system_prompt(overrides).encode("utf-8")).hexdigest()[:12]


def validate_section(key: str, text: str) -> list:
    """Reasons this section cannot be saved. Empty list means it can."""
    errors = []
    if key not in DEFAULT_SECTIONS:
        return [f"{key}: not an editable prompt section"]
    body = str(text or "").strip()
    if not body:
        return []          # blank means "use the default", handled in _section
    if len(body) > MAX_SECTION_CHARS:
        errors.append(f"{key}: {len(body)} characters — the limit is "
                      f"{MAX_SECTION_CHARS}")
    # Every ticket in every batch carries the whole prompt, so an admin pasting a
    # policy document here is a standing bill, not a one-off.
    if "```" in body:
        errors.append(f"{key}: remove the ``` fence — the model is told to "
                      f"return raw JSON, and a fence in the rubric invites one "
                      f"in the reply")
    return errors
