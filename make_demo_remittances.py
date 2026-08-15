"""Generate demo customer remittance-advice PDFs for the accounts-receivable
demo. Each references sales invoices seeded in Ledger-IQ (Demo Co) so ingesting
them into Invoice-IQ marks those invoices paid.

Beats: Harbour/Moray/Aberdeen = clean full matches; Cairngorm references an
invoice (1183) that isn't in Ledger (-> "not found"); Deeside pays part of an
invoice (-> partially paid).

Output: ./demo_remittances/*.pdf
Run: .venv-local\\Scripts\\python.exe make_demo_remittances.py
"""
from pathlib import Path
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas

OUT = Path(__file__).parent / "demo_remittances"
OUT.mkdir(exist_ok=True)

W, H = A4
# Who the remittances are addressed TO (us — the seller)
US = ["Sol-IQ Demo", "12 Harbour Street", "Inverness", "IV1 1AA"]

# (payer, address[], date, cheque_ref, [(inv_date, ref, details, amount)])
REMITTANCES = [
    ("Harbour Healthcare (North) Ltd",
     ["The Lodge House", "Dodge Hill", "Stockport", "Cheshire", "SK4 1RD"],
     "13/03/2026", "BP MM LIST",
     [("25/02/2026", "1161", "VC/TONER", 386.34),
      ("25/02/2026", "1162", "HL/ TONER", 386.34),
      ("25/02/2026", "1163", "TLA/PRINTER REPAIR", 372.00)]),
    ("Moray Firth Hotels Ltd",
     ["Shore Street", "Nairn", "IV12 4EA"],
     "15/03/2026", "BACS 480021",
     [("25/02/2026", "1170", "Monthly service plan", 540.00),
      ("25/02/2026", "1171", "MFP lease - Q1", 1260.00)]),
    ("Aberdeen Marine Services Ltd",
     ["Harbour Road", "Aberdeen", "AB11 5DQ"],
     "16/03/2026", "BACS 771904",
     [("25/02/2026", "1195", "Printer fleet service", 840.00),
      ("25/02/2026", "1196", "Toner supply", 420.00)]),
    ("Cairngorm Outdoor Co",
     ["2 Grampian Road", "Aviemore", "PH22 1RH"],
     "14/03/2026", "BACS 559120",
     [("25/02/2026", "1182", "Toner cartridges", 228.00),
      ("25/02/2026", "1183", "Repair parts", 456.00),   # not in Ledger -> not found
      ("25/02/2026", "1184", "Callout / repair", 95.00)]),
    ("Deeside Farm Shop",
     ["Bridge of Dee", "Aberdeen", "AB14 0PT"],
     "17/03/2026", "BACS 331208",
     [("25/02/2026", "1190", "A4 paper & stationery (part payment)", 200.00)]),  # partial of £312
]

INK = (0.10, 0.10, 0.12)
GREY = (0.35, 0.35, 0.38)


