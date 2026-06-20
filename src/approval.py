from datetime import datetime
from src.database import db, Invoice, User, AuditLog
from src.config_manager import load_settings


class ApprovalWorkflow:
    def __init__(self):
        self.settings = load_settings()

    def check_requires_approval(self, invoice: Invoice) -> bool:
        s = self.settings.get('approval', {})
        if not s.get('enabled'):
            return False
        if not invoice.total_amount:
            return False
        threshold = float(s.get('threshold_amount', 0))
        return float(invoice.total_amount) >= threshold

    def assign_approver(self, invoice: Invoice):
        approvers = self.settings.get('approval', {}).get('approvers', [])
        if not approvers:
            return
        # Assign to first configured approver; future: round-robin or rule-based
        email = approvers[0].get('email')
        if email:
            user = User.query.filter_by(email=email, is_active=True).first()
            if user:
                invoice.assigned_approver_id = user.id

    def approve(self, invoice: Invoice, user_name: str, notes: str = '') -> bool:
        if invoice.status not in ('awaiting_approval', 'matched', 'partial_match'):
            return False
        invoice.status = 'approved'
        invoice.approved_at = datetime.utcnow()
        db.session.add(AuditLog(
            invoice_id=invoice.id,
            action='approved',
            user_name=user_name,
            notes=notes or 'Invoice approved'
        ))
        db.session.commit()
        return True

    def reject(self, invoice: Invoice, user_name: str, reason: str) -> bool:
        if invoice.status != 'awaiting_approval':
            return False
        invoice.status = 'rejected'
        invoice.rejection_reason = reason
        db.session.add(AuditLog(
            invoice_id=invoice.id,
            action='rejected',
            user_name=user_name,
            notes=f"Rejected: {reason}"
        ))
        db.session.commit()
        return True

    def get_pending_tasks(self, user_id: int) -> list:
        return Invoice.query.filter_by(
            assigned_approver_id=user_id,
            status='awaiting_approval'
        ).order_by(Invoice.created_at.desc()).all()
