"""
Google Cloud discovery: connect an account with OAuth, then list the projects
and Vertex models it can actually use.

This removes the two things that were previously typed from memory — the GCP
project ID and the Gemini model names — and replaces them with pickers built
from what the active credential is genuinely allowed to see.

The connect flow reuses the app's existing OAuth client and redirect URI, so
no extra redirect URI needs registering in Google Cloud Console.
"""

import logging

import httpx

import vault

logger = logging.getLogger(__name__)

CLOUD_SCOPE   = "https://www.googleapis.com/auth/cloud-platform"
CRM_URL       = "https://cloudresourcemanager.googleapis.com/v1/projects"
TOKEN_URI     = "https://oauth2.googleapis.com/token"

# Re-exported so callers here can ask for an unbilled credential without
# importing qc_runner internals.
NO_QUOTA_PROJECT = "__none__"

# Regions where Vertex serves Gemini. Kept short and ordered by usefulness
# here rather than listing every Google Cloud region.
LOCATIONS = [
    ("us-central1",      "Iowa (us-central1)"),
    ("us-east4",         "N. Virginia (us-east4)"),
    ("us-west1",         "Oregon (us-west1)"),
    ("europe-west1",     "Belgium (europe-west1)"),
    ("europe-west2",     "London (europe-west2)"),
    ("europe-west4",     "Netherlands (europe-west4)"),
    ("asia-south1",      "Mumbai (asia-south1)"),
    ("asia-southeast1",  "Singapore (asia-southeast1)"),
    ("asia-northeast1",  "Tokyo (asia-northeast1)"),
    ("australia-southeast1", "Sydney (australia-southeast1)"),
    ("global",           "Global endpoint"),
]


class NotConnected(RuntimeError):
    """No Google Cloud credential is available to browse with."""


# ── connected-account credentials ─────────────────────────────────────────────

def oauth_credentials():
    """Build credentials from the stored refresh token, or None if not connected."""
    refresh_token = vault.get_credential("google_cloud_refresh_token")
    if not refresh_token:
        return None

    client_id     = vault.get_credential("google_oauth_client_id")
    client_secret = vault.get_credential("google_oauth_client_secret")
    if not (client_id and client_secret):
        return None

    from google.oauth2.credentials import Credentials
    return Credentials(
        token=None,
        refresh_token=refresh_token,
        token_uri=TOKEN_URI,
        client_id=client_id,
        client_secret=client_secret,
        scopes=[CLOUD_SCOPE],
    )


_adc_available: bool | None = None


def _adc_plausible() -> bool:
    """Cheap check for whether Application Default Credentials could exist.

    google.auth.default() ends by probing the GCE metadata server, which off
    GCP costs the better part of ten seconds before failing. Every source it
    checks before that is a file or an env var we can test instantly, so only
    call it when one of those is present or we might genuinely be on GCP.
    """
    import os
    from pathlib import Path

    if os.getenv("GOOGLE_APPLICATION_CREDENTIALS"):
        return True

    # The file `gcloud auth application-default login` writes. Probing it must
    # never raise: in a container HOME can point somewhere this user cannot
    # stat, and Path.exists() propagates PermissionError rather than saying no.
    try:
        if os.name == "nt":
            well_known = Path(os.getenv("APPDATA", "")) / "gcloud" / "application_default_credentials.json"
        else:
            well_known = Path.home() / ".config" / "gcloud" / "application_default_credentials.json"
        if well_known.exists():
            return True
    except OSError:
        pass

    # Running on Google infrastructure, where the metadata server does answer.
    return any(os.getenv(v) for v in (
        "GCE_METADATA_HOST", "GCE_METADATA_IP", "K_SERVICE",
        "GAE_ENV", "FUNCTION_TARGET", "CLOUD_RUN_JOB",
    ))


def _has_adc() -> bool:
    """Whether Application Default Credentials exist. Probed at most once."""
    global _adc_available
    if _adc_available is None:
        if not _adc_plausible():
            _adc_available = False
            return False
        try:
            import google.auth
            google.auth.default(scopes=[CLOUD_SCOPE])
            _adc_available = True
        except BaseException:
            _adc_available = False
    return _adc_available


def reset_probe_cache() -> None:
    global _adc_available
    _adc_available = None


def connection_status() -> dict:
    """What the Admin UI shows above the project picker."""
    account = vault.get_setting("google_cloud_account") or ""
    sa_info = None
    try:
        sa_info = vault.service_account_info()
    except ValueError:
        pass

    if sa_info:
        return {
            "connected": True,
            "kind": "service_account",
            "account": sa_info.get("client_email", "service account"),
            "can_browse": True,
        }
    if vault.get_credential("google_cloud_refresh_token"):
        return {"connected": True, "kind": "oauth", "account": account, "can_browse": True}

    if _has_adc():
        return {"connected": True, "kind": "adc",
                "account": "Application Default Credentials", "can_browse": True}
    return {"connected": False, "kind": None, "account": "", "can_browse": False}


