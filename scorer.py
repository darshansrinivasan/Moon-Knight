"""
Deterministic R1–R7 scoring.
No LLM calls — pure Python logic from the field mapping spec.
A1–A5 are left to Claude (run via the SKILL).
"""

import os
import re
import requests
from datetime import datetime, timezone, timedelta

from bs4 import BeautifulSoup

import rules as qc_rules

# ── constants ────────────────────────────────────────────────────────────────

INTERNAL_ACCOUNT_IDS = {
    "3890c56a-d883-49db-8de0-3164657007f6",  # Support (internal catch-all)
    "7ade86a8-6b74-497a-a983-fcd15b785965",  # SpotDraft Internal
}

INVALID_NAME_FRAGMENTS = [
    "support", "dogfooding", "sales trial", "sales_trials",
    "sales-trial", "live chat", "live_chat", "live-chat",
]

ROOTLY_RE = re.compile(r"ROOT-\d+|rootly\.com|rootly\.io", re.I)
JIRA_RE   = re.compile(r"atlassian\.net/browse/[A-Z]+-\d+|SPD-\d+", re.I)

TICKET_ID_ACK_RE = re.compile(
    r"ticket\s*(id|number|#)\s*[:#]?\s*\d+", re.I
)

ENGG_STATES = {"waiting_on_engg", "waiting_on_engineering"}
TERMINAL_STATES = {"closed", "archived"}
WAITING_CUSTOMER = {"waiting_on_customer"}

ONCALL_CATEGORIES = {
    "oncall",
    "oncall_integration_issues",
    "oncall_oncall_tasks",
    "oncall_performance_issues",
    "oncall_third_party_dependency_issue",
}

# R5: states where no ownership check applies
_R5_NA_STATES = {
    "investigating", "on_hold",
    "closed", "archived", "handled_by_ai_donot_use",
}

# R5: states that require a group mention in the thread → Pass; missing → Fail
_R5_GROUP_STATES = {
    "waiting_on_legal": ["@legal-ops"],
}

# Slack user IDs of Customer Success team members (sourced from Slack @cs group).
# Update this set when the CS team changes.
CS_SLACK_USER_IDS = {
    "U02RTGHRYJK",  # Mohammad Moiz — VP, Customer Success
    "U06CR1YU9SL",  # Nikhil Verghese — Senior CSM
    "U08GNSQ1H16",  # Aditya Khole — CSM
    "U025Z1D20RX",  # Pranjali Jaiswal — CSM
    "U04PBQVCARX",  # Adithyan Venu — Senior CSM
    "U0989NUR33L",  # Aryan Mann — Customer Success APAC
    "U0AM421CTDJ",  # Ayush P — Senior CSM
    "U04TSJ98BF1",  # Abhilash Chowdhury — Head CS (EU, UK & Africa)
    "U046GDSGLBA",  # Seetha Preetha — Staff CSM
    "U0784G9BYR5",  # Alice Pacheco — Lead Customer Success
    "U04R946UZDK",  # Roshanjit Kar Bhowmik — CSM Strategic Accounts APAC
    "U063L3UM3AQ",  # Muskan Patawari — Head CS SMB APAC
    "U08EUFGKTHD",  # Deepti Shukla — CSM EMEA
    "U06H9HM4KN0",  # Vanshika Rustagi — CS Associate
    "U0A3XMWJ0S0",  # Shubham Om Bhardwaj — Sr CSM
    "U06H75PEFD0",  # Aakriti Priyadarshi — CSM
    "U09V267K9TL",  # Yamini Palreddy — CSM EMEA
    "U0B02V4FJ4T",  # Yati Rana — Customer Success APAC
    "U06GSJEPKPH",  # Tanya Gupta — CS Associate
    "U0APUNBBGNA",  # Venu Vijay — Director Operations, CS & Services
    "U0AA2UP6EDP",  # Aniruddh Panikker — CSM NAM
    "U094K64686S",  # Harleen Kaur — CSM
    "U09N7BAFVPE",  # Krithi Shreekrishna — CS Associate NAM
    "U0ASHFKFZGB",  # Sarette Joylin Dsouza — CS Enablement Manager
    "U06H74W28TC",  # Cyma Akhter — CSM
    "U0344F6P20Z",  # Pooja Unnithan — Staff CSM NAM
    "U0AA30891LZ",  # Meghana Mantha — CSM NAM
    "U072T5B2G64",  # Sam Ferin — Manager, CS Americas
    "U0ANLEQNWGL",  # Rahul Kumar — Director, CS NAM
}

