"""Seed the local SQLite DB with realistic demo invoices/POs for viewing the UI.
Idempotent: clears invoice/PO/audit data (keeps users) then reinserts. Local dev only."""
import os
from pathlib import Path
from datetime import datetime, date, timedelta
from decimal import Decimal

os.environ.pop("DATABASE_URL", None)  # force local SQLite

from flask import Flask
from src.database import db, User, Invoice, InvoiceLine, PurchaseOrder, POLine, AuditLog

app = Flask(__name__)
app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///" + str(Path("data/invoiceiq.db").resolve())
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
db.init_app(app)

now = datetime.utcnow()
def days_ago(n, h=9, m=15):
    d = now - timedelta(days=n)
    return d.replace(hour=h, minute=m, second=0, microsecond=0)
D = lambda x: Decimal(str(x))

with app.app_context():
    # Rebuild schema from the models so it matches exactly (the pre-existing
    # tables predate later migrations). All transactional tables were empty.
    db.drop_all()
    db.create_all()

    admin = User(name="Test Admin", email="test-wizard@example.com",
                 role="admin", is_active=True)
    admin.set_password("InvoiceIQ2026")
    db.session.add(admin)
    db.session.commit()
    approver_id = admin.id

    # ---- Purchase orders ------------------------------------------------- #
    pos = {}
    po_defs = [
        ("PO-10021", "Highland Paper Co",         1240, 248),
        ("PO-10022", "Islay Logistics Ltd",        486.50, 97.30),
        ("PO-10023", "Caledonian Print Services",  2300, 460),
        ("PO-10024", "Ben Nevis Hardware",          870, 174),
        ("PO-10025", "Orkney Office Supplies",      275, 55),
    ]
    for num, supp, sub, vat in po_defs:
        po = PurchaseOrder(
            po_number=num, supplier_name=supp, po_date=days_ago(20).date(),
            expected_delivery=days_ago(6).date(), subtotal=D(sub), vat_amount=D(vat),
            total_amount=D(sub) + D(vat), currency="GBP", status="open", source="sage",
            last_synced=days_ago(1),
        )
        po.lines.append(POLine(line_number=1, description=f"{supp} — goods/services",
                               quantity=D(1), unit_price=D(sub), line_total=D(sub),
                               product_code="GEN-001"))
        db.session.add(po); pos[num] = po
    db.session.flush()

    # ---- Invoices -------------------------------------------------------- #
    # (supplier, inv#, po_num|None, subtotal, vat, status, recv_datetime, kwargs)
    rows = [
        ("Highland Paper Co",        "INV-2043", "PO-10021", 1240, 248, "matched", days_ago(0),
            dict(extraction_confidence=0.99, match_confidence=0.98)),
        ("Caledonian Print Services","INV-8830", "PO-10023", 2300, 460, "matched", days_ago(0),
            dict(extraction_confidence=0.97, match_confidence=0.96)),
        ("Ben Nevis Hardware",       "INV-5521", "PO-10024",  870, 174, "matched", days_ago(1),
            dict(extraction_confidence=0.98, match_confidence=0.99)),
        ("Orkney Office Supplies",   "INV-3320", "PO-10025",  275,  55, "matched", days_ago(2),
            dict(extraction_confidence=0.95, match_confidence=0.94)),
        ("Islay Logistics Ltd",      "INV-9981", "PO-10022",  502,  99.80, "partial_match", days_ago(0),
            dict(extraction_confidence=0.9, match_confidence=0.72,
                 match_discrepancies='["Line total £601.80 exceeds PO £583.80 by £18.00", "Delivery date differs"]')),
        ("Skye Fresh Produce",       "INV-4410", None,        178.75, 35.75, "partial_match", days_ago(1),
            dict(extraction_confidence=0.83, match_confidence=0.55,
                 match_discrepancies='["No matching PO reference found on invoice"]')),
        ("North Coast Supplies",     "INV-7014", None,         76.67, 15.33, "no_match", days_ago(1),
            dict(extraction_confidence=0.88)),
        ("Tay Valley Fuels",         "INV-6642", None,       1508.33, 301.67, "exception", days_ago(3),
            dict(extraction_confidence=0.92, push_failed=True,
                 status_message="Sage push failed: connection timeout (will retry)")),
        ("Grampian IT Solutions",    "INV-3390", None,       3500, 700, "awaiting_approval", days_ago(1),
            dict(extraction_confidence=0.94, requires_approval=True, assigned_approver_id=approver_id)),
        ("Loch Ness Cleaning",       "INV-1120", None,        650, 130, "awaiting_approval", days_ago(2),
            dict(extraction_confidence=0.91, requires_approval=True, assigned_approver_id=approver_id)),
        ("Highland Paper Co",        "INV-2101", None,       1350, 270, "awaiting_approval", days_ago(0),
            dict(extraction_confidence=0.96, requires_approval=True, assigned_approver_id=approver_id)),
        ("Islay Logistics Ltd",      "INV-9820", None,        450,  90, "approved", days_ago(4),
            dict(extraction_confidence=0.95, requires_approval=True, assigned_approver_id=approver_id,
                 approved_at=days_ago(3), approved_by_id=approver_id)),
        ("Caledonian Print Services","INV-8790", None,       1650, 330, "posted", days_ago(5),
            dict(extraction_confidence=0.97, sage_transaction_ref="SG-778201", posted_to_sage_at=days_ago(4))),
        ("Ben Nevis Hardware",       "INV-5480", None,        305,  61, "posted", days_ago(6),
            dict(extraction_confidence=0.98, sage_transaction_ref="SG-778190", posted_to_sage_at=days_ago(5))),
        ("Orkney Office Supplies",   "INV-3299", None,        275,   0, "ready_to_pay", days_ago(4),
            dict(extraction_confidence=0.93)),
        ("Thistle Trading Ltd",      "INV-0001", None,       8332.50, 1666.50, "rejected", days_ago(3),
            dict(extraction_confidence=0.7, rejection_reason="Duplicate — already paid under INV-9820")),
        ("Grampian IT Solutions",    "INV-3401", None,       1075, 215, "received", days_ago(0),
            dict()),
        ("Skye Fresh Produce",       "INV-4433", None,        275,  55, "extracting", days_ago(0),
            dict(extraction_confidence=0.6)),
        ("Tay Valley Fuels",         "INV-6700", None,        783.33, 156.67, "matching", days_ago(1),
            dict(extraction_confidence=0.9)),
    ]

    made = []
    for supp, invno, po_num, sub, vat, status, recv, extra in rows:
        inv = Invoice(
            supplier_name=supp, invoice_number=invno,
            invoice_date=(recv - timedelta(days=2)).date(),
            po_reference=po_num, subtotal=D(sub), vat_amount=D(vat),
            total_amount=D(sub) + D(vat), currency="GBP",
            status=status, email_from=f"accounts@{supp.split()[0].lower()}.co.uk",
            email_subject=f"Invoice {invno} from {supp}",
            attachment_filename=f"{invno}.pdf",
            email_received_at=recv, created_at=recv, updated_at=recv,
            **extra,
        )
        if po_num:
            inv.matched_po_id = pos[po_num].id
        inv.lines.append(InvoiceLine(line_number=1, description=f"{supp} — goods/services",
                                     quantity=D(1), unit_price=D(sub), line_total=D(sub),
                                     vat_rate=D(20), product_code="GEN-001",
                                     matched=(status == "matched")))
        db.session.add(inv); made.append((inv, status, recv))
    db.session.flush()

    # ---- Audit log entries ---------------------------------------------- #
    who = admin.name if admin else "System"
    for inv, status, recv in made:
        db.session.add(AuditLog(invoice_id=inv.id, action="received", user_name="Email poller",
                                timestamp=recv, notes=f"Received {inv.attachment_filename}"))
        if status in ("approved", "posted"):
            db.session.add(AuditLog(invoice_id=inv.id, action="approved", user_name=who,
                                    timestamp=recv + timedelta(days=1), notes="Approved for payment"))
        if status == "posted":
            db.session.add(AuditLog(invoice_id=inv.id, action="posted", user_name=who,
                                    timestamp=recv + timedelta(days=1, hours=2),
                                    notes=f"Posted to Sage ({inv.sage_transaction_ref})"))
        if status == "rejected":
            db.session.add(AuditLog(invoice_id=inv.id, action="rejected", user_name=who,
                                    timestamp=recv + timedelta(hours=5), notes=inv.rejection_reason))

    db.session.commit()

    # summary
    from collections import Counter
    cc = Counter(s for _, s, _ in made)
    print("Seeded:", sum(cc.values()), "invoices,", len(pos), "POs")
    for s, n in sorted(cc.items()):
        print(f"  {s:18} {n}")
    today = now.date()
    rt = sum(1 for _, _, r in made if r.date() == today)
    print("received today:", rt)
