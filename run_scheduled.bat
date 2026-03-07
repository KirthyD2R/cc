@echo off
title CC Statement Automation - Dashboard
color 0A

cd /d "d:\Downloads\cc-statement-automation 2\cc-statement-automation"

REM Set UTF-8 encoding for console output
chcp 65001 >nul 2>&1
set PYTHONIOENCODING=utf-8

REM Check Python is available
python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python not found in PATH!
    pause
    exit /b 1
)

REM Ensure output directory exists
if not exist output mkdir output

echo ============================================================
echo   CC STATEMENT AUTOMATION - ZOHO BOOKS
echo   Starting Web Dashboard...
echo ============================================================
echo.

python app.py

echo.
pause
