"""
soliq_licence — offline licence VERIFIER for Python / Flask on-prem products.

Python port of @sol-iq/licence (packages/licence/verify.js). Verifies the SAME
Ed25519-signed licence files the Node issuer produces, so one issuer serves every
product regardless of language. Customer-side only: embeds the PUBLIC key, cannot issue.

Design (identical to the Node original):
  - Offline-first: no phone-home. Validity from a locally signed token.
  - Hidden grace period in software config, never in the licence file.
  - Fail-soft: once grace lapses, block the LICENSED ACTION but never lock the customer
    out of data already on their server.
  - Clock-rollback guard via a persisted "last seen" date.
  - Product scoping: a licence for one product will not unlock another.
  - Module entitlements drive "see-but-locked" upsell.

Dependency: `cryptography` (Ed25519). Install: pip install cryptography

Usage (Flask):
    from soliq_licence import create_licensing
    licence = create_licensing(product_id="INVOICEIQ", product_name="Invoice-IQ",
                               locked_action="approving new invoices")
    status = licence.evaluate_file("/config/licence.soliq")
    if not status["action_allowed"]:
        pause_processing(status["message"])
    if not licence.has_module(status, "export"):
        show_locked_upsell("export")
"""
import base64
import json
import math
import os
from datetime import datetime, timedelta, timezone

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.serialization import load_pem_public_key

MS_PER_DAY = 86_400_000
_DAY = timedelta(days=1)

# Shared Sol-IQ signing PUBLIC key (must match packages/licence/verify.js).
DEFAULT_PUBLIC_KEY_PEM = (
    "-----BEGIN PUBLIC KEY-----\n"
    "MCowBQYDK2VwAyEAc0VQkItxqE36N9r2M1sVCIJTWEFveEiNfp7+NTp6K7Y=\n"
    "-----END PUBLIC KEY-----\n"
)


def _parse_dt(value):
    """Parse an ISO-8601 string (accepts trailing 'Z') into an aware UTC datetime."""
    if isinstance(value, datetime):
        dt = value
    else:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _fmt(dt):
    # e.g. "1 August 2027" — %-d is not portable to Windows, so strip a leading zero.
    return dt.strftime("%d %B %Y").lstrip("0")


