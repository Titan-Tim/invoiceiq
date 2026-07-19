"""Process an uploaded customer remittance: AI-extract the paid invoice
references, then push them to the finance system (Ledger-IQ) to mark the
matching sales invoices paid."""
import json
from datetime import datetime

from src.database import db, Remittance
from src.invoice_extractor import InvoiceExtractor
from src.connectors.factory import get_connector, get_system_name


def _parse_date(value):
    if not value:
        return None
    for fmt in ('%Y-%m-%d', '%d/%m/%Y', '%d-%m-%Y'):
        try:
            return datetime.strptime(str(value)[:10], fmt).date()
        except ValueError:
            continue
    return None


def process_remittance(remittance_id: int) -> None:
    rem = db.session.get(Remittance, remittance_id)
    if not rem:
        return

    # ---- Extract -------------------------------------------------------- #
    try:
        rem.status = 'extracting'
        db.session.commit()
        data = InvoiceExtractor().extract_remittance(rem.attachment_path)
    except Exception as e:
        rem.status = 'exception'
        rem.status_message = f"Extraction failed: {e}"
        db.session.commit()
        return

    lines = [
        {
            'invoice_number': str(l.get('invoice_number') or '').strip(),
            'amount': float(l.get('amount') or 0),
            'description': l.get('description'),
        }
        for l in (data.get('lines') or [])
        if str(l.get('invoice_number') or '').strip()
    ]
    rem.customer_name = data.get('customer_name')
    rem.remittance_date = _parse_date(data.get('remittance_date'))
    rem.reference = data.get('reference')
    rem.currency = data.get('currency') or 'GBP'
    rem.total_amount = data.get('total_amount')
    rem.extraction_confidence = data.get('confidence')
    rem.extraction_raw = json.dumps(data)
    rem.lines_json = json.dumps(lines)
    db.session.commit()

    if not lines:
        rem.status = 'exception'
        rem.status_message = 'No paid invoices could be read from this remittance.'
        db.session.commit()
        return

    # ---- Post to the finance system to mark invoices paid --------------- #
    connector = get_connector()
    if not hasattr(connector, 'post_receipt'):
        rem.status = 'exception'
        rem.status_message = (
            f"{get_system_name()} does not support remittance posting. "
            f"Set the finance system to Ledger-IQ in Settings."
        )
        db.session.commit()
        return

    try:
        rem.status = 'posting'
        db.session.commit()
        result = connector.post_receipt({
            'external_id':   f'remit-{rem.id}',
            'customer_name': rem.customer_name,
            'date':          rem.remittance_date or datetime.utcnow().date(),
            'reference':     rem.reference,
            'currency':      rem.currency,
            'lines':         lines,
        })
    except Exception as e:
        rem.status = 'exception'
        rem.status_message = f"{get_system_name()} post failed: {e}"
        db.session.commit()
        return

    matched = result.get('matched', []) or []
    unmatched = result.get('unmatched', []) or []
    rem.result_json = json.dumps(result)
    rem.matched_count = len(matched)
    rem.unmatched_count = len(unmatched)
    if unmatched:
        rem.status = 'partial'
        rem.status_message = (
            f"Marked {len(matched)} invoice(s) paid in {get_system_name()}; "
            f"{len(unmatched)} not found: {', '.join(unmatched)}"
        )
    else:
        rem.status = 'posted'
        rem.status_message = f"Marked {len(matched)} invoice(s) paid in {get_system_name()}."
    db.session.commit()
