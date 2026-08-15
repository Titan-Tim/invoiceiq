"""Generate demo PURCHASE ORDER PDFs for the folder PO-source.

Six POs are produced. They match six of the eight demo invoices so those
invoices sail through PO matching; the other two demo invoices (Skye Fresh
Produce, Grampian IT Solutions) have no PO here and flag as 'no match'.

Matching notes (see src/po_matcher.py):
- PO-10021..10025 carry the same PO number the invoices quote -> exact match.
- PO-10026 (Loch Ness) has no PO number on its invoice, so it matches on
  supplier name + total amount (within 2%).
- Totals are grossed to include 20% VAT so the PO total equals the invoice total.
- The vendor is the prominent 'supplier' so the AI extractor reads it correctly.

Run:  python make_demo_purchase_orders.py   (writes PDFs to demo_purchase_orders/)
"""
from datetime import datetime
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm
from reportlab.lib.enums import TA_RIGHT
from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer, Table,
                                TableStyle)

OUT = Path(__file__).parent / "demo_purchase_orders"
OUT.mkdir(exist_ok=True)

# The buyer issuing the POs
BUYER = ["Sol-IQ Demo", "12 Harbour Street", "Inverness", "IV1 1AA"]

INK    = colors.HexColor("#0F2749")
GREY   = colors.HexColor("#64748b")
LINE   = colors.HexColor("#e2e8f0")
HEADBG = colors.HexColor("#0F2749")

# supplier(vendor), addr, PO no, date, lines[(desc, qty, unit)]  — lines mirror the invoices
POS = [
    dict(supplier="Highland Paper Co", addr=["Unit 4, Nairn Industrial Estate", "Nairn", "IV12 5QR"],
         no="PO-10021", date="2026-06-24",
         lines=[("A4 white copier paper (box of 5 reams)", 40, 22.00),
                ("C4 manila envelopes (box of 250)", 20, 14.00),
                ("Lever-arch files, assorted colours", 40, 2.00)]),
    dict(supplier="Islay Logistics Ltd", addr=["Distillery Road", "Port Ellen, Islay", "PA42 7DU"],
         no="PO-10022", date="2026-06-23",
         lines=[("Pallet delivery — Highlands & Islands", 7, 58.00),
                ("Next-day courier surcharge", 5, 16.10)]),
    dict(supplier="Caledonian Print Services", addr=["17 Longman Road", "Inverness", "IV1 1RY"],
         no="PO-10023", date="2026-06-24",
         lines=[("Tri-fold brochures, full colour (per unit)", 5000, 0.36),
                ("Business cards, 350gsm (pack of 250)", 10, 25.00),
                ("A2 posters, full colour", 25, 10.00)]),
    dict(supplier="Ben Nevis Hardware", addr=["3 High Street", "Fort William", "PH33 6DH"],
         no="PO-10024", date="2026-06-22",
         lines=[("Cordless drill driver 18V", 6, 95.00),
                ("Mixed screws & fixings (tub)", 20, 12.00),
                ("Heavy-duty cloth tape 50m", 20, 3.00)]),
    dict(supplier="Orkney Office Supplies", addr=["9 Albert Street", "Kirkwall, Orkney", "KW15 1HP"],
         no="PO-10025", date="2026-06-21",
         lines=[("Ballpoint pens (box of 50)", 10, 6.50),
                ("Compatible printer toner cartridge", 4, 45.00),
                ("Sticky notes, multipack", 10, 3.00)]),
    dict(supplier="Loch Ness Cleaning", addr=["Balmacaan Road", "Drumnadrochit", "IV63 6WJ"],
         no="PO-10026", date="2026-06-20",
         lines=[("Monthly office cleaning — July 2026", 1, 560.00),
                ("Washroom consumables", 1, 90.00)]),
]