# Implementation team Slack user IDs (sourced 2026-08-13 from org structure message).
IMPL_SLACK_USER_IDS = {
    # APAC & EU (Sakshi Sharma)
    "U056VFBAF8Q",  # Sakshi Sharma — Manager, Implementations APAC & EU
    "U06TDAUF5CJ",  # Sarat Babu S — Sr. IM
    "U07TZ1RREAG",  # Nimisha Srivastava — Sr. IM
    "U0AF6V83HQD",  # Tushar Dhanani — IM
    "U0A0WBH16VC",  # Vaishakh Anil — IM
    "U09BS4N56SU",  # Vivek Rajesh — IM
    "U092R5T5FUJ",  # Ikhlaas Rasib — IA
    # NAM / Americas (Subhash Menon)
    "U06AGC2D532",  # Subhash Menon — Manager, Implementations NAM
    "U0754RMUM9C",  # Faris Soomar — Sr. IM
    "U08CPHEK0HZ",  # Devdutt Kannoth — Sr. IM
    "U0BMFGLRTE1",  # Mehar Singh — Sr. IM
    "U09PHFG62TX",  # Chaithanya A — IM
    "U09N3RVJSCS",  # Soham Paul Majumder — IM
    "U09KK5AQYRY",  # Astik Shrivastava — IM
}

# Slack group IDs for the implementation team.
IMPL_SLACK_GROUP_IDS = {
    "S055SJZ940Z",  # @implementation
    "S09BRCC3BBK",  # @im-americas
}

# Slack user-group IDs for engineering sub-teams.
# Used to recognise group @-mentions (e.g. <!subteam^S06BPEGE9J7>) in threads.
ENG_SLACK_GROUP_IDS = {
    "S06BPEGE9J7",  # @eng-be
    "S06B924435M",  # @eng-fe
    "S044ANANWKA",  # @eng--oncall (rotating oncall group)
    "S08CU0VPLUV",  # @pod-sidebar
    "S09DF5AM2JD",  # @pod-ai-infra
    "S09352QUBC3",  # @pod-ai-infra / sidebar (appears in both contexts)
    "S0B0X5EHGGZ",  # sidebar engineering sub-group
}

