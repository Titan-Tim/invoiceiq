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


class InvoiceExtractor:
    def __init__(self):
        self.settings = load_settings()
        self.client = anthropic.Anthropic(api_key=self.settings['claude']['api_key'])
        self.model = self.settings['claude'].get('model', 'claude-opus-4-7')

    def extract(self, file_path: str) -> dict:
        return self._extract_with_prompt(file_path, EXTRACTION_PROMPT)

    def extract_remittance(self, file_path: str) -> dict:
        return self._extract_with_prompt(file_path, REMITTANCE_PROMPT)

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
