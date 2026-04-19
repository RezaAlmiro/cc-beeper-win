@echo off
REM CC-Beeper-Win — create desktop + Start-Menu shortcuts that launch the
REM widget (and server if needed). After running this, right-click either
REM shortcut -> "Pin to taskbar" to dock it in your taskbar.

set HERE=%~dp0
set DESKTOP=%USERPROFILE%\Desktop
set START_MENU=%APPDATA%\Microsoft\Windows\Start Menu\Programs

REM --- Build a Windows .ico from one of the sprites (done.png) ---
python -c "from PIL import Image; im=Image.open(r'%HERE%assets\done.png'); im.save(r'%HERE%assets\icon.ico', format='ICO', sizes=[(16,16),(32,32),(48,48),(64,64),(128,128),(256,256)])"
if errorlevel 1 (
    echo [warn] Could not generate icon.ico - falling back to no custom icon
)

REM --- Desktop shortcut ---
powershell -NoProfile -Command ^
  "$s=(New-Object -ComObject WScript.Shell).CreateShortcut('%DESKTOP%\CC-Beeper-Win.lnk');" ^
  "$s.TargetPath='%HERE%launcher.pyw';" ^
  "$s.WorkingDirectory='%HERE%';" ^
  "$s.IconLocation='%HERE%assets\icon.ico';" ^
  "$s.Description='CC-Beeper-Win - Claude Code monitor widget';" ^
  "$s.Save()"

REM --- Start Menu shortcut (makes it findable via Windows search + pinnable to taskbar) ---
powershell -NoProfile -Command ^
  "$s=(New-Object -ComObject WScript.Shell).CreateShortcut('%START_MENU%\CC-Beeper-Win.lnk');" ^
  "$s.TargetPath='%HERE%launcher.pyw';" ^
  "$s.WorkingDirectory='%HERE%';" ^
  "$s.IconLocation='%HERE%assets\icon.ico';" ^
  "$s.Description='CC-Beeper-Win - Claude Code monitor widget';" ^
  "$s.Save()"

echo.
echo Shortcuts created:
echo   - %DESKTOP%\CC-Beeper-Win.lnk
echo   - %START_MENU%\CC-Beeper-Win.lnk
echo.
echo To dock it in your taskbar:
echo   right-click either shortcut -^> "Pin to taskbar"
echo.
echo Clicking the shortcut launches the widget. If server + widget are
echo already running, it's a no-op (safe to click as many times as you want).
