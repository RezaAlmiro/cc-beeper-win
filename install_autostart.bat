@echo off
REM Creates a shortcut in the Windows Startup folder so the widget (and
REM its hook server) launch automatically whenever you log in. Run once.
REM To undo: delete the shortcut from shell:startup.

set HERE=%~dp0
set STARTUP_DIR=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup
set SHORTCUT=%STARTUP_DIR%\CC-Beeper-Win.lnk

if not exist "%STARTUP_DIR%" (
    echo Startup folder not found: %STARTUP_DIR%
    exit /b 1
)

powershell -NoProfile -Command ^
  "$s=(New-Object -ComObject WScript.Shell).CreateShortcut('%SHORTCUT%');" ^
  "$s.TargetPath='%HERE%start_all.bat';" ^
  "$s.WorkingDirectory='%HERE%';" ^
  "$s.WindowStyle=7;" ^
  "$s.Description='CC-Beeper-Win — Claude Code monitor widget';" ^
  "$s.Save()"

if %ERRORLEVEL% EQU 0 (
    echo Autostart shortcut created: %SHORTCUT%
    echo cc-beeper-win will launch on login. Delete the shortcut to undo.
) else (
    echo Failed to create shortcut.
    exit /b 1
)
