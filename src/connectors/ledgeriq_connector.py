"""
Ledger-IQ connector — treats a Ledger-IQ organisation as the finance backend.

Unlike Sage/QBO/Xero this talks to Ledger-IQ's own REST API (/api/v1) using the
organisation's API key (Authorization: Bearer <key>). It lets Invoice-IQ:
  • pull open purchase orders for 3-way matching   (GET /api/v1/purchase-orders)
  • look a supplier up by name                      (GET /api/v1/contacts)
  • post an approved invoice as a purchase bill     (POST /api/v1/purchase-invoices)

Config lives in settings['ledgeriq'] = {api_base_url, api_key}.
"""
import requests
from typing import Optional
from difflib import SequenceMatcher

from src.connectors.base import BaseConnector

# Ledger-IQ purchase orders are stored NET (no VAT on the PO). Invoice-IQ's
# matcher compares the GROSS invoice total, so we gross PO lines up at the
# standard UK VAT rate for matching. (A per-line VAT rate on POs would remove
# this assumption; fine for standard-rated supplies.)
VAT_RATE = 0.20
_TIMEOUT = 15


class LedgerIQConnector(BaseConnector):

    def __init__(self, settings: dict):
        cfg = (settings or {}).get('ledgeriq', {})
        self.base_url = (cfg.get('api_base_url') or '').rstrip('/')
        self.api_key = cfg.get('api_key') or ''

    @property
    def system_name(self) -> str:
        return 'Ledger-IQ'

    # ------------------------------------------------------------------ #
    # HTTP helpers
    # ------------------------------------------------------------------ #

    def _headers(self) -> dict:
        return {'Authorization': f'Bearer {self.api_key}', 'Accept': 'application/json'}

    def _require_config(self):
        if not self.base_url or not self.api_key:
            raise ValueError('Ledger-IQ base URL or API key is not configured (Settings → Finance System)')

    def _get(self, path: str, params: dict = None):
        self._require_config()
        r = requests.get(f'{self.base_url}{path}', headers=self._headers(),
                         params=params, timeout=_TIMEOUT)
        r.raise_for_status()
        return r.json()

    # ------------------------------------------------------------------ #
    # BaseConnector interface
    # ------------------------------------------------------------------ #

    def test_connection(self) -> tuple[bool, str]:
        try:
            self._get('/api/v1/purchase-orders', params={'status': 'OPEN'})
            return True, 'Connected to Ledger-IQ successfully.'
        except requests.HTTPError as e:
            code = e.response.status_code if e.response is not None else 0
            if code == 401:
                return False, 'Ledger-IQ rejected the API key — check it in Settings.'
            return False, f'Ledger-IQ returned HTTP {code}.'
        except ValueError as e:
            return False, str(e)
        except Exception as e:
            return False, f'Could not reach Ledger-IQ: {e}'

    def get_purchase_orders(self) -> list[dict]:
        data = self._get('/api/v1/purchase-orders', params={'status': 'OPEN'})
        out = []
        for po in data.get('purchaseOrders', []):
            net = float(po.get('totalNet') or 0)
            vat = round(net * VAT_RATE, 2)
            supplier = po.get('supplier') or {}
            lines = []
            for i, l in enumerate(po.get('lines', []), 1):
                lines.append({
                    'line_number':       i,
                    'description':       l.get('description', ''),
                    'product_code':      '',
                    'quantity':          float(l.get('quantity') or 0),
                    'unit_price':        float(l.get('unitPrice') or 0),
                    'line_total':        float(l.get('netAmount') or 0),
                    'quantity_invoiced': 0,
                })
            out.append({
                'po_number':         po.get('poNumber'),
                'supplier_name':     supplier.get('name'),
                'supplier_ref':      supplier.get('id'),
                'po_date':           po.get('date'),
                'expected_delivery': po.get('expectedAt'),
                'subtotal':          net,
                'vat_amount':        vat,
                'total_amount':      round(net + vat, 2),
                'currency':          'GBP',
                'status':            (po.get('status') or 'open').lower(),
                'source':            'ledgeriq',
                'lines':             lines,
            })
        return out

    def find_vendor(self, supplier_name: str) -> Optional[str]:
        if not supplier_name:
            return None
        try:
            data = self._get('/api/v1/contacts',
                             params={'name': supplier_name, 'type': 'SUPPLIER'})
        except (requests.HTTPError, ValueError):
            return None
        contacts = data.get('contacts', [])
        best, best_score = None, 0.0
        for c in contacts:
            name = c.get('name') or ''
            if name.lower() == supplier_name.lower():          # exact wins
                return c.get('externalRef') or c.get('id')
            score = SequenceMatcher(None, name.lower(), supplier_name.lower()).ratio()
            if score > best_score:
                best_score, best = score, c
        if best and best_score >= 0.85:                        # confident fuzzy
            return best.get('externalRef') or best.get('id')
        return None

    def post_invoice(self, invoice_data: dict) -> str:
        self._require_config()
        lines = invoice_data.get('lines') or []
        inv_date = invoice_data.get('invoice_date')
        inv_date = inv_date.isoformat() if hasattr(inv_date, 'isoformat') else (inv_date or '')
        payload = {
            'externalId':    str(invoice_data.get('external_id')
                                 or invoice_data.get('invoice_number') or ''),
            'supplierName':  invoice_data.get('supplier_name') or 'Unknown supplier',
            'supplierRef':   invoice_data.get('supplier_ref') or None,
            'invoiceNumber': invoice_data.get('invoice_number') or '',
            'invoiceDate':   inv_date,
            'poReference':   invoice_data.get('po_reference') or None,
            'currency':      invoice_data.get('currency') or 'GBP',
            'lines': [{
                'description': l.get('description') or '(no description)',
                'quantity':    float(l.get('quantity') or 1),
                'unitPrice':   float(l.get('unit_price') or 0),
                'lineTotal':   float(l.get('line_total') or 0),
                'vatRate':     float(l.get('vat_rate') or 0),
            } for l in lines] or [{
                'description': invoice_data.get('invoice_number') or 'Invoice',
                'quantity':    1,
                'unitPrice':   float(invoice_data.get('subtotal') or 0),
                'lineTotal':   float(invoice_data.get('subtotal') or 0),
                'vatRate':     0,
            }],
        }
        r = requests.post(f'{self.base_url}/api/v1/purchase-invoices',
                          headers={**self._headers(), 'Content-Type': 'application/json'},
                          json=payload, timeout=_TIMEOUT)
        if not r.ok:
            raise RuntimeError(f"{r.status_code} {r.reason}: {r.text[:400]}")
        res = r.json()
        return str(res.get('invoiceId') or res.get('id') or '')
