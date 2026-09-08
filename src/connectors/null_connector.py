"""
NullConnector — the connector used when the finance system is set to "other".

"Other" means the customer's accounting package has no built-in Invoice-IQ
connector yet: it's a bespoke integration Titan has to build before the app can
sync purchase orders or post invoices. Until then this connector deliberately
does nothing harmful — invoice capture, extraction and approval all work as
normal (they don't depend on a connector), but anything that would reach out to
a finance system fails safe with a clear "custom integration pending" message
rather than erroring obscurely or silently pretending to post.
"""
from typing import Optional

from src.connectors.base import BaseConnector

_PENDING_MSG = (
    "This install is set to a custom ('Other') accounting package. "
    "A bespoke integration has to be built by Titan before invoices can be "
    "posted or purchase orders synced. Invoices can still be captured, "
    "extracted and approved in the meantime."
)


class NullConnector(BaseConnector):
    """A safe do-nothing connector for the bespoke ('other') finance system."""

    def __init__(self, settings: dict = None):
        self._settings = settings or {}

    @property
    def system_name(self) -> str:
        return "Other (custom integration)"

    @property
    def system_key(self) -> str:
        return "other"

    def test_connection(self) -> tuple[bool, str]:
        # Not an error state — just not connectable until the integration exists.
        return (False, _PENDING_MSG)

    def get_purchase_orders(self) -> list[dict]:
        # No PO source; returning an empty list keeps PO sync a clean no-op.
        return []

    def find_vendor(self, supplier_name: str) -> Optional[str]:
        return None

    def post_invoice(self, invoice_data: dict) -> str:
        raise NotImplementedError(_PENDING_MSG)
