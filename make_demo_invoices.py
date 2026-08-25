r"""Generate realistic, text-based demo invoice PDFs for the Invoice-IQ demo.
Drop the output into the invoice hot folder / mailbox to show ingestion,
extraction, PO matching, approval and push-to-Xero/Ledger-IQ end to end.

Run this ONCE before each demo. Every run produces a FRESH set:
  * unique invoice numbers  — a per-run batch tag (mmddHHMM) is appended, so the
    numbers have never been used in Xero before. (Xero reserves an invoice
    number permanently once used — even after the bill is voided/deleted — so
    reusing a number is rejected with "not of valid status for modification".)
  * recent invoice dates    — dated a few days ago, so bills don't post already
    overdue (Xero derives the due date from the invoice date + 30-day terms,
    not from the upload time).
The demo_invoices/ folder is cleared at the start of each run so you always
drop a clean batch.

Output: ./demo_invoices/*.pdf   Run: .\.venv-local\Scripts\python.exe make_demo_invoices.py
"""
from datetime import datetime, timedelta
from pathlib import Path

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib import colors
from reportlab.lib.enums import TA_RIGHT, TA_LEFT
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import (SimpleDocTemplate, Table, TableStyle,
                                Paragraph, Spacer)

OUT = Path(__file__).parent / "demo_invoices"
OUT.mkdir(exist_ok=True)

# Per-run batch tag makes every generated invoice number unique. Minute
# resolution is plenty — you run this once per demo.
TODAY   = datetime.now()
RUN_TAG = TODAY.strftime("%m%d%H%M")

# Who the invoices are billed TO (the demo customer / Invoice-IQ account holder)
BILL_TO = ["Sol-IQ Demo", "12 Harbour Street",
           "Inverness", "IV1 1AA"]

INK = colors.HexColor("#0F2749")     # header navy
GREY = colors.HexColor("#64748b")
LINE = colors.HexColor("#e2e8f0")
HEADBG = colors.HexColor("#0F2749")

