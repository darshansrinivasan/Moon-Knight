"""
Configuration resolution: environment variables first, then the encrypted
database vault, then defaults.

On a platform like Railway the environment IS the secret store, so every
credential and setting can be supplied as an env var and the app needs no
interactive setup at all. Anything not set in the environment can still be
managed at runtime through the Admin UI, where it is encrypted at rest with
Fernet. Values coming from the environment are shown as read-only, because
the process cannot write back to the platform's config.
"""

import base64
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

import db

logger = logging.getLogger(__name__)

_KEY_FILE = Path(os.getenv("QC_KEY_FILE", Path(__file__).parent / ".master.key"))
_fernet: Fernet | None = None
_key_is_ephemeral = False


class ConfigLocked(RuntimeError):
    """Raised when trying to write a value that the environment owns."""


# ── master key ────────────────────────────────────────────────────────────────

def _load_master_key() -> bytes:
    """QC_MASTER_KEY wins. A generated file is a local-dev convenience only."""
    global _key_is_ephemeral

    env_key = os.getenv("QC_MASTER_KEY", "").strip()
    if env_key:
        try:
            Fernet(env_key.encode())          # validate before trusting it
        except Exception as e:
            raise RuntimeError(
                "QC_MASTER_KEY is not a valid Fernet key. Generate one with: "
                'python3 -c "from cryptography.fernet import Fernet; '
                'print(Fernet.generate_key().decode())"'
            ) from e
        return env_key.encode()

    if _KEY_FILE.exists():
        return _KEY_FILE.read_bytes().strip()

    key = Fernet.generate_key()
    try:
        _KEY_FILE.write_bytes(key)
        _KEY_FILE.chmod(0o600)
    except OSError:
        # Read-only filesystem (containers) — key lives only in memory.
        _key_is_ephemeral = True
        logger.warning(
            "Could not persist a master key. Set QC_MASTER_KEY, or sessions and "
            "stored credentials will not survive a restart."
        )
    return key


def _f() -> Fernet:
    global _fernet
    if _fernet is None:
        _fernet = Fernet(_load_master_key())
    return _fernet


def is_hosted() -> bool:
    """True when running on a container platform with an ephemeral filesystem."""
    return any(_env(v) for v in (
        "RAILWAY_PUBLIC_DOMAIN", "RAILWAY_ENVIRONMENT", "RAILWAY_PROJECT_ID",
        "RENDER", "FLY_APP_NAME", "DYNO", "K_SERVICE",
    ))


def master_key_is_durable() -> bool:
    """False when the key would be lost on restart.

    A container filesystem is writable, so the key file *saves* happily and
    then disappears on the next deploy — taking every session and every
    stored credential with it. Only an explicit QC_MASTER_KEY survives.
    """
    if _env("QC_MASTER_KEY"):
        return True
    if is_hosted():
        return False
    return _KEY_FILE.exists() and not _key_is_ephemeral


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _env(name: str) -> str:
    return os.getenv(name, "").strip()


def _query(sql: str, params: tuple = ()):
    """Read one row, tolerating a database that has not been initialised yet.

    /healthz and the startup log both read config before (or independently of)
    init_db; on a fresh volume the tables don't exist, and a raised exception
    there would fail the platform healthcheck and roll the deploy back.
    """
    import sqlite3
    try:
        with db.get_conn() as conn:
            return conn.execute(sql, params).fetchone()
    except sqlite3.OperationalError:
        return None


def _query_all(sql: str, params: tuple = ()):
    import sqlite3
    try:
        with db.get_conn() as conn:
            return conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError:
        return []


def _as_bool(value: str) -> str:
    return "1" if value.lower() in ("1", "true", "yes", "on") else "0"


# ── credential registry ───────────────────────────────────────────────────────

