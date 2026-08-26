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


def browse_credentials():
    """The credential used to list projects/models: SA → connected account → ADC."""
    import qc_runner
    try:
        creds, _ = qc_runner._vertex_credentials()
        return creds
    except qc_runner.VertexNotConfigured as e:
        raise NotConnected(str(e))


def _access_token(creds) -> str:
    from google.auth.transport.requests import Request
    if not creds.valid:
        creds.refresh(Request())
    return creds.token


# ── discovery ─────────────────────────────────────────────────────────────────

def list_projects() -> list[dict]:
    """Active Google Cloud projects the current credential can see."""
    creds = browse_credentials()
    token = _access_token(creds)

    projects: list[dict] = []
    page_token = None
    with httpx.Client(timeout=20) as client:
        while True:
            params = {"filter": "lifecycleState:ACTIVE", "pageSize": 200}
            if page_token:
                params["pageToken"] = page_token
            r = client.get(CRM_URL, headers={"Authorization": f"Bearer {token}"},
                           params=params)
            if r.status_code == 403:
                raise NotConnected(
                    "This account cannot list projects. Enable the Cloud Resource "
                    "Manager API on the project, or type the project ID manually."
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

    creds = browse_credentials()
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
