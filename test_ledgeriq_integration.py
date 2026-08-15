"""End-to-end check of the Ledger-IQ finance connector against the demo org.
Configures Invoice-IQ for Ledger-IQ, syncs POs, matches 3 representative
invoices, resolves vendors, and posts a matched one back to Ledger-IQ."""
import os
os.environ.pop("DATABASE_URL", None)
from pathlib import Path
from datetime import date
from decimal import Decimal as D

from datetime import datetime
from flask import Flask
from src.database import db, Invoice, InvoiceLine, PurchaseOrder, POLine
from src.config_manager import load_settings, save_settings
from src.connectors.factory import get_connector, get_system_name
from src.po_matcher import POMatcher


def sync_pos_from_connector():
    """Inlined from invoice_processor to avoid importing email/PDF modules."""
    pos = get_connector().get_purchase_orders()
    for pd in pos:
        po = PurchaseOrder.query.filter_by(po_number=pd["po_number"]).first()
        if not po:
            po = PurchaseOrder(po_number=pd["po_number"]); db.session.add(po)
        po.supplier_name = pd.get("supplier_name"); po.supplier_ref = pd.get("supplier_ref")
        po.subtotal = pd.get("subtotal", 0); po.vat_amount = pd.get("vat_amount", 0)
        po.total_amount = pd.get("total_amount", 0); po.currency = pd.get("currency", "GBP")
        po.status = pd.get("status", "open"); po.source = "ledgeriq"; po.last_synced = datetime.utcnow()
        db.session.flush()
        POLine.query.filter_by(po_id=po.id).delete()
        for ld in pd.get("lines", []):
            db.session.add(POLine(po_id=po.id, line_number=ld["line_number"], description=ld["description"],
                                  quantity=ld["quantity"], unit_price=ld["unit_price"], line_total=ld["line_total"]))
    db.session.commit()
    return len(pos)


def _resolve_vendor_ref(inv):
    ref = get_connector().find_vendor(inv.supplier_name)
    if ref:
        inv.supplier_ref = ref
    else:
        note = f"Supplier '{inv.supplier_name}' not found in {get_system_name()}"
        inv.status_message = f"{inv.status_message} · {note}" if inv.status_message else note
    db.session.commit()

LEDGER_URL = "http://localhost:3002"
API_KEY = "lgr_demo_03fa4f24e34722eb4d97a3102f2fa7b5e509a5ad"

app = Flask(__name__)
app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///" + str(Path("data/invoiceiq.db").resolve())
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
db.init_app(app)

# Point Invoice-IQ at the local Ledger-IQ demo org.
s = load_settings()
s["finance_system"] = "ledgeriq"
s["ledgeriq"] = {"api_base_url": LEDGER_URL, "api_key": API_KEY}
s.setdefault("po_source", {})["enabled"] = True
s["po_source"]["type"] = "connector"
save_settings(s)

# (supplier, inv#, po_ref, subtotal, vat, total, lines[(desc,qty,unit,linetotal)])
CASES = [
    ("Highland Paper Co", "TEST-2043", "PO-10021", 1240, 248, 1488.00,
     [("A4 white copier paper (box of 5 reams)", 40, 22.00, 880.00),
      ("C4 manila envelopes (box of 250)", 20, 14.00, 280.00),
      ("Lever-arch files, assorted colours", 40, 2.00, 80.00)]),
    ("Islay Logistics Ltd", "TEST-9981", "PO-10022", 486.50, 97.30, 583.80,
     [("Pallet delivery — Highlands & Islands", 7, 58.00, 406.00),
      ("Next-day courier surcharge", 5, 16.10, 80.50)]),
    ("Skye Fresh Produce", "TEST-4410", None, 178.75, 35.75, 214.50,
     [("Seasonal vegetable box", 15, 8.25, 123.75),
      ("Fresh fruit platter", 5, 11.00, 55.00)]),
]

with app.app_context():
    conn = get_connector()
    print("finance system:", conn.system_name)
    print("test_connection:", conn.test_connection())

    n = sync_pos_from_connector()
    print(f"\nsynced {n} POs from Ledger-IQ:")
    for po in PurchaseOrder.query.filter(PurchaseOrder.po_number.like("PO-10%")).all():
        print(f"  {po.po_number}  {po.supplier_name:26} total £{float(po.total_amount):.2f}")

    matcher = POMatcher()
    # clean previous test rows
    for inv in Invoice.query.filter(Invoice.invoice_number.like("TEST-%")).all():
        db.session.delete(inv)
    db.session.commit()

    print("\n--- matching + vendor resolve ---")
    made = []
    for supp, invno, po_ref, sub, vat, total, lines in CASES:
        inv = Invoice(supplier_name=supp, invoice_number=invno, po_reference=po_ref,
                      invoice_date=date(2026, 7, 6), subtotal=D(str(sub)), vat_amount=D(str(vat)),
                      total_amount=D(str(total)), currency="GBP", status="matching")
        db.session.add(inv); db.session.flush()
        for i, (desc, qty, unit, lt) in enumerate(lines, 1):
            db.session.add(InvoiceLine(invoice_id=inv.id, line_number=i, description=desc,
                                       quantity=D(str(qty)), unit_price=D(str(unit)),
                                       line_total=D(str(lt)), vat_rate=D("20")))
        db.session.commit()

        result = matcher.find_and_match(inv)
        if result.po:
            inv.status = "matched" if result.matched else "partial_match"
            inv.match_confidence = result.confidence
            if not result.matched:
                inv.status_message = "; ".join(result.discrepancies[:3])
        else:
            inv.status = "no_match"
            inv.status_message = "No matching purchase order found"
        db.session.commit()
        _resolve_vendor_ref(inv)
        made.append(inv)
        print(f"\n{supp} ({invno}):")
        print(f"  status       : {inv.status}")
        print(f"  matched PO   : {result.po.po_number if result.po else '—'} (conf {inv.match_confidence})")
        print(f"  supplier_ref : {inv.supplier_ref or '(not found)'}")
        if inv.status_message:
            print(f"  message      : {inv.status_message}")

    # Post the cleanly-matched Highland invoice back to Ledger-IQ.
    highland = made[0]
    print("\n--- posting matched invoice to Ledger-IQ ---")
    ref = conn.post_invoice({
        "external_id": "itest-highland-2043", "supplier_name": highland.supplier_name,
        "supplier_ref": highland.supplier_ref, "invoice_number": highland.invoice_number,
        "invoice_date": highland.invoice_date, "po_reference": highland.po_reference,
        "currency": "GBP",
        "lines": [{"description": l.description, "quantity": float(l.quantity),
                   "unit_price": float(l.unit_price), "line_total": float(l.line_total),
                   "vat_rate": float(l.vat_rate)} for l in highland.lines],
    })
    print(f"  posted -> Ledger-IQ invoice id: {ref}")

    # cleanup test invoices
    for inv in made:
        db.session.delete(inv)
    db.session.commit()
    print("\n(cleaned up test invoices)")