# Runtime credentials live in the Admin UI. `legacy_env` is read once, on first
# boot, so an existing .env migrates itself — after that the vault is the only
# source of truth and removing a value here keeps it removed.
CREDENTIAL_SPECS = [
    {
        "key": "pylon_api_token",
        "legacy_env": "PYLON_API_TOKEN",
        "label": "Pylon API token",
        "help": "Bearer token from Pylon → Settings → API. Used to fetch tickets.",
        "testable": True,
    },
    {
        "key": "slack_bot_token",
        "legacy_env": "SLACK_BOT_TOKEN",
        "label": "Slack bot token",
        "help": "xoxb-… token with chat:write, users:read, and users:read.email. Used to post QC reports and to pick reviewers by email.",
        "testable": True,
    },
    {
        "key": "slack_user_token",
        "legacy_env": "SLACK_USER_TOKEN",
        "label": "Slack user token (optional)",
        "help": "xoxp-… with channels:history — lets R5 read linked oncall threads.",
        "testable": False,
    },
    # Sign-in credentials are needed *before* anyone can reach the Admin UI, so
    # they are environment-only. That removes the bootstrap problem entirely —
    # no setup page, no loopback exception, nothing to self-lock.
    {
        "key": "google_oauth_client_id",
        "env": "GOOGLE_OAUTH_CLIENT_ID",
        "label": "Google OAuth Client ID",
        "help": "Web application client. Redirect URI must be <base-url>/auth/callback.",
        "testable": False,
        "env_only": True,
    },
    {
        "key": "google_oauth_client_secret",
        "env": "GOOGLE_OAUTH_CLIENT_SECRET",
        "label": "Google OAuth Client Secret",
        "help": "Paired secret for the OAuth client above.",
        "testable": False,
        "env_only": True,
    },
    {
        "key": "vertex_service_account_json",
        "legacy_env": "GOOGLE_SERVICE_ACCOUNT_JSON",
        "label": "Vertex service account JSON",
        "help": "Raw JSON or base64. An alternative to connecting a Google account "
                "below — useful for unattended hosting.",
        "testable": True,
    },
    {
        # Obtained through the Connect Google Cloud flow, never pasted, so it is
        # hidden from the credentials list in the Admin UI.
        "key": "google_cloud_refresh_token",
        "label": "Google Cloud connection",
        "help": "Set by connecting a Google account in the AI section.",
        "testable": False,
        "internal": True,
    },
]

CREDENTIAL_KEYS = {c["key"] for c in CREDENTIAL_SPECS}
_CRED_ENV  = {c["key"]: c["env"] for c in CREDENTIAL_SPECS if c.get("env")}
_ENV_ONLY  = {c["key"] for c in CREDENTIAL_SPECS if c.get("env_only")}


# ── settings registry ─────────────────────────────────────────────────────────

# Everything here is managed in the Admin UI. Only the base URL keeps a live
# env override, because it decides the OAuth redirect and so is bootstrap-ish;
# it is auto-detected from the hosting platform anyway.
SETTING_SPECS = [
    {"key": "vertex_project",     "legacy_env": "VERTEX_PROJECT",   "default": ""},
    {"key": "vertex_quota_project", "legacy_env": "VERTEX_QUOTA_PROJECT", "default": ""},
    {"key": "vertex_location",    "legacy_env": "VERTEX_LOCATION",  "default": "us-central1"},
    {"key": "vertex_models",      "legacy_env": "VERTEX_MODELS",    "default": ""},
    {"key": "slack_enabled",      "legacy_env": "SLACK_ENABLED",    "default": "0", "bool": True},
    {"key": "slack_channel",      "legacy_env": "SLACK_CHANNEL",    "default": ""},
    # off | leads | all. Defaults to leads: @-mentioning every assignee about
    # their own failed tickets, daily, in a group channel is a deliberate choice
    # and needs to be reversible without a deploy.
    {"key": "slack_mention_mode", "default": "leads"},
    {"key": "dashboard_base_url", "env": "QC_BASE_URL",             "default": ""},
    {"key": "schedule_enabled",   "legacy_env": "SCHEDULE_ENABLED", "default": "0", "bool": True},
    {"key": "schedule_time",      "legacy_env": "SCHEDULE_TIME",    "default": "09:30"},
    {"key": "schedule_tz",        "legacy_env": "SCHEDULE_TZ",      "default": "Asia/Kolkata"},
    {"key": "schedule_target",    "legacy_env": "SCHEDULE_TARGET",  "default": "yesterday"},
    # Display-only: which Google account was connected for Cloud access.
    {"key": "google_cloud_account", "default": ""},
]

SETTING_DEFAULTS = {s["key"]: s["default"] for s in SETTING_SPECS}
_SETTING_BY_KEY = {s["key"]: s for s in SETTING_SPECS}


def import_legacy_env(actor: str = "startup") -> list[str]:
    """One-time migration of any values still supplied as environment variables.

    Runs once, guarded by a marker, so a credential an admin later removes in
    the UI does not reappear on the next restart.
    """
    if get_setting("env_import_done") == "1":
        return []

    imported: list[str] = []
    for spec in CREDENTIAL_SPECS:
        name = spec.get("legacy_env")
        if name and _env(name) and not get_credential(spec["key"]):
            set_credential(spec["key"], _env(name), actor)
            imported.append(spec["key"])

    pending = {}
    for spec in SETTING_SPECS:
        name = spec.get("legacy_env")
        if name and _env(name):
            value = _as_bool(_env(name)) if spec.get("bool") else _env(name)
            pending[spec["key"]] = value
            imported.append(spec["key"])
    if pending:
        set_settings(pending, actor)

    _set_raw_setting("env_import_done", "1", actor)
    if imported:
        audit(actor, "config.import_from_env", ", ".join(imported))
    return imported


