"""
Xero connector — uses the Xero REST API with OAuth 2.0.
Tokens are stored in config/tokens_xero.json and refreshed automatically.
Access tokens expire after 30 minutes; refresh tokens last 60 days.
"""
import base64
import json
import re
from datetime import datetime, timezone, timedelta
from difflib import SequenceMatcher
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode

import requests

from src.connectors.base import BaseConnector
from src.config_manager import load_settings, CONFIG_DIR

# ---------- Constants ----------

TOKEN_FILE    = CONFIG_DIR / 'tokens_xero.json'
AUTH_URL      = 'https://login.xero.com/identity/connect/authorize'
TOKEN_URL     = 'https://identity.xero.com/connect/token'
CONNECTIONS   = 'https://api.xero.com/connections'
API_BASE      = 'https://api.xero.com/api.xro/2.0'
SCOPE         = ('offline_access openid profile email '
                 'accounting.transactions accounting.contacts accounting.settings')


class XeroConnector(BaseConnector):

    @property
    def system_name(self) -> str:
        return 'Xero'

    @property
    def system_key(self) -> str:
        return 'xero'

    def __init__(self, settings: dict = None):
        self.settings = settings or load_settings()
        self.cfg      = self.settings.get('xero', {})

    # ------------------------------------------------------------------ #
    # OAuth
    # ------------------------------------------------------------------ #

    def requires_oauth(self) -> bool:
        return True

    def is_authenticated(self) -> bool:
        t = self._load_tokens()
        return bool(t.get('access_token') and t.get('refresh_token') and t.get('tenant_id'))

    def get_auth_url(self, state: str) -> str:
        params = {
            'response_type': 'code',
            'client_id':     self.cfg['client_id'],
            'redirect_uri':  self._redirect_uri(),
            'scope':         SCOPE,
            'state':         state,
        }
        return f"{AUTH_URL}?{urlencode(params)}"

    def handle_callback(self, code: str, state: str, **kwargs) -> dict:
        resp = requests.post(
            TOKEN_URL,
            headers={
                'Authorization': f"Basic {self._b64_creds()}",
                'Content-Type':  'application/x-www-form-urlencoded',
            },
            data={
                'grant_type':   'authorization_code',
                'code':         code,
                'redirect_uri': self._redirect_uri(),
            }
        )
        resp.raise_for_status()
        data = resp.json()

        # Fetch connected organisations
        conn_resp = requests.get(
            CONNECTIONS,
            headers={'Authorization': f"Bearer {data['access_token']}",
                     'Content-Type':  'application/json'}
        )
        conn_resp.raise_for_status()
        connections = conn_resp.json()

        tenant_id   = connections[0]['tenantId']   if connections else ''
        tenant_name = connections[0].get('tenantName', '') if connections else ''

        tokens = {
            'access_token':  data['access_token'],
            'refresh_token': data['refresh_token'],
            'expires_at':    self._expiry(data.get('expires_in', 1800)),
            'tenant_id':     tenant_id,
            'tenant_name':   tenant_name,
            'connections':   connections,
        }
        self._save_tokens(tokens)
        return tokens

    def disconnect(self):
        TOKEN_FILE.unlink(missing_ok=True)

    # ------------------------------------------------------------------ #
    # Core operations
    # ------------------------------------------------------------------ #

    def test_connection(self) -> tuple[bool, str]:
        try:
            if not self.is_authenticated():
                return False, "Not connected — authorise Xero in Settings"
            name = self._load_tokens().get('tenant_name', 'your Xero organisation')
            # Lightweight call — just check the token works
            requests.get(
                f"{API_BASE}/Currencies",
                headers=self._headers(),
            ).raise_for_status()
            return True, f"Connected to {name}"
        except Exception as e:
            return False, str(e)

    def get_purchase_orders(self) -> list[dict]:
        resp = requests.get(
            f"{API_BASE}/PurchaseOrders",
            headers=self._headers(),
            params={'Status': 'SUBMITTED,AUTHORISED'}
        )
        resp.raise_for_status()
        pos = []
        for po in resp.json().get('PurchaseOrders', []):
            contact = po.get('Contact', {})
            lines   = self._parse_lines(po.get('LineItems', []))
            pos.append({
                'po_number':        po.get('PurchaseOrderNumber', po.get('PurchaseOrderID', '')),
                'supplier_name':    contact.get('Name', ''),
                'supplier_ref':     contact.get('ContactID', ''),
                'po_date':          self._xero_date(po.get('Date')),
                'expected_delivery':self._xero_date(po.get('DeliveryDate')),
                'total_amount':     float(po.get('Total', 0)),
                'vat_amount':       float(po.get('TotalTax', 0)),
                'subtotal':         float(po.get('SubTotal', 0)),
                'currency':         po.get('CurrencyCode', 'GBP'),
                'status':           po.get('Status', ''),
                'source':           'xero',
                'lines':            lines,
            })
        return pos

    def find_vendor(self, supplier_name: str) -> Optional[str]:
        resp = requests.get(
            f"{API_BASE}/Contacts",
            headers=self._headers(),
            params={'searchTerm': supplier_name[:50], 'includeArchived': 'false'}
        )
        resp.raise_for_status()
        contacts = resp.json().get('Contacts', [])

        if not contacts:
            return None

        best_id, best_score = None, 0.0
        for c in contacts:
            score = SequenceMatcher(
                None, supplier_name.lower(), c.get('Name', '').lower()
            ).ratio()
            if score > best_score:
                best_score, best_id = score, c['ContactID']

        return best_id if best_score >= 0.70 else None

    def post_invoice(self, invoice_data: dict) -> str:
        account_code = self.cfg.get('default_expense_account', '300')

        if invoice_data.get('lines'):
            line_items = [{
                'Description': l.get('description', ''),
                'Quantity':    float(l.get('quantity', 1)),
                'UnitAmount':  float(l.get('unit_price', 0)),
                'LineAmount':  float(l.get('line_total', 0)),
                'AccountCode': account_code,
                'TaxType':     'INPUT2',   # UK standard rated input VAT
            } for l in invoice_data['lines']]
        else:
            line_items = [{
                'Description': f"Invoice {invoice_data.get('invoice_number', '')}",
                'Quantity':    1,
                'UnitAmount':  float(invoice_data['subtotal']),
                'LineAmount':  float(invoice_data['subtotal']),
                'AccountCode': account_code,
                'TaxType':     'INPUT2',
            }]

        xero_inv = {
            'Type':            'ACCPAY',
            'Contact':         {'ContactID': invoice_data['supplier_ref']},
            'Date':            str(invoice_data['invoice_date']),
            'InvoiceNumber':   invoice_data.get('invoice_number', ''),
            'Reference':       invoice_data.get('po_reference', ''),
            'Status':          'AUTHORISED',
            'LineAmountTypes': 'Exclusive',
            'SubTotal':        float(invoice_data['subtotal']),
            'TotalTax':        float(invoice_data['vat_amount']),
            'Total':           float(invoice_data['total_amount']),
            'LineItems':       line_items,
        }
        if invoice_data.get('due_date'):
            xero_inv['DueDate'] = str(invoice_data['due_date'])

        resp = requests.post(
            f"{API_BASE}/Invoices",
            headers=self._headers(),
            json={'Invoices': [xero_inv]}
        )
        resp.raise_for_status()
        invoices = resp.json().get('Invoices', [])
        return invoices[0].get('InvoiceID', '') if invoices else ''

    # ------------------------------------------------------------------ #
    # Internal helpers
    # ------------------------------------------------------------------ #

    def _headers(self) -> dict:
        tokens = self._load_tokens()
        if not tokens:
            raise RuntimeError("Xero is not connected. Authorise in Settings.")

        exp = tokens.get('expires_at', '')
        if exp and datetime.now(timezone.utc) >= datetime.fromisoformat(exp) - timedelta(seconds=60):
            tokens = self._refresh(tokens)

        return {
            'Authorization':  f"Bearer {tokens['access_token']}",
            'Xero-Tenant-Id': tokens['tenant_id'],
            'Accept':         'application/json',
            'Content-Type':   'application/json',
        }

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
            'expires_at':    self._expiry(data.get('expires_in', 1800)),
        })
        self._save_tokens(tokens)
        return tokens

    def _b64_creds(self) -> str:
        return base64.b64encode(
            f"{self.cfg['client_id']}:{self.cfg['client_secret']}".encode()
        ).decode()

    def _redirect_uri(self) -> str:
        return self.cfg.get('redirect_uri', 'http://localhost:5000/auth/xero/callback')

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
    def _parse_lines(raw: list) -> list:
        return [{
            'line_number':      n,
            'description':      l.get('Description', ''),
            'product_code':     l.get('ItemCode', ''),
            'quantity':         float(l.get('Quantity', 1)),
            'unit_price':       float(l.get('UnitAmount', 0)),
            'line_total':       float(l.get('LineAmount', 0)),
            'quantity_invoiced': 0,
        } for n, l in enumerate(raw, 1)]

    @staticmethod
    def _xero_date(value: str) -> Optional[str]:
        if not value:
            return None
        # /Date(milliseconds+offset)/ format
        m = re.search(r'/Date\((\d+)', value or '')
        if m:
            return datetime.fromtimestamp(
                int(m.group(1)) / 1000, tz=timezone.utc
            ).date().isoformat()
        return value[:10] if value else None
