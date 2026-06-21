"""
QuickBooks Online connector — uses the Intuit QBO REST API with OAuth 2.0.
Tokens are stored in config/tokens_qbo.json and refreshed automatically.
"""
import base64
import json
import secrets
import threading
from datetime import datetime, timezone, timedelta
from difflib import SequenceMatcher
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode

import requests

from src.connectors.base import BaseConnector
from src.config_manager import load_settings, CONFIG_DIR

# ---------- Constants ----------

TOKEN_FILE  = CONFIG_DIR / 'tokens_qbo.json'
AUTH_URL    = 'https://appcenter.intuit.com/connect/oauth2'
TOKEN_URL   = 'https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer'
REVOKE_URL  = 'https://developer.api.intuit.com/v2/oauth2/tokens/revoke'
SCOPE       = 'com.intuit.quickbooks.accounting'
PROD_BASE   = 'https://quickbooks.api.intuit.com/v3/company'
SAND_BASE   = 'https://sandbox-quickbooks.api.intuit.com/v3/company'

# QBO rotates the refresh token on every use and invalidates the old one
# immediately. The background email-poll scheduler and a user action in the
# browser run on separate threads in the same process and can both decide to
# refresh at once — this lock makes that impossible by serialising access.
_refresh_lock = threading.Lock()


