@echo off
REM CC-Beeper-Win — relaunch just the widget (server keeps running from before).
REM Use this if the widget was closed or crashed and you want it back without
REM restarting the hook server.
cd /d "%~dp0"
start "cc-beeper-win-widget" /B pythonw widget.py
echo cc-beeper-win widget launched.
