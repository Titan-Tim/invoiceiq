"""
Main orchestration pipeline:
  email capture → AI extraction → PO match → approval routing

Called by the scheduler (background) and the manual poll API endpoint.
All finance-system operations go through the connector factory so the
pipeline is identical regardless of whether Sage, QBO, or Xero is used.
"""
from datetime import datetime
from pathlib import Path

from src.database import db, Invoice, InvoiceLine, PurchaseOrder, POLine, AuditLog
from src.email_monitor import EmailMonitor
from src.invoice_extractor import InvoiceExtractor
from src.po_matcher import POMatcher
from src.approval import ApprovalWorkflow
from src.config_manager import load_settings


# ------------------------------------------------------------------ #
# Main pipeline
# ------------------------------------------------------------------ #

def process_new_emails() -> dict:
    settings = load_settings()
    monitor  = EmailMonitor()
    extractor = InvoiceExtractor()
    matcher   = POMatcher()
    workflow  = ApprovalWorkflow()

    storage = settings['app'].get('attachment_storage_path', 'invoices')
    Path(storage).mkdir(parents=True, exist_ok=True)

    emails = monitor.get_unread_invoice_emails()
    stats  = {'processed': 0, 'errors': 0, 'skipped': 0}

    for msg in emails:
        message_id  = msg['id']

        if Invoice.query.filter_by(email_message_id=message_id).first():
            stats['skipped'] += 1
            continue

        attachments = monitor.get_invoice_attachments(message_id)
        if not attachments:
            monitor.mark_as_read(message_id)
            stats['skipped'] += 1
            continue

        for att in attachments:
            invoice = Invoice(
                email_message_id=message_id,
                email_received_at=_parse_dt(msg.get('receivedDateTime', '')),
                email_from=msg['from']['emailAddress']['address'],
                email_subject=msg.get('subject', ''),
                attachment_filename=att['name'],
                status='received',
            )
            db.session.add(invoice)
            db.session.flush()

            # Download
            safe_name = f"inv_{invoice.id}_{att['name']}"
            save_path = str(Path(storage) / safe_name)
            try:
                monitor.download_attachment(message_id, att['id'], save_path)
            except Exception as e:
                invoice.status = 'exception'
                invoice.status_message = f"Download failed: {e}"
                db.session.commit()
                stats['errors'] += 1
                continue

            invoice.attachment_path = save_path
            db.session.add(AuditLog(
                invoice_id=invoice.id, action='received', user_name='system',
                notes=f"Received from {invoice.email_from}"
            ))
            db.session.commit()

            # Extract
            try:
                invoice.status = 'extracting'
                db.session.commit()
                data = extractor.extract(save_path)
                _apply_extraction(invoice, data)
                invoice.status = 'extracted'
                db.session.commit()
            except Exception as e:
                invoice.status = 'exception'
                invoice.status_message = f"Extraction failed: {e}"
                db.session.add(AuditLog(
                    invoice_id=invoice.id, action='extraction_error',
                    user_name='system', notes=str(e)
                ))
                db.session.commit()
                stats['errors'] += 1
                continue

            # Resolve vendor reference in finance system
            _resolve_vendor_ref(invoice)

            # Match PO
            try:
                invoice.status = 'matching'
                db.session.commit()
                result = matcher.find_and_match(invoice)

                if result.po:
                    invoice.matched_po_id      = result.po.id
                    invoice.match_confidence   = result.confidence
                    invoice.match_discrepancies = str(result.discrepancies)
                    invoice.status = 'matched' if result.matched else 'partial_match'
                    if not result.matched:
                        invoice.status_message = '; '.join(result.discrepancies[:3])
                else:
                    invoice.status = 'no_match'
                    invoice.status_message = 'No matching purchase order found'
                db.session.commit()
            except Exception as e:
                invoice.status = 'exception'
                invoice.status_message = f"Matching failed: {e}"
                db.session.commit()
                stats['errors'] += 1
                continue

            # Approval routing
            if invoice.status == 'matched' and workflow.check_requires_approval(invoice):
                invoice.status          = 'awaiting_approval'
                invoice.requires_approval = True
                workflow.assign_approver(invoice)
                db.session.add(AuditLog(
                    invoice_id=invoice.id, action='sent_for_approval',
                    user_name='system',
                    notes=f"Amount £{invoice.total_amount} exceeds approval threshold"
                ))
                db.session.commit()

            stats['processed'] += 1

        monitor.mark_as_read(message_id)
        monitor.move_to_processed(message_id)

    return stats


