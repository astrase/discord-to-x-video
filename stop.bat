@echo off
setlocal
cd /d "%~dp0"
> "stop.request" echo stop
echo Stop request created.
echo If the bot is running, it will stop after draining the queue or reaching the configured timeout.
echo If the bot is already stopped, the request will be ignored at next startup.
echo See shutdown_drain_timeout_seconds in config.json.
pause
