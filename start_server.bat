@echo off
REM CC-Beeper-Win — start the hook server in the background.
cd /d "%~dp0"
start "cc-beeper-win" /B pythonw server\server.py
echo cc-beeper-win launched. Check server.log for status.