def get_raw_setting(key: str) -> str | None:
    """Read a setting outside the public registry (e.g. the rules document)."""
    row = _query("SELECT value FROM app_settings WHERE key = ?", (key,))
    return row["value"] if row else None


def get_setting_meta(key: str) -> dict | None:
    row = _query("SELECT updated_by, updated_at FROM app_settings WHERE key = ?", (key,))
    return dict(row) if row else None


def set_raw_setting(key: str, value: str, updated_by: str) -> None:
    _set_raw_setting(key, value, updated_by)


def _set_raw_setting(key: str, value: str, updated_by: str) -> None:
    """Write a setting that is not part of the public registry."""
    with db.get_conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO app_settings (key, value, updated_by, updated_at)"
            " VALUES (?, ?, ?, ?)",
            (key, value, updated_by, _now()),
        )


def bootstrap_admin_emails() -> set[str]:
    raw = _env("QC_ADMIN_EMAILS") or _env("QC_BOOTSTRAP_ADMINS")
    return {e.strip().lower() for e in raw.split(",") if e.strip()}


def railway_domain() -> str:
    """Railway publishes the public hostname; use it so redirect URIs just work."""
    for var in ("RAILWAY_PUBLIC_DOMAIN", "RAILWAY_STATIC_URL"):
        host = _env(var)
        if host:
            return host if host.startswith("http") else f"https://{host}"
    return ""


# ── credentials ───────────────────────────────────────────────────────────────

def credential_source(key: str) -> str:
    """'env' | 'admin' | 'unset' — where this credential's value comes from."""
    if _env(_CRED_ENV.get(key, "")):
        return "env"
    if key in _ENV_ONLY:
        return "unset"
    row = _query("SELECT 1 FROM credentials WHERE key = ?", (key,))
    return "admin" if row else "unset"


def get_credential(key: str) -> str | None:
    """Resolve a secret: environment first, then the encrypted vault."""
    env_val = _env(_CRED_ENV.get(key, ""))
    if env_val:
        return env_val
    if key in _ENV_ONLY:
        return None          # bootstrap credentials never live in the database

    row = _query("SELECT value_enc FROM credentials WHERE key = ?", (key,))
    if not row:
        return None
    try:
        return _f().decrypt(row["value_enc"]).decode()
    except InvalidToken:
        # Master key changed or was lost — treat as unset rather than crash.
        logger.warning("Could not decrypt credential %r — is QC_MASTER_KEY stable?", key)
        return None


def set_credential(key: str, value: str, updated_by: str) -> None:
    if key not in CREDENTIAL_KEYS:
        raise ValueError(f"Unknown credential key: {key}")
    if key in _ENV_ONLY:
        raise ConfigLocked(
            f"{key} is a sign-in credential and can only be set through the "
            f"{_CRED_ENV[key]} environment variable, since it is needed before "
            "anyone can reach this page."
        )
    env_name = _CRED_ENV.get(key)
    if env_name and _env(env_name):
        raise ConfigLocked(
            f"{key} is set by the {env_name} environment variable. "
            "Change it where the app is deployed."
        )

    with db.get_conn() as conn:
        if not value:
            conn.execute("DELETE FROM credentials WHERE key = ?", (key,))
        else:
            conn.execute(
                "INSERT OR REPLACE INTO credentials (key, value_enc, updated_by, updated_at)"
                " VALUES (?, ?, ?, ?)",
                (key, _f().encrypt(value.encode()), updated_by, _now()),
            )


def _mask(value: str) -> str:
    return "••••" if len(value) <= 4 else "••••" + value[-4:]


def list_credentials() -> list[dict]:
    """Metadata only — masked hints, never plaintext."""
    rows = {
        r["key"]: dict(r)
        for r in _query_all("SELECT key, updated_by, updated_at FROM credentials")
    }

    out = []
    for spec in CREDENTIAL_SPECS:
        if spec.get("internal"):
            continue          # managed by a flow, not by pasting a value
        key    = spec["key"]
        val    = get_credential(key)
        source = credential_source(key)
        meta   = rows.get(key, {})
        out.append({
            **spec,
            "is_set":     bool(val),
            "hint":       _mask(val) if val else "",
            "source":     source,
            # env_only credentials are never editable here, set or not.
            "locked":     source == "env" or bool(spec.get("env_only")),
            "updated_by": meta.get("updated_by"),
            "updated_at": meta.get("updated_at"),
        })
    return out


