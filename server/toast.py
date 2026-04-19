"""Windows toast notifications using winotify (modern Action Center toasts)."""
from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger("cc-beeper.toast")

try:
    from winotify import Notification, audio
    _HAS_WINOTIFY = True
except ImportError:
    _HAS_WINOTIFY = False
    log.warning("winotify not installed — falling back to console")


APP_ID = "CC-Beeper-Win"
ICON_PATH = Path(__file__).resolve().parents[1] / "assets" / "icon.png"


def notify(title: str, message: str, *, sound: bool = True, launch: str | None = None) -> None:
    """Send a Windows toast. `launch` can be a URL or file path the toast opens on click."""
    if not _HAS_WINOTIFY:
        print(f"[TOAST] {title}: {message}")
        return
    try:
        kwargs = {"app_id": APP_ID, "title": title, "msg": message}
        if ICON_PATH.exists():
            kwargs["icon"] = str(ICON_PATH)
        if launch:
            kwargs["launch"] = launch
        toast = Notification(**kwargs)
        toast.set_audio(audio.Default if sound else audio.Silent, loop=False)
        toast.show()
    except Exception as e:
        log.error("toast failed: %s", e)
        print(f"[TOAST-FALLBACK] {title}: {message}")