class _Licensing:
    def __init__(self, product_id, product_name=None, brand="Sol-IQ", public_key_pem=None,
                 env_prefix=None, state_dir=None, grace_days=21, remind_at_days=None,
                 locked_action="processing new work"):
        if not product_id:
            raise ValueError("create_licensing: product_id is required")
        self.product_id = product_id
        self.product_name = product_name or product_id
        self.brand = brand
        self.env = env_prefix or "".join(c for c in product_id.upper() if c.isalnum())
        self.public_key_pem = (
            public_key_pem or os.environ.get(f"{self.env}_PUBLIC_KEY_PEM") or DEFAULT_PUBLIC_KEY_PEM
        )
        self.grace_days = 21 if grace_days is None else grace_days
        self.remind_at_days = remind_at_days or [30, 14, 7, 3]
        self.action = locked_action
        self.default_state = (
            os.path.join(state_dir, "licstate.json") if state_dir
            else os.path.join(os.getcwd(), f".{product_id.lower()}", "licstate.json")
        )

    # ----- signature + product scope -----
    def parse_and_verify(self, licence_string, public_key_pem=None):
        pem = (public_key_pem or self.public_key_pem).encode() if isinstance(
            public_key_pem or self.public_key_pem, str) else (public_key_pem or self.public_key_pem)
        parts = str(licence_string).strip().split(".")
        if len(parts) != 2:
            raise ValueError("Malformed licence file")
        body = base64.b64decode(parts[0])
        sig = base64.b64decode(parts[1])
        key = load_pem_public_key(pem)
        try:
            key.verify(sig, body)  # Ed25519: raises InvalidSignature on mismatch
        except InvalidSignature:
            raise ValueError("Licence signature invalid (tampered or wrong key)")
        payload = json.loads(body.decode())
        if payload.get("product") and payload["product"] != self.product_id:
            raise ValueError(f'Licence is for "{payload["product"]}", not {self.product_id}')
        return payload

    # ----- clock-rollback guard -----
    @staticmethod
    def _read_last_seen(state_path):
        try:
            with open(state_path, "r", encoding="utf-8") as f:
                return _parse_dt(json.load(f)["lastSeen"])
        except Exception:
            return None

    @staticmethod
    def _write_last_seen(state_path, when):
        try:
            os.makedirs(os.path.dirname(state_path), exist_ok=True)
            with open(state_path, "w", encoding="utf-8") as f:
                json.dump({"lastSeen": when.isoformat()}, f)
        except Exception:
            pass  # non-fatal: guard simply weakens if we cannot persist

    def evaluate(self, licence, now=None, state_path=None, install_id=None):
        now = _parse_dt(now) if now else datetime.now(timezone.utc)
        state_path = state_path or os.environ.get(f"{self.env}_LICENCE_STATE_PATH") or self.default_state

        last_seen = self._read_last_seen(state_path)
        clock_suspect = bool(last_seen and now < last_seen - _DAY)
        effective_now = last_seen if clock_suspect else now
        self._write_last_seen(state_path, effective_now if effective_now > now else now)

        expiry = _parse_dt(licence["expiry"])
        grace_end = expiry + timedelta(days=self.grace_days)
        days_to_expiry = math.ceil((expiry - effective_now).total_seconds() / 86400)
        modules = licence.get("modules") or licence.get("features") or []
        fmt = _fmt(expiry)

        # INSTALL BINDING — anti-copy: a bound licence must not run on another install.
        local_install_id = install_id or os.environ.get(f"{self.env}_INSTALL_ID")
        bound = licence.get("installId") and licence["installId"] not in ("ANY", "*")
        if bound and local_install_id and local_install_id != licence["installId"]:
            return self._pack("wrong_host", False, days_to_expiry=None, grace_days_remaining=None,
                              expiry=expiry, modules=modules, clock_suspect=clock_suspect, severity="critical",
                              message=(f"This {self.product_name} licence was issued for a different "
                                       f"installation. {self.action.capitalize()} will not run here. "
                                       f"Contact {self.brand} to re-issue it for this machine."))

        if effective_now < expiry:
            crossed = sorted([d for d in self.remind_at_days if days_to_expiry <= d])
            if not crossed:
                return self._pack("active", True, days_to_expiry=days_to_expiry, grace_days_remaining=None,
                                  expiry=expiry, modules=modules, clock_suspect=clock_suspect,
                                  severity="none", message="")
            sev = "urgent" if days_to_expiry <= 3 else "warn" if days_to_expiry <= 7 else "info"
            plural = "" if days_to_expiry == 1 else "s"
            return self._pack("reminder", True, days_to_expiry=days_to_expiry, grace_days_remaining=None,
                              expiry=expiry, modules=modules, clock_suspect=clock_suspect, severity=sev,
                              message=(f"Your {self.product_name} licence expires on {fmt} "
                                       f"({days_to_expiry} day{plural} away). Please arrange renewal "
                                       f"to avoid interruption."))

        if effective_now < grace_end:
            grace_left = math.ceil((grace_end - effective_now).total_seconds() / 86400)
            sev = "critical" if grace_left <= 5 else "urgent"
            plural = "" if grace_left == 1 else "s"
            return self._pack("expired_grace", True, days_to_expiry=None, grace_days_remaining=grace_left,
                              expiry=expiry, modules=modules, clock_suspect=clock_suspect, severity=sev,
                              message=(f"Your {self.product_name} licence expired on {fmt}. You have "
                                       f"{grace_left} day{plural} to renew. The system is still running "
                                       f"as normal — please renew now to avoid interruption."))

        return self._pack("lapsed", False, days_to_expiry=None, grace_days_remaining=0, expiry=expiry,
                          modules=modules, clock_suspect=clock_suspect, severity="critical",
                          message=(f"Your {self.product_name} licence and renewal grace period have ended. "
                                   f"{self.action.capitalize()} will not run until the licence is renewed. "
                                   f"Data already stored remains fully accessible. Please contact "
                                   f"{self.brand} to renew."))

    @staticmethod
    def _pack(state, action_allowed, **rest):
        return {"state": state, "action_allowed": action_allowed, **rest}

    def evaluate_file(self, licence_path, now=None, state_path=None, public_key_pem=None, install_id=None):
        try:
            with open(licence_path, "r", encoding="utf-8") as f:
                raw = f.read()
            payload = self.parse_and_verify(raw, public_key_pem)
            status = self.evaluate(payload, now=now, state_path=state_path, install_id=install_id)
            status["licence"] = {
                "licence_no": payload.get("licenceNo"), "product": payload.get("product", self.product_id),
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
                "message": (f"No valid {self.product_name} licence found ({e}). "
                            f"{self.action.capitalize()} will not run. Contact {self.brand}."),
            }

    @staticmethod
    def has_module(status_or_modules, module_name):
        mods = status_or_modules.get("modules", []) if isinstance(status_or_modules, dict) else (status_or_modules or [])
        return "*" in mods or module_name in mods


def create_licensing(**cfg):
    """Build a licensing helper bound to one product's configuration."""
    return _Licensing(**cfg)
