"""Generate Ledger-IQ import XML for the demo:
  - ledgeriq_suppliers.xml        -> Contact records (type=SUPPLIER)
  - ledgeriq_purchase_orders.xml  -> PurchaseOrder + lines

Deliberate demo gaps:
  * Skye Fresh Produce is OMITTED from suppliers -> invoice INV-4410 has no
    matching supplier in Ledger-IQ (prompts to create/confirm a new supplier).
  * PO-10022 (Islay Logistics) net = 440.00 while invoice INV-9981 nets 486.50
    -> a deliberate PO/invoice mismatch to demo the discrepancy flow.

Fields map 1:1 to Ledger-IQ's Prisma models (Contact, PurchaseOrder,
PurchaseOrderLine). Run: .venv-local\\Scripts\\python.exe make_ledgeriq_import.py
"""
from pathlib import Path
from xml.sax.saxutils import escape

OUT = Path(__file__).parent / "demo_invoices"
OUT.mkdir(exist_ok=True)

# supplier: name, ref, vat, email, address(list). Skye omitted on purpose.
SUPPLIERS = [
    ("Highland Paper Co", "HPC", "GB 214 5567 89", "accounts@highlandpaper.co.uk",
     ["Unit 4, Nairn Industrial Estate", "Nairn", "IV12 5QR"]),
    ("Caledonian Print Services", "CPS", "GB 331 8890 12", "accounts@caledonianprint.co.uk",
     ["17 Longman Road", "Inverness", "IV1 1RY"]),
    ("Ben Nevis Hardware", "BNH", "GB 442 0091 34", "accounts@bennevishardware.co.uk",
     ["3 High Street", "Fort William", "PH33 6DH"]),
    ("Orkney Office Supplies", "OOS", "GB 556 7712 08", "accounts@orkneyoffice.co.uk",
     ["9 Albert Street", "Kirkwall, Orkney", "KW15 1HP"]),
    ("Islay Logistics Ltd", "ISL", "GB 667 4432 55", "accounts@islaylogistics.co.uk",
     ["Distillery Road", "Port Ellen, Islay", "PA42 7DU"]),
    # Skye Fresh Produce (SKY) intentionally omitted — missing-supplier demo.
    ("Grampian IT Solutions", "GIT", "GB 889 5567 21", "accounts@grampianit.co.uk",
     ["44 Union Street", "Aberdeen", "AB10 1BB"]),
    ("Loch Ness Cleaning", "LNC", "GB 990 2278 43", "accounts@lochnesscleaning.co.uk",
     ["Balmacaan Road", "Drumnadrochit", "IV63 6WJ"]),
]

# po: number, supplier_name, supplier_ref, date, expected, lines[(desc, qty, unit)]
# The first four match their invoices exactly. PO-10022 is the deliberate
# mismatch (nets 440.00 vs invoice INV-9981 at 486.50).
POS = [
    ("PO-10021", "Highland Paper Co", "HPC", "2026-06-24", "2026-07-08",
     [("A4 white copier paper (box of 5 reams)", 40, 22.00),
      ("C4 manila envelopes (box of 250)", 20, 14.00),
      ("Lever-arch files, assorted colours", 40, 2.00)]),
    ("PO-10023", "Caledonian Print Services", "CPS", "2026-06-24", "2026-07-06",
     [("Tri-fold brochures, full colour (per unit)", 5000, 0.36),
      ("Business cards, 350gsm (pack of 250)", 10, 25.00),
      ("A2 posters, full colour", 25, 10.00)]),
    ("PO-10024", "Ben Nevis Hardware", "BNH", "2026-06-22", "2026-07-05",
     [("Cordless drill driver 18V", 6, 95.00),
      ("Mixed screws & fixings (tub)", 20, 12.00),
      ("Heavy-duty cloth tape 50m", 20, 3.00)]),
    ("PO-10025", "Orkney Office Supplies", "OOS", "2026-06-21", "2026-07-04",
     [("Ballpoint pens (box of 50)", 10, 6.50),
      ("Compatible printer toner cartridge", 4, 45.00),
      ("Sticky notes, multipack", 10, 3.00)]),
    ("PO-10022", "Islay Logistics Ltd", "ISL", "2026-06-23", "2026-07-05",
     [("Pallet delivery — Highlands & Islands", 6, 58.00),
      ("Next-day courier surcharge", 4, 23.00)]),  # nets 440.00 (invoice=486.50)
]


def el(tag, val, indent):
    return f"{'  '*indent}<{tag}>{escape(str(val))}</{tag}>\n"


# ---- suppliers ------------------------------------------------------------ #
s = ['<?xml version="1.0" encoding="UTF-8"?>\n',
     "<!-- Ledger-IQ supplier import. Each <supplier> maps to a Contact\n",
     "     (type=SUPPLIER). Skye Fresh Produce is intentionally omitted so\n",
     "     invoice INV-4410 has no matching supplier in Ledger-IQ. -->\n",
     "<suppliers>\n"]
for name, ref, vat, email, addr in SUPPLIERS:
    s.append("  <supplier>\n")
    s.append(el("name", name, 2))
    s.append(el("type", "SUPPLIER", 2))
    s.append(el("externalRef", ref, 2))
    s.append(el("vatNumber", vat, 2))
    s.append(el("email", email, 2))
    s.append(el("address", ", ".join(addr), 2))
    s.append(el("invoiceTermsDays", 30, 2))
    s.append("  </supplier>\n")
s.append("</suppliers>\n")
(OUT / "ledgeriq_suppliers.xml").write_text("".join(s), encoding="utf-8")

# ---- purchase orders ------------------------------------------------------ #
p = ['<?xml version="1.0" encoding="UTF-8"?>\n',
     "<!-- Ledger-IQ purchase order import. Each <purchaseOrder> maps to a\n",
     "     PurchaseOrder + PurchaseOrderLine[]. supplierName/supplierRef pick\n",
     "     the Contact. PO-10022 net (440.00) intentionally differs from\n",
     "     invoice INV-9981 (486.50) to demo the discrepancy flow. -->\n",
     "<purchaseOrders>\n"]
for num, sup, ref, date, exp, lines in POS:
    net = sum(q * u for _, q, u in lines)
    p.append("  <purchaseOrder>\n")
    p.append(el("poNumber", num, 2))
    p.append(el("supplierName", sup, 2))
    p.append(el("supplierRef", ref, 2))
    p.append(el("date", date, 2))
    p.append(el("expectedAt", exp, 2))
    p.append(el("status", "OPEN", 2))
    p.append("    <lines>\n")
    for desc, q, u in lines:
        p.append("      <line>\n")
        p.append(el("description", desc, 4))
        p.append(el("quantity", f"{q}", 4))
        p.append(el("unitPrice", f"{u:.4f}", 4))
        p.append(el("netAmount", f"{q*u:.2f}", 4))
        p.append("      </line>\n")
    p.append("    </lines>\n")
    p.append(el("netTotal", f"{net:.2f}", 2))
    p.append("  </purchaseOrder>\n")
p.append("</purchaseOrders>\n")
(OUT / "ledgeriq_purchase_orders.xml").write_text("".join(p), encoding="utf-8")

print(f"Wrote {len(SUPPLIERS)} suppliers (Skye Fresh Produce omitted) and {len(POS)} POs to {OUT}")
for num, sup, ref, date, exp, lines in POS:
    net = sum(q * u for _, q, u in lines)
    flag = "  <-- deliberate mismatch (invoice nets 486.50)" if num == "PO-10022" else ""
    print(f"  {num}  {sup:28} net {net:>8.2f}{flag}")