# Each invoice: supplier, address, VAT reg, bank, base invoice no, days_ago
# (invoice dated this many days before today), PO, lines.
# The final invoice number is  "<no>-<RUN_TAG>"  so it is unique every run.
# line = (description, qty, unit_price).  subtotal = sum(qty*unit); VAT = 20%.
# Two deliberate demo talking points are built in:
#   * Skye Fresh Produce has no PO           -> "no match / missing PO" prompt.
#   * Orkney invoices 6 toner cartridges vs   -> total + line-qty mismatch, held
#     4 on PO-10025 (£438 vs £330)               for review (not auto-matched).
# Every other pair matches cleanly (header + total + line items).
INVOICES = [
    dict(supplier="Highland Paper Co",
         addr=["Unit 4, Nairn Industrial Estate", "Nairn", "IV12 5QR"],
         vat="GB 214 5567 89", bank="Sort 80-22-60  Acc 10045567",
         no="INV-2043", days_ago=8, po="PO-10021",
         lines=[("A4 white copier paper (box of 5 reams)", 40, 22.00),
                ("C4 manila envelopes (box of 250)", 20, 14.00),
                ("Lever-arch files, assorted colours", 40, 2.00)]),
    dict(supplier="Caledonian Print Services",
         addr=["17 Longman Road", "Inverness", "IV1 1RY"],
         vat="GB 331 8890 12", bank="Sort 82-11-09  Acc 20337781",
         no="INV-8830", days_ago=8, po="PO-10023",
         lines=[("Tri-fold brochures, full colour (per unit)", 5000, 0.36),
                ("Business cards, 350gsm (pack of 250)", 10, 25.00),
                ("A2 posters, full colour", 25, 10.00)]),
    dict(supplier="Ben Nevis Hardware",
         addr=["3 High Street", "Fort William", "PH33 6DH"],
         vat="GB 442 0091 34", bank="Sort 83-40-12  Acc 30559120",
         no="INV-5521", days_ago=9, po="PO-10024",
         lines=[("Cordless drill driver 18V", 6, 95.00),
                ("Mixed screws & fixings (tub)", 20, 12.00),
                ("Heavy-duty cloth tape 50m", 20, 3.00)]),
    dict(supplier="Orkney Office Supplies",
         addr=["9 Albert Street", "Kirkwall, Orkney", "KW15 1HP"],
         vat="GB 556 7712 08", bank="Sort 80-05-33  Acc 40771208",
         no="INV-3320", days_ago=10, po="PO-10025",
         # DELIBERATE DEMO MISMATCH: PO-10025 ordered 4 toner cartridges but the
         # supplier has invoiced 6. Invoice total £438 vs PO £330 (>2% tolerance)
         # -> Invoice-IQ flags a "Total mismatch" + line qty discrepancy and holds
         # it for review instead of auto-matching. Great AP-controls talking point.
         lines=[("Ballpoint pens (box of 50)", 10, 6.50),
                ("Compatible printer toner cartridge", 6, 45.00),
                ("Sticky notes, multipack", 10, 3.00)]),
    dict(supplier="Islay Logistics Ltd",
         addr=["Distillery Road", "Port Ellen, Islay", "PA42 7DU"],
         vat="GB 667 4432 55", bank="Sort 82-63-14  Acc 50664432",
         no="INV-9981", days_ago=8, po="PO-10022",
         lines=[("Pallet delivery — Highlands & Islands", 7, 58.00),
                ("Next-day courier surcharge", 5, 16.10)]),
    dict(supplier="Skye Fresh Produce",
         addr=["Bridge Road", "Portree, Isle of Skye", "IV51 9ER"],
         vat="GB 778 1123 77", bank="Sort 83-27-01  Acc 60778112",
         no="INV-4410", days_ago=9, po="",
         lines=[("Seasonal vegetable box", 15, 8.25),
                ("Fresh fruit platter", 5, 11.00)]),
    dict(supplier="Grampian IT Solutions",
         addr=["44 Union Street", "Aberdeen", "AB10 1BB"],
         vat="GB 889 5567 21", bank="Sort 80-91-22  Acc 70889556",
         no="INV-3390", days_ago=9, po="",
         lines=[("Business laptop — Core i5, 16GB RAM", 3, 850.00),
                ("Annual IT support contract", 1, 950.00)]),
    dict(supplier="Loch Ness Cleaning",
         addr=["Balmacaan Road", "Drumnadrochit", "IV63 6WJ"],
         vat="GB 990 2278 43", bank="Sort 82-40-77  Acc 80990227",
         no="INV-1120", days_ago=10, po="PO-10026",
         lines=[("Monthly office cleaning", 1, 560.00),
                ("Washroom consumables", 1, 90.00)]),
]

styles = getSampleStyleSheet()
S = lambda name, **kw: ParagraphStyle(name, parent=styles["Normal"], **kw)
sup_name = S("sup", fontName="Helvetica-Bold", fontSize=16, textColor=INK, leading=19)
small    = S("small", fontSize=8.5, textColor=GREY, leading=12)
small_r  = S("small_r", fontSize=8.5, textColor=GREY, leading=12, alignment=TA_RIGHT)
body     = S("body", fontSize=9.5, leading=13)
label_r  = S("label_r", fontSize=9, textColor=GREY, alignment=TA_RIGHT, leading=13)
val_r    = S("val_r", fontSize=9.5, fontName="Helvetica-Bold", alignment=TA_RIGHT, leading=13)
title    = S("title", fontName="Helvetica-Bold", fontSize=26, textColor=INK, alignment=TA_RIGHT, leading=30)
gbp = lambda x: f"£{x:,.2f}"


