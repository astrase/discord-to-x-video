@echo off
setlocal
cd /d "%~dp0"
chcp 65001 >nul
title Discord X Media Bot - Pixiv OAuth
if not exist ".venv\Scripts\python.exe" (
  echo [ERROR] Python virtual environment not found. Run setup.bat first.
  pause
  exit /b 1
)
echo ============================================================
echo Pixiv OAuth for gallery-dl
echo ============================================================
echo This opens gallery-dl's official OAuth flow for your own Pixiv account.
echo Do not share the refresh token with anyone.
echo.
".venv\Scripts\python.exe" -m gallery_dl oauth:pixiv
set "OAUTH_RC=%ERRORLEVEL%"
echo.
if not "%OAUTH_RC%"=="0" (
  echo [ERROR] Pixiv OAuth failed. Check the messages above and README_ja.md.
) else (
  echo Copy the refresh token shown above into .env as PIXIV_REFRESH_TOKEN=YOUR_TOKEN
  echo Do not paste the token into Discord or config.json.
)
echo.
pause
exit /b %OAUTH_RC%
