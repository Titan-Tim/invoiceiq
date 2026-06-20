"""
build.py
========
PyInstaller build script for InvoiceIQ.

Usage
-----
  python build.py [--onefile] [--clean]

Flags
-----
  --onefile   Bundle into a single executable (slower startup; default: one-dir)
  --clean     Delete previous build artefacts before building

Output
------
  dist/InvoiceIQ/InvoiceIQ.exe    (one-dir, default)
  dist/InvoiceIQ.exe              (one-file, --onefile)
"""

import subprocess
import sys
import shutil
import argparse
from pathlib import Path

BASE     = Path(__file__).parent.resolve()
DIST     = BASE / 'dist'
BUILD    = BASE / 'build'
ICON_ICO = BASE / 'assets' / 'icon.ico'
SPEC     = BASE / 'InvoiceIQ.spec'


# ── Hidden imports required by runtime dynamic loading ───────────────────────
HIDDEN_IMPORTS = [
    # Connectors — imported via factory at runtime
    'src.connectors.sage_connector',
    'src.connectors.qbo_connector',
    'src.connectors.xero_connector',
    # Sage / win32com
    'win32com',
    'win32com.client',
    'pythoncom',
    'pywintypes',
    # Flask / Werkzeug internals
    'flask',
    'flask_sqlalchemy',
    'werkzeug',
    'werkzeug.serving',
    'werkzeug.debug',
    # SQLAlchemy dialects
    'sqlalchemy.dialects.sqlite',
    # Scheduler
    'apscheduler',
    'apscheduler.schedulers.background',
    'apscheduler.triggers.interval',
    # Anthropic
    'anthropic',
    # MSAL / requests
    'msal',
    'requests',
    'urllib3',
    # QBO
    'intuitlib',
    'intuitlib.client',
    # Tray
    'pystray',
    'pystray._win32',
    # Pillow
    'PIL',
    'PIL.Image',
    'PIL.ImageDraw',
    'PIL.ImageFont',
    # PyMuPDF
    'fitz',
    # Other stdlib / support
    'email',
    'email.mime',
    'email.mime.multipart',
    'email.mime.text',
    'jinja2',
    'jinja2.ext',
    'markupsafe',
    'itsdangerous',
    'click',
]

# ── Data files to include ─────────────────────────────────────────────────────
# Each tuple: (source_glob_or_path, destination_folder_inside_bundle)
DATA_FILES = [
    (str(BASE / 'templates'),           'templates'),
    (str(BASE / 'static'),              'static'),
    (str(BASE / 'assets'),              'assets'),
    (str(BASE / 'config'),              'config'),
    (str(BASE / 'src'),                 'src'),
]


def _ensure_icon():
    """Generate icon if it does not already exist."""
    if not ICON_ICO.exists():
        print('[build] Icon not found — running generate_icon.py …')
        subprocess.run([sys.executable, str(BASE / 'generate_icon.py')], check=True)


def _build(onefile: bool):
    cmd = [
        sys.executable, '-m', 'PyInstaller',
        '--name', 'InvoiceIQ',
        '--icon', str(ICON_ICO),
        '--noconsole',                # no console window
        '--noupx',                    # avoid UPX compression issues with win32
        '--distpath', str(DIST),
        '--workpath', str(BUILD),
        '--specpath', str(BASE),
    ]

    if onefile:
        cmd.append('--onefile')
    else:
        cmd.append('--onedir')

    for hi in HIDDEN_IMPORTS:
        cmd += ['--hidden-import', hi]

    for src, dst in DATA_FILES:
        cmd += ['--add-data', f'{src};{dst}']

    # Entry point
    cmd.append(str(BASE / 'launcher.pyw'))

    print('[build] Running PyInstaller …')
    print('  ' + ' '.join(cmd[:6]) + ' …')
    result = subprocess.run(cmd, cwd=str(BASE))
    if result.returncode != 0:
        print('[build] ERROR: PyInstaller failed.')
        sys.exit(result.returncode)
    print('[build] Build complete.')


def main():
    parser = argparse.ArgumentParser(description='Build InvoiceIQ executable')
    parser.add_argument('--onefile', action='store_true',
                        help='Bundle into a single .exe file')
    parser.add_argument('--clean', action='store_true',
                        help='Remove previous dist/build before building')
    args = parser.parse_args()

    if args.clean:
        for d in (DIST, BUILD, SPEC):
            if Path(str(d)).exists():
                if Path(str(d)).is_dir():
                    shutil.rmtree(d)
                else:
                    Path(str(d)).unlink()
                print(f'[build] Removed {d}')

    _ensure_icon()
    _build(args.onefile)

    print()
    if args.onefile:
        print(f'  Output → {DIST / "InvoiceIQ.exe"}')
    else:
        print(f'  Output → {DIST / "InvoiceIQ" / "InvoiceIQ.exe"}')


if __name__ == '__main__':
    main()
