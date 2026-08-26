"""
Google OAuth 2.0 sign-in, signed session cookies, and role-based access.

Only verified @spotdraft.com Google accounts may sign in. Sessions are
stateless: a HMAC-signed, HttpOnly cookie carrying the user's identity, with
role and active-status re-checked against the DB on every request so access
can be revoked immediately.
"""

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from datetime import datetime, timezone
from urllib.parse import urlencode

import httpx
from fastapi import HTTPException, Request

import db
import vault

ALLOWED_DOMAIN = os.getenv("QC_ALLOWED_DOMAIN", "spotdraft.com").lower()
COOKIE_NAME    = "qc_session"
SESSION_HOURS  = int(os.getenv("QC_SESSION_HOURS", "12"))
STATE_TTL      = 600          # 10 minutes


def _cookie_secure() -> bool:
    """Mark the session cookie Secure whenever we're served over HTTPS.

    Explicit QC_COOKIE_SECURE wins; otherwise infer it from the deployment —
    a hosted platform terminates TLS for us, and getting this wrong either
    breaks local http or ships an insecure cookie in production.
    """
    explicit = os.getenv("QC_COOKIE_SECURE", "").strip()
    if explicit:
        return explicit.lower() in ("1", "true", "yes", "on")
    import vault
    return vault.get_setting("dashboard_base_url").startswith("https://")

GOOGLE_AUTH_URL  = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"

# Paths reachable without a session.
PUBLIC_PATHS = {"/login", "/healthz", "/favicon.ico"}
PUBLIC_PREFIXES = ("/auth/", "/static/")


# ── signing ───────────────────────────────────────────────────────────────────

def _signing_key(purpose: bytes) -> bytes:
    from vault import _load_master_key  # master key doubles as signing material
    return hashlib.sha256(_load_master_key() + purpose).digest()


def _sign(payload: dict, purpose: bytes) -> str:
    raw = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=")
    sig = hmac.new(_signing_key(purpose), raw, hashlib.sha256).digest()
    return f"{raw.decode()}.{base64.urlsafe_b64encode(sig).rstrip(b'=').decode()}"


def _unsign(token: str, purpose: bytes) -> dict | None:
    try:
        raw_s, sig_s = token.split(".", 1)
    except ValueError:
        return None
    raw = raw_s.encode()
    expected = hmac.new(_signing_key(purpose), raw, hashlib.sha256).digest()
    try:
        given = base64.urlsafe_b64decode(sig_s + "=" * (-len(sig_s) % 4))
    except Exception:
        return None
    if not hmac.compare_digest(expected, given):
        return None
    try:
        payload = json.loads(base64.urlsafe_b64decode(raw + b"=" * (-len(raw) % 4)))
    except Exception:
        return None
    if payload.get("exp", 0) < time.time():
        return None
    return payload


# ── user records ──────────────────────────────────────────────────────────────

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def bootstrap_admins() -> set[str]:
    return vault.bootstrap_admin_emails()


def upsert_user(email: str, name: str, picture: str) -> dict:
    """Create or refresh a user.

    Admin is granted from QC_ADMIN_EMAILS. Only when that is unset do we fall
    back to "first user becomes admin" — convenient locally, but on a hosted
    deployment it would hand admin to whoever happens to sign in first.
    """
    email = email.lower()
    with db.get_conn() as conn:
        existing = conn.execute(
            "SELECT * FROM app_users WHERE email = ?", (email,)
        ).fetchone()

        if existing:
            # Keep the env admin list authoritative: adding someone to
            # QC_ADMIN_EMAILS promotes them on their next sign-in.
            promote = email in bootstrap_admins() and existing["role"] != "admin"
            conn.execute(
                "UPDATE app_users SET name = ?, picture = ?, last_login_at = ?"
                + (", role = 'admin'" if promote else "")
                + " WHERE email = ?",
                (name, picture, _now(), email),
            )
            row = conn.execute(
                "SELECT * FROM app_users WHERE email = ?", (email,)
            ).fetchone()
            return dict(row)

        admins = bootstrap_admins()
        if admins:
            role = "admin" if email in admins else "member"
        else:
            total = conn.execute("SELECT COUNT(*) AS n FROM app_users").fetchone()["n"]
            role = "admin" if total == 0 else "member"
        conn.execute(
            "INSERT INTO app_users (email, name, picture, role, is_active, created_at, last_login_at)"
            " VALUES (?, ?, ?, ?, 1, ?, ?)",
            (email, name, picture, role, _now(), _now()),
        )
        return {
            "email": email, "name": name, "picture": picture,
            "role": role, "is_active": 1,
        }


def get_user(email: str) -> dict | None:
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM app_users WHERE email = ?", (email.lower(),)
        ).fetchone()
    return dict(row) if row else None


def list_users() -> list[dict]:
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM app_users ORDER BY role, email"
        ).fetchall()
    return [dict(r) for r in rows]


# ── OAuth flow ────────────────────────────────────────────────────────────────

def oauth_configured() -> bool:
    return bool(
        vault.get_credential("google_oauth_client_id")
        and vault.get_credential("google_oauth_client_secret")
    )


def base_url(request: Request | None = None) -> str:
    """Public URL of this app: explicit setting, then platform domain, then request."""
    base = vault.get_setting("dashboard_base_url").rstrip("/")
    if base:
        return base
    if request is not None:
        # Behind a TLS-terminating proxy the request scheme reads as http.
        forwarded = request.headers.get("x-forwarded-proto", "").split(",")[0].strip()
        host = request.headers.get("host") or request.url.netloc
        scheme = forwarded or request.url.scheme
        return f"{scheme}://{host}"
    return ""


