import base64
import json
from pathlib import Path
import anthropic
import fitz  # PyMuPDF
from src.config_manager import load_settings

EXTRACTION_PROMPT = """You are an expert accounts payable clerk. Extract all data from this supplier invoice.

Return ONLY a JSON object — no explanation, no markdown — with this exact structure:
{
  "supplier_name": "string",
  "supplier_address": "string or null",
  "supplier_vat_number": "string or null",
  "invoice_number": "string",
  "invoice_date": "YYYY-MM-DD or null",
  "due_date": "YYYY-MM-DD or null",
  "po_reference": "string or null",
  "subtotal": number,
  "vat_amount": number,
  "total_amount": number,
  "currency": "GBP",
  "lines": [
    {
      "line_number": 1,
      "description": "string",
      "product_code": "string or null",
      "quantity": number,
      "unit_price": number,
      "line_total": number,
      "vat_rate": number
    }
  ],
  "confidence": 0.95,
  "notes": "any issues or uncertainties"
}

Rules:
- Use null for fields not found on the invoice
- Numbers must be numeric (not strings)
- confidence reflects how clearly the invoice was readable (0.0 to 1.0)
- po_reference: look for PO number, order number, purchase order reference
- currency defaults to GBP if not shown"""


REMITTANCE_PROMPT = """You are an expert accounts receivable clerk. This is a REMITTANCE ADVICE a customer sent us to tell us which of OUR sales invoices they have paid.

Return ONLY a JSON object — no explanation, no markdown — with this exact structure:
{
  "customer_name": "string",
  "remittance_date": "YYYY-MM-DD or null",
  "reference": "string or null",
  "currency": "GBP",
  "lines": [
    { "invoice_number": "string", "amount": number, "description": "string or null" }
  ],
  "total_amount": number,
  "confidence": 0.95,
  "notes": "any issues or uncertainties"
}

Rules:
- customer_name is the party who MADE the payment / SENT this remittance (usually the top/header address), NOT the recipient being paid.
- Each line is one paid invoice. invoice_number is the invoice reference (often a column labelled "Ref", "Reference", "Invoice", or "Invoice No"). amount is the value paid for that invoice (the "Credit", "Amount", "Paid" or "Value" column) as a positive number.
- reference is any payment reference shown (e.g. "Cheque No", BACS ref, payment number).
- total_amount is the overall amount paid (often labelled "Amount Paid" or "Total").
- Numbers must be numeric (not strings). Use null for fields not found.
- currency defaults to GBP if not shown."""


DELIVERY_NOTE_PROMPT = """You are an expert order-fulfilment clerk. This is a DELIVERY NOTE (proof of delivery) for goods WE dispatched to a customer. It should be signed by the customer to confirm receipt, and we invoice from it.

Return ONLY a JSON object — no explanation, no markdown — with this exact structure:
{
  "customer_name": "string",
  "order_reference": "string or null",
  "delivery_date": "YYYY-MM-DD or null",
  "signature_present": true,
  "signed_by": "string or null",
  "currency": "GBP",
  "lines": [
    { "description": "string", "quantity": number, "unit_price": number or null, "line_total": number or null }
  ],
  "confidence": 0.95,
  "notes": "any issues or uncertainties"
}

Rules:
- customer_name is the party the goods were DELIVERED TO (the "Deliver to" / "Customer" address), NOT our own company.
- order_reference is the originating order / PO number this delivery fulfils (labels like "Order No", "Order Ref", "PO", "Your Ref").
- signature_present: look carefully for a handwritten signature in a signature box or "Received by / Signature" area. Return true ONLY if you can see an actual handwritten mark/signature, false if the box is empty. This is the trigger for invoicing, so be accurate.
- signed_by: the printed name of the person who signed, if shown.
- lines: each delivered item with its quantity. Include unit_price and/or line_total only if the note shows values (delivery notes often omit prices — use null when absent).
- Numbers must be numeric (not strings). Use null for fields not found. currency defaults to GBP."""


class InvoiceExtractor:
    def __init__(self):
        self.settings = load_settings()
        self.client = anthropic.Anthropic(api_key=self.settings['claude']['api_key'])
        self.model = self.settings['claude'].get('model', 'claude-opus-4-7')

    def extract(self, file_path: str) -> dict:
        return self._extract_with_prompt(file_path, EXTRACTION_PROMPT)

    def extract_remittance(self, file_path: str) -> dict:
        return self._extract_with_prompt(file_path, REMITTANCE_PROMPT)

    def extract_delivery_note(self, file_path: str) -> dict:
        return self._extract_with_prompt(file_path, DELIVERY_NOTE_PROMPT)

    def suggest_line_accounts(self, lines: list, accounts: list) -> list:
        """Given invoice lines and a chart of accounts ([{code,name,...}]), return
        the best-matching account code per line, aligned to input order. Returns
        None for a line when unsure or if the model returns an unknown code, so
        the caller falls back to the default account. Never raises."""
        valid = {str(a['code']) for a in accounts if a.get('code')}
        if not lines or not valid:
            return [None] * len(lines)

        chart = "\n".join(f"{a['code']} — {a.get('name','')}" for a in accounts if a.get('code'))
        items = "\n".join(f"{i+1}. {(l.get('description') or '').strip()}"
                          for i, l in enumerate(lines))
        prompt = (
            "You are a bookkeeper coding a purchase (supplier) invoice to the nominal ledger.\n\n"
            "Chart of accounts (code — name):\n" + chart + "\n\n"
            "Invoice lines:\n" + items + "\n\n"
            "For each line, choose the single most appropriate account CODE from the chart "
            "above based on what was bought. If genuinely unsure, use null. "
            'Return ONLY JSON: {"codes": ["<code or null>", ...]} with exactly one entry '
            "per line, in the same order."
        )
        try:
            msg = self.client.messages.create(
                model=self.model, max_tokens=1024,
                messages=[{"role": "user", "content": prompt}],
            )
            raw = msg.content[0].text.strip()
            obj = json.loads(raw[raw.find('{'):raw.rfind('}') + 1])
            codes = obj.get('codes', []) or []
        except Exception:
            return [None] * len(lines)

        out = []
        for i in range(len(lines)):
            c = codes[i] if i < len(codes) else None
            c = str(c).strip() if c not in (None, "null", "") else None
            out.append(c if c in valid else None)
        return out

    def _extract_with_prompt(self, file_path: str, prompt: str) -> dict:
        path = Path(file_path)
        images = self._to_images(path)

        content = []
        for img_b64 in images:
            content.append({
                "type": "image",
                "source": {"type": "base64", "media_type": "image/png", "data": img_b64}
            })
        content.append({"type": "text", "text": prompt})

        message = self.client.messages.create(
            model=self.model,
            max_tokens=4096,
            messages=[{"role": "user", "content": content}]
        )

        raw = message.content[0].text.strip()
        start = raw.find('{')
        end = raw.rfind('}') + 1
        if start == -1:
            raise ValueError(f"No JSON in extraction response: {raw[:200]}")
        return json.loads(raw[start:end])

    def _to_images(self, path: Path) -> list:
        if path.suffix.lower() == '.pdf':
            return self._pdf_to_images(path)
        with open(path, 'rb') as f:
            return [base64.b64encode(f.read()).decode('utf-8')]

    def _pdf_to_images(self, path: Path) -> list:
        doc = fitz.open(str(path))
        images = []
        for page in doc:
            mat = fitz.Matrix(2.0, 2.0)  # 2x zoom for legibility
            pix = page.get_pixmap(matrix=mat)
            images.append(base64.b64encode(pix.tobytes('png')).decode('utf-8'))
        doc.close()
        return images
