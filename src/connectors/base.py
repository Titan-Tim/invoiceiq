"""
Abstract base class that every finance-system connector must implement.
The rest of the application depends only on this interface — never on a
specific connector directly.
"""
from abc import ABC, abstractmethod
from typing import Optional


class BaseConnector(ABC):

    # ------------------------------------------------------------------ #
    # Identity
    # ------------------------------------------------------------------ #

    @property
    @abstractmethod
    def system_name(self) -> str:
        """Human-readable name: 'Sage 50', 'QuickBooks Online', 'Xero'."""

    @property
    def system_key(self) -> str:
        """Short key used in settings: 'sage', 'qbo', 'xero'."""
        return self.system_name.lower().replace(' ', '_')

    # ------------------------------------------------------------------ #
    # Core operations
    # ------------------------------------------------------------------ #

    @abstractmethod
    def test_connection(self) -> tuple[bool, str]:
        """
        Test the connection to the finance system.
        Returns (success: bool, message: str).
        """

    @abstractmethod
    def get_purchase_orders(self) -> list[dict]:
        """
        Return all open purchase orders as a list of dicts.

        Each dict must contain:
          po_number, supplier_name, supplier_ref, po_date,
          expected_delivery, subtotal, vat_amount, total_amount,
          currency, status, source, lines[]

        Each line dict must contain:
          line_number, description, product_code, quantity,
          unit_price, line_total, quantity_invoiced
        """

    @abstractmethod
    def find_vendor(self, supplier_name: str) -> Optional[str]:
        """
        Find a vendor / supplier by name (fuzzy match).
        Returns the vendor reference / ID string, or None if not found.
        """

    @abstractmethod
    def post_invoice(self, invoice_data: dict) -> str:
        """
        Create / post the matched invoice in the finance system.

        invoice_data keys: supplier_ref, invoice_number, invoice_date,
        due_date (optional), po_reference (optional), subtotal,
        vat_amount, total_amount, lines[]

        Returns a transaction / document reference string.
        """

    # ------------------------------------------------------------------ #
    # OAuth support (optional — override for OAuth-based connectors)
    # ------------------------------------------------------------------ #

    def requires_oauth(self) -> bool:
        """Returns True for connectors that use browser-based OAuth 2.0."""
        return False

    def is_authenticated(self) -> bool:
        """
        For OAuth connectors: whether valid tokens are stored.
        Always True for credential-based connectors.
        """
        return True

    def get_auth_url(self, state: str) -> str:
        """
        For OAuth connectors: build and return the provider authorisation URL.
        The `state` parameter should be a random value stored in the session
        to prevent CSRF.
        """
        raise NotImplementedError(f"{self.system_name} does not use OAuth")

    def handle_callback(self, code: str, state: str, **kwargs) -> dict:
        """
        For OAuth connectors: exchange the authorisation code for tokens.
        Persists tokens to disk and returns them.
        """
        raise NotImplementedError(f"{self.system_name} does not use OAuth")

    def disconnect(self):
        """For OAuth connectors: revoke / delete stored tokens."""
        pass

    def get_expense_accounts(self) -> list[dict]:
        """
        Return accounts valid for use as the default expense account when
        posting invoices, as [{'id': ..., 'name': ..., 'type': ...}].
        Override where the finance system supports looking this up;
        defaults to empty (caller falls back to a plain text field).
        """
        return []