# ------------------------------------------------------------------ #
# Manual upload pipeline
# ------------------------------------------------------------------ #

def process_uploaded_invoice(invoice_id: int) -> None:
    """
    Run extraction → PO match → approval for an invoice that was
    manually uploaded (record already exists, file already saved).
    Called in a background thread from the upload API endpoint.
    """
    extractor = InvoiceExtractor()
    matcher   = POMatcher()
    workflow  = ApprovalWorkflow()

    invoice = Invoice.query.get(invoice_id)
    if not invoice or not invoice.attachment_path:
        return

    # Extract
    try:
        invoice.status = 'extracting'
        db.session.commit()
        data = extractor.extract(invoice.attachment_path)
        _apply_extraction(invoice, data)
        invoice.status = 'extracted'
        db.session.commit()
    except Exception as e:
        invoice.status = 'exception'
        invoice.status_message = f"Extraction failed: {e}"
        db.session.add(AuditLog(
            invoice_id=invoice.id, action='extraction_error',
            user_name='system', notes=str(e)
        ))
        db.session.commit()
        return

    _resolve_vendor_ref(invoice)

    # Match PO
    try:
        invoice.status = 'matching'
        db.session.commit()
        result = matcher.find_and_match(invoice)

        if result.po:
            invoice.matched_po_id       = result.po.id
            invoice.match_confidence    = result.confidence
            invoice.match_discrepancies = str(result.discrepancies)
            invoice.status = 'matched' if result.matched else 'partial_match'
            if not result.matched:
                invoice.status_message = '; '.join(result.discrepancies[:3])
        else:
            invoice.status = 'no_match'
            invoice.status_message = 'No matching purchase order found'
        db.session.commit()
    except Exception as e:
        invoice.status = 'exception'
        invoice.status_message = f"Matching failed: {e}"
        db.session.commit()
        return

    # Approval routing
    if invoice.status == 'matched' and workflow.check_requires_approval(invoice):
        invoice.status            = 'awaiting_approval'
        invoice.requires_approval = True
        workflow.assign_approver(invoice)
        db.session.add(AuditLog(
            invoice_id=invoice.id, action='sent_for_approval',
            user_name='system',
            notes=f"Amount {invoice.total_amount} exceeds approval threshold"
        ))
        db.session.commit()


# ------------------------------------------------------------------ #
# PO sync helpers
# ------------------------------------------------------------------ #

def sync_pos_from_connector() -> int:
    """Pull purchase orders from the configured finance system."""
    from src.connectors.factory import get_connector
    pos = get_connector().get_purchase_orders()
    return _upsert_pos(pos)


def sync_pos_from_folder() -> int:
    """Read PO PDFs from the configured folder and index them."""
    settings = load_settings()
    folder   = settings['po_source'].get('folder_path', '')
    if not folder or not Path(folder).exists():
        raise ValueError(f"PO folder not found: {folder}")

    extractor = InvoiceExtractor()
    count     = 0
    for pdf_path in Path(folder).glob('*.pdf'):
        try:
            data      = extractor.extract(str(pdf_path))
            po_number = data.get('invoice_number') or pdf_path.stem
            pos = [{
                'po_number':        po_number,
                'supplier_name':    data.get('supplier_name'),
                'supplier_ref':     '',
                'po_date':          data.get('invoice_date'),
                'expected_delivery':None,
                'subtotal':         data.get('subtotal', 0),
                'vat_amount':       data.get('vat_amount', 0),
                'total_amount':     data.get('total_amount', 0),
                'currency':         data.get('currency', 'GBP'),
                'status':           'open',
                'source':           'folder',
                'file_path':        str(pdf_path),
                'lines':            [{
                    'line_number':      n,
                    'description':      l.get('description', ''),
                    'product_code':     l.get('product_code', ''),
                    'quantity':         l.get('quantity', 0),
                    'unit_price':       l.get('unit_price', 0),
                    'line_total':       l.get('line_total', 0),
                    'quantity_invoiced':0,
                } for n, l in enumerate(data.get('lines', []), 1)],
            }]
            count += _upsert_pos(pos)
        except Exception:
            continue
    return count


