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


class InvoiceExtractor:
    def __init__(self):
        self.settings = load_settings()
        self.client = anthropic.Anthropic(api_key=self.settings['claude']['api_key'])
        self.model = self.settings['claude'].get('model', 'claude-opus-4-7')

    def extract(self, file_path: str) -> dict:
        path = Path(file_path)
        images = self._to_images(path)

        content = []
        for img_b64 in images:
            content.append({
                "type": "image",
                "source": {"type": "base64", "media_type": "image/png", "data": img_b64}
            })
        content.append({"type": "text", "text": EXTRACTION_PROMPT})

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
