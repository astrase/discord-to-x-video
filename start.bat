@echo off
setlocal
cd /d "%~dp0"
chcp 65001 >nul
set PYTHONUTF8=1
title Discord X Media Bot

echo ============================================================
echo Discord X Media Bot - Start
echo ============================================================

if not exist ".venv\Scripts\python.exe" (
  echo [ERROR] Virtual environment not found. Run setup.bat first.
  pause
  exit /b 1
)
if not exist ".env" (
  echo [ERROR] .env not found. Run setup.bat and configure .env first.
  pause
  exit /b 1
)

".venv\Scripts\python.exe" bot.py
set "BOT_EXIT=%ERRORLEVEL%"
echo.
if not "%BOT_EXIT%"=="0" echo [INFO] Bot exit code: %BOT_EXIT%. Check logs\bot.log.
echo Press any key to close this window.
pause >nul
exit /b %BOT_EXIT%
