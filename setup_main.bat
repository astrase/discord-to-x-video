@echo off
setlocal
cd /d "%~dp0"

echo ============================================================
echo Discord X Media Bot - Setup
echo ============================================================

where py >nul 2>&1
if errorlevel 1 (
  echo [ERROR] Python Launcher py.exe was not found.
  echo Install Python 3.11 64-bit and enable the Python Launcher.
  echo https://www.python.org/downloads/windows/
  goto :fail
)

py -3.11 --version >nul 2>&1
if errorlevel 1 (
  echo [ERROR] Python 3.11 was not found. No other version will be selected automatically.
  echo Installed Python versions:
  py -0p
  echo.
  echo Install Python 3.11 64-bit, then run setup.bat again.
  echo https://www.python.org/downloads/release/python-311/
  goto :fail
)

for /f "tokens=2" %%V in ('py -3.11 --version 2^>^&1') do set "PYVER=%%V"
echo [OK] Using Python %PYVER%.

if not exist ".venv\Scripts\python.exe" (
  echo [1/5] Creating virtual environment...
  py -3.11 -m venv .venv
  if errorlevel 1 goto :fail
) else (
  ".venv\Scripts\python.exe" --version 2^>^&1 | findstr /B /C:"Python 3.11." >nul
  if errorlevel 1 (
    echo [ERROR] Existing .venv does not use Python 3.11.
    echo Delete the .venv folder manually, then run setup.bat again.
    goto :fail
  )
  echo [1/5] Reusing the existing Python 3.11 virtual environment.
)

echo [2/5] Upgrading pip...
".venv\Scripts\python.exe" -m pip install --upgrade pip
if errorlevel 1 goto :fail

echo [3/5] Installing required packages...
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto :fail

echo [3b/5] Installing the recommended yt-dlp nightly build for frequently changing sites...
".venv\Scripts\python.exe" -m pip install --upgrade --pre "yt-dlp[default]"
if errorlevel 1 (
  echo [WARN] yt-dlp nightly install failed. The stable version remains installed.
  echo You can retry by running update.bat.
)

if not exist ".env" (
  copy /Y ".env.example" ".env" >nul
  if errorlevel 1 goto :fail
  echo [4/5] Created .env. Add your Discord bot token later.
) else (
  echo [4/5] Keeping the existing .env file.
)
if not exist "logs" mkdir logs
if not exist "temp" mkdir temp

echo [5/5] Checking Python syntax and unit tests...
".venv\Scripts\python.exe" -m compileall -q bot.py bot_core.py
if errorlevel 1 goto :fail
".venv\Scripts\python.exe" -m unittest discover -s tests -v
if errorlevel 1 goto :fail

echo.
echo [INFO] Checking FFmpeg and FFprobe...
where ffmpeg >nul 2>&1
if errorlevel 1 (
  echo [WARN] FFmpeg is not on PATH. Compression and merging require FFmpeg.
  echo See the FFmpeg setup section in README_ja.md.
) else (
  ffmpeg -version 2>nul | findstr /B /C:"ffmpeg version"
)
where ffprobe >nul 2>&1
if errorlevel 1 echo [WARN] FFprobe is not on PATH. Configure its path in config.json if needed.

echo.
echo Setup tasks completed successfully.
echo Read README_ja.md and configure config.json and .env before running start.bat.
exit /b 0

:fail
echo.
echo [ERROR] Setup failed. Read the error messages above.
echo Check your internet connection and the logs folder if relevant.
exit /b 1