# Engineering team Slack user IDs — sourced from #backend-merge-requests-only,
# #frontend-merge-requests-only, and #eng-sidebar-pull-requests-only channels
# via Slack MCP (2026-08-12). Update when the team changes.
ENG_SLACK_USER_IDS = {
    # ── Backend (@eng-be) ──────────────────────────────────────────────────
    "U07AMBE46N7",  # Tamoghno Bakshi
    "U05EXAHLFK9",  # Aditya Raj
    "U011B5W7SJD",  # Charish Anumukonda
    "U03CSM5URFX",  # Hemil Shah
    "U02RVPYV92T",  # Shubham Kawatgi
    "U09F9S4DTC3",  # Siddharth Prajosh
    "U0A2GJS1F7E",  # Gokuldas K M
    "U05EGS6RKCZ",  # Karthikeyan J
    "U0B6X2CBRRV",  # Hirdesh Gupta
    "U0A3CPTNPQF",  # Karthik S
    "U0AH3S4A6M7",  # Naman Samaiya
    "U094CS5M9J4",  # Ankush Mehra
    "U09PHGCTVU5",  # Prakasha G
    "U08TTFJJHJ4",  # Vinit Mittal
    "U08P1T7FLPM",  # Nikhil Singh
    "U0AV8834WLX",  # Tharun GN
    "U0B0304TWPM",  # Abhay Singh M
    "U099C06SPL2",  # Keshav Raj
    "U0AJJG5GULR",  # Ritesh Kumar
    "U09Q721MGT1",  # Pratham Verma
    "U05TL8JRERL",  # Jason Dsouza
    "U0A81CSC91R",  # Pranav Shridhar
    "U05LM9L5R44",  # Sahil Alam
    "U09KCR6ELDR",  # Khush Wasnik
    "U034S0UDRBQ",  # Akshay Vinod
    "U096S445AQN",  # Naresh Kumar
    "U0AHQ6TF8LF",  # Satyam Bhardwaj
    "U08SJTYL14P",  # Hirak Mahata
    "U0ADY3MQ6FP",  # Sharan CJ
    # ── Frontend (@eng-fe) ────────────────────────────────────────────────
    "U059EBYAJ6T",  # Ayush Seth
    "U04BUVALNHJ",  # Shubham Seth
    "U07EBLFJ456",  # Stephen Ilo
    "U078CE642TY",  # Gantavya Saraswat
    "U07NERMRVFY",  # Aaditya Raj
    "U07HKN8PUGP",  # Anurag Nigam
    "U07K3V8EX5M",  # Mayank Tiwari
    "U05HYK61EEQ",  # Guru Prasad
    "U09RTERHN9Z",  # Naresh Baleboina
    "U02CQ1YNYMU",  # Prasanna Kumar
    "U03NJ0DTPCP",  # Sharath Matta
    "U014L7AA95Z",  # Sethupathy Natrayan
    "U04HDBRL42X",  # Sonu Kumar
    "U020QR4T03X",  # Rishabh Verma
    "U08U4DM8CJY",  # Pratyaksh Jindal
    "U07MBUGN9UY",  # Abinash Samal
    "U03L0E0R5U0",  # Praveen Kumar
    # ── Sidebar Pod (@pod-sidebar) ────────────────────────────────────────
    "U03J85JMPE1",  # Aditya Gupta — EM, Sidebar
    "U08NUTXUFQE",  # Achuth Karakkat
    "U07JUCMAQ78",  # Kundan Rao
    "U07M4NRNJ12",  # Adith Dinesh
    "U0B1AS9TUAH",  # Ramkrishna Potdar
    "U09TZFSDSJV",  # Varsha S
    "U05SL8UB2SU",  # Jaskaran Bhatia
    "U0AQQTS1MHN",  # Arush Bhatia
    "U05E19EF9V3",  # Parth Srivastava
    "U07Q91NSXGX",  # Roy D'Souza
    "U0A7CTXM8KT",  # Sruthi Seetharaman
    "U0BDT7NG2HY",  # Tushar Selvakumar
    # ── AI Infra / Kratos (@pod-ai-infra) ────────────────────────────────
    "U07SZQ56562",  # Srekar Eskala
    "U08FY4E7ZUY",  # Rhea Sanjan
    "U0AAPD4CC9M",  # Brijesh Parmar
    "U07KQ7M4N2Y",  # Priest Sabo Ombugadu
    # ── DevOps / Infra (@eng--oncall) ────────────────────────────────────
    "U09KY2VNJ8H",  # Sachin Rathod
    "U0A4R479AUX",  # Shivanshu Singh
    # ── Engineering Managers ──────────────────────────────────────────────
    "U02UGTWGRB5",  # Krishna Dey — EM, Backend
    "UMYKYU681",    # Sumanth S Rao — EM, OBM
    "U68Q7F5Q9",    # Madhav Bhagat — EM
    "U01C591QXHA",  # Sanket Mishra — EM, Frontend
    "U019Q3SEC21",  # Bharath Ravi — EM, Frontend
}

# Slack group IDs for the product (@pt) team.
PT_SLACK_GROUP_IDS = {
    "S03N8FPLE5T",  # @pt (main product team group, found in #only-product channel)
}

