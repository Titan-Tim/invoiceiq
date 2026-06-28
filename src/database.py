from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime

db = SQLAlchemy()


class User(db.Model):
    __tablename__ = 'users'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    email = db.Column(db.String(200), unique=True)
    password_hash = db.Column(db.String(255))
    role = db.Column(db.String(50), default='approver')  # admin, approver, viewer
    is_active = db.Column(db.Boolean, default=True)
    must_change_password = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def set_password(self, password: str):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password: str) -> bool:
        return bool(self.password_hash) and check_password_hash(self.password_hash, password)

    def to_dict(self):
        return {'id': self.id, 'name': self.name, 'email': self.email,
                'role': self.role, 'is_active': self.is_active,
                'has_password': bool(self.password_hash)}


class Invoice(db.Model):
    __tablename__ = 'invoices'
    id = db.Column(db.Integer, primary_key=True)

    # Email source
    email_message_id = db.Column(db.String(500))
    email_received_at = db.Column(db.DateTime)
    email_from = db.Column(db.String(200))
    email_subject = db.Column(db.String(500))
    attachment_filename = db.Column(db.String(500))
    attachment_path = db.Column(db.String(1000))

    # Extracted invoice data
    supplier_name = db.Column(db.String(200))
    supplier_ref = db.Column(db.String(100))
    invoice_number = db.Column(db.String(100))
    invoice_date = db.Column(db.Date)
    po_reference = db.Column(db.String(100))
    subtotal = db.Column(db.Numeric(15, 2))
    vat_amount = db.Column(db.Numeric(15, 2))
    total_amount = db.Column(db.Numeric(15, 2))
    currency = db.Column(db.String(10), default='GBP')
    extraction_confidence = db.Column(db.Float)
    extraction_raw = db.Column(db.Text)

    # Status
    status = db.Column(db.String(50), default='received', index=True)
    status_message = db.Column(db.Text)
    push_failed = db.Column(db.Boolean, default=False, index=True)

    # PO Match
    matched_po_id = db.Column(db.Integer, db.ForeignKey('purchase_orders.id'))
    match_confidence = db.Column(db.Float)
    match_discrepancies = db.Column(db.Text)  # JSON list

    # Approval
    requires_approval = db.Column(db.Boolean, default=False)
    assigned_approver_id = db.Column(db.Integer, db.ForeignKey('users.id'))
    approved_at = db.Column(db.DateTime)
    approved_by_id = db.Column(db.Integer, db.ForeignKey('users.id'))
    rejection_reason = db.Column(db.Text)

    # Sage
    sage_transaction_ref = db.Column(db.String(100))
    posted_to_sage_at = db.Column(db.DateTime)

    # Timestamps
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    lines = db.relationship('InvoiceLine', backref='invoice', lazy=True,
                            cascade='all, delete-orphan')
    matched_po = db.relationship('PurchaseOrder', foreign_keys=[matched_po_id])
    assigned_approver = db.relationship('User', foreign_keys=[assigned_approver_id])
    approved_by = db.relationship('User', foreign_keys=[approved_by_id])
    audit_logs = db.relationship('AuditLog', backref='invoice', lazy=True,
                                 cascade='all, delete-orphan')

    STATUS_LABELS = {
        'received':          ('Received',          'secondary'),
        'extracting':        ('Extracting',         'info'),
        'extracted':         ('Extracted',          'info'),
        'matching':          ('Matching',           'info'),
        'matched':           ('Matched',            'success'),
        'partial_match':     ('Partial Match',      'warning'),
        'no_match':          ('No Match',           'danger'),
        'awaiting_approval': ('Awaiting Approval',  'warning'),
        'approved':          ('Approved',           'success'),
        'rejected':          ('Rejected',           'danger'),
        'posted':            ('Posted to Sage',     'success'),
        'ready_to_pay':      ('Ready to Pay',       'success'),
        'exception':         ('Exception',          'danger'),
    }

    def status_label(self):
        return self.STATUS_LABELS.get(self.status, (self.status, 'secondary'))

    def to_dict(self):
        label, color = self.status_label()
        return {
            'id': self.id,
            'supplier_name': self.supplier_name,
            'invoice_number': self.invoice_number,
            'invoice_date': self.invoice_date.isoformat() if self.invoice_date else None,
            'po_reference': self.po_reference,
            'total_amount': float(self.total_amount) if self.total_amount else None,
            'currency': self.currency,
            'status': self.status,
            'status_label': label,
            'status_color': color,
            'email_from': self.email_from,
            'email_received_at': self.email_received_at.isoformat() if self.email_received_at else None,
            'attachment_filename': self.attachment_filename,
            'created_at': self.created_at.isoformat(),
            'requires_approval': self.requires_approval,
            'match_confidence': self.match_confidence,
            'extraction_confidence': self.extraction_confidence,
            'push_failed': self.push_failed,
            'status_message': self.status_message,
        }


class InvoiceLine(db.Model):
    __tablename__ = 'invoice_lines'
    id = db.Column(db.Integer, primary_key=True)
    invoice_id = db.Column(db.Integer, db.ForeignKey('invoices.id'), nullable=False)
    line_number = db.Column(db.Integer)
    description = db.Column(db.String(500))
    quantity = db.Column(db.Numeric(15, 4))
    unit_price = db.Column(db.Numeric(15, 4))
    line_total = db.Column(db.Numeric(15, 2))
    vat_rate = db.Column(db.Numeric(5, 2))
    product_code = db.Column(db.String(100))
    matched = db.Column(db.Boolean, default=False)
    match_notes = db.Column(db.String(500))


class PurchaseOrder(db.Model):
    __tablename__ = 'purchase_orders'
    id = db.Column(db.Integer, primary_key=True)
    po_number = db.Column(db.String(100), unique=True, nullable=False)
    supplier_name = db.Column(db.String(200))
    supplier_ref = db.Column(db.String(100))
    po_date = db.Column(db.Date)
    expected_delivery = db.Column(db.Date)
    subtotal = db.Column(db.Numeric(15, 2))
    vat_amount = db.Column(db.Numeric(15, 2))
    total_amount = db.Column(db.Numeric(15, 2))
    currency = db.Column(db.String(10), default='GBP')
    status = db.Column(db.String(50))
    source = db.Column(db.String(20))  # 'sage' or 'folder'
    file_path = db.Column(db.String(1000))
    last_synced = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    lines = db.relationship('POLine', backref='po', lazy=True, cascade='all, delete-orphan')


class POLine(db.Model):
    __tablename__ = 'po_lines'
    id = db.Column(db.Integer, primary_key=True)
    po_id = db.Column(db.Integer, db.ForeignKey('purchase_orders.id'), nullable=False)
    line_number = db.Column(db.Integer)
    description = db.Column(db.String(500))
    quantity = db.Column(db.Numeric(15, 4))
    unit_price = db.Column(db.Numeric(15, 4))
    line_total = db.Column(db.Numeric(15, 2))
    product_code = db.Column(db.String(100))
    quantity_invoiced = db.Column(db.Numeric(15, 4), default=0)


class AuditLog(db.Model):
    __tablename__ = 'audit_log'
    id = db.Column(db.Integer, primary_key=True)
    invoice_id = db.Column(db.Integer, db.ForeignKey('invoices.id'))
    action = db.Column(db.String(100))
    user_name = db.Column(db.String(100))
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)
    notes = db.Column(db.Text)
