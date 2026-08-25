import json
import os
import threading
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Storage backend selection
#
# When DATABASE_URL is set (the Render/hosted deployment), settings and OAuth
# tokens are stored in a single `app_config` key/value table in Postgres. This
# is durable across restarts and deploys regardless of any disk configuration,
# which the old file-on-disk approach was not — an unmounted/ephemeral
# CONFIG_DIR would silently reset setup_complete and wipe the finance-system
# connection on every restart.
#
# When DATABASE_URL is NOT set (solo/on-prem/local dev) we keep the original
# behaviour: a plain settings.json (and tokens_*.json) under CONFIG_DIR.
# ---------------------------------------------------------------------------

_DATABASE_URL = os.environ.get('DATABASE_URL')
if _DATABASE_URL and _DATABASE_URL.startswith('postgres://'):
    # SQLAlchemy's psycopg2 driver requires the modern scheme.
    _DATABASE_URL = _DATABASE_URL.replace('postgres://', 'postgresql://', 1)

_DB_ENABLED = bool(_DATABASE_URL)

# True on a hosted deployment (Render) with a mounted persistent disk. Only
# relevant to the legacy file backend below.
_HOSTED = bool(os.environ.get('CONFIG_DIR'))

# CONFIG_DIR lets a file-backed deployment point settings + OAuth token storage
# at a mounted persistent disk instead of the app's own (ephemeral) directory.
CONFIG_DIR  = Path(os.environ['CONFIG_DIR']) if os.environ.get('CONFIG_DIR') \
    else Path(__file__).parent.parent / 'config'
CONFIG_PATH = CONFIG_DIR / 'settings.json'

DEFAULT_SETTINGS = {
    "finance_system": "sage",       # "sage" | "qbo" | "xero"
    "email": {
        "tenant_id":                "",
        "client_id":                "",
        "client_secret":            "",
        "mailbox":                  "",
        "polling_interval_minutes": 5,
        "processed_folder":         "AP Processed"
    },
    "sage": {
        "data_path":   "",
        "username":    "Manager",
        "password":    "",
        "sdo_version": "300"
    },
    "qbo": {
        "client_id":               "",
        "client_secret":           "",
        "environment":             "production",
        "redirect_uri":            "http://localhost:5000/auth/qbo/callback",
        "default_expense_account": "1"
    },
    "xero": {
        "client_id":               "",
        "client_secret":           "",
        "redirect_uri":            "http://localhost:5000/auth/xero/callback",
        "default_expense_account": "300",
        "post_status":             "AUTHORISED"  # "DRAFT" to post bills for review (safer for demos/pilots)
    },
    "ledgeriq": {
        "api_base_url":            "https://ledger.sol-iq.co.uk",
        "api_key":                 ""
    },
    "po_source": {
        "enabled":     True,           # whether to attempt PO matching at all
        "type":        "connector",   # "connector" | "folder"
        "folder_path": ""
    },
    "approval": {
        "enabled":          True,
        "threshold_amount": 1000.00,
        "approvers":        [],
        "rules":            []
    },
    "claude": {
        "api_key": "",
        "model":   "claude-opus-4-7"
    },
    "integrations": {
        "ledgeriq": {
            "enabled":      False,
            "api_base_url": "https://ledger.sol-iq.co.uk",
            "api_key":      ""
        }
    },
    "app": {
        "company_name":            "",
        "currency":                "GBP",
        "port":                    5000,
        "attachment_storage_path": "invoices",
        "setup_complete":          False
    }
}


# ---------------------------------------------------------------------------
# Database key/value backend
# ---------------------------------------------------------------------------

_engine = None
_engine_lock = threading.Lock()


def _get_engine():
    """Lazily build a standalone SQLAlchemy engine.

    Deliberately independent of the Flask-SQLAlchemy `db` object: settings and
    tokens are read/written from background threads (the email poll scheduler,
    OAuth token refresh) that have no Flask application context. A plain engine
    works in any thread.
    """
    global _engine
    if _engine is None:
        with _engine_lock:
            if _engine is None:
                from sqlalchemy import create_engine
                eng = create_engine(_DATABASE_URL, pool_pre_ping=True)
                _ensure_table(eng)
                _engine = eng
    return _engine


def _ensure_table(engine):
    from sqlalchemy import text
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE IF NOT EXISTS app_config ("
            "  key VARCHAR(64) PRIMARY KEY,"
            "  value TEXT NOT NULL"
            ")"
        ))


def _db_get(key: str):
    from sqlalchemy import text
    with _get_engine().connect() as conn:
        row = conn.execute(
            text("SELECT value FROM app_config WHERE key = :k"), {"k": key}
        ).fetchone()
    return row[0] if row else None


def _db_set(key: str, value: str):
    from sqlalchemy import text
    # Single web worker on the hosted deploy, so a plain update-else-insert is
    # safe and avoids dialect-specific upsert syntax.
    with _get_engine().begin() as conn:
        res = conn.execute(
            text("UPDATE app_config SET value = :v WHERE key = :k"),
            {"v": value, "k": key},
        )
        if res.rowcount == 0:
            conn.execute(
                text("INSERT INTO app_config (key, value) VALUES (:k, :v)"),
                {"k": key, "v": value},
            )


