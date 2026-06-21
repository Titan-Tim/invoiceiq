from datetime import datetime
from src.database import db, Invoice, User, AuditLog
from src.config_manager import load_settings

# Statuses that are eligible to be routed for approval at all.
# Exceptions/extraction failures are not — those need fixing, not approving.
ROUTABLE_STATUSES = ('matched', 'partial_match', 'no_match')


class ApprovalWorkflow:
    def __init__(self):
        self.settings = load_settings()

    def check_requires_approval(self, invoice: Invoice) -> bool:
        s = self.settings.get('approval', {})
        if not s.get('enabled'):
            return False
        if invoice.status not in ROUTABLE_STATUSES:
            return False

        rule = self._matching_rule(invoice)
        if rule:
            return True

        # No rule matched — fall back to the simple default threshold,
        # but only for fully-matched invoices. partial_match/no_match
        # without a rule covering them are left for a human regardless,
        # since there's nothing else to compare the bill against.
        if invoice.status != 'matched':
            return True
        if not invoice.total_amount:
            return False
        threshold = float(s.get('threshold_amount', 0))
        return float(invoice.total_amount) >= threshold

    def assign_approver(self, invoice: Invoice):
        rule = self._matching_rule(invoice)
        email = rule.get('approver_email') if rule else None

        if email:
            user = User.query.filter_by(email=email, is_active=True).first()
        else:
            # No rule matched (or it didn't specify an approver) — fall back
            # to the first active admin/approver, so invoices never end up
            # flagged "awaiting approval" with nobody actually assigned.
            user = User.query.filter(
                User.is_active == True, User.role.in_(('admin', 'approver'))
            ).order_by(User.id).first()

        if user:
            invoice.assigned_approver_id = user.id

    def _matching_rule(self, invoice: Invoice) -> dict | None:
        """Return the first rule (in saved order) that matches this invoice."""
        rules  = self.settings.get('approval', {}).get('rules', [])
        amount = float(invoice.total_amount or 0)

        for rule in rules:
            cond = rule.get('condition')
            if cond == 'no_match' and invoice.status == 'no_match':
                return rule
            if cond == 'partial_match' and invoice.status == 'partial_match':
                return rule
            if cond == 'amount_gte' and invoice.total_amount is not None:
                try:
                    if amount >= float(rule.get('value', 0)):
                        return rule
                except (TypeError, ValueError):
                    continue
            if cond == 'supplier_contains':
                needle = (rule.get('value') or '').strip().lower()
                if needle and needle in (invoice.supplier_name or '').lower():
                    return rule
        return None

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