# ── settings ──────────────────────────────────────────────────────────────────

def setting_source(key: str) -> str:
    spec = _SETTING_BY_KEY.get(key)
    if spec and spec.get("env") and _env(spec["env"]):
        return "env"
    row = _query("SELECT 1 FROM app_settings WHERE key = ? AND value != ''", (key,))
    return "admin" if row else "default"


def get_setting(key: str, default: str | None = None) -> str:
    spec = _SETTING_BY_KEY.get(key)

    if spec and spec.get("env"):
        env_val = _env(spec["env"])
        if env_val:
            return _as_bool(env_val) if spec.get("bool") else env_val

    row = _query("SELECT value FROM app_settings WHERE key = ?", (key,))
    if row and row["value"]:
        return row["value"]

    # Derive the base URL from the hosting platform when nothing else set it.
    if key == "dashboard_base_url":
        return railway_domain() or default or ""

    if default is not None:
        return default
    return SETTING_DEFAULTS.get(key, "")


def get_settings() -> dict:
    return {s["key"]: get_setting(s["key"]) for s in SETTING_SPECS}


def get_setting_sources() -> dict:
    return {s["key"]: setting_source(s["key"]) for s in SETTING_SPECS}


# Settings that must never be silently blanked. A UI control that has not
# finished loading reads as "" and used to overwrite a working value: that is
# exactly how a saved vertex_project disappeared and every QC run then failed
# with "No Google Cloud project configured". Clearing these requires
# allow_clear, so an accidental empty POST cannot erase them.
PROTECTED_SETTINGS = frozenset({
    "vertex_project", "vertex_location", "vertex_models",
    "schedule_time", "schedule_tz", "schedule_target",
})


def set_settings(values: dict, updated_by: str,
                 allow_clear: bool = False) -> list[str]:
    """Persist settings the environment does not own.

    Returns the keys it refused: those the environment owns, and those in
    PROTECTED_SETTINGS whose incoming value was empty while a value is already
    stored (unless `allow_clear`). Callers should surface refusals rather than
    reporting a clean save.
    """
    now = _now()
    refused = []
    with db.get_conn() as conn:
        for key, value in values.items():
            spec = _SETTING_BY_KEY.get(key)
            if not spec:
                continue
            if spec.get("env") and _env(spec["env"]):
                refused.append(key)
                continue

            text = "" if value is None else str(value)
            if not text.strip() and key in PROTECTED_SETTINGS and not allow_clear:
                if get_setting(key):
                    logger.warning(
                        "Refusing to clear %r — it already has a value and no "
                        "explicit clear was requested", key,
                    )
                    refused.append(key)
                    continue

            conn.execute(
                "INSERT OR REPLACE INTO app_settings (key, value, updated_by, updated_at)"
                " VALUES (?, ?, ?, ?)",
                (key, text, updated_by, now),
            )
    return refused


# ── service account normalisation ─────────────────────────────────────────────

def service_account_info() -> dict | None:
    """Parse the Vertex service account, accepting raw JSON or base64."""
    raw = get_credential("vertex_service_account_json")
    if not raw:
        return None

    text = raw.strip()
    if not text.startswith("{"):
        # Env vars carry multi-line JSON badly, so base64 is supported too.
        try:
            text = base64.b64decode(text, validate=True).decode()
        except Exception as e:
            raise ValueError(
                "Vertex service account must be raw JSON or base64-encoded JSON"
            ) from e
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise ValueError(f"Vertex service account JSON is not valid JSON: {e}") from e


# ── startup readiness ─────────────────────────────────────────────────────────

def database_is_persistent() -> bool:
    """Whether the database survives a redeploy.

    Checking that QC_DB_PATH is *set* is not enough — the image may set it to a
    directory that exists in the container but has no volume behind it, which
    looks fine and quietly loses every deploy's data. So verify the path really
    sits on a mount.
    """
    if not is_hosted():
        return True

    mount = _env("RAILWAY_VOLUME_MOUNT_PATH")
    if mount:
        return str(db.DB_PATH).startswith(mount.rstrip("/"))

    # No platform hint — fall back to the kernel's own mount table.
    try:
        with open("/proc/mounts") as fh:
            mounts = [line.split()[1] for line in fh if len(line.split()) > 1]
    except OSError:
        return False
    parent = str(db.DB_PATH.parent)
    return any(parent == m or parent.startswith(m.rstrip("/") + "/")
               for m in mounts if m not in ("/", ""))