# Product team Slack user IDs — sourced from #only-product and #productmanagers
# channels via Slack MCP (2026-08-12). Update when the team changes.
PT_SLACK_USER_IDS = {
    "U02JMTNUNSJ",  # Anagh Padmanabhan — Principal PM
    "U05SL8UB2SU",  # Jaskaran Bhatia — PM, Sidebar & AI Infra
    "U0528H08RD4",  # Abhijeet Vyas — PM
    "U0A73GV1Z7E",  # Rounak Khandelwal — Sr PM, Sign & Clickthrough
    "U0ABZKH90BS",  # Vaibhav Srinivasa — Senior PM
    "U0AJJJ4GZKK",  # Abhinav Jha — PM
    "U0ALYBFUY14",  # Dhanin Gupta — PM
    "U038LTWC8JU",  # Meghna Raghunathan — Design
    "U03VAL4G1QW",  # Arpit Singh — PM
    "U0ACGKX2D8D",  # Akshar Patel — Product Design Manager
    "U045C69A15M",  # Anushka Bhattacharjee — PM
    "U05QP7MSNTX",  # Mangal Joe Edwin — PM
    "U0A26JNQVT5",  # Bhavika Maheshwari — PM
    "U0AAJBGBJU8",  # Madhav Sridhar — PM
    "U0A7SE083P1",  # Sankalp Sanjay — PM
    "U0B4VJWHE5B",  # Vimal Stan Steven — Sr Director, Product Management
    "U096V8K06UA",  # Chandana Mendan — Technical PM
    "U07T8TVRY2G",  # Apoorva Saraswat — PM
    "U09UVQUQDQB",  # Sakshi Mishra — PM
    "U0ADL3ZUAJ3",  # Shubham Bhosale — PM
}

# Regex to parse Slack archive URLs into channel + timestamp
_SLACK_URL_RE = re.compile(
    r"slack\.com/archives/(?P<channel>[A-Z0-9]+)"
    r"(?:/p(?P<msg_ts_p>\d{16}))?"
    r"(?:[?&]thread_ts=(?P<thread_ts>[\d.]+))?"
)


# ── helpers ───────────────────────────────────────────────────────────────────

def _html_text(html: str | None) -> str:
    if not html:
        return ""
    return BeautifulSoup(html, "html.parser").get_text(" ", strip=True)


def _cf_val(field: dict | None) -> str | None:
    if not field:
        return None
    return field.get("interpreted_value") or field.get("value") or None


def _cf_filled(field: dict | None) -> bool:
    if not field:
        return False
    return bool(
        field.get("value") or field.get("values") or field.get("interpreted_value")
    )


def _is_bot_ack(msg: dict) -> bool:
    """True for automated ticket-ID acknowledgement messages."""
    text = _html_text(msg.get("message_html"))
    return bool(TICKET_ID_ACK_RE.search(text) and len(text) < 200)


def _public_substantive(messages: list[dict]) -> list[dict]:
    return [
        m for m in messages
        if not m.get("is_private") and not _is_bot_ack(m)
    ]


def _mentioned_slack_ids(messages: list[dict]) -> set[str]:
    """Return Slack user IDs of all individual users @-mentioned across messages."""
    ids: set[str] = set()
    for m in messages:
        soup = BeautifulSoup(m.get("message_html") or "", "html.parser")
        for span in soup.find_all("span", {"data-mention-type": "user"}):
            sid = span.get("slackid") or span.get("userid") or ""
            if sid:
                ids.add(sid)
    return ids


def _mentioned_group_ids(messages: list[dict]) -> set[str]:
    """Return Slack group IDs of all @-mentioned groups across messages."""
    ids: set[str] = set()
    for m in messages:
        soup = BeautifulSoup(m.get("message_html") or "", "html.parser")
        for span in soup.find_all("span", {"data-mention-type": "group"}):
            sid = span.get("slackid") or span.get("userid") or ""
            if sid:
                ids.add(sid)
    return ids


def _fetch_slack_thread(url: str) -> str:
    """Fetch text of a Slack thread from its archive URL.

    Requires SLACK_USER_TOKEN in the environment (xoxp-… token with
    channels:history and groups:history scopes). Returns empty string
    on any error or if the token is absent.
    """
    import vault
    token = vault.get_credential("slack_user_token") or ""
    if not token or not url:
        return ""

    m = _SLACK_URL_RE.search(url)
    if not m:
        return ""

    channel   = m.group("channel")
    msg_ts_p  = m.group("msg_ts_p")   # e.g. "1782126385033799"
    thread_ts = m.group("thread_ts")   # e.g. "1782124318.169049"

    if thread_ts:
        ts = thread_ts
    elif msg_ts_p:
        ts = f"{msg_ts_p[:-6]}.{msg_ts_p[-6:]}"
    else:
        ts = None

    try:
        if ts:
            resp = requests.get(
                "https://slack.com/api/conversations.replies",
                params={"channel": channel, "ts": ts, "limit": 100},
                headers={"Authorization": f"Bearer {token}"},
                timeout=8,
            )
        else:
            resp = requests.get(
                "https://slack.com/api/conversations.history",
                params={"channel": channel, "limit": 30},
                headers={"Authorization": f"Bearer {token}"},
                timeout=8,
            )
        data = resp.json()
        if not data.get("ok"):
            return ""
        return " ".join(msg.get("text", "") for msg in data.get("messages", []))
    except Exception:
        return ""


