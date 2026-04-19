@echo off
REM CC-Beeper-Win — starts the hook server + widget in the background.
cd /d "%~dp0"
start "cc-beeper-win-server" /B pythonw server\server.py
timeout /t 2 /nobreak >nul
start "cc-beeper-win-widget" /B pythonw widget.py
echo cc-beeper-win server + widget launched.
