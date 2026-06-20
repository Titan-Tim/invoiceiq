"""
InvoiceIQ — Windows System Tray Launcher
============================================
Starts the Flask web server in a background thread, opens the dashboard
in the default browser, and provides a system-tray icon with a context
menu.  File extension .pyw so Windows runs it without a console window.

Requirements: pystray, Pillow, all normal app requirements.
"""

import sys
import os
import threading
import webbrowser
import time
import logging
from pathlib import Path

# ── Make sure we can import the app regardless of CWD ──────────────────────
BASE_DIR = Path(__file__).parent.resolve()
os.chdir(BASE_DIR)
sys.path.insert(0, str(BASE_DIR))

# ── Suppress Flask/Werkzeug startup banner ──────────────────────────────────
log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)

import pystray
from PIL import Image as PILImage

# ── Resolve icon path (works both from source and PyInstaller bundle) ────────
def _resource(rel_path: str) -> Path:
    """Return absolute path to a bundled or source resource."""
    if getattr(sys, '_MEIPASS', None):
        return Path(sys._MEIPASS) / rel_path          # PyInstaller bundle
    return BASE_DIR / rel_path


# ── Flask server ─────────────────────────────────────────────────────────────
_flask_started = threading.Event()

def _run_flask():
    """Boot the Flask application in a daemon thread."""
    from src.config_manager import load_settings
    settings = load_settings()
    port = settings.get('app', {}).get('port', 5000)

    from app import create_app
    application = create_app()

    _flask_started.set()
    # use_reloader=False is required when running in a thread
    application.run(host='127.0.0.1', port=port, debug=False, use_reloader=False)


# ── Tray helpers ──────────────────────────────────────────────────────────────
def _open_dashboard(icon, item):
    from src.config_manager import load_settings
    port = load_settings().get('app', {}).get('port', 5000)
    webbrowser.open(f'http://127.0.0.1:{port}/')


def _open_settings(icon, item):
    from src.config_manager import load_settings
    port = load_settings().get('app', {}).get('port', 5000)
    webbrowser.open(f'http://127.0.0.1:{port}/settings')


def _quit_app(icon, item):
    icon.stop()
    # Give the icon time to hide before killing the process
    threading.Timer(0.5, lambda: os._exit(0)).start()


# ── Build tray icon image ────────────────────────────────────────────────────
def _load_icon_image() -> PILImage.Image:
    ico_path = _resource('assets/icon.ico')
    if ico_path.exists():
        try:
            img = PILImage.open(str(ico_path))
            # pystray works best with a plain RGBA/RGB image at 64x64
            img = img.convert('RGBA').resize((64, 64), PILImage.LANCZOS)
            return img
        except Exception:
            pass
    # Fallback: generate a simple blue square with a white "AP" text
    return _make_fallback_icon()


def _make_fallback_icon() -> PILImage.Image:
    from PIL import ImageDraw, ImageFont
    size = 64
    img  = PILImage.new('RGBA', (size, size), (59, 130, 246, 255))
    draw = ImageDraw.Draw(img)
    # White rounded rectangle feel via inset square
    draw.rectangle([4, 4, size - 5, size - 5], outline=(255, 255, 255, 80), width=2)
    try:
        font = ImageFont.truetype('arialbd.ttf', 22)
    except Exception:
        font = ImageFont.load_default()
    text = 'AP'
    bbox = draw.textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    draw.text(((size - tw) // 2, (size - th) // 2 - 2), text, fill=(255, 255, 255), font=font)
    return img


# ── Main ─────────────────────────────────────────────────────────────────────
def main():
    # Start Flask in background
    flask_thread = threading.Thread(target=_run_flask, daemon=True, name='flask-server')
    flask_thread.start()

    # Wait up to 15 s for Flask to be ready before opening the browser
    _flask_started.wait(timeout=15)
    time.sleep(0.4)   # tiny extra pause for first-request readiness

    from src.config_manager import load_settings
    port = load_settings().get('app', {}).get('port', 5000)
    webbrowser.open(f'http://127.0.0.1:{port}/')

    # Build the tray menu
    menu = pystray.Menu(
        pystray.MenuItem('Open Dashboard', _open_dashboard, default=True),
        pystray.MenuItem('Settings',       _open_settings),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem('Quit',           _quit_app),
    )

    icon = pystray.Icon(
        name    = 'InvoiceIQ',
        icon    = _load_icon_image(),
        title   = 'InvoiceIQ',
        menu    = menu,
    )

    icon.run()   # blocks until icon.stop() is called


if __name__ == '__main__':
    main()
