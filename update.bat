@echo off
setlocal
cd /d "%~dp0"
chcp 65001 >nul

title Discord X Media Bot Update
if not exist ".venv\Scripts\python.exe" (
  echo [ERROR] Virtual environment not found. Run setup.bat first.
  pause
  exit /b 1
)
echo Updating discord.py, gallery-dl and python-dotenv...
".venv\Scripts\python.exe" -m pip install --upgrade "discord.py>=2.5.2,<3.0" "gallery-dl>=1.32.17" "python-dotenv>=1.0.1"
if errorlevel 1 (
  echo [ERROR] Package update failed. Check your internet connection.
  pause
  exit /b 1
)
echo Updating yt-dlp to its recommended nightly channel...
".venv\Scripts\python.exe" -m pip install --upgrade --pre "yt-dlp[default]"
if errorlevel 1 (
  echo [ERROR] yt-dlp update failed. Check your internet connection and package output.
  pause
  exit /b 1
)
echo.
echo [OK] Packages updated. Restart the bot.
echo If downloads still fail, check logs\bot.log and access to the post.
pause
