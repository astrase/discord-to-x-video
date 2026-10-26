@echo off
setlocal
cd /d "%~dp0"
title Discord X Media Bot Setup

rem Keep this window open so setup errors remain visible.
call "%~dp0setup_main.bat"
set "SETUP_RC=%ERRORLEVEL%"
echo.
if "%SETUP_RC%"=="0" (
  echo Setup finished.
  echo Next: read README_ja.md, then edit config.json and .env.
) else (
  echo Setup failed with exit code %SETUP_RC%.
  echo Check the error messages above and README_ja.md.
)
echo.
pause
exit /b %SETUP_RC%
