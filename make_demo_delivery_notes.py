"""Generate demo SIGNED delivery notes (proof of delivery) for the
Deliver-to-Invoice flow. Each note has our company as sender, a customer as
deliver-to (names match the Xero customer CSV), an order ref, delivery date,
priced line items, and a signature box. One note is left UNSIGNED to demo the
'no signature -> no invoice' guard.

Run:  python make_demo_delivery_notes.py   (writes PDFs to demo_deliveries/)
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
from reportlab.graphics.shapes import Drawing, PolyLine, String

OUT = Path(__file__).parent / "demo_deliveries"
OUT.mkdir(exist_ok=True)

# Our company (the one dispatching goods and raising the sales invoice)
FROM = ["Highlands & Islands Trading Ltd", "12 Harbour Street", "Inverness", "IV1 1AA"]

INK    = colors.HexColor("#0F2749")
GREY   = colors.HexColor("#64748b")
LINE   = colors.HexColor("#e2e8f0")
HEADBG = colors.HexColor("#0F2749")

NOTES = [
    dict(no="DN-7001", customer="Cairngorm Outdoor Co",
         deliver_to=["2 Grampian Road", "Aviemore", "PH22 1RH"],
         order="ORD-5501", date="2026-07-28", signed_by="J. MacLeod",
         lines=[("Waterproof hiking jackets (adult)", 12, 48.00),
                ("Insulated flasks 1L", 30, 9.50),
                ("Trail map packs — Cairngorms", 25, 4.00)]),
    dict(no="DN-7002", customer="Moray Firth Hotels",
         deliver_to=["Shore Street", "Nairn", "IV12 4EA"],
         order="ORD-5502", date="2026-07-29", signed_by="A. Fraser",
         lines=[("Bath towel sets (white)", 40, 12.00),
                ("Guest toiletry kits", 200, 1.35),
                ("Table linen — banquet", 30, 7.50)]),
    # Deliberately UNSIGNED — the app should refuse to invoice this one.
    dict(no="DN-7003", customer="Deeside Farm Shop",
         deliver_to=["Bridge of Dee", "Aberdeen", "AB14 0PT"],
         order="ORD-5503", date="2026-07-30", signed_by=None,
         lines=[("Wooden produce crates", 25, 6.00),
                ("Paper carrier bags (box 250)", 8, 14.00),
                ("Chalkboard price tags", 100, 0.45)]),
]

styles = getSampleStyleSheet()
S = lambda name, **kw: ParagraphStyle(name, parent=styles["Normal"], **kw)
co_name = S("co", fontName="Helvetica-Bold", fontSize=15, textColor=INK, leading=18)
small   = S("small", fontSize=8.5, textColor=GREY, leading=12)
body    = S("body", fontSize=9.5, leading=13)
label_r = S("label_r", fontSize=9, textColor=GREY, alignment=TA_RIGHT, leading=13)
val_r   = S("val_r", fontSize=9.5, fontName="Helvetica-Bold", alignment=TA_RIGHT, leading=13)
title   = S("title", fontName="Helvetica-Bold", fontSize=22, textColor=INK, alignment=TA_RIGHT, leading=26)


def signature_drawing(name):
    """A handwritten-looking scrawl (PolyLine) plus the printed name — reads
    clearly as a signature to the vision model. Returns an empty box if unsigned."""
    d = Drawing(150, 44)
    if name:
        pts = [4,14, 14,30, 22,6, 30,26, 40,10, 52,28, 60,12, 74,24,
               86,8, 96,26, 110,14, 124,28, 138,12, 146,20]
        d.add(PolyLine(pts, strokeColor=colors.HexColor("#12356b"), strokeWidth=1.6))
        d.add(String(6, 0, name, fontName="Helvetica-Oblique", fontSize=7, fillColor=GREY))
    return d


def build(n):
    story = []
    left = [Paragraph(FROM[0], co_name)] + [Paragraph(x, small) for x in FROM[1:]]
    d = datetime.strptime(n["date"], "%Y-%m-%d")
    meta = [[Paragraph("Delivery No", label_r), Paragraph(n["no"], val_r)],
            [Paragraph("Order Ref", label_r), Paragraph(n["order"], val_r)],
            [Paragraph("Date", label_r), Paragraph(d.strftime("%d %b %Y"), val_r)]]
    meta_tbl = Table(meta, colWidths=[26 * mm, 32 * mm], hAlign="RIGHT")
    meta_tbl.setStyle(TableStyle([("TOPPADDING", (0, 0), (-1, -1), 1),
                                  ("BOTTOMPADDING", (0, 0), (-1, -1), 1)]))
    right = [Paragraph("DELIVERY NOTE", title), Spacer(1, 12), meta_tbl]
    head = Table([[left, right]], colWidths=[100 * mm, 70 * mm])
    head.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP")]))
    story += [head, Spacer(1, 16)]

    story.append(Paragraph("DELIVER TO", small))
    story.append(Paragraph("<br/>".join([f"<b>{n['customer']}</b>"] + n["deliver_to"]), body))
    story.append(Spacer(1, 16))

    rows = [["Description", "Qty", "Unit Price"]]
    for desc, q, u in n["lines"]:
        rows.append([Paragraph(desc, body), str(q), f"£{u:,.2f}"])
    t = Table(rows, colWidths=[110 * mm, 20 * mm, 30 * mm])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), HEADBG),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, 0), 9),
        ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("LINEBELOW", (0, 1), (-1, -1), 0.4, LINE),
    ]))
    story += [t, Spacer(1, 28)]

    # Received-by / signature block
    sig = signature_drawing(n["signed_by"])
    recv = [
        [Paragraph("Received in good condition by:", small), ""],
        [sig, ""],
        [Paragraph("Signature", small),
         Paragraph(f"Print name: <b>{n['signed_by']}</b>" if n["signed_by"] else "Print name:", small)],
    ]
    recv_tbl = Table(recv, colWidths=[85 * mm, 85 * mm])
    recv_tbl.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "BOTTOM"),
        ("LINEBELOW", (0, 1), (0, 1), 0.6, GREY),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
    ]))
    story.append(recv_tbl)

    slug = n["customer"].replace(" ", "-").replace("&", "and")
    tag = "" if n["signed_by"] else "_UNSIGNED"
    path = OUT / f"{n['no']}_{slug}{tag}.pdf"
    doc = SimpleDocTemplate(str(path), pagesize=A4,
                            leftMargin=20 * mm, rightMargin=20 * mm,
                            topMargin=18 * mm, bottomMargin=18 * mm,
                            title=f"Delivery Note {n['no']}")
    doc.build(story)
    return path.name


if __name__ == "__main__":
    for n in NOTES:
        print("Wrote", build(n))
    print(f"\nDone — {len(NOTES)} delivery notes in {OUT}")
