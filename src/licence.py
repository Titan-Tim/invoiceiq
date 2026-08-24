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


def _grace_days():
    """Renewal grace after expiry. Default 21; set INVOICEIQ_LICENCE_GRACE_DAYS=0 for a
    hard-stop trial (processing stops the moment the trial term ends)."""
    raw = os.environ.get("INVOICEIQ_LICENCE_GRACE_DAYS", "").strip()
    if raw == "":
        return None  # let the verifier use its default (21)
    try:
        return max(0, int(raw))
    except ValueError:
        return None


def _get_engine():
    global _engine
    if _engine is None:
        # Lazy: pulls in cryptography only on-prem, never on the cloud SaaS.
        from .soliq_licence import create_licensing
        _engine = create_licensing(
            product_id="INVOICEIQ",
            product_name="Invoice-IQ",
            locked_action="processing new invoices",
            grace_days=_grace_days(),
        )
    return _engine


def _evaluate_current():
    """Evaluate the licence from the INVOICEIQ_LICENCE env string if set (handy for cloud
    instances / trials — no file needed), else from INVOICEIQ_LICENCE_PATH on disk."""
    eng = _get_engine()
    raw = os.environ.get("INVOICEIQ_LICENCE", "").strip()
    if not raw:
        return eng.evaluate_file(_LICENCE_PATH)
    try:
        payload = eng.parse_and_verify(raw)
        status = eng.evaluate(payload)
        status["licence"] = {
            "licence_no": payload.get("licenceNo"), "product": payload.get("product", "INVOICEIQ"),
            "customer": payload.get("customer"), "edition": payload.get("edition"),
            "install_id": payload.get("installId"), "seats": payload.get("seats"),
            "modules": status.get("modules", []),
        }
        return status
    except Exception as e:
        return {
            "state": "invalid", "action_allowed": False, "severity": "critical",
            "days_to_expiry": None, "grace_days_remaining": None, "expiry": None,
            "modules": [], "clock_suspect": False, "licence": None,
            "message": (f"No valid Invoice-IQ licence found ({e}). "
                        f"Processing new invoices will not run. Contact Sol-IQ."),
        }


def licence_status(ttl=30):
    """Current licence status dict, or None when enforcement is off. Cached briefly."""
    if not licence_enforced():
        return None
    now = time.time()
    if _cache["status"] is None or now - _cache["at"] > ttl:
        _cache["status"] = _evaluate_current()
        _cache["at"] = now
    return _cache["status"]


def licence_has_module(name):
    """True if the module is entitled (always True when enforcement is off)."""
    s = licence_status()
    return True if s is None else _get_engine().has_module(s, name)


def is_licensed_action(method, path):
    """Is this request a mutating 'processing' action that a lapsed licence should pause?"""
    return method in ("POST", "PUT", "DELETE") and any(path.startswith(p) for p in _ACTION_PREFIXES)