styles = getSampleStyleSheet()
S = lambda name, **kw: ParagraphStyle(name, parent=styles["Normal"], **kw)
sup_name = S("sup", fontName="Helvetica-Bold", fontSize=16, textColor=INK, leading=19)
small    = S("small", fontSize=8.5, textColor=GREY, leading=12)
body     = S("body", fontSize=9.5, leading=13)
label_r  = S("label_r", fontSize=9, textColor=GREY, alignment=TA_RIGHT, leading=13)
val_r    = S("val_r", fontSize=9.5, fontName="Helvetica-Bold", alignment=TA_RIGHT, leading=13)
title    = S("title", fontName="Helvetica-Bold", fontSize=22, textColor=INK, alignment=TA_RIGHT, leading=26)

gbp = lambda x: f"£{x:,.2f}"


def build(po):
    subtotal = sum(q * u for _, q, u in po["lines"])
    vat = round(subtotal * 0.20, 2)
    total = subtotal + vat
    d = datetime.strptime(po["date"], "%Y-%m-%d")

    story = []
    # Vendor (supplier) prominent left; PURCHASE ORDER + meta right
    left = [Paragraph(po["supplier"], sup_name)] + [Paragraph(l, small) for l in po["addr"]]
    meta = [[Paragraph("PO Number", label_r), Paragraph(po["no"], val_r)],
            [Paragraph("Date", label_r), Paragraph(d.strftime("%d %b %Y"), val_r)]]
    meta_tbl = Table(meta, colWidths=[26 * mm, 32 * mm], hAlign="RIGHT")
    meta_tbl.setStyle(TableStyle([("TOPPADDING", (0, 0), (-1, -1), 1), ("BOTTOMPADDING", (0, 0), (-1, -1), 1)]))
    right = [Paragraph("PURCHASE ORDER", title), Spacer(1, 12), meta_tbl]
    head = Table([[left, right]], colWidths=[100 * mm, 70 * mm])
    head.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP")]))
    story += [head, Spacer(1, 16)]

    story.append(Paragraph("ORDERED BY", small))
    story.append(Paragraph("<br/>".join([f"<b>{BUYER[0]}</b>"] + BUYER[1:]), body))
    story.append(Spacer(1, 16))

    rows = [["Description", "Qty", "Unit", "Amount"]]
    for desc, q, u in po["lines"]:
        rows.append([Paragraph(desc, body), str(q), gbp(u), gbp(q * u)])
    rows += [["", "", Paragraph("Subtotal", label_r), Paragraph(gbp(subtotal), val_r)],
             ["", "", Paragraph("VAT 20%", label_r), Paragraph(gbp(vat), val_r)],
             ["", "", Paragraph("<b>Order Total</b>", label_r), Paragraph(f"<b>{gbp(total)}</b>", val_r)]]
    t = Table(rows, colWidths=[95 * mm, 15 * mm, 25 * mm, 30 * mm])
    n = len(po["lines"])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), HEADBG),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, 0), 9),
        ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("LINEBELOW", (0, 1), (-1, n), 0.4, LINE),
        ("LINEABOVE", (2, n + 1), (-1, n + 1), 0.6, GREY),
        ("LINEABOVE", (2, n + 3), (-1, n + 3), 1.0, INK),
    ]))
    story += [t, Spacer(1, 24)]
    story.append(Paragraph(
        f"Please supply the goods/services above and invoice quoting purchase order "
        f"number <b>{po['no']}</b>.", small))

    slug = po["supplier"].replace(" ", "-").replace("&", "and")
    path = OUT / f"{po['no']}_{slug}.pdf"
    doc = SimpleDocTemplate(str(path), pagesize=A4, leftMargin=20 * mm, rightMargin=20 * mm,
                            topMargin=18 * mm, bottomMargin=18 * mm, title=f"Purchase Order {po['no']}")
    doc.build(story)
    return path.name, total


if __name__ == "__main__":
    for po in POS:
        name, total = build(po)
        print(f"Wrote {name}  (total {gbp(total)})")
    print(f"\nDone — {len(POS)} purchase orders in {OUT}")
    print("Matches: PO-10021 Highland, PO-10022 Islay, PO-10023 Caledonian, "
          "PO-10024 Ben Nevis, PO-10025 Orkney, PO-10026 Loch Ness.")
    print("No PO (will flag): Skye Fresh Produce (INV-4410), Grampian IT Solutions (INV-3390).")
