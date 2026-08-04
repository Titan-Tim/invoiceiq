import json
import os
import time
from pathlib import Path

# True on a hosted deployment (Render) with a mounted persistent disk.
_HOSTED = bool(os.environ.get('CONFIG_DIR'))

# CONFIG_DIR lets a hosted deployment (e.g. Render) point settings + OAuth
# token storage at a mounted persistent disk instead of the app's own
# (ephemeral, rebuilt-on-every-deploy) directory. Solo/on-prem installs
# leave this unset and get the existing local config/ folder.
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


def _read_stored() -> dict:
    """Return the parsed settings.json, or None if it can't be read.

    On a hosted deploy the persistent disk can take a moment to mount after a
    restart, so we retry briefly rather than treating a not-yet-mounted disk as
    "no settings". Crucially, this NEVER writes anything — see load_settings for
    why writing defaults over a slow/absent mount is dangerous."""
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


def load_settings() -> dict:
    stored = _read_stored()

    if stored is None:
        # No usable settings file. On a HOSTED deploy we deliberately do NOT
        # write defaults: if the persistent disk is merely slow to mount (or
        # briefly unreadable), writing blank defaults would clobber the real
        # settings and bounce every user back to the setup wizard — losing the
        # Xero connection in the process. Return in-memory defaults instead;
        # once the disk is readable the real file is picked up again untouched.
        if _HOSTED:
            print(f"WARNING: {CONFIG_PATH} missing/unreadable — using in-memory "
                  f"defaults WITHOUT overwriting. The persistent disk may not be "
                  f"mounted yet; NOT resetting saved settings.", flush=True)
            return _deep_copy(DEFAULT_SETTINGS)
        # Local/dev first run: safe to create a starter file.
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        save_settings(DEFAULT_SETTINGS)
        return _deep_copy(DEFAULT_SETTINGS)

    merged = _deep_copy(DEFAULT_SETTINGS)
    _deep_merge(merged, stored)
    # On a hosted deployment a persistent disk is mounted at a fixed path —
    # STORAGE_PATH overrides whatever was saved so attachments land on the volume.
    storage_override = os.environ.get('STORAGE_PATH')
    if storage_override:
        merged.setdefault('app', {})['attachment_storage_path'] = storage_override
    return merged


def save_settings(settings: dict):
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    # Atomic write: write to a temp file then replace, so a concurrent reader
    # never sees a half-written or truncated settings.json.
    tmp = CONFIG_PATH.with_name(CONFIG_PATH.name + '.tmp')
    with open(tmp, 'w') as f:
        json.dump(settings, f, indent=2)
    os.replace(tmp, CONFIG_PATH)


def _deep_copy(obj):
    return json.loads(json.dumps(obj))


def _deep_merge(base: dict, override: dict):
    for key, value in override.items():
        if key in base and isinstance(base[key], dict) and isinstance(value, dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value
