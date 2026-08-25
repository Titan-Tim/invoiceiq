"""
Xero connector — uses the Xero REST API with OAuth 2.0.
Tokens are stored in config/tokens_xero.json and refreshed automatically.
Access tokens expire after 30 minutes; refresh tokens last 60 days.
"""
import base64
import re
import threading
from datetime import datetime, date, timezone, timedelta
from difflib import SequenceMatcher
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode

import requests

from src.connectors.base import BaseConnector
from src.config_manager import (load_settings,
                                load_tokens, save_tokens, delete_tokens)

# ---------- Constants ----------

AUTH_URL      = 'https://login.xero.com/identity/connect/authorize'
TOKEN_URL     = 'https://identity.xero.com/connect/token'
CONNECTIONS   = 'https://api.xero.com/connections'
API_BASE      = 'https://api.xero.com/api.xro/2.0'
# Granular scopes (required for apps created on/after 2 Mar 2026 — broad
# scopes like accounting.transactions are rejected with invalid_scope).
# accounting.invoices covers ACCPAY bills + purchase orders; contacts and
# settings are unchanged in the granular model.
SCOPE         = ('offline_access openid profile email '
                 'accounting.invoices accounting.contacts accounting.settings')

# Xero rotates the refresh token on every use and invalidates the old one
# immediately. The background email-poll scheduler and a user action in the
# browser run on separate threads in the same process and can both decide to
# refresh at once — this lock makes that impossible by serialising access.
_refresh_lock = threading.Lock()


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
        delete_tokens('xero')

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

    def get_expense_accounts(self) -> list[dict]:
        """Return the org's expense-class accounts (for coding bill lines):
        [{'id','code','name','type'}]. Used by the AI line-coder and the
        Settings default-account picker."""
        resp = requests.get(
            f"{API_BASE}/Accounts",
            headers=self._headers(),
            params={'where': 'Status=="ACTIVE"'}
        )
        resp.raise_for_status()
        accounts = []
        for a in resp.json().get('Accounts', []):
            # EXPENSE class covers Expenses, Overheads and Direct Costs — the
            # accounts a purchase (bill) line would sensibly be coded to.
            if a.get('Class') == 'EXPENSE' and a.get('Code'):
                accounts.append({
                    'id':   a['Code'],
                    'code': a['Code'],
                    'name': a.get('Name', ''),
                    'type': a.get('Type', ''),
                })
        return accounts

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

    def find_or_create_vendor(self, supplier_name: str,
                              invoice_data: dict = None) -> Optional[str]:
        """Match an existing Xero contact, or create one if none is found.
        Keeps a live demo (and real usage) from stalling when the AP invoice
        is from a supplier not yet in Xero — the bill can still post."""
        existing = self.find_vendor(supplier_name)
        if existing:
            return existing
        return self.create_vendor(supplier_name)

    def create_vendor(self, supplier_name: str) -> Optional[str]:
        return self._create_contact(supplier_name, is_supplier=True)

    def find_or_create_customer(self, customer_name: str,
                                extra: dict = None) -> Optional[str]:
        """AR counterpart to find_or_create_vendor. Xero contacts are unified,
        so the same fuzzy lookup applies; a new contact is flagged IsCustomer."""
        existing = self.find_vendor(customer_name)
        if existing:
            return existing
        return self._create_contact(customer_name, is_customer=True)

    def _create_contact(self, name: str, is_supplier: bool = False,
                        is_customer: bool = False) -> Optional[str]:
        contact = {'Name': name}
        if is_supplier:
            contact['IsSupplier'] = True
        if is_customer:
            contact['IsCustomer'] = True
        resp = requests.post(
            f"{API_BASE}/Contacts",
            headers=self._headers(),
            json={'Contacts': [contact]},
        )
        # A duplicate-name error means someone/something created it in a race
        # (or the fuzzy search missed an exact match) — fall back to a lookup
        # rather than failing.
        if resp.status_code == 400:
            found = self.find_vendor(name)
            if found:
                return found
            self._raise_xero_error(resp, 'contact')
        resp.raise_for_status()
        contacts = resp.json().get('Contacts', [])
        return contacts[0].get('ContactID', '') if contacts else None

    def post_invoice(self, invoice_data: dict) -> str:
        account_code = self.cfg.get('default_expense_account', '300')
        # 'DRAFT' posts the bill for review (nothing hits the ledger until a
        # human approves it in Xero) — the safe choice for demos and pilots.
        # 'AUTHORISED' posts it straight to awaiting-payment. Configurable per
        # install; defaults to AUTHORISED to preserve existing behaviour.
        post_status = self.cfg.get('post_status', 'AUTHORISED')

        if invoice_data.get('lines'):
            line_items = [{
                'Description': l.get('description', ''),
                'Quantity':    float(l.get('quantity', 1)),
                'UnitAmount':  float(l.get('unit_price', 0)),
                'LineAmount':  float(l.get('line_total', 0)),
                # Use the AI-assigned per-line nominal, falling back to the default.
                'AccountCode': l.get('account_code') or account_code,
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
            'Status':          post_status,
            'LineAmountTypes': 'Exclusive',
            # SubTotal/TotalTax/Total are deliberately omitted — with Exclusive
            # line amounts Xero computes them from the LineItems + TaxType. Sending
            # our own extracted figures risks a penny-level mismatch that Xero
            # rejects with a 400.
            'LineItems':       line_items,
        }
        # Xero (depending on org settings) requires a DueDate on bills. Use the
        # extracted one; otherwise fall back to invoice date + payment terms so
        # the post never fails on a missing due date.
        xero_inv['DueDate'] = str(self._due_date(invoice_data))

        resp = requests.post(
            f"{API_BASE}/Invoices",
            headers=self._headers(),
            json={'Invoices': [xero_inv]}
        )
        if resp.status_code >= 400:
            try:
                self._raise_xero_error(resp, 'bill')
            except ValueError as e:
                msg = str(e).lower()
                # Xero matches a POST to an existing bill by invoice number; if that
                # bill is already AUTHORISED/PAID it rejects the change with a cryptic
                # "not of valid status for modification". Make it actionable.
                if ('not of valid status for modification' in msg
                        or 'invoice number must be unique' in msg
                        or 'the document is already' in msg):
                    org = self._load_tokens().get('tenant_name', 'the connected Xero organisation')
                    num = invoice_data.get('invoice_number', '') or '(no invoice number)'
                    raise ValueError(
                        f"A bill numbered {num} already exists in {org} and can't be "
                        f"modified. Void or delete that bill in Xero, or use a unique "
                        f"invoice number, then post again."
                    ) from e
                raise
        invoices = resp.json().get('Invoices', [])
        return invoices[0].get('InvoiceID', '') if invoices else ''

    def post_sales_invoice(self, invoice_data: dict) -> str:
        """Create an ACCREC (sales) invoice — used by the Deliver-to-Invoice
        flow to raise a customer invoice from a signed delivery note."""
        sales_account = self.cfg.get('default_sales_account', '200')
        # Sales invoices default to DRAFT so a human eyeballs them before they
        # go to the customer; override with sales_post_status='AUTHORISED'.
        post_status   = self.cfg.get('sales_post_status', 'DRAFT')

        line_items = []
        for l in (invoice_data.get('lines') or []):
            qty  = float(l.get('quantity', 1) or 1)
            item = {
                'Description': l.get('description', ''),
                'Quantity':    qty,
                'AccountCode': sales_account,
                'TaxType':     'OUTPUT2',   # UK standard rated output VAT (20% on income)
            }
            # Delivery notes may carry a unit price or a line total (or neither).
            if l.get('unit_price') is not None:
                item['UnitAmount'] = float(l['unit_price'])
            elif l.get('line_total') is not None:
                item['UnitAmount'] = round(float(l['line_total']) / qty, 4) if qty else float(l['line_total'])
            else:
                item['UnitAmount'] = 0.0
            line_items.append(item)

        xero_inv = {
            'Type':            'ACCREC',
            'Contact':         {'ContactID': invoice_data['customer_ref']},
            'Date':            str(invoice_data.get('invoice_date') or datetime.now(timezone.utc).date()),
            'DueDate':         str(self._due_date(invoice_data)),
            'Reference':       invoice_data.get('reference', ''),
            'Status':          post_status,
            'LineAmountTypes': 'Exclusive',
            'LineItems':       line_items,
        }
        # Only set InvoiceNumber if provided; blank lets Xero auto-generate the
        # next sales invoice number ("Xero generates the invoice").
        if invoice_data.get('invoice_number'):
            xero_inv['InvoiceNumber'] = invoice_data['invoice_number']

        resp = requests.post(
            f"{API_BASE}/Invoices",
            headers=self._headers(),
            json={'Invoices': [xero_inv]}
        )
        if resp.status_code >= 400:
            self._raise_xero_error(resp, 'sales invoice')
        invoices = resp.json().get('Invoices', [])
        return invoices[0].get('InvoiceID', '') if invoices else ''

    def _due_date(self, invoice_data: dict):
        """Return the invoice's due date, or invoice date + payment terms when
        none was extracted (Xero can reject a bill with no DueDate)."""
        due = invoice_data.get('due_date')
        if due:
            return due
        terms_days = int(self.cfg.get('default_payment_terms_days', 30))
        base = invoice_data.get('invoice_date')
        if isinstance(base, str):
            base = date.fromisoformat(base[:10])
        elif isinstance(base, datetime):
            base = base.date()
        if not isinstance(base, date):
            base = datetime.now(timezone.utc).date()
        return base + timedelta(days=terms_days)

    @staticmethod
    def _raise_xero_error(resp, context: str):
        """Turn Xero's opaque 4xx into the actual validation message(s) so a
        failed post says *why* (bad account code, invalid tax type, …) instead
        of a bare '400 Bad Request'."""
        try:
            data = resp.json()
        except Exception:
            raise ValueError(f"Xero rejected the {context} ({resp.status_code}): {resp.text[:300]}")
        msgs = []
        for el in (data.get('Elements') or []):
            for ve in (el.get('ValidationErrors') or []):
                if ve.get('Message'):
                    msgs.append(ve['Message'])
        if not msgs and data.get('Message'):
            msgs.append(data['Message'])
        detail = '; '.join(msgs) if msgs else resp.text[:300]
        raise ValueError(f"Xero rejected the {context}: {detail}")

    # ------------------------------------------------------------------ #
    # Internal helpers
    # ------------------------------------------------------------------ #

    def _headers(self) -> dict:
        with _refresh_lock:
            # Re-read from disk while holding the lock — if another thread
            # just refreshed, this picks up its fresh token instead of
            # racing to refresh again with the now-invalid old one.
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
        return load_tokens('xero')

    def _save_tokens(self, tokens: dict):
        save_tokens('xero', tokens)

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
