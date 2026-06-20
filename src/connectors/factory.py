"""
Returns the correct connector instance for the configured finance system.
All application code should obtain connectors through this module so that
switching systems requires only a settings change.
"""
from src.connectors.base import BaseConnector
from src.config_manager import load_settings

SYSTEM_NAMES = {
    'sage': 'Sage 50',
    'qbo':  'QuickBooks Online',
    'xero': 'Xero',
}


def get_connector(settings: dict = None) -> BaseConnector:
    """Return an initialised connector for the currently configured system."""
    if settings is None:
        settings = load_settings()

    system = settings.get('finance_system', 'sage')

    if system == 'sage':
        from src.connectors.sage_connector import SageConnector
        return SageConnector(settings)
    elif system == 'qbo':
        from src.connectors.qbo_connector import QBOConnector
        return QBOConnector(settings)
    elif system == 'xero':
        from src.connectors.xero_connector import XeroConnector
        return XeroConnector(settings)
    else:
        raise ValueError(
            f"Unknown finance system '{system}'. "
            f"Valid options: {', '.join(SYSTEM_NAMES)}"
        )


def get_system_name(settings: dict = None) -> str:
    """Return the human-readable name of the currently configured system."""
    if settings is None:
        settings = load_settings()
    return SYSTEM_NAMES.get(settings.get('finance_system', 'sage'), 'Unknown')
