import json
import os
from pathlib import Path

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
        "default_expense_account": "300"
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


def load_settings() -> dict:
    if not CONFIG_PATH.exists():
        if os.environ.get('CONFIG_DIR'):
            # CONFIG_DIR is only set on a hosted deployment with a persistent
            # disk — settings.json missing there means either a genuinely
            # fresh install, or the disk wasn't mounted/available when this
            # was checked. Log loudly so a silent settings reset is visible
            # in the deploy logs rather than just appearing as "wizard again".
            print(f"WARNING: settings.json not found at {CONFIG_PATH} — "
                  f"writing fresh defaults. If this is not a new install, "
                  f"the persistent disk may not have been mounted yet.", flush=True)
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        save_settings(DEFAULT_SETTINGS)
        return _deep_copy(DEFAULT_SETTINGS)
    with open(CONFIG_PATH) as f:
        stored = json.load(f)
    merged = _deep_copy(DEFAULT_SETTINGS)
    _deep_merge(merged, stored)
    # On a hosted deployment (e.g. Render) a persistent disk is mounted at a
    # fixed path — STORAGE_PATH overrides whatever was saved in settings.json
    # so invoice attachments always land on the durable volume.
    storage_override = os.environ.get('STORAGE_PATH')
    if storage_override:
        merged.setdefault('app', {})['attachment_storage_path'] = storage_override
    return merged


def save_settings(settings: dict):
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_PATH, 'w') as f:
        json.dump(settings, f, indent=2)


def _deep_copy(obj):
    return json.loads(json.dumps(obj))


def _deep_merge(base: dict, override: dict):
    for key, value in override.items():
        if key in base and isinstance(base[key], dict) and isinstance(value, dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value
