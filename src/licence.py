"""
Invoice-IQ on-prem licence gate (a Sol-IQ product).

Wraps the shared verifier (src/soliq_licence.py, vendored from
suite-onprem-licensing/packages/licence-py). Enforcement is OFF unless
INVOICEIQ_LICENCE_ENFORCED is set, so the cloud/Render SaaS deployment is a complete
no-op here — cryptography is imported lazily and only when enforcement is on.

On-prem/Solo deployments set:
    INVOICEIQ_LICENCE_ENFORCED=1
    INVOICEIQ_LICENCE_PATH=/config/licence.soliq   (optional; this is the default-ish)
"""
import os
import time

_LICENCE_PATH = os.environ.get("INVOICEIQ_LICENCE_PATH", "config/licence.soliq")

# Mutating requests under these path prefixes are the licensed "processing" action.
# GET/read routes are never gated, so an expired licence never hides existing data.
_ACTION_PREFIXES = ("/api/invoices", "/api/remittances", "/api/email/poll", "/api/finance/sync-pos")

_engine = None
_cache = {"at": 0.0, "status": None}


def licence_enforced():
    """On-prem enforcement flag. Cloud/Render leaves this unset -> full no-op."""
    return os.environ.get("INVOICEIQ_LICENCE_ENFORCED", "").strip().lower() in ("1", "true", "yes", "on")


def _get_engine():
    global _engine
    if _engine is None:
        # Lazy: pulls in cryptography only on-prem, never on the cloud SaaS.
        from .soliq_licence import create_licensing
        _engine = create_licensing(
            product_id="INVOICEIQ",
            product_name="Invoice-IQ",
            locked_action="processing new invoices",
        )
    return _engine


def licence_status(ttl=30):
    """Current licence status dict, or None when enforcement is off. Cached briefly."""
    if not licence_enforced():
        return None
    now = time.time()
    if _cache["status"] is None or now - _cache["at"] > ttl:
        _cache["status"] = _get_engine().evaluate_file(_LICENCE_PATH)
        _cache["at"] = now
    return _cache["status"]


def licence_has_module(name):
    """True if the module is entitled (always True when enforcement is off)."""
    s = licence_status()
    return True if s is None else _get_engine().has_module(s, name)


def is_licensed_action(method, path):
    """Is this request a mutating 'processing' action that a lapsed licence should pause?"""
    return method in ("POST", "PUT", "DELETE") and any(path.startswith(p) for p in _ACTION_PREFIXES)