def browse_credentials(quota_override: str | None = None):
    """The credential used to list projects/models: SA → connected account → ADC.

    While configuring, nothing is saved yet, so the project being browsed is
    passed in as the one to bill. Without it the call names no project and
    Google answers 403 — which looks identical to the API being disabled.
    """
    import qc_runner
    try:
        creds, _ = qc_runner._vertex_credentials(quota_override)
        return creds
    except qc_runner.VertexNotConfigured as e:
        raise NotConnected(str(e))


def _auth_headers(creds) -> dict:
    """Let the credential build its own headers.

    Taking `.token` and hand-writing `Authorization` skips Credentials.apply(),
    which is what injects `x-goog-user-project`. Without that header a user
    credential names no project to bill and Google answers 403 — a failure that
    reads exactly like the API being disabled. Every request here must go
    through apply(), so there is deliberately no way to get a bare token.
    """
    from google.auth.transport.requests import Request
    if not creds.valid:
        creds.refresh(Request())
    headers: dict = {}
    creds.apply(headers)
    return headers


# ── discovery ─────────────────────────────────────────────────────────────────

def _explain_project_list_failure(status: int, body: str) -> str:
    """Say what actually blocks project listing, in its own terms.

    This used to be routed through `explain_vertex_error`, which reads the
    response as a Vertex problem and tells the admin to enable
    `aiplatform.googleapis.com`. That advice is wrong here and unfollowable:
    listing projects never touches Vertex, so the API it names is usually
    already enabled and the message sends people to re-authenticate instead.
    """
    low = (body or "").lower()

    # Google writes the service either as the host name or as prose
    # ("Cloud Resource Manager API"), so match both spellings.
    names_crm = "cloudresourcemanager" in low or "cloud resource manager" in low
    if names_crm and ("has not been used" in low or "disabled" in low):
        return (
            "The Cloud Resource Manager API is not enabled on the project being "
            "billed for this request, so the account cannot list projects:\n"
            "    gcloud services enable cloudresourcemanager.googleapis.com\n"
            "This is unrelated to Vertex AI — scoring can work while this fails."
        )
    if "serviceusage.services.use" in low or "quota project" in low:
        return (
            "The connected Google account cannot bill the configured quota "
            "project, which is what project listing needs. Grant it "
            "roles/serviceusage.serviceUsageConsumer on that project, or clear "
            "the Quota project field."
        )
    if status == 403:
        return (
            "The connected Google account is not allowed to list projects "
            "(403). This does not affect scoring."
        )
    return f"Google returned HTTP {status} when listing projects."


def list_projects() -> list[dict]:
    """Active Google Cloud projects the current credential can see.

    Deliberately billed to *no* project. Project listing is not project-scoped,
    but attaching a quota project makes the request carry
    `x-goog-user-project`, which then demands Cloud Resource Manager be enabled
    there plus `serviceusage.services.use` on it. That is why this call used to
    403 while Vertex generation worked fine — the picker was asking for a
    permission it never needed.
    """
    creds = browse_credentials(quota_override=NO_QUOTA_PROJECT)
    headers = _auth_headers(creds)

    projects: list[dict] = []
    page_token = None
    with httpx.Client(timeout=20) as client:
        while True:
            params = {"filter": "lifecycleState:ACTIVE", "pageSize": 200}
            if page_token:
                params["pageToken"] = page_token
            r = client.get(CRM_URL, headers=headers, params=params)
            if r.status_code == 403:
                raise NotConnected(
                    _explain_project_list_failure(r.status_code, r.text)
                    + "\n\nYou can also skip the dropdown and type the project "
                      "ID directly."
                )
            r.raise_for_status()
            body = r.json()
            for p in body.get("projects", []):
                projects.append({
                    "id":     p.get("projectId"),
                    "name":   p.get("name") or p.get("projectId"),
                    "number": p.get("projectNumber"),
                })
            page_token = body.get("nextPageToken")
            if not page_token:
                break

    projects.sort(key=lambda p: p["name"].lower())
    return projects


# Models we never want to offer for this workload, whatever the API returns.
_EXCLUDE = ("embedding", "imagen", "veo", "vision", "textembedding", "medlm", "gemma")


def list_models(project: str, location: str) -> list[dict]:
    """Gemini models available for text generation in this project and region."""
    if not project:
        raise NotConnected("Choose a Google Cloud project first")

    from google import genai
    import qc_runner

    # Bill the project being browsed unless an explicit quota project is set.
    quota = vault.get_setting("vertex_quota_project").strip() or project
    creds = browse_credentials(quota_override=quota)
    client = genai.Client(vertexai=True, project=project,
                          location=location or "us-central1", credentials=creds)

    models: list[dict] = []
    for m in client.models.list(config={"query_base": True}):
        name = (m.name or "").split("/")[-1]
        if not name:
            continue
        low = name.lower()
        if not low.startswith("gemini") or any(x in low for x in _EXCLUDE):
            continue
        actions = getattr(m, "supported_actions", None)
        if actions and "generateContent" not in actions:
            continue
        models.append({
            "id":    name,
            "label": getattr(m, "display_name", None) or name,
        })

    # Newest-looking first so the default fallback order is sensible.
    models.sort(key=lambda x: x["id"], reverse=True)

    seen, unique = set(), []
    for m in models:
        if m["id"] not in seen:
            seen.add(m["id"])
            unique.append(m)
    return unique