def _upsert_pos(pos: list) -> int:
    from src.database import PurchaseOrder, POLine
    for po_data in pos:
        po = PurchaseOrder.query.filter_by(po_number=po_data['po_number']).first()
        if not po:
            po = PurchaseOrder(po_number=po_data['po_number'])
            db.session.add(po)

        po.supplier_name    = po_data.get('supplier_name')
        po.supplier_ref     = po_data.get('supplier_ref')
        po.po_date          = _parse_date(po_data.get('po_date'))
        po.expected_delivery= _parse_date(po_data.get('expected_delivery'))
        po.subtotal         = po_data.get('subtotal', 0)
        po.vat_amount       = po_data.get('vat_amount', 0)
        po.total_amount     = po_data.get('total_amount', 0)
        po.currency         = po_data.get('currency', 'GBP')
        po.status           = po_data.get('status', '')
        po.source           = po_data.get('source', 'connector')
        po.file_path        = po_data.get('file_path')
        po.last_synced      = datetime.utcnow()
        db.session.flush()

        POLine.query.filter_by(po_id=po.id).delete()
        for ld in po_data.get('lines', []):
            db.session.add(POLine(
                po_id           = po.id,
                line_number     = ld['line_number'],
                description     = ld['description'],
                product_code    = ld.get('product_code'),
                quantity        = ld['quantity'],
                unit_price      = ld['unit_price'],
                line_total      = ld['line_total'],
                quantity_invoiced = ld.get('quantity_invoiced', 0),
            ))

    db.session.commit()
    return len(pos)


# ------------------------------------------------------------------ #
# Internal helpers
# ------------------------------------------------------------------ #

def _apply_extraction(invoice: Invoice, data: dict):
    invoice.supplier_name       = data.get('supplier_name')
    invoice.invoice_number      = data.get('invoice_number')
    invoice.po_reference        = data.get('po_reference')
    invoice.invoice_date        = _parse_date(data.get('invoice_date'))
    invoice.subtotal            = data.get('subtotal')
    invoice.vat_amount          = data.get('vat_amount')
    invoice.total_amount        = data.get('total_amount')
    invoice.currency            = data.get('currency', 'GBP')
    invoice.extraction_confidence = data.get('confidence')
    invoice.extraction_raw      = str(data)

    for ld in data.get('lines', []):
        db.session.add(InvoiceLine(
            invoice_id  = invoice.id,
            line_number = ld.get('line_number'),
            description = ld.get('description'),
            quantity    = ld.get('quantity'),
            unit_price  = ld.get('unit_price'),
            line_total  = ld.get('line_total'),
            vat_rate    = ld.get('vat_rate'),
            product_code = ld.get('product_code'),
        ))


def _resolve_vendor_ref(invoice: Invoice):
    """Look up the vendor/supplier ref in the finance system by name."""
    if not invoice.supplier_name:
        return
    try:
        from src.connectors.factory import get_connector
        ref = get_connector().find_vendor(invoice.supplier_name)
        if ref:
            invoice.supplier_ref = ref
        db.session.commit()
    except Exception:
        pass


def _parse_dt(dt_str: str):
    if not dt_str:
        return None
    try:
        return datetime.fromisoformat(dt_str.replace('Z', '+00:00'))
    except ValueError:
        return None


def _parse_date(value):
    if not value:
        return None
    if isinstance(value, str):
        try:
            from datetime import date
            return datetime.strptime(value[:10], '%Y-%m-%d').date()
        except ValueError:
            return None
    return value