class QBOConnector(BaseConnector):

    @property
    def system_name(self) -> str:
        return 'QuickBooks Online'

    @property
    def system_key(self) -> str:
        return 'qbo'

    def __init__(self, settings: dict = None):
        self.settings = settings or load_settings()
        self.cfg  = self.settings.get('qbo', {})
        sandbox   = self.cfg.get('environment', 'production') == 'sandbox'
        self._base = SAND_BASE if sandbox else PROD_BASE

    # ------------------------------------------------------------------ #
    # OAuth
    # ------------------------------------------------------------------ #

    def requires_oauth(self) -> bool:
        return True

    def is_authenticated(self) -> bool:
        t = self._load_tokens()
        return bool(t.get('access_token') and t.get('refresh_token') and t.get('realm_id'))

    def get_auth_url(self, state: str) -> str:
        params = {
            'client_id':     self.cfg['client_id'],
            'response_type': 'code',
            'scope':         SCOPE,
            'redirect_uri':  self._redirect_uri(),
            'state':         state,
        }
        return f"{AUTH_URL}?{urlencode(params)}"

    def handle_callback(self, code: str, state: str, **kwargs) -> dict:
        realm_id = kwargs.get('realmId', '')
        resp = requests.post(
            TOKEN_URL,
            headers={
                'Authorization': f"Basic {self._b64_creds()}",
                'Content-Type':  'application/x-www-form-urlencoded',
                'Accept':        'application/json',
            },
            data={
                'grant_type':   'authorization_code',
                'code':         code,
                'redirect_uri': self._redirect_uri(),
            }
        )
        resp.raise_for_status()
        data = resp.json()
        tokens = {
            'access_token':  data['access_token'],
            'refresh_token': data['refresh_token'],
            'realm_id':      realm_id,
            'expires_at':    self._expiry(data.get('expires_in', 3600)),
        }
        self._save_tokens(tokens)
        return tokens

    def disconnect(self):
        tokens = self._load_tokens()
        if tokens.get('refresh_token'):
            try:
                requests.post(
                    REVOKE_URL,
                    headers={'Authorization': f"Basic {self._b64_creds()}",
                             'Accept': 'application/json',
                             'Content-Type': 'application/x-www-form-urlencoded'},
                    data={'token': tokens['refresh_token']}
                )
            except Exception:
                pass
        TOKEN_FILE.unlink(missing_ok=True)

    # ------------------------------------------------------------------ #
    # Core operations
    # ------------------------------------------------------------------ #

    def test_connection(self) -> tuple[bool, str]:
        try:
            if not self.is_authenticated():
                return False, "Not connected — authorise QuickBooks Online in Settings"
            realm = self._realm_id()
            url   = f"{self._base}/{realm}/companyinfo/{realm}"
            resp  = requests.get(url, headers=self._headers())
            if not resp.ok:
                return False, f"{resp.status_code} {resp.reason}: {resp.text[:500]}"
            name  = resp.json().get('CompanyInfo', {}).get('CompanyName', '')
            return True, f"Connected to {name}"
        except Exception as e:
            return False, str(e)

    def get_expense_accounts(self) -> list[dict]:
        """Accounts valid for use as AccountRef on a Bill expense line."""
        qr = self._query(
            "SELECT Id, Name, AccountType FROM Account "
            "WHERE AccountType IN ('Expense', 'Other Expense', 'Cost of Goods Sold') "
            "MAXRESULTS 200"
        )
        return [{'id': a['Id'], 'name': a['Name'], 'type': a.get('AccountType', '')}
                for a in qr.get('Account', [])]

    def get_purchase_orders(self) -> list[dict]:
        qr = self._query(
            "SELECT * FROM PurchaseOrder WHERE POStatus = 'Open' MAXRESULTS 1000"
        )
        pos = []
        for po in qr.get('PurchaseOrder', []):
            lines  = self._parse_po_lines(po.get('Line', []))
            total  = float(po.get('TotalAmt', 0))
            tax    = float((po.get('TxnTaxDetail') or {}).get('TotalTax', 0))
            pos.append({
                'po_number':        po.get('DocNumber', po.get('Id', '')),
                'supplier_name':    po.get('VendorRef', {}).get('name', ''),
                'supplier_ref':     po.get('VendorRef', {}).get('value', ''),
                'po_date':          po.get('TxnDate'),
                'expected_delivery':po.get('ShipDate'),
                'total_amount':     total,
                'vat_amount':       tax,
                'subtotal':         total - tax,
                'currency':         po.get('CurrencyRef', {}).get('value', 'GBP'),
                'status':           po.get('POStatus', 'Open'),
                'source':           'qbo',
                'lines':            lines,
            })
        return pos

    def find_vendor(self, supplier_name: str) -> Optional[str]:
        # Try name search first
        safe = supplier_name.replace("'", "\\'")[:30]
        qr = self._query(
            f"SELECT * FROM Vendor WHERE DisplayName LIKE '%{safe}%' MAXRESULTS 20"
        )
        vendors = qr.get('Vendor', [])

        # Fallback: fetch all and fuzzy-match
        if not vendors:
            qr2    = self._query("SELECT * FROM Vendor MAXRESULTS 500")
            vendors = qr2.get('Vendor', [])

        if not vendors:
            return None

        best_id, best_score = None, 0.0
        for v in vendors:
            score = SequenceMatcher(
                None, supplier_name.lower(), v.get('DisplayName', '').lower()
            ).ratio()
            if score > best_score:
                best_score, best_id = score, v['Id']

        return best_id if best_score >= 0.70 else None

    def post_invoice(self, invoice_data: dict) -> str:
        account = self.cfg.get('default_expense_account', '1')

        # BillableStatus is only meaningful when "Track expenses and items
        # by customer" is enabled in QBO's account preferences — if it's
        # off (the common case for a company that doesn't rebill expenses
        # to customers), QBO rejects the property outright as unsupported.
        # It isn't needed for an ordinary AP bill, so it's omitted entirely.
        if invoice_data.get('lines'):
            lines = [{
                'Amount':     float(l.get('line_total', 0)),
                'DetailType': 'AccountBasedExpenseLineDetail',
                'Description': l.get('description', ''),
                'AccountBasedExpenseLineDetail': {
                    'AccountRef': {'value': account},
                },
            } for l in invoice_data['lines']]
        else:
            lines = [{
                'Amount':     float(invoice_data['subtotal']),
                'DetailType': 'AccountBasedExpenseLineDetail',
                'Description': f"Invoice {invoice_data.get('invoice_number', '')}",
                'AccountBasedExpenseLineDetail': {
                    'AccountRef': {'value': account},
                },
            }]

        # VAT isn't broken out per line item, so add it as its own line —
        # otherwise the lines only sum to the subtotal and QuickBooks
        # calculates TotalAmt from the lines itself (it's not a field you
        # set directly; sending it explicitly is what triggers QBO's
        # generic "unsupported property" validation error).
        vat_amount = float(invoice_data.get('vat_amount', 0) or 0)
        if vat_amount:
            lines.append({
                'Amount':     vat_amount,
                'DetailType': 'AccountBasedExpenseLineDetail',
                'Description': 'VAT',
                'AccountBasedExpenseLineDetail': {
                    'AccountRef': {'value': account},
                },
            })

        bill = {
            'VendorRef': {'value': invoice_data['supplier_ref']},
            'TxnDate':   str(invoice_data['invoice_date']),
            'DocNumber': invoice_data.get('invoice_number', ''),
            'Line':      lines,
        }
        if invoice_data.get('due_date'):
            bill['DueDate'] = str(invoice_data['due_date'])

        url     = f"{self._base}/{self._realm_id()}/bill"
        payload = {'Bill': bill}
        resp    = requests.post(url, headers=self._headers(), json=payload)
        if not resp.ok:
            raise RuntimeError(
                f"{resp.status_code} {resp.reason}: {resp.text[:500]} "
                f"| Sent: {json.dumps(payload)}"
            )
        return str(resp.json().get('Bill', {}).get('Id', ''))

    # ------------------------------------------------------------------ #
    # Internal helpers
    # ------------------------------------------------------------------ #

    def _realm_id(self) -> str:
        return self._load_tokens().get('realm_id', '')

    def _headers(self) -> dict:
        return {
            'Authorization': f"Bearer {self._access_token()}",
            'Accept':        'application/json',
            'Content-Type':  'application/json',
        }

    def _access_token(self) -> str:
        with _refresh_lock:
            # Re-read from disk while holding the lock — if another thread
            # just refreshed, this picks up its fresh token instead of
            # racing to refresh again with the now-invalid old one.
            tokens = self._load_tokens()
            if not tokens:
                raise RuntimeError(
                    "QuickBooks Online is not connected. Authorise in Settings."
                )
            exp = tokens.get('expires_at', '')
            if exp and datetime.now(timezone.utc) >= datetime.fromisoformat(exp) - timedelta(seconds=60):
                tokens = self._refresh(tokens)
            return tokens['access_token']

    def _refresh(self, tokens: dict) -> dict:
        resp = requests.post(
            TOKEN_URL,
            headers={
                'Authorization': f"Basic {self._b64_creds()}",
                'Content-Type':  'application/x-www-form-urlencoded',
            },
            data={
                'grant_type':    'refresh_token',
                'refresh_token': tokens['refresh_token'],
            }
        )
        resp.raise_for_status()
        data = resp.json()
        tokens.update({
            'access_token':  data['access_token'],
            'refresh_token': data.get('refresh_token', tokens['refresh_token']),
            'expires_at':    self._expiry(data.get('expires_in', 3600)),
        })
        self._save_tokens(tokens)
        return tokens

    def _query(self, sql: str) -> dict:
        url  = f"{self._base}/{self._realm_id()}/query"
        resp = requests.get(url, headers=self._headers(), params={'query': sql})
        self._raise_detailed(resp)
        return resp.json().get('QueryResponse', {})

    @staticmethod
    def _raise_detailed(resp):
        """Raise with Intuit's actual error body, not just the HTTP status line."""
        if not resp.ok:
            raise RuntimeError(f"{resp.status_code} {resp.reason}: {resp.text[:500]}")

    def _b64_creds(self) -> str:
        raw = f"{self.cfg['client_id']}:{self.cfg['client_secret']}"
        return base64.b64encode(raw.encode()).decode()

    def _redirect_uri(self) -> str:
        return self.cfg.get('redirect_uri', 'http://localhost:5000/auth/qbo/callback')

    def _load_tokens(self) -> dict:
        if TOKEN_FILE.exists():
            with open(TOKEN_FILE) as f:
                return json.load(f)
        return {}

    def _save_tokens(self, tokens: dict):
        TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(TOKEN_FILE, 'w') as f:
            json.dump(tokens, f, indent=2)

    @staticmethod
    def _expiry(seconds: int) -> str:
        return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()

    @staticmethod
    def _parse_po_lines(raw: list) -> list:
        lines = []
        for n, line in enumerate(raw, 1):
            det = (line.get('ItemBasedExpenseLineDetail') or
                   line.get('AccountBasedExpenseLineDetail') or {})
            lines.append({
                'line_number':      n,
                'description':      line.get('Description', ''),
                'product_code':     det.get('ItemRef', {}).get('value', ''),
                'quantity':         float(det.get('Qty', 1)),
                'unit_price':       float(det.get('UnitPrice', line.get('Amount', 0))),
                'line_total':       float(line.get('Amount', 0)),
                'quantity_invoiced': 0,
            })
        return lines
