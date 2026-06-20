import os
import ssl

# ── Windows SSL fix ──────────────────────────────────────────────────────────
# On Windows, antivirus / corporate proxies (Sophos, Zscaler, etc.) intercept
# HTTPS and present their own certificates.  Windows trusts them but Python's
# bundled certifi does not.  truststore injects the Windows certificate store
# into Python's ssl module so any cert Windows trusts, Python trusts too.
try:
    import truststore
    truststore.inject_into_ssl()
except ImportError:
    # Fallback to certifi if truststore not available
    try:
        import certifi
        os.environ.setdefault('REQUESTS_CA_BUNDLE', certifi.where())
        os.environ.setdefault('SSL_CERT_FILE',      certifi.where())
    except ImportError:
        pass

from app import create_app

app = create_app()

if __name__ == '__main__':
    from src.config_manager import load_settings
    port = load_settings().get('app', {}).get('port', 5000)
    app.run(host='127.0.0.1', port=port, debug=False)