def _db_delete(key: str):
    from sqlalchemy import text
    with _get_engine().begin() as conn:
        conn.execute(text("DELETE FROM app_config WHERE key = :k"), {"k": key})


# ---------------------------------------------------------------------------
# Settings — public API (unchanged signatures)
# ---------------------------------------------------------------------------

def _apply_storage_override(merged: dict) -> dict:
    # On a hosted deployment attachments land on a fixed path (the mounted
    # volume); STORAGE_PATH overrides whatever was saved.
    storage_override = os.environ.get('STORAGE_PATH')
    if storage_override:
        merged.setdefault('app', {})['attachment_storage_path'] = storage_override
    return merged


def load_settings() -> dict:
    if _DB_ENABLED:
        try:
            raw = _db_get('settings')
        except Exception as e:
            # Transient DB hiccup: return defaults WITHOUT persisting so we never
            # clobber the stored row. save_settings is always a separate,
            # explicit call, so nothing is lost here.
            print(f"WARNING: could not read settings from DB ({e}); "
                  f"using in-memory defaults without writing.", flush=True)
            return _deep_copy(DEFAULT_SETTINGS)
        if not raw:
            # First run: no settings row yet. Defaults have setup_complete=False,
            # so the wizard runs; completing it writes the row.
            return _apply_storage_override(_deep_copy(DEFAULT_SETTINGS))
        try:
            stored = json.loads(raw)
        except Exception:
            stored = None
        if not isinstance(stored, dict) or not stored:
            return _apply_storage_override(_deep_copy(DEFAULT_SETTINGS))
        merged = _deep_copy(DEFAULT_SETTINGS)
        _deep_merge(merged, stored)
        return _apply_storage_override(merged)

    # ---- Legacy file backend (local / on-prem, no DATABASE_URL) ----
    stored = _read_stored()
    if stored is None:
        if _HOSTED:
            print(f"WARNING: {CONFIG_PATH} missing/unreadable — using in-memory "
                  f"defaults WITHOUT overwriting. The persistent disk may not be "
                  f"mounted yet; NOT resetting saved settings.", flush=True)
            return _deep_copy(DEFAULT_SETTINGS)
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        save_settings(DEFAULT_SETTINGS)
        return _deep_copy(DEFAULT_SETTINGS)

    merged = _deep_copy(DEFAULT_SETTINGS)
    _deep_merge(merged, stored)
    return _apply_storage_override(merged)


def save_settings(settings: dict):
    if _DB_ENABLED:
        _db_set('settings', json.dumps(settings))
        return

    # ---- Legacy file backend ----
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    # Atomic write: write to a temp file then replace, so a concurrent reader
    # never sees a half-written or truncated settings.json.
    tmp = CONFIG_PATH.with_name(CONFIG_PATH.name + '.tmp')
    with open(tmp, 'w') as f:
        json.dump(settings, f, indent=2)
    os.replace(tmp, CONFIG_PATH)


# ---------------------------------------------------------------------------
# OAuth token storage — used by the QBO and Xero connectors
#
# `name` is 'qbo' or 'xero'. In the DB backend tokens live under the key
# 'tokens_<name>'; on the file backend they live in CONFIG_DIR/tokens_<name>.json
# (the connectors' original location).
# ---------------------------------------------------------------------------

def load_tokens(name: str) -> dict:
    if _DB_ENABLED:
        try:
            raw = _db_get(f'tokens_{name}')
        except Exception as e:
            print(f"WARNING: could not read {name} tokens from DB ({e}).", flush=True)
            return {}
        if not raw:
            return {}
        try:
            data = json.loads(raw)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    path = CONFIG_DIR / f'tokens_{name}.json'
    if path.exists():
        with open(path) as f:
            return json.load(f)
    return {}


def save_tokens(name: str, tokens: dict):
    if _DB_ENABLED:
        _db_set(f'tokens_{name}', json.dumps(tokens))
        return

    path = CONFIG_DIR / f'tokens_{name}.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, 'w') as f:
        json.dump(tokens, f, indent=2)


def delete_tokens(name: str):
    if _DB_ENABLED:
        try:
            _db_delete(f'tokens_{name}')
        except Exception as e:
            print(f"WARNING: could not delete {name} tokens from DB ({e}).", flush=True)
        return

    path = CONFIG_DIR / f'tokens_{name}.json'
    path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

def _read_stored() -> dict:
    """Return the parsed settings.json, or None if it can't be read (file
    backend only). Retries briefly on a hosted deploy in case the persistent
    disk is slow to mount. NEVER writes anything."""
    attempts = 5 if _HOSTED else 1
    for i in range(attempts):
        try:
            if CONFIG_PATH.exists():
                with open(CONFIG_PATH) as f:
                    data = json.load(f)
                if isinstance(data, dict) and data:
                    return data
        except Exception as e:
            print(f"WARNING: settings.json at {CONFIG_PATH} unreadable: {e}", flush=True)
        if i < attempts - 1:
            time.sleep(0.5)
    return None


def _deep_copy(obj):
    return json.loads(json.dumps(obj))


def _deep_merge(base: dict, override: dict):
    for key, value in override.items():
        if key in base and isinstance(base[key], dict) and isinstance(value, dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value