def _redirect_uri(request: Request) -> str:
    return f"{base_url(request)}/auth/callback"


CLOUD_SCOPE = "https://www.googleapis.com/auth/cloud-platform"


def _authorize_url(request: Request, next_path: str, flow: str,
                   scope: str, extra: dict) -> str:
    client_id = vault.get_credential("google_oauth_client_id")
    if not client_id:
        raise HTTPException(503, "Google sign-in is not configured yet")

    state = _sign(
        {"n": secrets.token_urlsafe(16), "next": next_path,
         "flow": flow, "exp": time.time() + STATE_TTL},
        b"oauth-state",
    )
    params = {
        "client_id":     client_id,
        "redirect_uri":  _redirect_uri(request),
        "response_type": "code",
        "scope":         scope,
        "state":         state,
        **extra,
    }
    return f"{GOOGLE_AUTH_URL}?{urlencode(params)}"


def login_url(request: Request, next_path: str = "/") -> str:
    return _authorize_url(
        request, next_path, "signin", "openid email profile",
        # hd is a hint only — the domain is enforced server-side after verification.
        {"hd": ALLOWED_DOMAIN, "prompt": "select_account", "access_type": "online"},
    )


def cloud_connect_url(request: Request, next_path: str = "/admin") -> str:
    """Authorize Google Cloud access so the app can list projects and models.

    Shares the sign-in OAuth client and redirect URI — the state carries the
    flow — so only one redirect URI ever needs registering.
    """
    return _authorize_url(
        request, next_path, "gcp", f"openid email {CLOUD_SCOPE}",
        # offline + consent are what actually return a refresh token.
        {"access_type": "offline", "prompt": "consent",
         "include_granted_scopes": "true", "hd": ALLOWED_DOMAIN},
    )


async def exchange_tokens(request: Request, code: str) -> dict:
    """Swap an auth code for Google's raw token response."""
    client_id     = vault.get_credential("google_oauth_client_id")
    client_secret = vault.get_credential("google_oauth_client_secret")
    if not (client_id and client_secret):
        raise HTTPException(503, "Google sign-in is not configured")

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(GOOGLE_TOKEN_URL, data={
            "code":          code,
            "client_id":     client_id,
            "client_secret": client_secret,
            "redirect_uri":  _redirect_uri(request),
            "grant_type":    "authorization_code",
        })
    if resp.status_code != 200:
        raise HTTPException(401, "Google rejected the sign-in attempt")
    return resp.json()


async def verify_identity(id_tok: str) -> dict:
    """Verify an id_token and enforce the domain restriction."""
    client_id = vault.get_credential("google_oauth_client_id")
    if not id_tok:
        raise HTTPException(401, "Google did not return an identity token")

    # Verify the JWT signature against Google's published keys (blocking → thread).
    import asyncio

    def _verify():
        from google.auth.transport import requests as greq
        from google.oauth2 import id_token as gid
        return gid.verify_oauth2_token(id_tok, greq.Request(), client_id)

    try:
        claims = await asyncio.to_thread(_verify)
    except Exception:
        raise HTTPException(401, "Could not verify Google identity token")

    email = (claims.get("email") or "").lower()
    if not claims.get("email_verified"):
        raise HTTPException(403, "Your Google email address is not verified")

    hd = (claims.get("hd") or "").lower()
    if hd != ALLOWED_DOMAIN or not email.endswith(f"@{ALLOWED_DOMAIN}"):
        raise HTTPException(403, f"Access is restricted to @{ALLOWED_DOMAIN} accounts")

    return {
        "email":   email,
        "name":    claims.get("name") or email.split("@")[0],
        "picture": claims.get("picture") or "",
    }


async def exchange_code(request: Request, code: str) -> dict:
    """Swap an auth code for a verified, domain-checked Google identity."""
    tokens = await exchange_tokens(request, code)
    return await verify_identity(tokens.get("id_token"))


# ── session cookie ────────────────────────────────────────────────────────────

def issue_session(user: dict) -> str:
    return _sign(
        {
            "email": user["email"],
            "name":  user.get("name") or "",
            "exp":   time.time() + SESSION_HOURS * 3600,
        },
        b"session",
    )


def set_session_cookie(response, token: str) -> None:
    response.set_cookie(
        COOKIE_NAME, token,
        max_age=SESSION_HOURS * 3600,
        httponly=True,
        samesite="lax",
        secure=_cookie_secure(),
        path="/",
    )


def clear_session_cookie(response) -> None:
    response.delete_cookie(COOKIE_NAME, path="/")


def current_user(request: Request) -> dict | None:
    """Resolve the signed cookie to a live user record, or None."""
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        return None
    payload = _unsign(token, b"session")
    if not payload:
        return None
    user = get_user(payload["email"])
    if not user or not user["is_active"]:
        return None      # revoked access takes effect immediately
    return user


# ── FastAPI dependencies ──────────────────────────────────────────────────────

def require_user(request: Request) -> dict:
    user = current_user(request)
    if not user:
        raise HTTPException(401, "Sign-in required")
    return user


def require_admin(request: Request) -> dict:
    user = require_user(request)
    if user["role"] != "admin":
        raise HTTPException(403, "Administrator access required")
    return user


def is_public_path(path: str) -> bool:
    return path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIXES)