def build(inv):
    subtotal = sum(q * u for _, q, u in inv["lines"])
    vat = round(subtotal * 0.20, 2)
    total = subtotal + vat
    inv_no = f"{inv['no']}-{RUN_TAG}"              # unique per run
    d = TODAY - timedelta(days=inv["days_ago"])    # recent -> not born overdue
    due = d + timedelta(days=30)

    story = []
    # Header: supplier (left)  /  INVOICE + meta (right)
    left = [Paragraph(inv["supplier"], sup_name)] + \
           [Paragraph(l, small) for l in inv["addr"]] + \
           [Paragraph(f"VAT Reg: {inv['vat']}", small)]
    meta = [[Paragraph("Invoice No", label_r), Paragraph(inv_no, val_r)],
            [Paragraph("Date", label_r), Paragraph(d.strftime("%d %b %Y"), val_r)],
            [Paragraph("Due", label_r), Paragraph(due.strftime("%d %b %Y"), val_r)]]
    if inv["po"]:
        meta.append([Paragraph("PO Ref", label_r), Paragraph(inv["po"], val_r)])
    meta_tbl = Table(meta, colWidths=[24 * mm, 34 * mm], hAlign="RIGHT")
    meta_tbl.setStyle(TableStyle([("TOPPADDING", (0, 0), (-1, -1), 1),
                                  ("BOTTOMPADDING", (0, 0), (-1, -1), 1)]))
    right = [Paragraph("INVOICE", title), Spacer(1, 12), meta_tbl]
    head = Table([[left, right]], colWidths=[100 * mm, 70 * mm])
    head.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP")]))
    story += [head, Spacer(1, 16)]

    # Bill to
    story.append(Paragraph("BILL TO", small))
    story.append(Paragraph("<br/>".join([f"<b>{BILL_TO[0]}</b>"] + BILL_TO[1:]), body))
    story.append(Spacer(1, 16))

    # Line items
    rows = [["Description", "Qty", "Unit", "Amount"]]
    for desc, q, u in inv["lines"]:
        rows.append([Paragraph(desc, body), str(q), gbp(u), gbp(q * u)])
    rows += [["", "", Paragraph("Subtotal", label_r), Paragraph(gbp(subtotal), val_r)],
             ["", "", Paragraph("VAT 20%", label_r), Paragraph(gbp(vat), val_r)],
             ["", "", Paragraph("<b>Total Due</b>", label_r), Paragraph(f"<b>{gbp(total)}</b>", val_r)]]
    t = Table(rows, colWidths=[95 * mm, 15 * mm, 25 * mm, 30 * mm])
    n = len(inv["lines"])
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

    # Footer
    story.append(Paragraph(
        f"Payment terms: 30 days from invoice date. Please quote invoice "
        f"number {inv_no} with payment.", small))
    story.append(Paragraph(f"Bank: {inv['bank']}", small))
    story.append(Spacer(1, 6))
    story.append(Paragraph("Thank you for your business.", small))

    slug = inv["supplier"].replace(" ", "-").replace("&", "and")
    path = OUT / f"{inv_no}_{slug}.pdf"
    doc = SimpleDocTemplate(str(path), pagesize=A4,
                            leftMargin=20 * mm, rightMargin=20 * mm,
                            topMargin=18 * mm, bottomMargin=18 * mm,
                            title=f"{inv['supplier']} {inv_no}")
    doc.build(story)
    return path.name, subtotal, vat, total, inv["po"]


# Clear the previous batch so you always drop a clean set into the hot folder.
removed = 0
for old in OUT.glob("*.pdf"):
    old.unlink()
    removed += 1

print(f"Writing to {OUT}")
print(f"Batch tag: {RUN_TAG}   (invoice numbers end -{RUN_TAG})")
if removed:
    print(f"Cleared {removed} PDF(s) from the previous batch.")
print()
print(f"{'File':52} {'PO':10} {'Subtotal':>10} {'VAT':>9} {'Total':>10}")
print("-" * 94)
for inv in INVOICES:
    name, sub, vat, total, po = build(inv)
    print(f"{name:52} {po or '-':10} {sub:>10,.2f} {vat:>9,.2f} {total:>10,.2f}")
print(f"\n{len(INVOICES)} invoices written — all dated within the last ~2 weeks, "
      f"numbers suffixed -{RUN_TAG}.")
