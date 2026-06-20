@echo off
title InvoiceIQ
cd /d "%~dp0"

echo Starting InvoiceIQ...
echo.
echo When you see "Running on http://127.0.0.1:5000" open your browser and go to:
echo.
echo     http://localhost:5000
echo.
echo Keep this window open while using InvoiceIQ.
echo Close this window to stop InvoiceIQ.
echo.
echo -------------------------------------------------------

"C:\Users\ts\AppData\Local\Microsoft\WindowsApps\PythonSoftwareFoundation.Python.3.12_qbz5n2kfra8p0\python.exe" run.py

pause