def readiness() -> dict:
    """What is configured and what still blocks each feature."""
    problems, warnings = [], []

    if not master_key_is_durable():
        problems.append(
            "QC_MASTER_KEY is not set — every deploy signs all users out and "
            "makes stored credentials unreadable. Set it before going live."
        )

    if is_hosted() and not database_is_persistent():
        problems.append(
            f"The database at {db.DB_PATH} is not on a mounted volume, so every deploy "
            "erases all QC history. Add a volume in Railway and point QC_DB_PATH at its "
            "mount path."
        )

    oauth_ok = bool(get_credential("google_oauth_client_id")
                    and get_credential("google_oauth_client_secret"))
    if not oauth_ok:
        missing = [v for v in ("GOOGLE_OAUTH_CLIENT_ID", "GOOGLE_OAUTH_CLIENT_SECRET")
                   if not _env(v)]
        problems.append(
            f"Nobody can sign in: {' and '.join(missing)} not set. "
            "These are environment-only because they are needed before the Admin UI "
            "is reachable."
        )

    if not bootstrap_admin_emails():
        warnings.append(
            "QC_ADMIN_EMAILS is not set — the first person to sign in becomes admin"
        )

    base = get_setting("dashboard_base_url")
    platform = railway_domain()
    if platform and base and platform not in base:
        # A stale localhost URL here silently breaks the OAuth redirect.
        warnings.append(
            f"Base URL is {base} but this host serves {platform}. "
            "Set QC_BASE_URL (or clear it in Admin) so the OAuth redirect matches."
        )
    elif not base:
        warnings.append(
            "Application URL is unset and no platform domain was detected, so sign-in "
            "redirects and Slack links fall back to whatever host the request arrived on. "
            "Set it in Admin \u2192 Slack, or via QC_BASE_URL."
        )

    pylon_ok  = bool(get_credential("pylon_api_token"))
    vertex_ok = bool(get_setting("vertex_project"))
    slack_on  = get_setting("slack_enabled") == "1"
    slack_ok  = bool(get_credential("slack_bot_token") and get_setting("slack_channel"))

    if not pylon_ok:
        warnings.append("No Pylon API token yet — add it in Admin → Credentials")
    if not vertex_ok:
        warnings.append("No Google Cloud project yet — set it in Admin → AI")
    if slack_on and not slack_ok:
        warnings.append("Slack posting is on but the token or channel is missing")

    return {
        "ready":      not problems,
        "problems":   problems,
        "warnings":   warnings,
        "oauth":      oauth_ok,
        "pylon":      pylon_ok,
        "vertex":     vertex_ok,
        "slack":      slack_ok,
        "durable_key": master_key_is_durable(),
        "base_url":   get_setting("dashboard_base_url"),
    }


def log_startup_config() -> None:
    """One clear block in the deploy logs saying what is and isn't configured."""
    # Force the key to load now: an invalid QC_MASTER_KEY should fail here, not
    # silently at the first credential write.
    _f()
    r = readiness()
    logger.info("── Pylon QC configuration ─────────────────────────────")
    logger.info("  base URL     : %s", r["base_url"] or "(from request host)")
    logger.info("  database     : %s", db.DB_PATH)
    logger.info("  master key   : %s", "durable" if r["durable_key"] else "EPHEMERAL")
    logger.info("  sign-in      : %s", credential_source("google_oauth_client_id"))
    logger.info("  admins       : %s", ", ".join(sorted(bootstrap_admin_emails())) or "(first sign-in wins)")
    for key in ("pylon_api_token", "slack_bot_token", "vertex_service_account_json"):
        logger.info("  %-13s: %s", key.replace("_", " ")[:13], credential_source(key))
    logger.info("  vertex proj  : %s", get_setting("vertex_project") or "(not set)")
    logger.info("  schedule     : %s at %s %s",
                "on" if get_setting("schedule_enabled") == "1" else "off",
                get_setting("schedule_time"), get_setting("schedule_tz"))
    for p in r["problems"]:
        logger.error("  BLOCKED: %s", p)
    for w in r["warnings"]:
        logger.warning("  WARNING: %s", w)
    logger.info("───────────────────────────────────────────────────────")


# ── audit log ─────────────────────────────────────────────────────────────────

def audit(user_email: str, action: str, detail: str = "") -> None:
    """Record a mutation. Never pass secret values in `detail`."""
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO audit_log (ts, user_email, action, detail) VALUES (?, ?, ?, ?)",
            (_now(), user_email, action, detail),
        )


def recent_audit(limit: int = 100) -> list[dict]:
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]
