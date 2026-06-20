"""
Sage 50 UK connector — uses the SageDataObject (SDO) COM API via pywin32.
Sage 50 must be installed on the same Windows machine.
"""
from datetime import datetime, date
from difflib import SequenceMatcher
from typing import Optional

from src.connectors.base import BaseConnector
from src.config_manager import load_settings


class SageConnector(BaseConnector):

    @property
    def system_name(self) -> str:
        return 'Sage 50'

    @property
    def system_key(self) -> str:
        return 'sage'

    def __init__(self, settings: dict = None):
        self.settings = settings or load_settings()
        self._engine = None
        self._workspace = None

    # ------------------------------------------------------------------ #
    # Connection management
    # ------------------------------------------------------------------ #

    def _connect(self):
        try:
            import win32com.client
        except ImportError:
            raise RuntimeError(
                "pywin32 is required for Sage 50. Run: pip install pywin32"
            )
        s = self.settings['sage']
        prog_id = f"SageDataObject{s.get('sdo_version', '300')}.SDOEngine"
        self._engine = win32com.client.Dispatch(prog_id)
        self._workspace = self._engine.Workspaces.Add("InvoiceIQ")
        self._workspace.Connect(
            s['data_path'], s['username'], s['password'], "InvoiceIQ"
        )

    def _disconnect(self):
        if self._workspace:
            try:
                self._workspace.Disconnect()
            except Exception:
                pass
        self._engine = None
        self._workspace = None

    def test_connection(self) -> tuple[bool, str]:
        try:
            self._connect()
            self._disconnect()
            return True, "Sage 50 connection successful"
        except Exception as e:
            self._disconnect()
            return False, str(e)

    # ------------------------------------------------------------------ #
    # Purchase Orders
    # ------------------------------------------------------------------ #

    def get_purchase_orders(self) -> list[dict]:
        self._connect()
        try:
            pos = []
            po_ds = self._workspace.CreateDataset("PurchaseOrder")
            po_ds.MoveFirst()

            while not po_ds.EOF:
                po_number = str(po_ds.Fields('ORDER_NUMBER').Value or '').strip()
                if not po_number:
                    po_ds.MoveNext()
                    continue

                po = {
                    'po_number':        po_number,
                    'supplier_ref':     str(po_ds.Fields('ACCOUNT_REF').Value or ''),
                    'supplier_name':    str(po_ds.Fields('NAME').Value or ''),
                    'po_date':          self._to_date(po_ds.Fields('ORDER_DATE').Value),
                    'expected_delivery':self._to_date(po_ds.Fields('DELIVERY_DATE').Value),
                    'total_amount':     self._to_float(po_ds.Fields('GROSS_AMOUNT').Value),
                    'vat_amount':       self._to_float(po_ds.Fields('TAX_AMOUNT').Value),
                    'subtotal':         self._to_float(po_ds.Fields('NET_AMOUNT').Value),
                    'status':           str(po_ds.Fields('RECORD_STATUS').Value or ''),
                    'currency':         'GBP',
                    'source':           'sage',
                    'lines':            [],
                }

                line_ds = self._workspace.CreateDataset("PurchaseOrderItem")
                line_ds.Filter = f"ORDER_NUMBER = '{po_number}'"
                line_ds.MoveFirst()
                n = 1
                while not line_ds.EOF:
                    po['lines'].append({
                        'line_number':      n,
                        'description':      str(line_ds.Fields('DESCRIPTION').Value or ''),
                        'product_code':     str(line_ds.Fields('STOCK_CODE').Value or ''),
                        'quantity':         self._to_float(line_ds.Fields('QTY_ORDER').Value),
                        'unit_price':       self._to_float(line_ds.Fields('UNIT_PRICE').Value),
                        'line_total':       self._to_float(line_ds.Fields('NET_AMOUNT').Value),
                        'quantity_invoiced':self._to_float(line_ds.Fields('QTY_INVOICE').Value),
                    })
                    n += 1
                    line_ds.MoveNext()

                pos.append(po)
                po_ds.MoveNext()

            return pos
        finally:
            self._disconnect()

    # ------------------------------------------------------------------ #
    # Vendor lookup
    # ------------------------------------------------------------------ #

    def find_vendor(self, supplier_name: str) -> Optional[str]:
        self._connect()
        try:
            ds = self._workspace.CreateDataset("Supplier")
            ds.MoveFirst()
            best_ref, best_score = None, 0.0
            name_lower = supplier_name.lower()

            while not ds.EOF:
                sage_name = str(ds.Fields('NAME').Value or '').lower()
                score = SequenceMatcher(None, name_lower, sage_name).ratio()
                if score > best_score:
                    best_score = score
                    best_ref = str(ds.Fields('ACCOUNT_REF').Value)
                ds.MoveNext()

            return best_ref if best_score >= 0.70 else None
        finally:
            self._disconnect()

    # ------------------------------------------------------------------ #
    # Invoice posting
    # ------------------------------------------------------------------ #

    def post_invoice(self, invoice_data: dict) -> str:
        self._connect()
        try:
            ds = self._workspace.CreateDataset("Invoice")
            ds.AddNew()
            ds.Fields('ACCOUNT_REF').Value  = invoice_data['supplier_ref']
            ds.Fields('INV_REF').Value      = invoice_data.get('invoice_number', '')
            ds.Fields('DATE').Value         = invoice_data['invoice_date']
            ds.Fields('DUE_DATE').Value     = invoice_data.get('due_date', invoice_data['invoice_date'])
            ds.Fields('NET_AMOUNT').Value   = float(invoice_data['subtotal'])
            ds.Fields('TAX_AMOUNT').Value   = float(invoice_data['vat_amount'])
            ds.Fields('GROSS_AMOUNT').Value = float(invoice_data['total_amount'])
            if invoice_data.get('po_reference'):
                ds.Fields('ORDER_NUMBER').Value = invoice_data['po_reference']
            ds.Post()
            return str(ds.Fields('TRAN_NUMBER').Value)
        finally:
            self._disconnect()

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    @staticmethod
    def _to_float(value) -> float:
        try:
            return float(value or 0)
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _to_date(value):
        if not value:
            return None
        if isinstance(value, datetime):
            return value.date().isoformat()
        if isinstance(value, date):
            return value.isoformat()
        return None
