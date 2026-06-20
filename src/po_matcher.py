import json
from difflib import SequenceMatcher
from src.config_manager import load_settings

MATCH_THRESHOLD = 0.75
AMOUNT_TOLERANCE = 0.02  # 2% tolerance


class MatchResult:
    def __init__(self):
        self.matched = False
        self.po = None
        self.confidence = 0.0
        self.discrepancies = []
        self.line_matches = []

    def to_dict(self):
        return {
            'matched': self.matched,
            'po_number': self.po.po_number if self.po else None,
            'confidence': self.confidence,
            'discrepancies': self.discrepancies,
            'line_matches': self.line_matches,
        }


class POMatcher:
    def __init__(self):
        self.settings = load_settings()

    def find_and_match(self, invoice) -> MatchResult:
        from src.database import PurchaseOrder

        result = MatchResult()

        # Try exact PO reference first
        po = None
        if invoice.po_reference:
            po = PurchaseOrder.query.filter(
                PurchaseOrder.po_number.ilike(invoice.po_reference.strip())
            ).first()

        # Fall back to fuzzy match on supplier + amount
        if not po:
            po = self._fuzzy_find_po(invoice)

        if not po:
            return result

        return self._compare(invoice, po, result)

    def _fuzzy_find_po(self, invoice):
        from src.database import PurchaseOrder

        candidates = PurchaseOrder.query.filter(
            PurchaseOrder.status.notin_(['complete', 'cancelled', 'closed'])
        ).all()

        best_po, best_score = None, 0.0

        for po in candidates:
            score = 0.0

            if invoice.supplier_name and po.supplier_name:
                score += SequenceMatcher(
                    None,
                    invoice.supplier_name.lower(),
                    po.supplier_name.lower()
                ).ratio() * 0.5

            if invoice.total_amount and po.total_amount:
                inv_t, po_t = float(invoice.total_amount), float(po.total_amount)
                if po_t > 0:
                    diff = abs(inv_t - po_t) / po_t
                    if diff <= AMOUNT_TOLERANCE:
                        score += 0.5
                    elif diff <= 0.10:
                        score += 0.2

            if score > best_score:
                best_score, best_po = score, po

        return best_po if best_score >= MATCH_THRESHOLD else None

    def _compare(self, invoice, po, result: MatchResult) -> MatchResult:
        result.po = po
        checks, score = 0, 0.0

        # Total amount
        checks += 1
        if invoice.total_amount and po.total_amount:
            inv_t, po_t = float(invoice.total_amount), float(po.total_amount)
            diff = abs(inv_t - po_t)
            if diff <= 0.01:
                score += 1.0
            elif po_t > 0 and (diff / po_t) <= AMOUNT_TOLERANCE:
                score += 0.8
            else:
                result.discrepancies.append(
                    f"Total mismatch: Invoice £{inv_t:,.2f} vs PO £{po_t:,.2f}"
                )

        # Supplier name
        checks += 1
        if invoice.supplier_name and po.supplier_name:
            sim = SequenceMatcher(
                None, invoice.supplier_name.lower(), po.supplier_name.lower()
            ).ratio()
            score += sim
            if sim < 0.70:
                result.discrepancies.append(
                    f"Supplier: Invoice '{invoice.supplier_name}' vs PO '{po.supplier_name}'"
                )

        # Line items
        if invoice.lines and po.lines:
            checks += 1
            line_result = self._match_lines(invoice.lines, po.lines)
            result.line_matches = line_result['matches']
            result.discrepancies.extend(line_result['discrepancies'])
            score += line_result['score']

        result.confidence = round(score / checks, 3) if checks else 0.0
        amount_mismatch = any('Total mismatch' in d for d in result.discrepancies)
        result.matched = result.confidence >= MATCH_THRESHOLD and not amount_mismatch
        return result

    def _match_lines(self, inv_lines, po_lines) -> dict:
        matches, discrepancies, matched_count = [], [], 0

        for inv in inv_lines:
            best, best_score = None, 0.0
            for po in po_lines:
                s = 0.0
                if inv.description and po.description:
                    s += SequenceMatcher(
                        None, inv.description.lower(), po.description.lower()
                    ).ratio() * 0.5
                if inv.quantity and po.quantity and float(inv.quantity) == float(po.quantity):
                    s += 0.25
                if inv.unit_price and po.unit_price and abs(
                        float(inv.unit_price) - float(po.unit_price)) <= 0.01:
                    s += 0.25
                if s > best_score:
                    best_score, best = s, po

            if best and best_score >= 0.5:
                matched_count += 1
                matches.append({
                    'invoice_line': inv.line_number,
                    'po_line': best.line_number,
                    'score': round(best_score, 3)
                })
                if inv.quantity and best.quantity and float(inv.quantity) != float(best.quantity):
                    discrepancies.append(
                        f"Line {inv.line_number}: Qty Invoice={inv.quantity} PO={best.quantity}"
                    )
                if inv.unit_price and best.unit_price:
                    if abs(float(inv.unit_price) - float(best.unit_price)) > 0.01:
                        discrepancies.append(
                            f"Line {inv.line_number}: Price Invoice=£{inv.unit_price} PO=£{best.unit_price}"
                        )
            else:
                discrepancies.append(f"Line {inv.line_number} ('{inv.description}'): no PO line matched")

        total = max(len(inv_lines), 1)
        return {'score': matched_count / total, 'matches': matches, 'discrepancies': discrepancies}
