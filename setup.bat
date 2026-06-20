@echo off
setlocal

echo ============================================================
echo  InvoiceIQ - Developer Setup
echo  SmartIQ Product Family
echo ============================================================
echo.

rem ── Check Python ────────────────────────────────────────────
python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python not found.
    echo Please install Python 3.12 from https://python.org/downloads
    echo Make sure to tick "Add Python to PATH" during installation.
    pause
    exit /b 1
)

for /f "tokens=2 delims= " %%v in ('python --version 2^>^&1') do set PY_VER=%%v
echo [OK] Python %PY_VER% found.
echo.

rem ── Install dependencies ─────────────────────────────────────
echo Installing Python dependencies...
pip install -r requirements.txt
if errorlevel 1 (
    echo [ERROR] pip install failed. Check the error above.
    pause
    exit /b 1
)
echo.

rem ── pywin32 post-install ─────────────────────────────────────
echo Running pywin32 post-install step...
python -m pywin32_postinstall -install 2>nul
echo.

rem ── Create settings.json if missing ──────────────────────────
if not exist "config\settings.json" (
    echo Creating config\settings.json from defaults...
    copy "config\settings.default.json" "config\settings.json" >nul
    echo [NOTE] Open config\settings.json and fill in your credentials.
) else (
    echo [OK] config\settings.json already exists.
)

rem ── Create data directory ─────────────────────────────────────
if not exist "data" mkdir data
if not exist "invoices" mkdir invoices
if not exist "logs" mkdir logs

echo.
echo ============================================================
echo  Setup complete!
echo.
echo  Next steps:
echo    1. Edit config\settings.json with your credentials
echo       (Azure AD, Sage 50, Claude API key)
echo    2. Run:  python run.py
echo    3. Open: http://localhost:5000
echo ============================================================
echo.
pause