def _thread_has_eng_group(thread_text: str) -> bool:
    """True if any engineering Slack group is @-mentioned in fetched thread text."""
    for gid in qc_rules.id_set("eng_group_ids"):
        if f"<!subteam^{gid}>" in thread_text or f"<@{gid}>" in thread_text:
            return True
    return False


_SLACK_USER_MENTION_RE = re.compile(r"<@(U[A-Z0-9]+)>")


def _slack_text_user_ids(text: str) -> set[str]:
    """Return user IDs from <@UXXXXXXX> mentions in Slack API thread text."""
    return set(_SLACK_USER_MENTION_RE.findall(text))


def _is_customer_msg(msg: dict) -> bool:
    return "contact" in msg.get("author", {})


def _parse_ts(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


# ── individual checks ─────────────────────────────────────────────────────────

def r1(custom_fields: dict) -> str:
    return "Pass" if _cf_filled(custom_fields.get("functionalities")) else "Fail"


def r2(custom_fields: dict) -> str:
    return "Pass" if _cf_filled(custom_fields.get("request_category")) else "Fail"


def r3(issue: dict, account: dict | None) -> str:
    account_id = (issue.get("account") or {}).get("id")
    if not account_id or not account:
        return "Fail"
    if account_id in qc_rules.internal_account_ids():
        return "Fail"
    if account.get("type") == "internal":
        return "Fail"
    name = (account.get("name") or "").lower()
    if any(frag in name for frag in qc_rules.invalid_name_fragments()):
        return "Fail"
    return "Pass"


def r4(issue: dict, messages: list[dict]) -> str:
    state = issue.get("state", "")
    if state in TERMINAL_STATES | WAITING_CUSTOMER:
        return "Pass"

    substantive = [m for m in _public_substantive(messages) if m.get("timestamp")]
    if not substantive:
        return "N/A"

    latest = max(substantive, key=lambda m: m["timestamp"])
    if not _is_customer_msg(latest):
        return "Pass"  # last public msg is from support

    age = datetime.now(timezone.utc) - _parse_ts(latest["timestamp"])
    return "Fail" if age > timedelta(hours=qc_rules.sla_hours()) else "Pass"


def _has_jira(
    issue: dict,
    messages: list[dict],
    external_issues: list[dict] | None = None,
) -> bool:
    """True if a Jira link is found in external_issues or message/body text."""
    for ext in (external_issues or []):
        src  = (ext.get("source") or "").lower()
        link = ext.get("link") or ""
        if src == "jira" or JIRA_RE.search(link):
            return True
    all_text = _html_text(issue.get("body_html", ""))
    for m in messages:
        all_text += " " + _html_text(m.get("message_html", ""))
    return bool(JIRA_RE.search(all_text))


def _has_rootly_ref(issue: dict) -> bool:
    """True if rootly.incident_reference custom field has a value (e.g. ROOT-1234)."""
    cf = issue.get("custom_fields") or {}
    return bool((cf.get("rootly.incident_reference") or {}).get("value"))


def _has_rootly_or_jira(
    issue: dict,
    messages: list[dict],
    external_issues: list[dict] | None = None,
) -> bool:
    """True if any Rootly or Jira evidence is found on the ticket."""
    cf = issue.get("custom_fields") or {}

    if (cf.get("does_rootly_exist") or {}).get("value") == "Yes":
        return True
    if _has_rootly_ref(issue):
        return True

    for ext in (external_issues or []):
        src  = (ext.get("source") or "").lower()
        link = ext.get("link") or ""
        if src in ("jira", "rootly") or JIRA_RE.search(link) or ROOTLY_RE.search(link):
            return True

    all_text = _html_text(issue.get("body_html", ""))
    for m in messages:
        all_text += " " + _html_text(m.get("message_html", ""))
    return bool(ROOTLY_RE.search(all_text) or JIRA_RE.search(all_text))


def r5(issue: dict, messages: list[dict], external_issues: list[dict] | None = None) -> str:
    state    = issue.get("state", "")
    assignee = issue.get("assignee")

    # States where ownership check doesn't apply
    if state in _R5_NA_STATES:
        return "N/A"

    # Rep responded, ball is in customer's court — status is correct
    if state in WAITING_CUSTOMER:
        return "Pass"

    # New ticket — must have an assignee
    if state == "new":
        return "Pass" if assignee else "Fail"

    # Support is yet to respond to a customer message — always flagged
    if state == "waiting_on_you":
        return "Fail"

    # waiting_on_csm: pass if any CS or Implementation member is tagged in the Pylon thread
    # (individually or as a group), or if found in the oncall_slack_chat_link thread.
    if state == "waiting_on_csm":
        mentioned = _mentioned_slack_ids(messages)
        if mentioned & qc_rules.id_set("cs_user_ids"):
            return "Pass"
        if mentioned & qc_rules.id_set("impl_user_ids"):
            return "Pass"
        if _mentioned_group_ids(messages) & qc_rules.id_set("impl_group_ids"):
            return "Pass"
        all_text = " ".join(_html_text(m.get("message_html")) for m in messages)
        if "@cs" in all_text or "@implementation" in all_text:
            return "Pass"
        cf = issue.get("custom_fields") or {}
        oncall_link = (_cf_val(cf.get("oncall_slack_chat_link")) or
                       (cf.get("oncall_slack_chat_link") or {}).get("value") or "")
        if oncall_link:
            thread_text = _fetch_slack_thread(oncall_link)
            if thread_text:
                thread_ids = _slack_text_user_ids(thread_text)
                if thread_ids & qc_rules.id_set("cs_user_ids"):
                    return "Pass"
                if thread_ids & qc_rules.id_set("impl_user_ids"):
                    return "Pass"
                for gid in qc_rules.id_set("impl_group_ids"):
                    if f"<!subteam^{gid}>" in thread_text:
                        return "Pass"
        return "Fail"

    # waiting_on_product: pass if any PT member individually tagged OR @pt group tagged
    # in Pylon thread, then check oncall_slack_chat_link thread as fallback.
    if state == "waiting_on_product":
        if _mentioned_slack_ids(messages) & qc_rules.id_set("pt_user_ids"):
            return "Pass"
        if _mentioned_group_ids(messages) & qc_rules.id_set("pt_group_ids"):
            return "Pass"
        all_text = " ".join(_html_text(m.get("message_html")) for m in messages)
        if "@pt" in all_text:
            return "Pass"
        cf = issue.get("custom_fields") or {}
        oncall_link = (_cf_val(cf.get("oncall_slack_chat_link")) or
                       (cf.get("oncall_slack_chat_link") or {}).get("value") or "")
        if oncall_link:
            thread_text = _fetch_slack_thread(oncall_link)
            if thread_text:
                if _slack_text_user_ids(thread_text) & qc_rules.id_set("pt_user_ids"):
                    return "Pass"
                for gid in qc_rules.id_set("pt_group_ids"):
                    if f"<!subteam^{gid}>" in thread_text:
                        return "Pass"
        return "Fail"

    # waiting_on_engg: require evidence that an engineer or eng group was notified.
    # Check Pylon thread first (HTML mentions), then oncall_slack_chat_link thread,
    # then fall back to Rootly/Jira presence.
    if state in ENGG_STATES:
        # 1a. Pylon thread HTML — individual engineer @-mentioned?
        if _mentioned_slack_ids(messages) & qc_rules.id_set("eng_user_ids"):
            return "Pass"
        # 1b. Pylon thread HTML — engineering group @-mentioned?
        if _mentioned_group_ids(messages) & qc_rules.id_set("eng_group_ids"):
            return "Pass"

        # 2. oncall_slack_chat_link — fetch thread, check for eng user/group tags
        cf = issue.get("custom_fields") or {}
        oncall_link = (_cf_val(cf.get("oncall_slack_chat_link")) or
                       (cf.get("oncall_slack_chat_link") or {}).get("value") or "")
        if oncall_link:
            thread_text = _fetch_slack_thread(oncall_link)
            if thread_text:
                if _slack_text_user_ids(thread_text) & qc_rules.id_set("eng_user_ids"):
                    return "Pass"
                if _thread_has_eng_group(thread_text):
                    return "Pass"

        # 3. Rootly/Jira as final fallback
        return "Pass" if _has_rootly_or_jira(issue, messages, external_issues) else "Fail"

    # Other delegation states — require group mention in thread text
    if state in qc_rules.group_states():
        tags     = qc_rules.group_states()[state]
        all_text = " ".join(_html_text(m.get("message_html")) for m in messages)
        return "Pass" if any(tag in all_text for tag in tags) else "Fail"

    # Unknown/future states — don't penalise
    return "N/A"


def r6(issue: dict) -> str:
    priority = issue.get("priority")
    if not priority:
        cf = issue.get("custom_fields") or {}
        for key in ("priority", "issue_priority"):
            field = cf.get(key)
            if field:
                priority = field.get("value") or field.get("interpreted_value")
                break
    if not priority or str(priority).lower() in ("", "not set", "none", "null"):
        return "Fail"
    return "Pass"


def r7(issue: dict, messages: list[dict], external_issues: list[dict] | None = None) -> str:
    state = issue.get("state", "")
    if state not in ENGG_STATES:
        return "N/A"

    cf = issue.get("custom_fields") or {}

    rootly_exists = (cf.get("does_rootly_exist") or {}).get("value", "")
    if rootly_exists == "Yes":
        return "Pass"
    if rootly_exists == "No":
        return "Fail"

    if (cf.get("rootly.incident_reference") or {}).get("value"):
        return "Pass"

    # check external_issues array (primary place Jira links live)
    for ext in (external_issues or []):
        src  = (ext.get("source") or "").lower()
        link = ext.get("link") or ""
        if src in ("jira", "rootly") or JIRA_RE.search(link) or ROOTLY_RE.search(link):
            return "Pass"

    all_text = _html_text(issue.get("body_html"))
    for m in messages:
        all_text += " " + _html_text(m.get("message_html"))

    if ROOTLY_RE.search(all_text) or JIRA_RE.search(all_text):
        return "Pass"

    return "Fail"


def r8(issue: dict, messages: list[dict], external_issues: list[dict] | None = None) -> str:
    """
    Oncall completeness check (combines former R8 + R9).
    Fires when resolution_category = 'Escalated to Oncall' OR rootly.incident_reference is set.
    All four fields must be consistent: does_rootly_exist=Yes, rootly ref filled,
    Jira link present, and request_category is an Oncall category.
    N/A when no oncall evidence exists.
    """
    cf = issue.get("custom_fields") or {}
    res_cat = (_cf_val(cf.get("resolution_category")) or "").strip().lower()
    has_ref = _has_rootly_ref(issue)

    if res_cat != "escalated to oncall" and not has_ref:
        return "N/A"

    has_rootly_yes = (cf.get("does_rootly_exist") or {}).get("value") == "Yes"
    has_jira_link  = _has_jira(issue, messages, external_issues)
    req_cat        = (_cf_val(cf.get("request_category")) or "").strip().lower()

    return "Pass" if (
        has_rootly_yes and has_ref and has_jira_link
        and req_cat in qc_rules.oncall_categories()
    ) else "Fail"


def r9(issue: dict, messages: list[dict], external_issues: list[dict] | None = None) -> str:
    """Deprecated — logic merged into r8. Always returns N/A."""
    return "N/A"


# ── entry point ───────────────────────────────────────────────────────────────

def score_all(
    issue: dict,
    messages: list[dict],
    account: dict | None,
    external_issues: list[dict] | None = None,
) -> dict:
    cf = issue.get("custom_fields") or {}
    return {
        "r1": r1(cf),
        "r2": r2(cf),
        "r3": r3(issue, account),
        "r4": r4(issue, messages),
        "r5": r5(issue, messages, external_issues),
        "r7": r7(issue, messages, external_issues),
        "r8": r8(issue, messages, external_issues),
        "r9": r9(issue, messages, external_issues),
    }