def draw(c, payer, addr, date, cheque, lines):
    def text(x, y, s, size=9.5, font="Helvetica", col=INK, right=False):
        c.setFont(font, size); c.setFillColorRGB(*col)
        (c.drawRightString if right else c.drawString)(x, y, s)

    # Payer address, top-left
    text(20 * mm, H - 22 * mm, payer, 11, "Helvetica-Bold")
    y = H - 27 * mm
    for ln in addr:
        text(20 * mm, y, ln, 9.5, col=GREY); y -= 4.6 * mm

    # REMITTANCE ADVICE heading (right)
    text(W - 20 * mm, H - 70 * mm, "REMITTANCE ADVICE", 13, "Helvetica-Bold", right=True)

    # "To" box (us) — left
    bx, by, bw, bh = 20 * mm, H - 108 * mm, 82 * mm, 30 * mm
    c.setStrokeColorRGB(0.6, 0.6, 0.6); c.setLineWidth(0.8); c.rect(bx, by, bw, bh)
    ty = by + bh - 6 * mm
    text(bx + 4 * mm, ty, US[0], 9.5, "Helvetica-Bold"); ty -= 4.6 * mm
    for ln in US[1:]:
        text(bx + 4 * mm, ty, ln, 9, col=GREY); ty -= 4.4 * mm

    # Meta box (Date / Account Ref / Cheque No) — right, 3 rows
    mx, mw = W - 20 * mm - 78 * mm, 78 * mm
    rows = [("Date", date), ("Account Ref", "SOL-IQ"), ("Cheque No", cheque)]
    rh = 9 * mm; mtop = H - 82 * mm
    for i, (k, v) in enumerate(rows):
        ry = mtop - i * rh
        c.rect(mx, ry - rh, mw, rh)
        c.line(mx + 34 * mm, ry - rh, mx + 34 * mm, ry)
        text(mx + 3 * mm, ry - rh + 3 * mm, k, 9, col=GREY)
        text(mx + 37 * mm, ry - rh + 3 * mm, v, 9.5, "Helvetica-Bold")

    text(20 * mm, H - 120 * mm, "NOTE:  All values are shown in Pound Sterling", 9, col=GREY)

    # Line-item table
    top = H - 132 * mm
    cols = {"date": 22 * mm, "ref": 48 * mm, "details": 74 * mm, "debit": 150 * mm, "credit": W - 22 * mm}
    c.setStrokeColorRGB(0.5, 0.5, 0.5); c.setLineWidth(0.8)
    c.line(20 * mm, top + 5 * mm, W - 20 * mm, top + 5 * mm)
    for k, label, right in [("date", "Date", False), ("ref", "Ref", False), ("details", "Details", False),
                            ("debit", "Debit", True), ("credit", "Credit", True)]:
        text(cols[k], top, label, 9, "Helvetica-Bold", right=right)
    c.line(20 * mm, top - 2 * mm, W - 20 * mm, top - 2 * mm)

    ry = top - 8 * mm
    total = 0.0
    for (idt, ref, details, amt) in lines:
        text(cols["date"], ry, idt, 9, col=GREY)
        text(cols["ref"], ry, ref, 9.5, "Helvetica-Bold")
        text(cols["details"], ry, details, 9)
        text(cols["credit"], ry, f"{amt:,.2f}", 9.5, right=True)
        total += amt
        ry -= 6.5 * mm

    # Amount Paid box, bottom-right
    ax, aw, ah = W - 20 * mm - 70 * mm, 70 * mm, 16 * mm
    ay = 26 * mm
    c.setStrokeColorRGB(0.5, 0.5, 0.5); c.rect(ax, ay, aw, ah)
    c.line(ax, ay + 8 * mm, ax + aw, ay + 8 * mm)
    text(ax + aw / 2, ay + 9.5 * mm, "Amount Paid", 9, "Helvetica-Bold")
    text(ax + 4 * mm, ay + 2.5 * mm, "£", 10, "Helvetica-Bold")
    text(ax + aw - 4 * mm, ay + 2.5 * mm, f"{total:,.2f}", 11, "Helvetica-Bold", right=True)


print(f"Writing to {OUT}\n")
for payer, addr, date, cheque, lines in REMITTANCES:
    slug = payer.replace(" ", "-").replace("(", "").replace(")", "").replace(",", "")
    path = OUT / f"Remittance_{slug}.pdf"
    c = canvas.Canvas(str(path), pagesize=A4)
    c.setTitle(f"Remittance Advice - {payer}")
    draw(c, payer, addr, date, cheque, lines)
    c.showPage(); c.save()
    total = sum(l[3] for l in lines)
    refs = ", ".join(l[1] for l in lines)
    print(f"  {path.name:52} refs {refs:22} £{total:,.2f}")
print(f"\n{len(REMITTANCES)} remittance PDFs written.")
