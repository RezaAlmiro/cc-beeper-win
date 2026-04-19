"""CC-Beeper-Win — glass HUD widget for Claude Code sessions.

Layout (one session visible at a time, prev/next cycles through):

  ┌──────────────────────────────────────────────────┐
  │ [sprite]  Session title              [● state]   │
  │  64×64    cwd / model                            │
  │                                                   │
  │  42%  ●──────○─────────────           ctx left   │
  │                                                   │
  │ rename   ◀     allow     ▶     /compact          │
  └──────────────────────────────────────────────────┘

Semi-translucent frosted background, rounded corners, soft highlight
at the top edge. Backend (server, hooks, trust store) is unchanged — this
file is just the presentation layer.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import math
import struct
import time
import wave
import requests
from PySide6.QtCore import (
    Qt, QTimer, QPoint, QPropertyAnimation, QEasingCurve, QRect, QSize,
    QEvent, QRectF, Signal,
)
from PySide6.QtGui import (
    QAction, QBrush, QColor, QCursor, QFont, QFontMetrics, QGuiApplication,
    QIcon, QLinearGradient, QMouseEvent, QPainter, QPainterPath, QPen, QPixmap,
    QRadialGradient,
)
from PySide6.QtWidgets import (
    QApplication, QDialog, QFileDialog, QFrame, QHBoxLayout, QInputDialog,
    QLabel, QLineEdit, QMainWindow, QMenu, QMessageBox, QPushButton,
    QScrollArea, QSizePolicy, QSystemTrayIcon, QVBoxLayout, QWidget,
    QGraphicsOpacityEffect, QGraphicsDropShadowEffect, QProgressBar,
)

ROOT = Path(__file__).resolve().parent
ASSETS = ROOT / "assets"
SOUNDS_DIR = ASSETS / "sounds"
CONFIG_PATH = ROOT / "config.json"
PORT_FILE = ROOT / ".port"
POLL_MS = 500
SOUND_COOLDOWN_S = 1.5   # throttle re-plays of the same sound per session

STATE_TO_SPRITE = {
    "snoozing":       "snoozing.png",
    "working":        "working.png",
    "done":           "done.png",
    "awaiting_input": "input.png",
    "error":          "error.png",
    "allow":          "allow.png",
    "input":          "input.png",
    "listening":      "listening.png",
    "recap":          "listening.png",
}
ATTENTION_STATES = {"allow", "input", "error", "awaiting_input"}
STATE_COLOR = {
    "snoozing":       "#9AA3B2",
    "working":        "#ff7a7a",   # red: in progress
    "done":           "#4CD98D",   # green: finished
    "awaiting_input": "#7CE0A8",   # softer green: reply needed
    "error":          "#FF5E5E",
    "allow":          "#FFB74D",   # amber: approval pending
    "input":          "#C084FC",
    "listening":      "#77C9EB",
    "recap":          "#77C9EB",
}

# --------------------------------------------------------------------------
# Glass HUD palette — two variants share the same structural keys.
# ACTIVE is mutated in-place by apply_theme(); paintEvents and CSS builders
# read from it at render time, so a live swap is possible without restart.
# --------------------------------------------------------------------------

CORNER_RADIUS = 22

LIGHT_THEME: dict[str, Any] = {
    "bg_rgba":           (246, 243, 238, 225),
    "highlight_rgba":    (255, 255, 255, 90),
    "border_rgba":       (255, 255, 255, 120),
    "text_title":        "#1F2430",
    "text_subtitle":     "#60667A",
    "text_meta":         "#7A8299",
    "bar_track":         "#d7d2c8",
    "bar_fill":          "#1F2430",
    "btn_bg":            "rgba(255, 255, 255, 150)",
    "btn_bg_hover":      "rgba(255, 255, 255, 220)",
    "btn_border":        "rgba(255, 255, 255, 200)",
    "btn_fg":            "#1F2430",
    "tab_bg":            "rgba(255, 255, 255, 110)",
    "tab_bg_hover":      "rgba(255, 255, 255, 180)",
    "tab_bg_active":     "rgba(255, 255, 255, 245)",
    "tab_border":        "rgba(0, 0, 0, 40)",
    "tab_border_active": "rgba(0, 0, 0, 75)",
    "tab_fg":            "#1F2430",
    "tab_fg_active":     "#0B1020",
    "idle_circle_bg":    "#ffffff",
    "idle_circle_fg":    "#1F2430",
    "idle_circle_border":"#bfc4cc",
    "ticker_fg":         "#1F2430",
    "arc_fg":            "#5a3b00",
    "badge_idle_bg":     "#ffffff",
    "badge_idle_fg":     "#1F2430",
    "hover_outline":     "#1F2430",
}

DARK_THEME: dict[str, Any] = {
    "bg_rgba":           (24, 27, 36, 230),   # deep blue-grey translucent
    "highlight_rgba":    (255, 255, 255, 28),
    "border_rgba":       (255, 255, 255, 40),
    "text_title":        "#F2F5FA",
    "text_subtitle":     "#A8B0BF",
    "text_meta":         "#7A8299",
    "bar_track":         "#2a2f3d",
    "bar_fill":          "#E4E8F2",
    "btn_bg":            "rgba(255, 255, 255, 22)",
    "btn_bg_hover":      "rgba(255, 255, 255, 48)",
    "btn_border":        "rgba(255, 255, 255, 55)",
    "btn_fg":            "#F2F5FA",
    "tab_bg":            "rgba(255, 255, 255, 16)",
    "tab_bg_hover":      "rgba(255, 255, 255, 34)",
    "tab_bg_active":     "rgba(255, 255, 255, 55)",
    "tab_border":        "rgba(255, 255, 255, 45)",
    "tab_border_active": "rgba(255, 255, 255, 100)",
    "tab_fg":            "#E4E8F2",
    "tab_fg_active":     "#FFFFFF",
    "idle_circle_bg":    "#3a3f4e",
    "idle_circle_fg":    "#F2F5FA",
    "idle_circle_border":"#5a6070",
    "ticker_fg":         "#E4E8F2",
    "arc_fg":            "#FFD591",
    "badge_idle_bg":     "#3a3f4e",
    "badge_idle_fg":     "#F2F5FA",
    "hover_outline":     "#E4E8F2",
}

ACTIVE: dict[str, Any] = dict(LIGHT_THEME)


def apply_theme(name: str) -> str:
    """Swap ACTIVE to the requested theme. Returns the theme name actually
    applied (falls back to 'light' if unknown)."""
    name = (name or "light").lower()
    src = DARK_THEME if name == "dark" else LIGHT_THEME
    ACTIVE.clear()
    ACTIVE.update(src)
    return "dark" if src is DARK_THEME else "light"


def load_cfg() -> dict[str, Any]:
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def server_port() -> int:
    try:
        return int(PORT_FILE.read_text().strip())
    except Exception:
        return 19222


def server_url(path: str) -> str:
    return f"http://127.0.0.1:{server_port()}{path}"


def _synth_chime(path: Path, notes: list[tuple[float, float, float]],
                 sample_rate: int = 44100) -> None:
    """Render a simple bell/chime WAV by summing a fundamental + one octave
    overtone with an attack/decay/release envelope. `notes` is a list of
    (frequency Hz, duration s, gain 0..1) played in sequence."""
    frames = bytearray()
    for freq, dur, gain in notes:
        n = int(sample_rate * dur)
        attack = max(1, int(n * 0.015))
        decay = max(1, int(n * 0.22))
        release = max(1, n - attack - decay)
        for i in range(n):
            if i < attack:
                env = i / attack
            elif i < attack + decay:
                env = 1.0 - 0.25 * ((i - attack) / decay)
            else:
                env = 0.75 * math.exp(-3.2 * ((i - attack - decay) / release))
            t = i / sample_rate
            sample = (
                math.sin(2 * math.pi * freq * t)
                + 0.28 * math.sin(2 * math.pi * freq * 2 * t)
                + 0.08 * math.sin(2 * math.pi * freq * 3 * t)
            )
            value = int(32767 * 0.55 * gain * env * sample)
            frames += struct.pack("<h", max(-32768, min(32767, value)))
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(bytes(frames))


# Three melodic cue patterns. All share the E5/G5 shelf so consecutive
# cues feel harmonically consistent.
#   Approve → E5 → C5            (descending ding-dong, attention)
#   Input   → A4 → C#5 → E5      (rising question intonation, gentle)
#   Done    → C5 → E5 → G5       (rising C-major triad, resolved "complete")
APPROVE_NOTES = [(659.25, 0.22, 1.0), (523.25, 0.34, 0.9)]
INPUT_NOTES   = [(440.00, 0.14, 0.8), (554.37, 0.14, 0.85), (659.25, 0.26, 0.9)]
DONE_NOTES    = [(523.25, 0.11, 0.75), (659.25, 0.11, 0.8), (783.99, 0.34, 0.9)]


def ensure_sounds() -> dict[str, Path]:
    """Render the three WAVs into ASSETS/sounds on first run. Returns
    {'approve': path, 'input': path, 'done': path}."""
    out = {
        "approve": SOUNDS_DIR / "approve.wav",
        "input":   SOUNDS_DIR / "input.wav",
        "done":    SOUNDS_DIR / "done.wav",
    }
    try:
        if not out["approve"].exists():
            _synth_chime(out["approve"], APPROVE_NOTES)
        if not out["input"].exists():
            _synth_chime(out["input"], INPUT_NOTES)
        if not out["done"].exists():
            _synth_chime(out["done"], DONE_NOTES)
    except Exception:
        pass
    return out


def fmt_tokens(n: int) -> str:
    if n < 1000:
        return f"{n}"
    if n < 1_000_000:
        return f"{n/1000:.1f}k"
    return f"{n/1_000_000:.2f}M"


def session_label(s: dict[str, Any]) -> str:
    custom = (s.get("custom_name") or "").strip()
    if custom:
        return custom
    ft = (s.get("first_task") or "").strip()
    if ft:
        return ft[:60]
    cwd = (s.get("cwd") or "").replace("\\", "/").rstrip("/")
    if cwd:
        return cwd.rsplit("/", 1)[-1]
    return (s.get("session_id") or "?")[:8]


def session_subtitle(s: dict[str, Any]) -> str:
    cwd = (s.get("cwd") or "").replace("\\", "/").rstrip("/")
    base = cwd.rsplit("/", 1)[-1] if cwd else ""
    stats = s.get("stats") or {}
    model = stats.get("model_label", "")
    bits = [x for x in (base, model) if x]
    return "  ·  ".join(bits)


# ==========================================================================
# Glass background painter
# ==========================================================================

class GlassPanel(QWidget):
    """Paints the frosted-glass background: semi-translucent rounded rect,
    inner top highlight, hairline border."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)

        bg_rgba = ACTIVE["bg_rgba"]
        hl_rgba = ACTIVE["highlight_rgba"]
        br_rgba = ACTIVE["border_rgba"]

        path = QPainterPath()
        path.addRoundedRect(r, CORNER_RADIUS, CORNER_RADIUS)

        # Base translucent fill with a subtle top-to-bottom gradient
        g = QLinearGradient(r.topLeft(), r.bottomLeft())
        g.setColorAt(0.0, QColor(*[c if i < 3 else min(255, bg_rgba[3] + 10) for i, c in enumerate(bg_rgba)]))
        g.setColorAt(1.0, QColor(*bg_rgba))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(g))
        p.drawPath(path)

        # Inner top highlight (light strip 1–6 px below the top border)
        hl_rect = QRectF(r.left() + 4, r.top() + 2, r.width() - 8, 14)
        hl_path = QPainterPath()
        hl_path.addRoundedRect(hl_rect, CORNER_RADIUS - 6, CORNER_RADIUS - 6)
        hg = QLinearGradient(hl_rect.topLeft(), hl_rect.bottomLeft())
        hg.setColorAt(0.0, QColor(*hl_rgba))
        hg.setColorAt(1.0, QColor(255, 255, 255, 0))
        p.setBrush(QBrush(hg))
        p.drawPath(hl_path)

        # Hairline outer border
        p.setPen(QPen(QColor(*br_rgba), 1))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPath(path)


# ==========================================================================
# The big circular state button — custom-painted so we can animate per state
# ==========================================================================

class TickerLine(QWidget):
    """News-ticker-style horizontally scrolling label. Displays a single
    string; if it's wider than the widget, it crawls leftward, wrapping
    seamlessly. Stationary (no animation) when the text already fits."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setFixedHeight(20)
        self._text = ""
        self._scroll_x = 0.0
        self._speed = 1.3       # pixels per tick
        self._timer = QTimer(self)
        self._timer.setInterval(35)   # ~28 fps
        self._timer.timeout.connect(self._tick)

    def setText(self, text: str) -> None:
        text = (text or "").strip()
        if text == self._text:
            return
        self._text = text
        self._scroll_x = 0.0
        self._configure_timer()
        self.update()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._configure_timer()

    def _configure_timer(self):
        if not self._text:
            self._timer.stop()
            return
        fm = self.fontMetrics()
        text_w = fm.horizontalAdvance(self._ticker_string())
        if text_w <= self.width():
            self._timer.stop()
            self._scroll_x = 0.0
        else:
            if not self._timer.isActive():
                self._timer.start()

    def _ticker_string(self) -> str:
        # Separator between repeats so the scroll never shows a flush-join.
        return self._text + "    ·    "

    def _tick(self):
        self._scroll_x -= self._speed
        fm = self.fontMetrics()
        text_w = fm.horizontalAdvance(self._ticker_string())
        if self._scroll_x <= -text_w:
            self._scroll_x += text_w
        self.update()

    def paintEvent(self, event):
        if not self._text:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
        font = QFont("Segoe UI"); font.setPointSize(9); font.setWeight(QFont.Weight.DemiBold)
        p.setFont(font)
        fm = p.fontMetrics()
        y = (self.height() + fm.ascent() - fm.descent()) // 2
        text = self._ticker_string()
        text_w = fm.horizontalAdvance(text)
        p.setPen(QColor(ACTIVE["ticker_fg"]))
        if text_w <= self.width():
            p.drawText(0, y, self._text)
            return
        # Scroll mode — draw two copies side-by-side so wrap-around is seamless
        x = int(self._scroll_x)
        p.drawText(x, y, text)
        p.drawText(x + text_w, y, text)


class ActionCircle(QWidget):
    """Circular state button. Each mode has its own colour and animation:

    - idle     → white, steady
    - done     → green, steady
    - working  → amber, rotating clock-arc around the rim
    - input    → blue, soft pulsing flash
    - approval → amber letter with a pulsing red halo ring outside
    - error    → dark crimson, fast hard flash
    """
    clicked = Signal()

    # (background, letter colour, border) per mode
    _PALETTE = {
        "idle":     ("#ffffff", "#1F2430", "#bfc4cc"),
        "done":     ("#4CD98D", "#062615", "#2fa35a"),
        "working":  ("#FFB74D", "#2a1d00", "#c78a2a"),
        "input":    ("#3DA1FF", "#001830", "#1a6fbf"),
        "approval": ("#FF3B3B", "#ffffff", "#8B0000"),   # bright red body + pulsing red halo
        "error":    ("#8B0000", "#ffeaea", "#5a0000"),
    }

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(52, 52)           # slightly larger to leave room for halo
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._mode = "idle"
        self._letter = "I"
        self._tooltip_text = "Idle"
        self._tick = 0                       # monotonically increments every timer fire
        self._timer = QTimer(self)
        self._timer.setInterval(40)          # 25 fps is plenty for this
        self._timer.timeout.connect(self._on_tick)

    def set_state(self, mode: str, letter: str, tooltip: str) -> None:
        if mode not in self._PALETTE:
            mode = "idle"
        self._mode = mode
        self._letter = letter
        self._tooltip_text = tooltip
        self.setToolTip(tooltip)
        # Start the timer for any animated mode; idle + done are static.
        animated = mode in {"working", "input", "approval", "error"}
        if animated and not self._timer.isActive():
            self._timer.start()
        elif not animated and self._timer.isActive():
            self._timer.stop()
            self._tick = 0
        self.update()

    def _on_tick(self) -> None:
        self._tick = (self._tick + 1) % 100000
        self.update()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
            event.accept()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        if self._mode == "idle":
            bg_hex = ACTIVE["idle_circle_bg"]
            fg_hex = ACTIVE["idle_circle_fg"]
            border_hex = ACTIVE["idle_circle_border"]
        else:
            bg_hex, fg_hex, border_hex = self._PALETTE[self._mode]
        # Circle rect — leave margin for halo / stroke.
        margin = 4
        rect = QRectF(self.rect()).adjusted(margin, margin, -margin, -margin)

        # ---- Halo for APPROVAL (pulsing red ring outside the circle) ----
        if self._mode == "approval":
            # Faster / harder pulse so it reads as urgent
            phase = (math.sin(self._tick * 0.28) + 1) / 2   # 0..1
            ring_alpha = int(80 + 170 * phase)
            for i in range(4, 0, -1):
                pen = QPen(QColor(255, 46, 46, max(20, ring_alpha // i)), i * 2)
                p.setPen(pen); p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawEllipse(rect.adjusted(-i * 2, -i * 2, i * 2, i * 2))

        # ---- Main circle body (with flash/alpha depending on mode) ----
        bg = QColor(bg_hex)
        if self._mode == "error":
            # Fast, sharp flash — ~3× per second
            phase = (math.sin(self._tick * 0.45) + 1) / 2
            bg.setAlphaF(0.55 + 0.45 * phase)
        elif self._mode == "input":
            # Softer, slower blue flash
            phase = (math.sin(self._tick * 0.22) + 1) / 2
            bg.setAlphaF(0.6 + 0.4 * phase)

        p.setBrush(QBrush(bg))
        p.setPen(QPen(QColor(border_hex), 2))
        p.drawEllipse(rect)

        # ---- Working: rotating clock-sweep arc around the inner rim ----
        if self._mode == "working":
            arc_rect = rect.adjusted(4, 4, -4, -4)
            start = (-self._tick * 6) % 360          # negative = clockwise
            span = 70                                  # degrees of arc
            pen = QPen(QColor(ACTIVE["arc_fg"]), 4, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
            p.setPen(pen); p.setBrush(Qt.BrushStyle.NoBrush)
            # Qt angles are in 1/16 of a degree
            p.drawArc(arc_rect, int(start * 16), int(-span * 16))

        # ---- State letter (centered) ----
        font = QFont("Segoe UI")
        font.setWeight(QFont.Weight.Black)
        font.setPointSize(14 if len(self._letter) <= 1 else 11)
        p.setFont(font)
        p.setPen(QPen(QColor(fg_hex)))
        p.drawText(rect, Qt.AlignmentFlag.AlignCenter, self._letter)


# ==========================================================================
# Small icon button with glass-friendly pressed state
# ==========================================================================

def button_css() -> str:
    t = ACTIVE
    return f"""
QPushButton#iconBtn {{
    background: {t['btn_bg']};
    color: {t['btn_fg']};
    border: 1px solid {t['btn_border']};
    border-radius: 18px;
    font-family: 'Segoe UI', sans-serif;
    font-size: 13px; font-weight: 700;
    padding: 4px 10px;
}}
QPushButton#iconBtn:hover {{
    background: {t['btn_bg_hover']};
    border: 1px solid {t['hover_outline']};
}}
QPushButton#iconBtn[accent="red"]   {{ background: #FF5E5E; color: white; border: 1px solid #ff2e2e; }}
QPushButton#iconBtn[accent="green"] {{ background: #4CD98D; color: #062615; border: 1px solid #2fa35a; }}
QPushButton#iconBtn[accent="amber"] {{ background: #FFB74D; color: #2a1d00; border: 1px solid #c78a2a; }}

/* Matching-size circular prev/next arrows. */
QPushButton#arrowCircle {{
    min-width: 36px; max-width: 36px; min-height: 36px; max-height: 36px;
    border-radius: 18px;
    border: 1px solid {t['btn_border']};
    background: {t['btn_bg']};
    color: {t['btn_fg']};
    font-family: 'Segoe UI', sans-serif;
    font-size: 14px; font-weight: 800;
}}
QPushButton#arrowCircle:hover {{ border: 1px solid {t['hover_outline']}; background: {t['btn_bg_hover']}; }}

/* Browser-style session tabs integrated into the top of the glass panel.
   Tabs fill nearly the full height of the strip so labels are legible;
   the active tab body blends into the panel below. Top-edge stripe is
   state-coloured. */
QPushButton.miniTab {{
    background: {t['tab_bg']};
    color: {t['tab_fg']};
    border: 1px solid {t['tab_border']};
    border-bottom: none;
    border-top-left-radius: 10px;
    border-top-right-radius: 10px;
    border-bottom-left-radius: 0;
    border-bottom-right-radius: 0;
    font-family: 'Segoe UI', sans-serif;
    font-size: 12px; font-weight: 600;
    padding: 3px 10px 12px 10px;
    min-height: 28px;
    min-width: 40px;
}}
QPushButton.miniTab:hover {{
    background: {t['tab_bg_hover']};
}}
QPushButton.miniTab[active="true"] {{
    background: {t['tab_bg_active']};
    color: {t['tab_fg_active']};
    border: 1px solid {t['tab_border_active']};
    border-bottom: none;
    font-weight: 800;
    padding: 4px 10px 14px 10px;
}}
/* State-colour stripe on the top edge — 3 px for inactive, 4 px for active.
   Keys mirror the action-circle's state names exactly. */
QPushButton.miniTab[stateKey="idle"]     {{ border-top: 3px solid #9AA3B2; }}
QPushButton.miniTab[stateKey="done"]     {{ border-top: 3px solid #4CD98D; }}
QPushButton.miniTab[stateKey="working"]  {{ border-top: 3px solid #FFB74D; }}
QPushButton.miniTab[stateKey="input"]    {{ border-top: 3px solid #3DA1FF; }}
QPushButton.miniTab[stateKey="approval"] {{ border-top: 3px solid #FF7A00; }}
QPushButton.miniTab[stateKey="error"]    {{ border-top: 3px solid #8B0000; }}
QPushButton.miniTab[active="true"][stateKey="idle"]     {{ border-top: 4px solid #9AA3B2; }}
QPushButton.miniTab[active="true"][stateKey="done"]     {{ border-top: 4px solid #4CD98D; }}
QPushButton.miniTab[active="true"][stateKey="working"]  {{ border-top: 4px solid #FFB74D; }}
QPushButton.miniTab[active="true"][stateKey="input"]    {{ border-top: 4px solid #3DA1FF; }}
QPushButton.miniTab[active="true"][stateKey="approval"] {{ border-top: 4px solid #FF7A00; }}
QPushButton.miniTab[active="true"][stateKey="error"]    {{ border-top: 4px solid #8B0000; }}

/* Playlist / session-picker button on the far left of the tab strip. */
QPushButton#playlistBtn {{
    background: {t['btn_bg']};
    color: {t['btn_fg']};
    border: 1px solid {t['tab_border']};
    border-bottom: none;
    border-top-left-radius: 10px;
    border-top-right-radius: 10px;
    border-bottom-left-radius: 0;
    border-bottom-right-radius: 0;
    font-family: 'Segoe UI', sans-serif;
    font-size: 14px; font-weight: 800;
    padding: 6px 6px;
    min-height: 28px;
    min-width: 26px; max-width: 28px;
}}
QPushButton#playlistBtn:hover {{ background: {t['btn_bg_hover']}; }}

QPushButton#smallBtn {{
    background: transparent; color: {t['btn_fg']};
    border: none; font-family: 'Segoe UI', sans-serif;
    font-size: 12px; font-weight: 600; padding: 2px 6px;
    border-radius: 10px;
}}
QPushButton#smallBtn:hover {{ background: {t['btn_bg_hover']}; }}

QLabel#title {{
    color: {t['text_title']}; font-family: 'Segoe UI', sans-serif;
    font-size: 16px; font-weight: 700;
}}
QLabel#subtitle {{
    color: {t['text_subtitle']}; font-family: 'Segoe UI', sans-serif;
    font-size: 11px; font-weight: 500;
}}
QLabel#meta {{
    color: {t['text_meta']}; font-family: 'Segoe UI', sans-serif;
    font-size: 10px; font-weight: 500;
}}
/* State badge at the top-right of the HUD. Pill-shape. */
QLabel#stateBadge {{
    font-family: 'Segoe UI', sans-serif;
    font-size: 11px; font-weight: 800;
    padding: 4px 10px 4px 10px;
    border-radius: 11px;
    letter-spacing: 0.5px;
}}
QLabel#ctxTime {{
    color: {t['text_title']}; font-family: 'Segoe UI', sans-serif;
    font-size: 11px; font-weight: 600;
}}
QProgressBar#ctxBar {{
    background: {t['bar_track']}; border: none; border-radius: 4px; height: 8px;
    text-align: center; color: transparent;
}}
QProgressBar#ctxBar::chunk {{ background: {t['bar_fill']}; border-radius: 4px; }}
QProgressBar#ctxBar[state="hot"]::chunk  {{ background: #ff8a4d; }}
QProgressBar#ctxBar[state="crit"]::chunk {{ background: #ff2e2e; }}
"""


# Backwards-compatibility shim: dialogs built at import time reference
# BUTTON_CSS. We keep it as a string of the current theme at import time;
# the BeeperWidget re-applies button_css() live on theme change.
BUTTON_CSS = button_css()


# --------------------------------------------------------------------------
# Approval popup (unchanged behaviour from the previous version)
# --------------------------------------------------------------------------

class ApprovalPopup(QWidget):
    def __init__(self, on_resolve) -> None:
        super().__init__()
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setStyleSheet(BUTTON_CSS + """
            QFrame#popupCard {
                background: rgba(248, 246, 242, 245);
                border: 1px solid rgba(0, 0, 0, 40);
                border-radius: 14px;
            }
            QLabel#popupTitle   {
                color: #1F2430; font-family: 'Segoe UI', sans-serif;
                font-size: 13px; font-weight: 800; padding: 0;
            }
            QLabel#popupMeta    {
                color: #60667A; font-family: 'Segoe UI', sans-serif;
                font-size: 11px; font-weight: 600; padding: 0;
            }
            QLabel#popupSummary {
                color: #FA6B2A; font-family: Consolas, 'Courier New', monospace;
                font-size: 11px; padding: 6px 10px;
                background: rgba(250, 107, 42, 25);
                border: 1px solid rgba(250, 107, 42, 80);
                border-radius: 6px;
            }
            QLabel#popupReason  {
                color: #C23A3A; font-family: 'Segoe UI', sans-serif;
                font-size: 11px; font-style: italic; padding: 0;
            }
        """)
        self._on_resolve = on_resolve

        outer = QVBoxLayout(self); outer.setContentsMargins(0, 0, 0, 0)
        self.card = QFrame(self); self.card.setObjectName("popupCard")
        outer.addWidget(self.card)

        v = QVBoxLayout(self.card)
        v.setContentsMargins(14, 12, 14, 12)   # generous outer padding
        v.setSpacing(8)                        # clear gaps between elements

        self.lbl_title = QLabel("Claude Wants To Run:"); self.lbl_title.setObjectName("popupTitle")
        self.lbl_meta = QLabel(""); self.lbl_meta.setObjectName("popupMeta")
        self.lbl_summary = QLabel(""); self.lbl_summary.setObjectName("popupSummary"); self.lbl_summary.setWordWrap(True)
        self.lbl_reason = QLabel(""); self.lbl_reason.setObjectName("popupReason"); self.lbl_reason.setWordWrap(True)

        v.addWidget(self.lbl_title)
        v.addWidget(self.lbl_meta)
        v.addWidget(self.lbl_summary)
        v.addWidget(self.lbl_reason)

        # Divider between info block and action buttons
        divider = QFrame()
        divider.setFrameShape(QFrame.Shape.HLine)
        divider.setStyleSheet("color: rgba(0,0,0,30); background: rgba(0,0,0,30); max-height: 1px;")
        v.addWidget(divider)

        btn_once = QPushButton("✓  Allow Once");                       btn_once.setObjectName("iconBtn")
        btn_sess = QPushButton("✓✓  Allow For This Session");          btn_sess.setObjectName("iconBtn"); btn_sess.setProperty("accent", "green")
        btn_fwd  = QPushButton("✓✓✓  Allow Forever (This Category)");  btn_fwd.setObjectName("iconBtn");  btn_fwd.setProperty("accent", "green")
        btn_deny = QPushButton("✗  Deny");                              btn_deny.setObjectName("iconBtn"); btn_deny.setProperty("accent", "red")
        btn_once.clicked.connect(lambda: on_resolve("allow", "once"))
        btn_sess.clicked.connect(lambda: on_resolve("allow", "session"))
        btn_fwd.clicked.connect(lambda: on_resolve("allow", "persistent"))
        btn_deny.clicked.connect(lambda: on_resolve("deny", "once"))
        for b in (btn_once, btn_sess, btn_fwd, btn_deny):
            b.setMinimumHeight(32)
            v.addWidget(b)

        # Sized to comfortably hold a ~3-line summary + 4-line reason + 4 buttons
        # without crowding.
        self.resize(380, 320)

    def show_for(self, pending: dict[str, Any], anchor_geom) -> None:
        self.lbl_meta.setText(f"{pending.get('tool','?')}  ·  {pending.get('category','?')}")
        self.lbl_summary.setText(pending.get("summary", "")[:600])
        self.lbl_reason.setText(f"why asking: {pending.get('reason','')}" if pending.get("reason") else "")
        scr = QGuiApplication.primaryScreen().availableGeometry()
        target_x = anchor_geom.right() - self.width()
        target_y = anchor_geom.top() - self.height() - 8
        if target_y < scr.top() + 8:
            target_y = anchor_geom.bottom() + 8
        self.move(max(scr.left() + 8, target_x), target_y)
        self.show(); self.raise_()


# --------------------------------------------------------------------------
# Trust + help dialogs (carried over from the previous UI)
# --------------------------------------------------------------------------

SETTINGS_CSS = """
QDialog { background: #f6f3ee; }
QLabel#hdr    { color: #1F2430; font-family: 'Segoe UI', sans-serif; font-size: 14px; font-weight: 700; padding: 8px 4px 4px 4px; }
QLabel#sub    { color: #60667A; font-family: 'Segoe UI', sans-serif; font-size: 11px; padding: 0 4px 6px 4px; }
QLabel#cat    { color: #1F2430; font-family: 'Segoe UI', sans-serif; font-size: 12px; font-weight: 600; }
QLabel#empty  { color: #9AA3B2; font-family: 'Segoe UI', sans-serif; font-size: 11px; font-style: italic; padding: 4px; }
QLabel#helpSection {
    color: #1F2430; font-family: 'Segoe UI', sans-serif; font-size: 12px; padding: 2px 6px;
}
"""


HELP_TEXT = """\
<h2 style="color:#1F2430">CC-Beeper-Win — Help</h2>

<p style="color:#60667A">
A Floating Glass HUD For Your Claude Code Sessions. One Session Is
Displayed At A Time; Use The Arrow Buttons (◀ / ▶) To Browse The Others.
The Widget Never Changes Claude's Output — It Only Gates Permission
Prompts And Gives You Quick Access To Session-Level Controls.
</p>

<h3 style="color:#1F2430">Anatomy Of The Widget</h3>
<ul style="color:#1F2430; line-height:1.6">
  <li><b>Sprite (top-left)</b> — The Pixel-Art Bedroom. Click It To Focus That Session's Terminal Window. If Claude Is Waiting On Tool Permission, Click Opens The Approval Popup Instead.</li>
  <li><b>Title</b> — The Session's Name. Starts As Your First Prompt And You Can Rename It Anytime (Button Or Right-Click). Right-Clicking The Title Also Reveals A Per-Session Slash-Command Menu.</li>
  <li><b>Subtitle</b> — Project Folder  ·  Model (E.g. "Cc-Beeper-Win  ·  Opus 4.7 (1M)").</li>
  <li><b>State Badge (Top-Right)</b> — A Coloured Pill Spelling The Live State ("Working", "Done", "Input", "Approve", "Error", "Idle"). Background Colour Matches The Action Circle So Everything Visually Agrees.</li>
  <li><b>Ticker Line (Between Title And Context Bar)</b> — A Single-Line Glance-At-A-Time Status Digest Tuned For Developers. The Shape Changes With State:
    <ul>
      <li><b>Working</b> → <code>⚙ Edit: Widget.Py · 4 Tools In 1m 23s · Ctx 34%</code></li>
      <li><b>Done</b> → <code>✓ Done — 5 Edits, 3 Bashes, 2 Reads · In 2m 15s · Awaiting Prompt</code></li>
      <li><b>Awaiting Your Reply</b> → <code>💬 Claude Asks: &lt;Short Question&gt;</code></li>
      <li><b>Approval Pending</b> → <code>⏸ Needs Approval: Bash — Curl …</code></li>
      <li><b>Error</b> → <code>✗ &lt;Error Summary&gt;</code></li>
      <li><b>Idle</b> → <code>Ready — Waiting For Your Input</code></li>
    </ul>
    Context Pressure Warnings (<code>⚠ Ctx 87% — /Compact Now</code>) Appear Automatically Past 70 / 85 Percent Thresholds. Scrolls Leftward Only If It Overflows The Widget.
  </li>
  <li><b>Context Bar</b> — Shows How Much Of The Model's Context Window Is In Use. The Left Label Is Percent Used; The Right Label Is Tokens Remaining. Bar Turns Orange Above 60% And Red Above 85%.</li>
  <li><b>Meta Line</b> — Lifetime Totals For This Session: In (Input Tokens), Out (Output Tokens), Cache (Cached Reads).</li>
  <li><b>Action Circle (Centre Bottom)</b> — The Big Round Button Shows A Single-Letter Code Plus A Colour That Mirrors The State Dot. Click Does The Natural Thing For The Current State (Approve Pending, Focus Terminal, Etc.). Flashes On States That Need Your Attention.</li>
  <li><b>◀ / ▶ Arrows</b> — Cycle Through Active Sessions. Size Matches The State Circle So The Bottom Row Feels Balanced.</li>
  <li><b>Session Tabs (Top Of The Panel)</b> — Browser-Style Tabs Integrated Into The Top Edge Of The Glass Body. Each Tab Carries A 3-Pixel State-Coloured Stripe Along Its Top, So You Can See Every Session's Status At A Glance. The Active Tab Lifts Slightly, Has A Thicker Coloured Stripe, And Blends Into The Body Below. Click Any Tab To Switch. <b>Right-Click A Tab</b> For A Per-Tab Menu: Rename, Send Slash Command, Export Stats, Close Tab. New Sessions Appear As New Tabs Automatically; <b>Close Tab Is Sticky</b> — A Dismissed Tab Won't Come Back Just Because Claude Fires Another Tool Call.</li>
  <li><b>☰ Playlist Button (Far Left Of The Tab Strip)</b> — Opens A Menu Listing Every Active Session With Its Coloured State Dot. A Third Way To Switch (Alongside Tabs And ◀ / ▶). Useful When You Have Many Tabs And Want A Compact Scroll-Able List.</li>
  <li><b>⇣ Commands ▾</b> — Dropdown With /compact, /clear, /cost, /model, /resume. Focuses The Session's Terminal And Types The Command.</li>
  <li><b>✎ Rename</b> — Opens A Small Text Dialog To Set A Custom Tab Name.</li>
</ul>

<h3 style="color:#1F2430">State — Badge + Action Circle</h3>
<p style="color:#60667A">The Top-Right Pill Badge And The Big Centre-Bottom Circle Always Agree. The Badge Spells The State In Plain Words; The Circle Adds A Single-Letter Code Plus A Distinctive Animation Per State:</p>
<ul style="color:#1F2430; line-height:1.7">
  <li><b style="background:#ffffff; color:#1F2430; padding:2px 6px; border:1px solid #bfc4cc; border-radius:6px">I</b>  ·  <b>Idle — White</b>, Steady. Session Registered, No Turn Running.</li>
  <li><b style="background:#4CD98D; color:#062615; padding:2px 6px; border-radius:6px">D</b>  ·  <b>Done — Green</b>, Steady. Turn Finished, Ready For Your Next Prompt.</li>
  <li><b style="background:#FFB74D; color:#2a1d00; padding:2px 6px; border-radius:6px">W</b>  ·  <b>Working — Amber</b>, With A Rotating Clock-Sweep Arc Around The Rim. Claude Is Mid-Turn.</li>
  <li><b style="background:#3DA1FF; color:#001830; padding:2px 6px; border-radius:6px">IN</b>  ·  <b>Input Needed — Blue</b>, Flashing Softly. Claude Asked A Follow-Up; Waiting On Your Reply.</li>
  <li><b style="background:#FF3B3B; color:#ffffff; padding:2px 6px; border:2px solid #8B0000; border-radius:6px">A</b>  ·  <b>Approval Pending — Red Body With A Pulsing Red Halo Ring</b>. A Tool Call Needs Your Yes/No. Click The Circle To Open The 4-Way Ladder.</li>
  <li><b style="background:#8B0000; color:#ffeaea; padding:2px 6px; border-radius:6px">E</b>  ·  <b>Error — Dark Red</b>, Fast Hard Flash. The Last Turn Failed.</li>
</ul>

<h3 style="color:#1F2430">The Approval Ladder</h3>
<p style="color:#60667A">When Claude Needs To Run A Tool That Isn't Already Trusted, A Popup Offers Four Choices:</p>
<ul style="color:#1F2430; line-height:1.6">
  <li><b>Allow Once</b> — Single-Shot. Next Identical Call Will Ask Again.</li>
  <li><b>Allow For This Session</b> — Approved Until You Quit The Widget. Use For "Do This Routinely During Today's Work".</li>
  <li><b>Allow Forever (This Category)</b> — Written To Trust.json. Survives Restarts. Use For Categories You Never Want To Be Asked About Again.</li>
  <li><b>Deny</b> — Tool Call Is Blocked And Claude Is Told Why.</li>
</ul>

<h3 style="color:#1F2430">Right-Click Menu (Anywhere On The Widget)</h3>
<p style="color:#60667A">Brings Up Every Settings Control. Same Menu Is Available From The System-Tray Icon.</p>
<ul style="color:#1F2430; line-height:1.6">
  <li><b>Show / Hide Widget</b></li>
  <li><b>Strategy</b> — Who Decides Permissions:
    <ul>
      <li><b>Assist</b> (default) — Widget Is The Permission UI With The 4-Way Ladder.</li>
      <li><b>Observer</b> — Widget Only Watches. Claude's Own Permission Prompt Runs Unchanged In The Terminal.</li>
      <li><b>Auto</b> — Headless Rule Engine + Optional Gemini Check. No Popup.</li>
    </ul>
  </li>
  <li><b>Mode</b> — How Lenient The Auto-Allow Policy Is:
    <ul>
      <li><b>Strict</b> — Ask For Everything Except Pure Reads.</li>
      <li><b>Relaxed</b> (default) — Reads + Git-Read Fly; Writes / Network / MCP-Write Ask.</li>
      <li><b>Trusted</b> — Also Auto-Allows Project Writes And Local Git.</li>
      <li><b>YOLO</b> — Allow Almost Everything (Safety Net Still Hard-Blocks rm -rf /, git push --force, Etc.).</li>
    </ul>
  </li>
  <li><b>Opacity</b> — Presets 60 / 75 / 85 / 95 / 100 % Or Custom Slider. Persists Across Restarts.</li>
  <li><b>Help</b> — This Dialog.</li>
  <li><b>Manage Trust</b> — View And Remove Any "Allow Forever" Or Session-Scoped Categories.</li>
  <li><b>Clear Session Trust</b> — Forget Every Session-Only Approval Immediately.</li>
  <li><b>Quit Widget</b> — Closes The Widget Process. The Hook Server Keeps Running So Your Claude Sessions Are Unaffected.</li>
</ul>

<h3 style="color:#1F2430">Slash Commands (Token Hygiene)</h3>
<p style="color:#60667A">Pick From The <b>⇣ Commands ▾</b> Dropdown Or Right-Click The Title:</p>
<ul style="color:#1F2430; line-height:1.6">
  <li><b>/compact</b> — Claude Summarises The Conversation So Far And Drops The Verbose History. Use Around 60–80 % Context For Minimal Info Loss.</li>
  <li><b>/clear</b> — Reset Conversation. Use Between Unrelated Tasks So Old Context Doesn't Bloat The New One.</li>
  <li><b>/cost</b> — Prints Input / Output / Cache Token Usage For The Session.</li>
  <li><b>/model</b> — Switch Model Mid-Session (E.g. Drop From Opus To Sonnet For Cheaper Runs).</li>
  <li><b>/resume</b> — Pick Up An Earlier Session Instead Of Starting Over.</li>
</ul>
<p style="color:#60667A">The Widget Refuses To Send A Slash Command While The State Is Red — The Keystrokes Would Be Mixed Into Claude's Live Input Stream. Wait For Green.</p>

<h3 style="color:#1F2430">Export Session Stats</h3>
<p style="color:#60667A">At The Bottom Of The Commands Dropdown (Or In The Title Right-Click Menu), <b>Export Session Stats To Txt…</b> Saves A Plain-Text Report For The Currently Selected Tab. The Report Includes:</p>
<ul style="color:#1F2430; line-height:1.6">
  <li>Session Identity (Name, ID, CWD, First Prompt)</li>
  <li>Model + Context Window Size</li>
  <li>Current Context Usage + Remaining</li>
  <li>Lifetime Token Totals (Input / Output / Cache-Read / Cache-Write / Turns)</li>
  <li>Derived Economics — Cache-Hit Rate, Output/Input Ratio, Turns-Per-Percent</li>
  <li>Insights Generated From Your Actual Usage (E.g. "Cache Is Doing Heavy Lifting", "Long-Running Session: 423 Turns")</li>
  <li>Tips Tailored To The Session: When To /compact, Whether Your Cache Is Warm, Whether To Drop To A Cheaper Model</li>
  <li>Current CC-Beeper Settings + Your Trust List</li>
</ul>
<p style="color:#60667A">Saves Anywhere You Like Via A File Dialog. Default Filename Includes The Session Label + Timestamp.</p>

<h3 style="color:#1F2430">Token-Saving Tips (Without Hurting Output)</h3>
<ul style="color:#1F2430; line-height:1.6">
  <li>Watch The Context Bar. Hit <b>/compact</b> Around 60 % And You Rarely See Red.</li>
  <li><b>/clear</b> Between Unrelated Tasks — Don't Carry Setup From Task A Into Task B.</li>
  <li>Cache Reads Cost About 10 % Of A Fresh Read — Huge Cache Numbers In The Meta Line Are Usually Good, Not Waste.</li>
  <li>Drop To A Cheaper Model With <b>/model</b> For Routine Edits; Promote Back To Opus For Hard Reasoning.</li>
</ul>

<h3 style="color:#1F2430">Session Insights</h3>
<p style="color:#60667A">Right-Click → <b>Session Insights…</b> Opens A Dark-Mode Analytics Panel Built From Your Local Transcripts. No Made-Up Progress Bars — Just The Numbers That Actually Describe Your Claude Usage:</p>
<ul style="color:#1F2430; line-height:1.6">
  <li><b>Summary</b> — Current Session (Rolling 5 H), Today, This Week, All-Time; Tokens + Turns + Reset Timing.</li>
  <li><b>When You Work</b> — Auto-Computed <b>Peak Hour</b>, <b>Off-Peak Hours</b> (Lowest Usage Slots Good For Long Background Tasks), And <b>Busiest Day Of Week</b>, Each With A Share Percentage.</li>
  <li><b>Tokens By Hour Of Day</b> — 24-Bar Chart Spanning 00 To 23, Peak Hour Highlighted Orange.</li>
  <li><b>Tokens By Day Of Week</b> — 7-Bar Chart With Busiest Day Highlighted.</li>
  <li><b>Last 14 Days</b> — Daily Trend Bar Chart With Today Highlighted.</li>
  <li><b>Efficiency</b> — Cache Hit Rate (Excellent / Healthy / Low), Output/Input Ratio (Generation vs Exploration Tag), Session Duration Median + Average.</li>
  <li><b>Model Mix (This Week)</b> — Bar Chart Across Opus / Sonnet / Haiku / Other.</li>
  <li><b>Top Projects</b> — All-Time Token Leaderboard Across Your Project Folders.</li>
  <li><b>Open Anthropic Usage Page</b> Button (Top-Right) — One-Click Launch Of <b>claude.ai/settings/usage</b> For Anthropic's Authoritative Plan-Enforced Percentages. Those Live Behind An Authenticated Session We Can't Read Locally.</li>
</ul>

<h3 style="color:#1F2430">Sound Cues</h3>
<p style="color:#60667A">Three Soft Melodic Chimes, Each For A Different State Transition:</p>
<ul style="color:#1F2430; line-height:1.6">
  <li><b>Approve</b> — A Two-Note Descending Ding-Dong (E5 → C5). Plays When A Tool Permission Becomes Pending. Attention-Getting Without Alarming.</li>
  <li><b>Input</b> — A Three-Note Rising Arpeggio (A4 → C♯5 → E5). Plays When Claude Asks You A Follow-Up Question. Softer And "Question-Shaped".</li>
  <li><b>Done</b> — A Three-Note Rising C-Major Triad (C5 → E5 → G5). Plays When A Turn Completes. Satisfying, Conclusive.</li>
</ul>
<p style="color:#60667A">Sounds Are Auto-Generated On First Run (Bell/Chime Synthesis, Saved To <b>assets/sounds/</b>) And Played Via Qt's Non-Blocking Audio. Toggle Via Right-Click → <b>Sound Cues</b>. Setting Persists. Each Cue Is Throttled Per Session So You Won't Get Spammed If A State Flips Rapidly, And Done Doesn't Fire When A Session First Registers — Only When A Real Turn Finishes.</p>

<h3 style="color:#1F2430">Move, Resize, Relaunch</h3>
<ul style="color:#1F2430; line-height:1.6">
  <li><b>Drag The Panel Body</b> → Move The Widget.</li>
  <li><b>Drag Any Edge Or Corner</b> → Resize. Cursor Hints The Direction When You're In The Resize Zone. New Size Persists.</li>
  <li><b>Tray Icon Click</b> → Show / Hide Widget.</li>
  <li><b>If The Widget Crashes Or Is Closed</b>: Double-Click <b>start_widget.bat</b> (Widget Only) Or <b>start_all.bat</b> (Widget + Server). <b>install_launcher.bat</b> Places A Desktop Shortcut You Can Pin To Taskbar.</li>
</ul>

<p style="color:#9AA3B2; font-size:10px; padding-top:8px">
Repo: <a href="https://github.com/RezaAlmiro/cc-beeper-win" style="color:#60667A">github.com/RezaAlmiro/cc-beeper-win</a>
</p>
"""


class HelpDialog(QDialog):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("CC-Beeper-Win — Help")
        self.setWindowFlags(self.windowFlags() | Qt.WindowType.WindowStaysOnTopHint)
        self.setStyleSheet(BUTTON_CSS + SETTINGS_CSS)
        self.resize(560, 620)
        outer = QVBoxLayout(self); outer.setContentsMargins(10, 10, 10, 10); outer.setSpacing(6)
        area = QScrollArea(); area.setWidgetResizable(True); area.setFrameShape(QScrollArea.Shape.NoFrame)
        inner = QWidget()
        inner.setStyleSheet("background: rgba(255, 255, 255, 240); border: 1px solid rgba(0,0,0,30); border-radius: 8px;")
        inner_layout = QVBoxLayout(inner); inner_layout.setContentsMargins(12, 10, 12, 10); inner_layout.setSpacing(0)
        body = QLabel(HELP_TEXT)
        body.setObjectName("helpSection"); body.setTextFormat(Qt.TextFormat.RichText)
        body.setWordWrap(True); body.setOpenExternalLinks(True)
        inner_layout.addWidget(body)
        area.setWidget(inner)
        outer.addWidget(area, stretch=1)
        close_btn = QPushButton("Close"); close_btn.setObjectName("iconBtn")
        close_btn.clicked.connect(self.hide)
        outer.addWidget(close_btn)


USAGE_CSS = """
QDialog#usage { background: #0B0E14; }
QLabel#u_title { color: #F2F5FA; font-family: 'Segoe UI', sans-serif; font-size: 18px; font-weight: 800; }
QLabel#u_section { color: #F2F5FA; font-family: 'Segoe UI', sans-serif; font-size: 13px; font-weight: 800; padding: 14px 0 4px 0; letter-spacing: 0.4px; }
QLabel#u_muted { color: #8A94A6; font-family: 'Segoe UI', sans-serif; font-size: 11px; }
QLabel#u_stat  { color: #F2F5FA; font-family: 'Segoe UI', sans-serif; font-size: 12px; }
QLabel#u_insight { color: #E4E8F2; font-family: 'Segoe UI', sans-serif; font-size: 12px; padding: 3px 0; }
QLabel#u_mono  { color: #C6CCD8; font-family: Consolas, 'Courier New', monospace; font-size: 11px; padding: 1px 0; }
QPushButton#u_btn {
    background: #1F2430;
    color: #F2F5FA;
    border: 1px solid #2c3344;
    border-radius: 6px;
    font-family: 'Segoe UI', sans-serif;
    font-size: 12px; font-weight: 700;
    padding: 6px 12px;
}
QPushButton#u_btn:hover { background: #2c3344; }
QPushButton#u_primary {
    background: #487CFF;
    color: #ffffff;
    border: 1px solid #3768e0;
    border-radius: 6px;
    font-family: 'Segoe UI', sans-serif;
    font-size: 12px; font-weight: 700;
    padding: 6px 12px;
}
QPushButton#u_primary:hover { background: #3768e0; }
"""


class MiniBarChart(QWidget):
    """Simple QPainter bar chart for token-by-hour / by-day-of-week /
    daily-trend histograms. Lightweight: no QtCharts dep needed."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._labels: list[str] = []
        self._values: list[float] = []
        self._highlight: set[int] = set()
        self._title: str = ""
        self._fg = QColor("#487CFF")
        self._hl = QColor("#FFB74D")
        self.setMinimumHeight(110)

    def setData(self, labels: list[str], values: list[float],
                highlight: list[int] | None = None, title: str = "") -> None:
        self._labels = labels
        self._values = values
        self._highlight = set(highlight or [])
        self._title = title
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
        if not self._values:
            p.setPen(QColor("#8A94A6"))
            p.setFont(QFont("Segoe UI", 10))
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "— no data —")
            return

        w, h = self.width(), self.height()
        m_top = 18 if self._title else 6
        m_bot = 18
        m_side = 6
        chart_h = h - m_top - m_bot
        chart_w = w - 2 * m_side

        if self._title:
            p.setPen(QColor("#C6CCD8"))
            p.setFont(QFont("Segoe UI", 9, QFont.Weight.DemiBold))
            p.drawText(m_side, m_top - 4, self._title)

        n = len(self._values)
        gap = 2
        bar_w = max(1.0, (chart_w - (n - 1) * gap) / n)
        max_v = max(self._values) or 1.0

        for i, v in enumerate(self._values):
            x = m_side + i * (bar_w + gap)
            bh = (v / max_v) * (chart_h - 4)
            y = m_top + (chart_h - 4) - bh
            color = self._hl if i in self._highlight else self._fg
            p.setBrush(QBrush(color))
            p.setPen(Qt.PenStyle.NoPen)
            p.drawRoundedRect(int(x), int(y), max(1, int(bar_w)), int(bh), 2, 2)

        # x-axis labels (show every other one if they'd collide)
        p.setFont(QFont("Segoe UI", 7))
        p.setPen(QColor("#8A94A6"))
        fm = p.fontMetrics()
        step = 1
        while step < n and fm.horizontalAdvance(self._labels[0] if self._labels else "") * step * 1.3 > (bar_w + gap) * step * 2:
            step += 1
        for i, lbl in enumerate(self._labels):
            if i % step != 0 and i != n - 1:
                continue
            x = m_side + i * (bar_w + gap) + bar_w / 2
            tw = fm.horizontalAdvance(lbl)
            p.drawText(int(x - tw / 2), h - 4, lbl)


class UsageDialog(QDialog):
    """Dark-mode developer-oriented usage insights. No made-up progress
    bars — just the numbers and charts that actually come out of the
    local transcripts, plus a one-click shortcut to Anthropic's
    authoritative usage page for the plan-enforced percentages."""

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("usage")
        self.setWindowTitle("CC-Beeper-Win — Session Insights")
        self.setWindowFlags(self.windowFlags() | Qt.WindowType.WindowStaysOnTopHint)
        self.setStyleSheet(BUTTON_CSS + SETTINGS_CSS + USAGE_CSS)
        self.resize(720, 700)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(20, 18, 20, 16); outer.setSpacing(0)

        # Header
        title_row = QHBoxLayout()
        title = QLabel("Session Insights"); title.setObjectName("u_title")
        title_row.addWidget(title); title_row.addStretch(1)
        open_page = QPushButton("Open Anthropic Usage Page  ↗")
        open_page.setObjectName("u_primary")
        open_page.setToolTip(
            "Opens claude.ai/settings/usage in your default browser.\n"
            "Anthropic's authoritative plan limits live behind an\n"
            "authenticated session we don't have locally, so this button\n"
            "is the reliable source for exact Session / Weekly percentages."
        )
        open_page.clicked.connect(self._open_anthropic)
        title_row.addWidget(open_page)
        outer.addLayout(title_row)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self._inner = QWidget()
        self._inner.setStyleSheet("background: transparent;")
        self._inner_layout = QVBoxLayout(self._inner)
        self._inner_layout.setContentsMargins(0, 8, 0, 8); self._inner_layout.setSpacing(2)
        self._scroll.setWidget(self._inner)
        outer.addWidget(self._scroll, stretch=1)

        btnrow = QHBoxLayout(); btnrow.setSpacing(8)
        btnrow.addStretch(1)
        reload_btn = QPushButton("↻  Reload"); reload_btn.setObjectName("u_btn")
        reload_btn.clicked.connect(self.refresh)
        close_btn = QPushButton("Close"); close_btn.setObjectName("u_btn")
        close_btn.clicked.connect(self.hide)
        btnrow.addWidget(reload_btn); btnrow.addWidget(close_btn)
        outer.addLayout(btnrow)

    # ----- rendering ----------------------------------------------------

    def showEvent(self, event) -> None:
        self.refresh(); super().showEvent(event)

    def _clear_inner(self) -> None:
        while self._inner_layout.count():
            item = self._inner_layout.takeAt(0)
            w = item.widget() if item else None
            if w is not None:
                w.deleteLater()

    def refresh(self) -> None:
        self._clear_inner()
        try:
            data = requests.get(server_url("/usage"), timeout=10).json()
        except Exception as e:
            err = QLabel(f"Couldn't reach server: {e}")
            err.setStyleSheet("color: #ff7a7a; font-family: Consolas, monospace; padding: 8px;")
            self._inner_layout.addWidget(err); return

        windows = data.get("windows") or {}
        insights = data.get("insights") or {}
        fam_week = data.get("by_family_week") or {}

        # ---- Summary strip (the "how much did I use" numbers) ----
        self._inner_layout.addWidget(self._section_header("Summary"))
        sess = windows.get("last_5h") or {}
        today = windows.get("today") or {}
        week = windows.get("this_week") or {}
        all_t = windows.get("all") or {}
        self._inner_layout.addWidget(self._kv(
            "Current Session (rolling 5 h)",
            f"{self._toks(sess)}  ·  {int(sess.get('turns',0)):,} turns",
            self._time_until(data.get("next_5h_reset"), fallback="rolling 5-hour window"),
        ))
        self._inner_layout.addWidget(self._kv(
            "Today",
            f"{self._toks(today)}  ·  {int(today.get('turns',0)):,} turns",
            f"{int(today.get('sessions',0))} sessions so far",
        ))
        self._inner_layout.addWidget(self._kv(
            "This Week",
            f"{self._toks(week)}  ·  {int(week.get('turns',0)):,} turns",
            self._time_until(data.get("next_week_reset"), fallback="resets Mon 00:00 local"),
        ))
        self._inner_layout.addWidget(self._kv(
            "All-Time",
            f"{self._toks(all_t)}  ·  {int(all_t.get('turns',0)):,} turns",
            f"{int(all_t.get('sessions',0)):,} sessions ever",
        ))

        # ---- Working patterns (peak / off-peak) ----
        self._inner_layout.addWidget(self._section_header("When You Work"))
        peak_h = insights.get("peak_hour")
        peak_s = insights.get("peak_hour_share", 0)
        off = insights.get("off_peak_hours") or []
        dow_names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
        busy_d = insights.get("busiest_dow")
        busy_s = insights.get("busiest_dow_share", 0)
        if peak_h is not None:
            self._inner_layout.addWidget(self._insight(
                f"⏰  Peak Hour: <b>{peak_h:02d}:00–{(peak_h+1)%24:02d}:00</b> "
                f"({peak_s*100:.0f}% of your lifetime tokens)"
            ))
        if off:
            off_txt = ", ".join(f"{h:02d}:00" for h in off)
            self._inner_layout.addWidget(self._insight(
                f"😴  Off-Peak Hours (lowest usage): <b>{off_txt}</b> — "
                f"good slots for long background tasks"
            ))
        if busy_d is not None:
            self._inner_layout.addWidget(self._insight(
                f"📅  Busiest Day: <b>{dow_names[busy_d]}</b> "
                f"({busy_s*100:.0f}% of your lifetime tokens)"
            ))

        # 24-hour chart
        hour_tokens = data.get("hour_tokens") or []
        hour_labels = [f"{h:02d}" for h in range(24)]
        peak_mark = {peak_h} if peak_h is not None else set()
        chart1 = MiniBarChart()
        chart1.setFixedHeight(140)
        chart1.setData(hour_labels, hour_tokens, list(peak_mark),
                       title="Tokens By Hour Of Day (all-time; peak = orange)")
        self._inner_layout.addWidget(chart1)

        # Day-of-week chart
        dow_tokens = data.get("dow_tokens") or []
        busy_mark = {busy_d} if busy_d is not None else set()
        chart2 = MiniBarChart()
        chart2.setFixedHeight(100)
        chart2.setData(dow_names, dow_tokens, list(busy_mark),
                       title="Tokens By Day Of Week (all-time)")
        self._inner_layout.addWidget(chart2)

        # ---- 14-day trend ----
        self._inner_layout.addWidget(self._section_header("Last 14 Days"))
        daily = data.get("daily_series") or []
        d_labels = [d.get("dow", "")[0] for d in daily]  # Mon→M etc
        d_values = [d.get("tokens", 0) for d in daily]
        # Highlight today (last element)
        chart3 = MiniBarChart()
        chart3.setFixedHeight(110)
        chart3.setData(d_labels, d_values, [len(daily) - 1] if daily else [],
                       title="Daily Tokens — Last 14 Days (today = orange)")
        self._inner_layout.addWidget(chart3)

        # ---- Efficiency ----
        self._inner_layout.addWidget(self._section_header("Efficiency"))
        cache_pct = insights.get("cache_hit_pct", 0)
        cache_tag = ("excellent" if cache_pct >= 90 else
                     "healthy" if cache_pct >= 70 else
                     "low — check file churn" if cache_pct < 50 else "okay")
        self._inner_layout.addWidget(self._insight(
            f"🧠  Cache Hit Rate: <b>{cache_pct:.1f}%</b> ({cache_tag})"
        ))
        oi = insights.get("output_input_ratio", 0)
        oi_tag = ("output-heavy (generation)" if oi >= 100 else
                  "balanced" if oi >= 10 else "read-heavy (exploration)")
        self._inner_layout.addWidget(self._insight(
            f"📝  Output / Input Ratio: <b>{oi:,.1f}×</b> ({oi_tag})"
        ))
        avg_s = insights.get("avg_session_minutes", 0)
        med_s = insights.get("median_session_minutes", 0)
        if avg_s:
            self._inner_layout.addWidget(self._insight(
                f"⏳  Session Duration: median <b>{med_s:.0f} min</b>, "
                f"average <b>{avg_s:.0f} min</b>"
            ))

        # ---- Model mix this week ----
        if any(f.get("input", 0) + f.get("output", 0) for f in fam_week.values()):
            self._inner_layout.addWidget(self._section_header("Model Mix (This Week)"))
            labels, values = [], []
            for f in ("opus", "sonnet", "haiku", "other"):
                v = fam_week.get(f, {}).get("input", 0) + fam_week.get(f, {}).get("output", 0)
                if v > 0:
                    labels.append(f.title()); values.append(v)
            if values:
                chart4 = MiniBarChart()
                chart4.setFixedHeight(90)
                chart4.setData(labels, values, [], title="")
                self._inner_layout.addWidget(chart4)

        # ---- Top projects ----
        projects = data.get("by_project") or []
        if projects:
            self._inner_layout.addWidget(self._section_header("Top Projects (All-Time)"))
            for row in projects[:8]:
                name = row.get("project", "?")[:44]
                tokens = row.get("input", 0) + row.get("output", 0)
                turns = row.get("turns", 0)
                sessions = row.get("sessions", 0)
                self._inner_layout.addWidget(self._mono(
                    f"  {name:<44}  {tokens:>12,} tk  {turns:>5,} turns  {sessions:>3} sessions"
                ))

        # ---- Footer ----
        import datetime as _dt
        stamp = _dt.datetime.fromtimestamp(data.get("generated_at", 0)).strftime("%H:%M:%S")
        foot = QLabel(
            f"Last updated: {stamp}  ·  scanned {data.get('files_scanned', 0)} transcripts.  "
            f"Anthropic's plan-enforced percentages live at "
            f"<a href='https://claude.ai/settings/usage' style='color:#7AA8FF'>claude.ai/settings/usage</a>."
        )
        foot.setObjectName("u_muted"); foot.setWordWrap(True)
        foot.setTextFormat(Qt.TextFormat.RichText); foot.setOpenExternalLinks(True)
        foot.setContentsMargins(0, 16, 0, 0)
        self._inner_layout.addWidget(foot)

        self._inner_layout.addStretch(1)

    # ----- formatting helpers ------------------------------------------

    def _toks(self, bucket: dict[str, Any]) -> str:
        n = int(bucket.get("input", 0)) + int(bucket.get("output", 0))
        if n >= 1_000_000:
            return f"{n/1_000_000:.2f}M tokens"
        if n >= 1_000:
            return f"{n/1000:.1f}k tokens"
        return f"{n:,} tokens"

    def _section_header(self, text: str) -> QLabel:
        lbl = QLabel(text.upper()); lbl.setObjectName("u_section")
        return lbl

    def _kv(self, key: str, value: str, sub: str = "") -> QWidget:
        row = QWidget()
        lay = QHBoxLayout(row); lay.setContentsMargins(0, 4, 0, 4); lay.setSpacing(16)
        k = QLabel(key); k.setObjectName("u_stat"); k.setFixedWidth(240)
        v = QLabel(value); v.setObjectName("u_stat")
        v.setStyleSheet("color: #F2F5FA; font-family: 'Segoe UI', sans-serif; font-size: 12px; font-weight: 700;")
        s = QLabel(sub); s.setObjectName("u_muted")
        lay.addWidget(k); lay.addWidget(v, stretch=1); lay.addWidget(s)
        return row

    def _insight(self, html: str) -> QLabel:
        lbl = QLabel(html); lbl.setObjectName("u_insight")
        lbl.setTextFormat(Qt.TextFormat.RichText); lbl.setWordWrap(True)
        return lbl

    def _mono(self, text: str) -> QLabel:
        lbl = QLabel(text); lbl.setObjectName("u_mono")
        lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        return lbl

    def _time_until(self, ts: float | None, *, fallback: str = "") -> str:
        if not ts:
            return fallback
        import datetime as _dt
        target = _dt.datetime.fromtimestamp(ts)
        now = _dt.datetime.now()
        delta = target - now
        if delta.total_seconds() <= 0:
            return fallback or "rolling"
        total_min = int(delta.total_seconds() // 60)
        if total_min < 60:
            return f"resets in {total_min} min"
        hours, minutes = divmod(total_min, 60)
        if hours < 24:
            return f"resets in {hours} hr {minutes} min"
        days = hours // 24
        return f"resets {target.strftime('%a %H:%M')}"

    # ----- actions ------------------------------------------------------

    def _open_anthropic(self) -> None:
        import webbrowser
        try:
            webbrowser.open("https://claude.ai/settings/usage", new=2)
        except Exception:
            pass


class TrustSettings(QDialog):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("CC-Beeper-Win — Trust settings")
        self.setWindowFlags(self.windowFlags() | Qt.WindowType.WindowStaysOnTopHint)
        self.setStyleSheet(BUTTON_CSS + SETTINGS_CSS)
        self.resize(480, 540)
        outer = QVBoxLayout(self); outer.setContentsMargins(10, 10, 10, 10); outer.setSpacing(4)
        outer.addWidget(self._hdr("Persistent trust (survives restart)"))
        outer.addWidget(self._sub("Auto-allowed on every call, every session, across restarts."))
        self.persistent_area = self._scroll(); outer.addWidget(self.persistent_area, stretch=1)
        outer.addWidget(self._hdr("Session trust (this run only)"))
        outer.addWidget(self._sub("Auto-allowed until you quit the widget."))
        self.session_area = self._scroll(); outer.addWidget(self.session_area, stretch=1)
        row = QHBoxLayout(); row.setSpacing(6)
        for label, cb in (
            ("↻  Refresh", self.refresh),
            ("Clear session", self._clear_session),
            ("✗  Clear ALL", self._clear_all),
            ("Close", self.hide),
        ):
            b = QPushButton(label); b.setObjectName("iconBtn")
            if "Clear ALL" in label: b.setProperty("accent", "red")
            b.clicked.connect(cb); row.addWidget(b)
        outer.addLayout(row)

    def _hdr(self, t): lbl = QLabel(t); lbl.setObjectName("hdr"); return lbl
    def _sub(self, t): lbl = QLabel(t); lbl.setObjectName("sub"); lbl.setWordWrap(True); return lbl
    def _scroll(self):
        area = QScrollArea(); area.setWidgetResizable(True); area.setFrameShape(QScrollArea.Shape.NoFrame)
        inner = QWidget()
        inner.setStyleSheet("background: rgba(255,255,255,230); border: 1px solid rgba(0,0,0,20); border-radius: 6px;")
        layout = QVBoxLayout(inner); layout.setContentsMargins(6, 6, 6, 6); layout.setSpacing(4); layout.addStretch()
        area.setWidget(inner)
        return area

    def _populate(self, area, categories):
        layout = area.widget().layout()
        while layout.count() > 1:
            item = layout.takeAt(0)
            w = item.widget() if item else None
            if w is not None: w.deleteLater()
        if not categories:
            empty = QLabel("— none —"); empty.setObjectName("empty")
            layout.insertWidget(layout.count() - 1, empty); return
        for cat in categories:
            row_w = QWidget(); row = QHBoxLayout(row_w); row.setContentsMargins(4, 2, 4, 2); row.setSpacing(6)
            lbl = QLabel(cat); lbl.setObjectName("cat")
            lbl.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
            rm = QPushButton("✗ Remove"); rm.setObjectName("iconBtn"); rm.setProperty("accent", "red")
            rm.clicked.connect(lambda _=False, c=cat: self._remove(c))
            row.addWidget(lbl); row.addWidget(rm)
            layout.insertWidget(layout.count() - 1, row_w)

    def refresh(self):
        try: data = requests.get(server_url("/trust"), timeout=1.5).json()
        except Exception: data = {"session": [], "persistent": []}
        self._populate(self.persistent_area, data.get("persistent", []))
        self._populate(self.session_area, data.get("session", []))

    def showEvent(self, event): self.refresh(); super().showEvent(event)

    def _remove(self, c):
        try: requests.post(server_url("/trust/remove"), json={"category": c}, timeout=2)
        except Exception: pass
        self.refresh()

    def _clear_session(self):
        try:
            for c in (requests.get(server_url("/trust"), timeout=1.5).json() or {}).get("session", []):
                requests.post(server_url("/trust/remove"), json={"category": c}, timeout=1)
        except Exception: pass
        self.refresh()

    def _clear_all(self):
        try:
            d = requests.get(server_url("/trust"), timeout=1.5).json() or {}
            for c in d.get("session", []) + d.get("persistent", []):
                requests.post(server_url("/trust/remove"), json={"category": c}, timeout=1)
        except Exception: pass
        self.refresh()


# ==========================================================================
# The main HUD widget
# ==========================================================================

class BeeperWidget(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.cfg = load_cfg()
        w_cfg = self.cfg.get("widget", {})
        self.w_px = int(w_cfg.get("width", 380))
        self.h_px = int(w_cfg.get("height", 180))

        # Theme — applied before any stylesheet construction so all widgets
        # pick up the correct palette at init time.
        self._theme_name = apply_theme(w_cfg.get("theme", "light"))
        self._theme_actions: dict[str, QAction] = {}
        self._compact_mode = bool(w_cfg.get("compact", False))
        # Height in expanded mode (restored when compact is turned off).
        # Bumped to at least current configured height so users who've
        # resized the widget larger don't lose their sizing.
        self._expanded_height = max(180, int(w_cfg.get("height", 235)))

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setWindowTitle("CC-Beeper-Win")
        self.setStyleSheet(button_css())

        container = QWidget(self)
        container.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setCentralWidget(container)

        # Glass panel background (drawn behind all content)
        self.glass = GlassPanel(container)
        # Drop shadow outside the panel
        shadow = QGraphicsDropShadowEffect(self.glass)
        shadow.setBlurRadius(28); shadow.setOffset(0, 6); shadow.setColor(QColor(0, 0, 0, 90))
        self.glass.setGraphicsEffect(shadow)

        # ---------------- Foreground layout ----------------
        # Root has NO margins: the tab strip hugs the glass top + left
        # edges (its own inner margins handle spacing). The main body is
        # wrapped in a separate inset container that keeps the generous
        # 16 px left/right gutters for sprite / meter / buttons.
        root = QVBoxLayout(container)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # Integrated browser-style tab strip sitting right on top of the
        # glass body. The ☰ playlist button is pinned hard-left (only a
        # thin 2 px inset so the border doesn't touch the glass rim);
        # tabs after it share the remaining width equally via stretch=1.
        self.tabbar = QWidget(container)
        self.tabbar.setFixedHeight(36)
        self.tabbar.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.tabbar_layout = QHBoxLayout(self.tabbar)
        self.tabbar_layout.setContentsMargins(2, 4, 6, 0)
        self.tabbar_layout.setSpacing(2)

        self.btn_playlist = QPushButton("☰", self.tabbar)
        self.btn_playlist.setObjectName("playlistBtn")
        self.btn_playlist.setToolTip("Sessions — click to pick")
        self.btn_playlist.clicked.connect(self._show_playlist_menu)
        self.tabbar_layout.addWidget(self.btn_playlist)
        # NO trailing stretch — tabs themselves carry stretch=1 so they
        # divide the remaining width equally, shrinking as more sessions
        # open (Windows Terminal behaviour).
        root.addWidget(self.tabbar)
        self._tab_buttons: dict[str, QPushButton] = {}

        # Inset container for everything below the tab strip. This is
        # where the generous 16 px side gutters live.
        body_wrap = QWidget(container)
        body_wrap.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        root.addWidget(body_wrap, stretch=1)
        root = QVBoxLayout(body_wrap)
        root.setContentsMargins(16, 4, 16, 12)
        root.setSpacing(8)

        # Top row: sprite + title block + state dot
        top = QHBoxLayout(); top.setSpacing(12)
        self.sprite = QLabel(container)
        self.sprite.setFixedSize(64, 64)
        self.sprite.setStyleSheet("border-radius: 12px; background: transparent;")
        self.sprite.setScaledContents(False)
        self.sprite.setCursor(Qt.CursorShape.PointingHandCursor)
        self.sprite.mousePressEvent = self._on_sprite_click  # type: ignore[assignment]
        top.addWidget(self.sprite)

        titles = QVBoxLayout(); titles.setSpacing(1)
        self.lbl_title = QLabel("— no sessions —"); self.lbl_title.setObjectName("title")
        self.lbl_title.setCursor(Qt.CursorShape.PointingHandCursor)
        self.lbl_title.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.lbl_title.customContextMenuRequested.connect(self._on_title_context_menu)
        self.lbl_subtitle = QLabel(""); self.lbl_subtitle.setObjectName("subtitle")
        titles.addWidget(self.lbl_title)
        titles.addWidget(self.lbl_subtitle)
        titles.addStretch()
        top.addLayout(titles, stretch=1)

        self.state_badge = QLabel(""); self.state_badge.setObjectName("stateBadge")
        self.state_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._set_state_badge("snoozing")
        top.addWidget(self.state_badge, 0, Qt.AlignmentFlag.AlignTop)
        root.addLayout(top)

        # News-ticker line between title and context bar — scrolls Claude's
        # current work (current tool + recent tool history when working;
        # status prose otherwise).
        self.ticker = TickerLine()
        root.addWidget(self.ticker)

        # Middle: context bar + time-style labels (wrapped so compact mode
        # can hide the whole row atomically).
        self.mid_row = QWidget(body_wrap)
        self.mid_row.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        mid = QHBoxLayout(self.mid_row); mid.setContentsMargins(0, 0, 0, 0); mid.setSpacing(8)
        self.lbl_ctx_used = QLabel(""); self.lbl_ctx_used.setObjectName("ctxTime")
        self.lbl_ctx_used.setMinimumWidth(44)
        self.lbl_ctx_left = QLabel(""); self.lbl_ctx_left.setObjectName("ctxTime")
        self.lbl_ctx_left.setMinimumWidth(52); self.lbl_ctx_left.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.ctx_bar = QProgressBar(); self.ctx_bar.setObjectName("ctxBar")
        self.ctx_bar.setMinimum(0); self.ctx_bar.setMaximum(100); self.ctx_bar.setValue(0)
        self.ctx_bar.setFixedHeight(8); self.ctx_bar.setTextVisible(False)
        mid.addWidget(self.lbl_ctx_used)
        mid.addWidget(self.ctx_bar, 1)
        mid.addWidget(self.lbl_ctx_left)
        root.addWidget(self.mid_row)

        # Meta line (tokens in/out/cache)
        self.lbl_meta = QLabel(""); self.lbl_meta.setObjectName("meta")
        self.lbl_meta.setAlignment(Qt.AlignmentFlag.AlignCenter)
        root.addWidget(self.lbl_meta)

        # Bottom controls (wrapped so compact mode can hide the whole row)
        self.btns_row = QWidget(body_wrap)
        self.btns_row.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        btns = QHBoxLayout(self.btns_row); btns.setContentsMargins(0, 0, 0, 0); btns.setSpacing(6)
        self.btn_rename = QPushButton("✎ Rename"); self.btn_rename.setObjectName("smallBtn")
        self.btn_prev   = QPushButton("◀");         self.btn_prev.setObjectName("arrowCircle")
        self.btn_action = ActionCircle();           self.btn_action.clicked.connect(self._on_action_click)
        self.btn_next   = QPushButton("▶");         self.btn_next.setObjectName("arrowCircle")
        # Slash-command dropdown (replaces the single /compact button)
        self.btn_slash  = QPushButton("⇣ Commands ▾"); self.btn_slash.setObjectName("smallBtn")
        slash_menu = QMenu(self)
        for label, cmd in (
            ("/compact  —  Summarise And Shrink Context", "/compact"),
            ("/clear  —  Reset Conversation",             "/clear"),
            ("/cost  —  Show Token Usage",                "/cost"),
            ("/model  —  Switch Model",                   "/model"),
            ("/resume  —  Resume An Earlier Session",     "/resume"),
        ):
            a = QAction(label, self)
            a.triggered.connect(lambda _=False, c=cmd: self._send_cmd(c))
            slash_menu.addAction(a)
        slash_menu.addSeparator()
        export_act = QAction("📄  Export Session Stats To Txt…", self)
        export_act.triggered.connect(self._export_session_stats)
        slash_menu.addAction(export_act)
        self.btn_slash.setMenu(slash_menu)

        self.btn_rename.clicked.connect(self._rename_active)
        self.btn_prev.clicked.connect(lambda: self._cycle_session(-1))
        self.btn_next.clicked.connect(lambda: self._cycle_session(+1))
        btns.addWidget(self.btn_rename)
        btns.addStretch()
        btns.addWidget(self.btn_prev)
        btns.addWidget(self.btn_action)
        btns.addWidget(self.btn_next)
        btns.addStretch()
        btns.addWidget(self.btn_slash)
        root.addWidget(self.btns_row)

        # ---------------- State ----------------
        self._pixmap_cache: dict[str, QPixmap] = {}
        self._sessions: list[dict[str, Any]] = []
        self._active_sid: str | None = None
        self._dragging = False
        self._drag_offset = QPoint(); self._drag_moved = False
        self._resize_margin = 6
        self.setMinimumSize(320, 160)
        self.resize(self.w_px, self.h_px)
        self._dock_corner = w_cfg.get("corner", "bottom-right")
        self._dock_margin = int(w_cfg.get("margin", 16))
        # Suppress position persistence during startup (programmatic moves
        # shouldn't overwrite what the user set last session).
        self._suppress_pos_save = True
        if not self._restore_position(w_cfg):
            self._dock_to_corner()
        self._suppress_pos_save = False

        self._popup = ApprovalPopup(self._resolve)
        self._settings = TrustSettings()
        self._usage = UsageDialog()
        self._help = HelpDialog()
        self._strategy_actions: dict[str, QAction] = {}
        self._mode_actions: dict[str, QAction] = {}
        self._opacity_actions: dict[int, QAction] = {}
        self._current_strategy: str | None = None
        self._current_mode: str | None = None

        # Sound effects for approve / input pings. Generated on first run,
        # played via QSoundEffect so overlap-safe and non-blocking.
        self._sound_enabled = bool(w_cfg.get("sound_enabled", True))
        self._sound_last_state: dict[str, str] = {}     # session_id -> last cue fired
        self._sound_last_time: dict[str, float] = {}    # session_id -> ts
        self._sound_effects: dict[str, Any] = {}
        try:
            from PySide6.QtMultimedia import QSoundEffect
            from PySide6.QtCore import QUrl
            paths = ensure_sounds()
            for name, p in paths.items():
                if not p.exists():
                    continue
                se = QSoundEffect(self)
                se.setSource(QUrl.fromLocalFile(str(p)))
                se.setVolume(0.55)
                self._sound_effects[name] = se
        except Exception as e:
            # QtMultimedia may be unavailable — widget still works, just silent
            self._sound_effects = {}

        self._tray = self._build_tray()

        # Apply saved opacity (default 95%)
        self._apply_opacity(int(w_cfg.get("opacity_pct", 95)))

        # Right-click anywhere on the widget → main menu (tray menu reused).
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._show_main_menu)

        self._size_save_timer = QTimer(self)
        self._size_save_timer.setSingleShot(True); self._size_save_timer.setInterval(400)
        self._size_save_timer.timeout.connect(self._persist_size)

        self._pos_save_timer = QTimer(self)
        self._pos_save_timer.setSingleShot(True); self._pos_save_timer.setInterval(400)
        self._pos_save_timer.timeout.connect(self._persist_position)

        self.setMouseTracking(True)
        container.setMouseTracking(True)
        QApplication.instance().installEventFilter(self)

        self._timer = QTimer(self); self._timer.timeout.connect(self._tick); self._timer.start(POLL_MS)

        # Apply compact mode last so the tray menu's checkmark is already
        # wired. No-op if the user launched in expanded mode.
        if self._compact_mode:
            self._set_compact(True, save=False)

    # -- geometry ---------------------------------------------------------

    def _dock_to_corner(self):
        scr = QGuiApplication.primaryScreen().availableGeometry()
        corner = self._dock_corner; margin = self._dock_margin
        x = scr.right() - self.width() - margin if "right" in corner else scr.left() + margin
        y = scr.bottom() - self.height() - margin if "bottom" in corner else scr.top() + margin
        self.move(x, y)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.glass.setGeometry(self.centralWidget().rect())
        if hasattr(self, "_size_save_timer"):
            self._size_save_timer.start()
        # Re-elide tab labels to their new widths — otherwise a grow-then-
        # shrink keeps the old wider text around till the next session poll.
        if hasattr(self, "_tab_buttons") and self._tab_buttons:
            QTimer.singleShot(0, lambda: self._refresh_tabbar(self._sessions))

    def _persist_size(self):
        try:
            cfg = load_cfg()
            w_cfg = cfg.setdefault("widget", {})
            w_cfg["width"] = int(self.width())
            w_cfg["height"] = int(self.height())
            CONFIG_PATH.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
            self.w_px = self.width(); self.h_px = self.height()
        except Exception:
            pass

    def moveEvent(self, event):
        super().moveEvent(event)
        if getattr(self, "_suppress_pos_save", True):
            return
        if hasattr(self, "_pos_save_timer"):
            self._pos_save_timer.start()

    def _current_screen_name(self) -> str:
        """QScreen.name() — on Windows this is "\\\\.\\DISPLAY1" etc.
        Returns '' if the widget isn't yet on any screen."""
        try:
            wh = self.windowHandle()
            scr = wh.screen() if wh is not None else None
            if scr is None:
                scr = QGuiApplication.screenAt(self.frameGeometry().center())
            return scr.name() if scr is not None else ""
        except Exception:
            return ""

    def _persist_position(self):
        """Save x/y + screen name so the widget reappears on the same
        monitor across restarts. Skipped during programmatic moves."""
        if getattr(self, "_suppress_pos_save", True):
            return
        try:
            cfg = load_cfg()
            w_cfg = cfg.setdefault("widget", {})
            w_cfg["x"] = int(self.x())
            w_cfg["y"] = int(self.y())
            name = self._current_screen_name()
            if name:
                w_cfg["screen"] = name
            CONFIG_PATH.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
        except Exception:
            pass

    def _restore_position(self, w_cfg: dict[str, Any]) -> bool:
        """Try to reattach to the remembered screen + x/y. Returns False
        if the remembered screen is gone or the stored point falls
        outside any currently attached screen (in which case the caller
        should fall back to _dock_to_corner)."""
        if "x" not in w_cfg or "y" not in w_cfg:
            return False
        try:
            x = int(w_cfg["x"]); y = int(w_cfg["y"])
        except (TypeError, ValueError):
            return False
        remembered = str(w_cfg.get("screen") or "")
        # Prefer the remembered screen if still connected.
        target_screen = None
        for s in QGuiApplication.screens():
            if remembered and s.name() == remembered:
                target_screen = s; break
        # Fall back: any screen whose geometry contains the stored point.
        if target_screen is None:
            for s in QGuiApplication.screens():
                if s.geometry().contains(x, y):
                    target_screen = s; break
        if target_screen is None:
            return False
        geom = target_screen.availableGeometry()
        # Clamp within the target screen so we don't spawn off-screen if
        # the monitor's resolution shrank between runs.
        x = max(geom.left(), min(x, geom.right() - max(100, self.width()  // 2)))
        y = max(geom.top(),  min(y, geom.bottom() - max(80,  self.height() // 2)))
        self.move(x, y)
        return True

    # -- state rendering --------------------------------------------------

    def _load_pixmap(self, fname: str) -> QPixmap:
        if fname not in self._pixmap_cache:
            p = ASSETS / fname
            self._pixmap_cache[fname] = QPixmap(str(p)) if p.exists() else QPixmap()
        return self._pixmap_cache[fname]

    def _dpr(self) -> float:
        """Device pixel ratio of the widget's current screen. Used to
        render sprites crisp on 150 / 200 % monitors — Qt upscales
        by ratio automatically, so we need to feed it an image sized
        at `logical * ratio` physical pixels with the ratio tag set."""
        try:
            wh = self.windowHandle()
            if wh is not None and wh.screen() is not None:
                return float(wh.screen().devicePixelRatio())
            scr = QGuiApplication.screenAt(self.frameGeometry().center()) \
                  or QGuiApplication.primaryScreen()
            return float(scr.devicePixelRatio()) if scr is not None else 1.0
        except Exception:
            return 1.0

    def _sprite_pixmap(self, fname: str, size_logical: int) -> QPixmap:
        """Return a DPI-aware pixmap sized for the sprite label.
        Scales to physical pixels, tags the ratio, so Qt renders crisply
        at any system zoom."""
        pm = self._load_pixmap(fname)
        if pm.isNull():
            return pm
        dpr = self._dpr()
        phys = max(1, int(round(size_logical * dpr)))
        scaled = pm.scaled(
            phys, phys,
            Qt.AspectRatioMode.KeepAspectRatioByExpanding,
            Qt.TransformationMode.SmoothTransformation,
        )
        scaled.setDevicePixelRatio(dpr)
        return scaled

    # Maps our internal state string → (display text, bg, fg) for the badge.
    # "snoozing" (idle) is theme-dependent — resolved at paint time.
    _BADGE_PALETTE = {
        "snoozing":       ("IDLE",     None,      None),
        "working":        ("WORKING",  "#FFB74D", "#2a1d00"),
        "done":           ("DONE",     "#4CD98D", "#062615"),
        "awaiting_input": ("INPUT",    "#3DA1FF", "#001830"),
        "allow":          ("APPROVE",  "#FF3B3B", "#ffffff"),
        "error":          ("ERROR",    "#8B0000", "#ffeaea"),
        "input":          ("INPUT",    "#3DA1FF", "#001830"),
        "listening":      ("LISTEN",   "#77C9EB", "#072833"),
        "recap":          ("SPEAK",    "#77C9EB", "#072833"),
    }

    def _set_state_badge(self, state: str, *, has_pending: bool = False):
        # Pending approval always wins over underlying state.
        key = "allow" if has_pending else state
        text, bg, fg = self._BADGE_PALETTE.get(key, ("IDLE", None, None))
        if bg is None or fg is None:
            bg = ACTIVE["badge_idle_bg"]; fg = ACTIVE["badge_idle_fg"]
        border = "#ffffff22" if ACTIVE["bg_rgba"] == DARK_THEME["bg_rgba"] else "#00000022"
        self.state_badge.setText(text)
        self.state_badge.setStyleSheet(
            f"background: {bg}; color: {fg};"
            "font-family: 'Segoe UI', sans-serif;"
            "font-size: 11px; font-weight: 800;"
            "padding: 4px 11px;"
            "border-radius: 11px;"
            f"border: 1px solid {border};"
            "letter-spacing: 0.6px;"
        )
        self.state_badge.setToolTip(key)

    def _active_session(self) -> dict[str, Any] | None:
        if not self._sessions:
            return None
        for s in self._sessions:
            if s["session_id"] == self._active_sid:
                return s
        self._active_sid = self._sessions[0]["session_id"]
        return self._sessions[0]

    def _tick(self):
        try:
            r = requests.get(server_url("/sessions"), timeout=0.8)
            data = r.json() if r.status_code == 200 else {}
        except requests.RequestException:
            data = {}
        self._sync_menu_selections(data.get("strategy"), data.get("mode"))
        self._sessions = data.get("sessions", []) or []
        self._refresh_tabbar(self._sessions)
        self._fire_sound_cues()

        s = self._active_session()
        if not s:
            self._render_empty(data.get("strategy"), data.get("mode"))
            return
        self._render_session(s)

    def _fire_sound_cues(self) -> None:
        """For each session, play a ping when the attention state enters
        approval or input (not on every poll — only on transition)."""
        if not self._sound_enabled or not self._sound_effects:
            return
        now = time.time()
        live_ids: set[str] = set()
        for s in self._sessions:
            sid = s["session_id"]
            live_ids.add(sid)
            key = self._state_key(s)
            prev = self._sound_last_state.get(sid)
            cue: str | None = None
            if key == "approval" and prev != "approval":
                cue = "approve"
            elif key == "input" and prev != "input":
                cue = "input"
            elif key == "done" and prev not in (None, "done"):
                # Only ping Done when a real turn finishes, not on first
                # registration (prev==None means we just saw this session).
                cue = "done"
            if cue:
                last_t = self._sound_last_time.get(sid + ":" + cue, 0)
                if now - last_t >= SOUND_COOLDOWN_S:
                    se = self._sound_effects.get(cue)
                    try:
                        if se is not None:
                            se.play()
                        self._sound_last_time[sid + ":" + cue] = now
                    except Exception:
                        pass
            self._sound_last_state[sid] = key
        # Drop state for sessions that no longer exist
        for gone in list(self._sound_last_state.keys()):
            if gone not in live_ids:
                self._sound_last_state.pop(gone, None)

    # State mapping used by both the tabs and the action circle.
    def _state_key(self, s: dict[str, Any]) -> str:
        if s.get("pending"):     return "approval"
        state = s.get("state", "snoozing")
        if state == "awaiting_input": return "input"
        if state == "working":         return "working"
        if state == "done":            return "done"
        if state == "error":           return "error"
        return "idle"

    def _refresh_tabbar(self, sessions: list[dict[str, Any]]) -> None:
        """Keep browser-style tabs in sync with the server session list."""
        current_ids = {s["session_id"] for s in sessions}
        # Drop tabs for gone sessions
        for sid in list(self._tab_buttons.keys()):
            if sid not in current_ids:
                btn = self._tab_buttons.pop(sid)
                self.tabbar_layout.removeWidget(btn)
                btn.deleteLater()
        # Add new tabs / refresh label + active marker
        for s in sessions:
            sid = s["session_id"]
            btn = self._tab_buttons.get(sid)
            if btn is None:
                btn = QPushButton(self.tabbar)
                btn.setProperty("class", "miniTab")
                # Expanding size policy + stretch=1 in the layout below
                # → every tab takes an equal slice of the strip's width.
                btn.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
                btn.clicked.connect(lambda _=False, x=sid: self._select_session(x))
                btn.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
                btn.customContextMenuRequested.connect(
                    lambda pos, x=sid: self._tab_context_menu(x, pos)
                )
                # No trailing stretch in the layout — append at the end
                # with stretch=1 so remaining space is split equally
                # across all tabs.
                self.tabbar_layout.addWidget(btn, 1)
                self._tab_buttons[sid] = btn
            btn.setProperty("stateKey", self._state_key(s))
            btn.setProperty("active", sid == self._active_sid)
            btn.style().unpolish(btn); btn.style().polish(btn)
            # Elide the label to whatever width this tab has been
            # allocated right now, so long session names don't spill and
            # short ones don't look lost in whitespace.
            full_label = session_label(s)
            try:
                from PySide6.QtGui import QFontMetrics
                fm = QFontMetrics(btn.font())
                inner_w = max(28, btn.width() - 28)  # leave room for padding + border
                btn.setText(fm.elidedText(full_label, Qt.TextElideMode.ElideRight, inner_w))
            except Exception:
                btn.setText(full_label[:22])
            btn.setToolTip(
                f"session: {sid[:8]}\n"
                f"state:   {s.get('state','?')}\n"
                f"cwd:     {s.get('cwd','')}\n"
                f"topic:   {full_label[:200]}\n"
                f"click to switch"
            )

    def _show_playlist_menu(self) -> None:
        """Drop-down menu listing every active session. Each row shows a
        coloured state dot + label + (active) marker. Click to switch."""
        menu = QMenu(self)
        if not self._sessions:
            a = QAction("— No Active Sessions —", self)
            a.setEnabled(False); menu.addAction(a)
        for s in self._sessions:
            sid = s["session_id"]
            label = session_label(s)[:48]
            state_key = self._state_key(s)
            state_color = STATE_COLOR.get(s.get("state", "snoozing"), "#9AA3B2")
            active_marker = "  ←  active" if sid == self._active_sid else ""
            # Draw a tiny coloured square as an icon
            pm = QPixmap(12, 12); pm.fill(Qt.GlobalColor.transparent)
            painter = QPainter(pm); painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setBrush(QBrush(QColor(state_color))); painter.setPen(Qt.PenStyle.NoPen)
            painter.drawEllipse(1, 1, 10, 10); painter.end()
            act = QAction(QIcon(pm), f"{label}{active_marker}", self)
            act.triggered.connect(lambda _=False, x=sid: self._select_session(x))
            menu.addAction(act)
        menu.exec_(self.btn_playlist.mapToGlobal(
            QPoint(0, self.btn_playlist.height())
        ))

    def _select_session(self, sid: str) -> None:
        if sid not in {s["session_id"] for s in self._sessions}:
            return
        self._active_sid = sid
        # Immediate re-render without waiting for next poll
        s = next((x for x in self._sessions if x["session_id"] == sid), None)
        if s: self._render_session(s)
        self._refresh_tabbar(self._sessions)

    # -- per-tab context menu (right-click a tab) -------------------------

    def _tab_context_menu(self, sid: str, pos) -> None:
        """Right-click on a tab: rename, send slash commands, close."""
        snap = next((s for s in self._sessions if s["session_id"] == sid), None)
        if snap is None:
            return
        menu = QMenu(self)
        rename = QAction("Rename Tab…", self)
        rename.triggered.connect(lambda: self._rename_session(sid))
        menu.addAction(rename)

        cmd_menu = menu.addMenu("Send Slash Command")
        for label, cmd in (
            ("/compact",  "/compact"),
            ("/clear",    "/clear"),
            ("/cost",     "/cost"),
            ("/model",    "/model"),
            ("/resume",   "/resume"),
        ):
            a = QAction(label, self)
            a.triggered.connect(lambda _=False, c=cmd, x=sid: self._send_cmd_for(x, c))
            cmd_menu.addAction(a)

        ex = QAction("📄  Export This Session's Stats To Txt…", self)
        ex.triggered.connect(lambda: self._export_session_stats(sid=sid))
        menu.addAction(ex)

        menu.addSeparator()
        close = QAction("✗  Close Tab", self)
        close.triggered.connect(lambda: self._close_tab(sid))
        menu.addAction(close)

        btn = self._tab_buttons.get(sid)
        if btn is not None:
            menu.exec_(btn.mapToGlobal(pos))

    def _rename_session(self, sid: str) -> None:
        snap = next((s for s in self._sessions if s["session_id"] == sid), None)
        if snap is None:
            return
        current = (snap.get("custom_name") or "").strip()
        name, ok = QInputDialog.getText(
            self, "Rename Session",
            f"Display Name For\n{session_label(snap)[:60]}:",
            QLineEdit.EchoMode.Normal, current,
        )
        if not ok:
            return
        try:
            requests.post(server_url("/session/name"),
                          json={"session_id": sid, "name": name.strip()}, timeout=2)
        except Exception:
            pass

    def _send_cmd_for(self, sid: str, cmd: str) -> None:
        """Variant of _send_cmd that lets a right-click menu target a
        non-active tab. Switches focus to that session first."""
        if sid != self._active_sid:
            self._select_session(sid)
        self._send_cmd(cmd)

    def _close_tab(self, sid: str) -> None:
        """Dismiss the session from the widget permanently. Future hook
        payloads from this session_id are silently dropped by the server
        so the tab stays gone even if the underlying Claude keeps
        running. Dismissal is cleared when the hook server restarts."""
        snap = next((s for s in self._sessions if s["session_id"] == sid), None)
        name = session_label(snap) if snap else sid[:8]
        reply = QMessageBox.question(
            self, "Close Tab",
            f"Stop showing this tab?\n\n  {name}\n\n"
            "The underlying Claude Code session isn't killed — it keeps "
            "running in your terminal. It just won't reappear on the "
            "widget, even on new tool calls. (Reopens if you restart the "
            "hook server, or the session naturally ends.)",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Yes,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        try:
            requests.post(server_url("/session/dismiss"),
                          json={"session_id": sid}, timeout=2)
        except Exception:
            pass
        # Optimistic local cleanup — the next poll will reconcile too.
        btn = self._tab_buttons.pop(sid, None)
        if btn is not None:
            self.tabbar_layout.removeWidget(btn)
            btn.deleteLater()
        if self._active_sid == sid:
            self._active_sid = None

    def _render_empty(self, strategy, mode):
        self.lbl_title.setText("— No Active Sessions —")
        self.lbl_subtitle.setText(f"Strategy {(strategy or '?').title()}   ·   Mode {(mode or '?').title()}")
        self._set_state_badge("snoozing")
        self.ctx_bar.setValue(0); self.lbl_ctx_used.setText(""); self.lbl_ctx_left.setText("")
        self.lbl_meta.setText("Open A Claude Code Session And Its Tab Will Appear Here")
        self.ticker.setText("")
        self.btn_action.set_state("idle", "I", "Idle — No Active Sessions")
        sprite_px = 36 if self._compact_mode else 64
        pm = self._sprite_pixmap("snoozing.png", sprite_px)
        if not pm.isNull():
            self.sprite.setPixmap(pm)


    def _render_session(self, s):
        state = s.get("state", "snoozing")
        label = session_label(s)
        subtitle = session_subtitle(s)
        self.lbl_title.setText(label)
        self.lbl_title.setToolTip(
            f"session: {s.get('session_id','')[:8]}\n"
            f"cwd: {s.get('cwd','')}\n"
            f"right-click to rename / send slash command"
        )
        self.lbl_subtitle.setText(subtitle)
        pending = s.get("pending") or []
        has_pending = bool(pending)
        self._set_state_badge(state, has_pending=has_pending)

        # Ticker content — assembles a running narrative of the turn:
        # numbered step list with ✓ for done / ⏳ for in-flight, plus
        # Claude's latest prose (pulled from the transcript). When there's
        # nothing running we fall back to quiet status prose.
        ticker_text = self._build_ticker_text(s, state, has_pending, pending)
        self.ticker.setText(ticker_text)

        # Action circle — letter + state; the ActionCircle class drives
        # its own animation based on mode.
        if has_pending:
            self.btn_action.set_state("approval", "A",
                "Approval Pending — tool wants permission (red body + pulsing red halo)")
        elif state == "awaiting_input":
            self.btn_action.set_state("input", "IN",
                "Input Needed — Claude is waiting on your reply (flashing blue)")
        elif state == "working":
            self.btn_action.set_state("working", "W",
                "Working — Claude is mid-turn (amber, rotating clock arc)")
        elif state == "done":
            self.btn_action.set_state("done", "D",
                "Done — turn finished, ready for next prompt (green)")
        elif state == "error":
            self.btn_action.set_state("error", "E",
                "Error — last turn failed (dark red, fast flash)")
        else:
            self.btn_action.set_state("idle", "I",
                "Idle — no active turn (white)")

        # Context bar + labels
        stats = s.get("stats") or {}
        pct = float(stats.get("context_pct", 0) or 0)
        cur = int(stats.get("current_context", 0) or 0)
        lim = int(stats.get("context_limit", 0) or 0)
        self.ctx_bar.setValue(int(min(100, pct)))
        if pct > 85:   self.ctx_bar.setProperty("state", "crit")
        elif pct > 60: self.ctx_bar.setProperty("state", "hot")
        else:           self.ctx_bar.setProperty("state", "")
        self.ctx_bar.style().unpolish(self.ctx_bar); self.ctx_bar.style().polish(self.ctx_bar)
        self.lbl_ctx_used.setText(f"{pct:.0f}%")
        if lim:
            left = max(0, lim - cur)
            self.lbl_ctx_left.setText(f"-{fmt_tokens(left)}")
        else:
            self.lbl_ctx_left.setText("")

        self.lbl_meta.setText(
            f"In {fmt_tokens(stats.get('total_input', 0))}   ·   "
            f"Out {fmt_tokens(stats.get('total_output', 0))}   ·   "
            f"Cache {fmt_tokens(stats.get('total_cache_read', 0))}"
        )

        # Sprite (DPI-aware)
        fname = STATE_TO_SPRITE.get(state, "snoozing.png")
        sprite_px = 36 if self._compact_mode else 64
        pm = self._sprite_pixmap(fname, sprite_px)
        if not pm.isNull():
            self.sprite.setPixmap(pm)

        if has_pending and self._popup.isVisible():
            self._popup.show_for(pending[0], self.frameGeometry())
        if not has_pending and self._popup.isVisible():
            self._popup.hide()

    # -- actions ----------------------------------------------------------

    @staticmethod
    def _ticker_short(text: str, max_chars: int = 60) -> str:
        """Collapse whitespace + clip at a word boundary with an ellipsis."""
        text = " ".join((text or "").split())
        if len(text) <= max_chars:
            return text
        clipped = text[:max_chars]
        sp = clipped.rfind(" ")
        if sp >= 16:
            clipped = clipped[:sp]
        return clipped.rstrip(",.;:—-") + "…"

    @staticmethod
    def _ticker_elapsed(start_ts: float | None, end_ts: float | None = None) -> str:
        """Human-friendly 's' / 'Xm Ys' / 'Xh Ym'."""
        if not start_ts:
            return ""
        import time as _t
        end = end_ts if end_ts else _t.time()
        sec = max(0, int(end - start_ts))
        if sec < 60:    return f"{sec}s"
        if sec < 3600:  return f"{sec // 60}m {sec % 60:02d}s"
        return f"{sec // 3600}h {(sec % 3600) // 60:02d}m"

    def _build_ticker_text(self, s: dict[str, Any], state: str,
                           has_pending: bool, pending: list) -> str:
        """A single-line digest tuned for glance-ability. Shape varies by
        state but always answers 'what's happening NOW' in ≤ 1 line:

            working  → ⚙ Edit: widget.py · 4 tools in 1m 23s · ctx 34%
            done     → ✓ Done in 2m 15s · 5 edits, 3 bash · awaiting prompt
            awaiting → 💬 Claude asks: <short question>
            approval → ⏸ Needs approval: Bash — curl …
            error    → ✗ <error summary>
            idle     → Ready — waiting for input
        """
        stats = s.get("stats") or {}
        ctx_pct = float(stats.get("context_pct", 0) or 0)

        if has_pending:
            p = pending[0]
            tool = p.get("tool") or "?"
            summary = self._ticker_short(p.get("summary") or "", 80)
            return f"⏸ Needs Approval: {tool}" + (f" — {summary}" if summary else "")

        if state == "awaiting_input":
            msg = self._ticker_short(s.get("message") or "Claude Asked A Follow-Up", 140)
            return f"💬 Claude Asks: {msg}"

        if state == "error":
            msg = self._ticker_short(s.get("message") or "Last turn failed", 160)
            return f"✗ {msg}"

        steps = s.get("current_turn_steps") or []
        narrative = self._ticker_short(s.get("narrative") or "", 80)
        start_ts = s.get("turn_start_ts")
        stopped = s.get("stopped_at")

        if state == "working":
            # Pick ONE focus line: the in-flight tool if any, else Claude's
            # latest thought, else a generic placeholder.
            current = None
            done_count = 0
            for step in steps:
                st = step.get("status")
                if st == "running" and current is None:
                    current = step
                elif st == "done":
                    done_count += 1
            if current:
                tool = current.get("tool") or "?"
                summary = self._ticker_short(current.get("summary") or "", 60)
                core = f"⚙ {tool}" + (f": {summary}" if summary else "")
            elif narrative:
                core = f"⚙ \u201C{narrative}\u201D"
            else:
                core = "⚙ Thinking…"

            parts = [core]
            elapsed = self._ticker_elapsed(start_ts)
            total = len(steps)
            if total >= 2 and elapsed:
                parts.append(f"{total} tool{'s' if total != 1 else ''} in {elapsed}")
            elif elapsed:
                parts.append(elapsed)

            # Context pressure warning — only when it starts to matter.
            if ctx_pct >= 85:
                parts.append(f"⚠ ctx {ctx_pct:.0f}% — /compact now")
            elif ctx_pct >= 70:
                parts.append(f"ctx {ctx_pct:.0f}% — /compact soon")

            return "  ·  ".join(parts)

        if state == "done":
            parts: list[str] = []
            elapsed = self._ticker_elapsed(start_ts, stopped)
            if steps:
                # Group tools by name so the summary is compact
                counts: dict[str, int] = {}
                for st in steps:
                    t = st.get("tool") or "?"
                    counts[t] = counts.get(t, 0) + 1
                # keep the top 3 most-used tools
                top = sorted(counts.items(), key=lambda kv: -kv[1])[:3]
                summary = ", ".join(
                    f"{c} {t.lower()}{'s' if c > 1 else ''}" for t, c in top
                )
                parts.append(f"✓ Done — {summary}")
            else:
                parts.append("✓ Done")
            if elapsed:
                parts.append(f"in {elapsed}")
            if ctx_pct >= 70:
                parts.append(f"ctx {ctx_pct:.0f}%")
            parts.append("awaiting prompt")
            return "  ·  ".join(parts)

        # idle / snoozing
        return "Ready — Waiting For Your Input"

    def _cycle_session(self, delta: int):
        if not self._sessions:
            return
        ids = [s["session_id"] for s in self._sessions]
        try: idx = ids.index(self._active_sid)
        except ValueError: idx = 0
        idx = (idx + delta) % len(ids)
        self._active_sid = ids[idx]
        self._render_session(self._sessions[idx])

    def _on_sprite_click(self, event):
        s = self._active_session()
        if not s:
            return
        pending = s.get("pending") or []
        if pending:
            self._popup.show_for(pending[0], self.frameGeometry())
        else:
            self._focus_terminal(s.get("terminal_hwnd"))

    def _on_action_click(self):
        s = self._active_session()
        if not s:
            return
        pending = s.get("pending") or []
        if pending:
            self._popup.show_for(pending[0], self.frameGeometry())
        else:
            self._focus_terminal(s.get("terminal_hwnd"))

    def _rename_active(self):
        s = self._active_session()
        if not s: return
        current = (s.get("custom_name") or "").strip()
        name, ok = QInputDialog.getText(
            self, "Rename session",
            "Display name:", QLineEdit.EchoMode.Normal, current,
        )
        if not ok: return
        try:
            requests.post(server_url("/session/name"),
                          json={"session_id": s["session_id"], "name": name.strip()}, timeout=2)
        except Exception:
            pass

    def _send_cmd(self, cmd: str):
        s = self._active_session()
        if not s:
            return
        state = s.get("state", "snoozing")
        if state in {"working", "allow", "input"}:
            QMessageBox.warning(self, "Wait for green",
                f"Session is {state!r}. Sending a command would mix with live input.")
            return
        hwnd = s.get("terminal_hwnd")
        if not hwnd:
            QMessageBox.warning(self, "No terminal", "Couldn't find a terminal for this session.")
            return
        name = session_label(s)
        reply = QMessageBox.question(self, "Send slash command",
            f"Send  {cmd}  to:\n\n  {name}\n\n(terminal will be focused and command typed + Enter)",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Yes,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        self._focus_terminal(hwnd)
        QTimer.singleShot(220, lambda: self._type_keystrokes(cmd))

    def _type_keystrokes(self, cmd: str):
        try:
            import keyboard  # type: ignore
            keyboard.write(cmd, delay=0.008); keyboard.send("enter")
        except Exception as e:
            self._tray.showMessage("CC-Beeper-Win", f"slash-command send failed: {e}",
                                   QSystemTrayIcon.MessageIcon.Warning, 3000)

    def _on_title_context_menu(self, pos):
        s = self._active_session()
        if not s: return
        menu = QMenu(self)
        cmd_menu = menu.addMenu("Send Slash Command")
        for label, cmd in (
            ("/compact",  "/compact"),
            ("/clear",    "/clear"),
            ("/cost",     "/cost"),
            ("/model",    "/model"),
            ("/resume",   "/resume"),
        ):
            a = QAction(label, self); a.triggered.connect(lambda _=False, c=cmd: self._send_cmd(c))
            cmd_menu.addAction(a)
        menu.addSeparator()
        ex = QAction("📄  Export Session Stats To Txt…", self)
        ex.triggered.connect(self._export_session_stats)
        menu.addAction(ex)
        menu.addSeparator()
        r = QAction("Rename…", self); r.triggered.connect(self._rename_active); menu.addAction(r)
        menu.exec_(self.lbl_title.mapToGlobal(pos))

    # --- session stats export -------------------------------------------

    def _build_session_report(self, s: dict) -> str:
        """Plain-text session report with token / context numbers + auto
        generated insights and tips keyed to the actual usage."""
        import datetime
        stats = s.get("stats") or {}

        model_label = stats.get("model_label") or "?"
        limit = int(stats.get("context_limit") or 0)
        current = int(stats.get("current_context") or 0)
        pct = float(stats.get("context_pct") or 0)
        total_in = int(stats.get("total_input") or 0)
        total_out = int(stats.get("total_output") or 0)
        cache_r = int(stats.get("total_cache_read") or 0)
        cache_w = int(stats.get("total_cache_write") or 0)
        turns = int(stats.get("turns") or 0)

        # Derived economics
        total_served_in = total_in + cache_r + cache_w
        cache_hit_pct = (100.0 * cache_r / total_served_in) if total_served_in else 0.0
        out_in_ratio = (total_out / total_in) if total_in else 0.0
        turns_per_pct = (turns / pct) if pct > 0 else 0.0

        # Try to get trust info (fresh snapshot; don't rely on cached)
        trust = {"persistent": [], "session": []}
        try:
            trust = requests.get(server_url("/trust"), timeout=1.5).json() or trust
        except Exception:
            pass

        lines = []
        lines.append("CC-BEEPER-WIN — SESSION STATS EXPORT")
        lines.append("=" * 60)
        lines.append(f"Generated: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        lines.append("")

        lines.append("SESSION")
        lines.append("-" * 60)
        lines.append(f"  Display Name : {session_label(s)}")
        lines.append(f"  Session ID   : {s.get('session_id','')}")
        lines.append(f"  CWD          : {s.get('cwd','')}")
        lines.append(f"  State        : {s.get('state','?')}")
        lines.append(f"  First Prompt : {(s.get('first_task') or '')[:300]}")
        if s.get("custom_name"):
            lines.append(f"  Custom Name  : {s['custom_name']}")
        lines.append("")

        lines.append("MODEL")
        lines.append("-" * 60)
        lines.append(f"  Model        : {model_label}")
        lines.append(f"  Raw ID       : {stats.get('model_raw','?')}")
        lines.append(f"  Context Limit: {limit:>12,} tokens")
        lines.append("")

        lines.append("CONTEXT USAGE")
        lines.append("-" * 60)
        lines.append(f"  Current      : {current:>12,}  ({pct:.1f}%)")
        lines.append(f"  Remaining    : {max(0, limit - current):>12,}  ({max(0.0, 100 - pct):.1f}%)")
        lines.append("")

        lines.append("TOKENS (LIFETIME)")
        lines.append("-" * 60)
        lines.append(f"  Turns        : {turns:>12,}")
        lines.append(f"  Input (fresh): {total_in:>12,}")
        lines.append(f"  Output       : {total_out:>12,}")
        lines.append(f"  Cache Read   : {cache_r:>12,}   (≈ 10% the cost of a fresh input token)")
        lines.append(f"  Cache Write  : {cache_w:>12,}")
        lines.append("")

        lines.append("ECONOMICS")
        lines.append("-" * 60)
        if total_served_in:
            lines.append(f"  Cache Hit Rate      : {cache_hit_pct:.1f}%  ({fmt_tokens(cache_r)} / {fmt_tokens(total_served_in)})")
        if total_in:
            lines.append(f"  Output / Input      : {out_in_ratio:.1f}×  ({fmt_tokens(total_out)} out per {fmt_tokens(total_in)} fresh in)")
        if turns and pct > 0:
            lines.append(f"  Turns Per 1% Context: {turns_per_pct:.1f}")
        lines.append("")

        # ---------- insights ----------
        lines.append("INSIGHTS")
        lines.append("-" * 60)
        if cache_hit_pct >= 90:
            lines.append("  • Cache is doing heavy lifting (≥90% of input served from cache).")
            lines.append("    Your prompt caching is working — keep reusing the same files.")
        elif cache_hit_pct >= 70:
            lines.append("  • Cache hit rate is healthy. Nothing to fix.")
        elif total_served_in > 0:
            lines.append("  • Cache hit rate is low — you're paying full freight on most input.")
            lines.append("    Likely cause: you're reading many different files or churning paths.")

        if pct >= 85:
            lines.append("  • Context is near the ceiling. /compact NOW or you'll start dropping info.")
        elif pct >= 60:
            lines.append("  • Context is in the \"getting full\" zone. /compact soon is a good move.")
        elif pct >= 30:
            lines.append("  • Context usage is comfortable. No rush.")
        else:
            lines.append("  • Plenty of room in the context window.")

        if out_in_ratio >= 500:
            lines.append("  • Output-heavy session (lots of generation).")
        elif out_in_ratio >= 50:
            lines.append("  • Balanced read/generate mix.")
        elif total_in > 0:
            lines.append("  • Read-heavy session (lots of exploration, less generation).")

        if turns >= 300:
            lines.append(f"  • Long-running session: {turns} turns.")
        lines.append("")

        # ---------- tips ----------
        lines.append("TIPS FOR IMPROVING TOKEN EFFICIENCY")
        lines.append("-" * 60)
        tip_n = 1
        def tip(txt: str):
            nonlocal tip_n
            lines.append(f"  {tip_n}. {txt}")
            tip_n += 1

        if pct >= 60:
            tip("Hit /compact soon. Summarising around 60-80% loses less than waiting to 95%.")
        else:
            tip("Watch the context bar. Target /compact around 60% for the best trade-off.")

        if cache_hit_pct < 70 and total_served_in > 0:
            tip("Stick to a stable set of files per task — re-opening the same paths warms the cache, swapping paths cold-loads them again.")
        else:
            tip("Huge cache totals in the meta line are fine; cache reads cost ≈10% of fresh reads.")

        tip("Use /clear between unrelated tasks so task-A context doesn't bloat task-B.")

        if "opus" in (model_label or "").lower():
            tip("For routine edits (renames, small refactors), /model to Sonnet can be ~5× cheaper. Promote back to Opus for hard reasoning.")

        tip("Batch related prompts in one turn instead of many tiny turns — each turn pays a system-prompt overhead.")
        tip("Avoid pasting long logs into prompts — file paths + Read tool calls cache, pasted text doesn't.")
        lines.append("")

        lines.append("CC-BEEPER-WIN SETTINGS")
        lines.append("-" * 60)
        lines.append(f"  Strategy : {self._current_strategy or '?'}")
        lines.append(f"  Mode     : {self._current_mode or '?'}")
        lines.append(f"  Trust (persistent): {', '.join(trust.get('persistent') or []) or '(none)'}")
        lines.append(f"  Trust (session)   : {', '.join(trust.get('session') or []) or '(none)'}")
        lines.append("")

        lines.append("=" * 60)
        lines.append("github.com/RezaAlmiro/cc-beeper-win")
        return "\n".join(lines)

    def _export_session_stats(self, sid: str | None = None) -> None:
        if sid is not None:
            s = next((x for x in self._sessions if x["session_id"] == sid), None)
        else:
            s = self._active_session()
        if not s:
            QMessageBox.warning(self, "No Session", "There's no session to export.")
            return
        import datetime
        ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        safe_name = "".join(c if c.isalnum() or c in " -_" else "_"
                            for c in session_label(s))[:40].strip().replace(" ", "-")
        default_name = f"cc-beeper-{safe_name or s['session_id'][:8]}-{ts}.txt"
        default_dir = str(Path.home() / "Documents")
        path, _ = QFileDialog.getSaveFileName(
            self, "Export Session Stats",
            str(Path(default_dir) / default_name),
            "Text files (*.txt);;All files (*)",
        )
        if not path:
            return
        try:
            report = self._build_session_report(s)
            Path(path).write_text(report, encoding="utf-8")
            self._tray.showMessage(
                "CC-Beeper-Win",
                f"Exported to\n{Path(path).name}",
                QSystemTrayIcon.MessageIcon.Information, 2800,
            )
        except Exception as e:
            QMessageBox.warning(self, "Export Failed", f"Couldn't write file:\n{e}")

    def _resolve(self, decision: str, scope: str):
        s = self._active_session()
        if not s: self._popup.hide(); return
        pending = s.get("pending") or []
        if not pending: self._popup.hide(); return
        rid = pending[0].get("id")
        try:
            requests.post(server_url("/resolve"),
                          json={"request_id": rid, "decision": decision, "scope": scope}, timeout=2)
        except Exception as e:
            self._tray.showMessage("CC-Beeper-Win", f"resolve error: {e}",
                                   QSystemTrayIcon.MessageIcon.Warning, 2500)
        finally:
            self._popup.hide()

    def _focus_terminal(self, hwnd):
        if not hwnd: return
        try:
            import ctypes
            from ctypes import wintypes
            import win32gui, win32con, win32process  # type: ignore
            user32 = ctypes.windll.user32
            h = int(hwnd)
            if win32gui.IsIconic(h): win32gui.ShowWindow(h, win32con.SW_RESTORE)
            user32.keybd_event(0x12, 0, 0, 0); user32.keybd_event(0x12, 0, 2, 0)
            cur_tid = ctypes.windll.kernel32.GetCurrentThreadId()
            target_tid, _ = win32process.GetWindowThreadProcessId(h)
            attached = False
            try:
                if target_tid and target_tid != cur_tid:
                    if user32.AttachThreadInput(target_tid, cur_tid, True): attached = True
                win32gui.BringWindowToTop(h)
                try: win32gui.SetForegroundWindow(h)
                except Exception: pass
                try: user32.SwitchToThisWindow(wintypes.HWND(h), True)
                except Exception: pass
            finally:
                if attached: user32.AttachThreadInput(target_tid, cur_tid, False)
        except Exception:
            pass

    # -- drag + edge resize -----------------------------------------------

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._dragging = True; self._drag_moved = False
            self._drag_offset = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()

    def mouseMoveEvent(self, event):
        if self._dragging:
            self._drag_moved = True
            self.move(event.globalPosition().toPoint() - self._drag_offset)
            event.accept()

    def mouseReleaseEvent(self, event):
        self._dragging = False

    def _edges_at(self, pos):
        m = self._resize_margin
        r = self.rect()
        edges = Qt.Edge(0)
        if pos.x() <= m:                     edges |= Qt.LeftEdge
        elif pos.x() >= r.width() - m:       edges |= Qt.RightEdge
        if pos.y() <= m:                     edges |= Qt.TopEdge
        elif pos.y() >= r.height() - m:      edges |= Qt.BottomEdge
        return edges

    def _cursor_for_edges(self, edges):
        if edges == (Qt.LeftEdge | Qt.TopEdge) or edges == (Qt.RightEdge | Qt.BottomEdge):
            return Qt.SizeFDiagCursor
        if edges == (Qt.RightEdge | Qt.TopEdge) or edges == (Qt.LeftEdge | Qt.BottomEdge):
            return Qt.SizeBDiagCursor
        if edges & Qt.LeftEdge or edges & Qt.RightEdge:
            return Qt.SizeHorCursor
        if edges & Qt.TopEdge or edges & Qt.BottomEdge:
            return Qt.SizeVerCursor
        return None

    def eventFilter(self, obj, event):
        et = event.type()
        if et not in (QEvent.Type.MouseMove, QEvent.Type.MouseButtonPress,
                      QEvent.Type.HoverMove, QEvent.Type.Leave):
            return False
        if not isinstance(obj, QWidget):
            return False
        if obj is not self and not self.isAncestorOf(obj):
            return False
        if et == QEvent.Type.Leave:
            QGuiApplication.restoreOverrideCursor(); return False
        try: global_pos = QCursor.pos()
        except Exception: return False
        pos = self.mapFromGlobal(global_pos)
        edges = self._edges_at(pos)
        if et in (QEvent.Type.MouseMove, QEvent.Type.HoverMove):
            shape = self._cursor_for_edges(edges)
            if shape is not None:
                cur = QGuiApplication.overrideCursor()
                if cur is None or cur.shape() != shape:
                    QGuiApplication.restoreOverrideCursor()
                    QGuiApplication.setOverrideCursor(shape)
            else:
                QGuiApplication.restoreOverrideCursor()
            return False
        if et == QEvent.Type.MouseButtonPress and edges:
            try:
                wh = self.windowHandle()
                if wh is not None:
                    wh.startSystemResize(edges); return True
            except Exception:
                pass
        return False

    # -- tray + menu ------------------------------------------------------

    def _build_tray(self):
        icon_path = ASSETS / "snoozing.png"
        icon = QIcon(str(icon_path)) if icon_path.exists() else QIcon()
        tray = QSystemTrayIcon(icon, self); tray.setToolTip("CC-Beeper-Win")
        self._main_menu = QMenu()
        menu = self._main_menu
        show_act = QAction("Show / Hide Widget", self); show_act.triggered.connect(self._toggle_visibility); menu.addAction(show_act)
        menu.addSeparator()
        self._strat_menu = menu.addMenu("Strategy:  …")
        for label, value in (
            ("Assist  (Widget Decides)  [default]", "assist"),
            ("Observer  (Never Override Claude)",   "observer"),
            ("Auto  (Headless Rules + Gemini)",     "auto"),
        ):
            a = QAction(label, self); a.setCheckable(True)
            a.triggered.connect(lambda _=False, v=value: self._set_strategy(v))
            self._strat_menu.addAction(a); self._strategy_actions[value] = a
        self._mode_menu = menu.addMenu("Mode:  …")
        for m, descr in (
            ("strict",  "Ask For Everything Except Reads"),
            ("relaxed", "Reads + Git-Read Fly; Writes + Network Ask  [default]"),
            ("trusted", "+ Project Writes And Local Git Operations"),
            ("yolo",    "Auto-Allow Almost Everything (Safety Net Still On)"),
        ):
            a = QAction(f"{m.upper()}  —  {descr}", self); a.setCheckable(True)
            a.triggered.connect(lambda _=False, mode=m: self._set_mode(mode))
            self._mode_menu.addAction(a); self._mode_actions[m] = a

        # Opacity submenu with checkable presets + custom slider dialog
        opacity_menu = menu.addMenu("Opacity:  …")
        self._opacity_menu = opacity_menu
        for pct in (60, 75, 85, 95, 100):
            a = QAction(f"{pct}%", self); a.setCheckable(True)
            a.triggered.connect(lambda _=False, v=pct: self._apply_opacity(v, save=True))
            opacity_menu.addAction(a)
            self._opacity_actions[pct] = a
        opacity_menu.addSeparator()
        custom = QAction("Custom…", self); custom.triggered.connect(self._set_opacity_custom)
        opacity_menu.addAction(custom)

        # Theme submenu (Light / Dark)
        theme_menu = menu.addMenu(f"Theme:  {self._theme_name.capitalize()}")
        self._theme_menu = theme_menu
        for tname, tlabel in (("light", "Light  (Warm Glass)"), ("dark", "Dark  (Deep Glass)")):
            a = QAction(tlabel, self); a.setCheckable(True)
            a.setChecked(tname == self._theme_name)
            a.triggered.connect(lambda _=False, v=tname: self._set_theme(v))
            theme_menu.addAction(a)
            self._theme_actions[tname] = a

        # Sound toggle
        self._sound_action = QAction("Sound Cues", self)
        self._sound_action.setCheckable(True)
        self._sound_action.setChecked(self._sound_enabled)
        self._sound_action.triggered.connect(self._toggle_sound)
        menu.addAction(self._sound_action)

        # Compact mode toggle — single-row heartbeat view.
        self._compact_action = QAction("Compact Mode", self)
        self._compact_action.setCheckable(True)
        self._compact_action.setChecked(self._compact_mode)
        self._compact_action.triggered.connect(lambda checked: self._set_compact(bool(checked), save=True))
        menu.addAction(self._compact_action)

        menu.addSeparator()
        u = QAction("Session Insights…", self); u.triggered.connect(self._show_usage); menu.addAction(u)
        h = QAction("Help…", self); h.triggered.connect(self._show_help); menu.addAction(h)
        s = QAction("Manage Trust…", self); s.triggered.connect(self._show_settings); menu.addAction(s)
        c = QAction("Clear Session Trust", self); c.triggered.connect(self._clear_session_trust); menu.addAction(c)
        menu.addSeparator()
        q = QAction("Quit Widget", self); q.triggered.connect(QApplication.instance().quit); menu.addAction(q)
        tray.setContextMenu(menu)
        tray.activated.connect(lambda reason: self._toggle_visibility() if reason == QSystemTrayIcon.ActivationReason.Trigger else None)
        tray.show()
        return tray

    def _show_main_menu(self, pos):
        """Right-click anywhere on the widget pops up the same menu the
        tray icon uses — so the user doesn't have to hunt for the tray."""
        if hasattr(self, "_main_menu"):
            self._main_menu.exec_(self.mapToGlobal(pos))

    def _apply_opacity(self, pct: int, *, save: bool = False) -> None:
        pct = max(20, min(100, int(pct)))
        self.setWindowOpacity(pct / 100.0)
        for v, act in self._opacity_actions.items():
            act.setChecked(v == pct)
        if hasattr(self, "_opacity_menu"):
            self._opacity_menu.setTitle(f"Opacity:  {pct}%")
        if save:
            try:
                cfg = load_cfg()
                cfg.setdefault("widget", {})["opacity_pct"] = pct
                CONFIG_PATH.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
            except Exception:
                pass

    def _set_theme(self, name: str) -> None:
        """Swap Light/Dark palette live. Reapplies stylesheet to the main
        window, prods painters to refresh, and persists the choice."""
        applied = apply_theme(name)
        self._theme_name = applied
        # Re-apply the top-level stylesheet; all child widgets inherit.
        self.setStyleSheet(button_css())
        # Force repaint of the custom-painted surfaces (they read ACTIVE
        # at paint time — just need a nudge).
        for w in (self.glass, self.ticker, self.btn_action):
            try: w.update()
            except Exception: pass
        # Badge will re-theme on the next 500 ms poll; no manual refresh.
        # Re-skin tabs (stylesheet-driven, already re-applied by parent
        # setStyleSheet, but force polish so [active=true] updates).
        for btn in self._tab_buttons.values():
            btn.style().unpolish(btn); btn.style().polish(btn)
        # Menu label + checkmarks
        if hasattr(self, "_theme_menu"):
            self._theme_menu.setTitle(f"Theme:  {applied.capitalize()}")
        for v, act in self._theme_actions.items():
            act.setChecked(v == applied)
        # Persist
        try:
            cfg = load_cfg()
            cfg.setdefault("widget", {})["theme"] = applied
            CONFIG_PATH.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
        except Exception:
            pass

    def _set_compact(self, enable: bool, *, save: bool = False) -> None:
        """Toggle single-row compact view: hide tab strip, subtitle, ctx
        meter, token meta and the bottom buttons — leaves sprite + title
        + state badge + ticker. Restores expanded layout and height when
        turned off."""
        enable = bool(enable)
        # Widgets hidden in compact mode
        hidable = [
            getattr(self, "tabbar", None),
            getattr(self, "lbl_subtitle", None),
            getattr(self, "mid_row", None),
            getattr(self, "lbl_meta", None),
            getattr(self, "btns_row", None),
        ]
        for w in hidable:
            if w is not None:
                w.setVisible(not enable)

        # Shrink / restore the sprite
        if hasattr(self, "sprite"):
            self.sprite.setFixedSize(36 if enable else 64, 36 if enable else 64)
            self._pixmap_cache.clear()   # force re-scale on next tick

        # Resize the window: compact = fixed 72 px tall; expanded = last
        # remembered expanded height.
        if enable:
            if not self._compact_mode:
                self._expanded_height = self.height()
            self.resize(self.width(), 72)
            self.setMinimumHeight(72)
            self.setMaximumHeight(72)
        else:
            self.setMinimumHeight(0)
            self.setMaximumHeight(16777215)
            self.resize(self.width(), self._expanded_height)

        self._compact_mode = enable
        if hasattr(self, "_compact_action"):
            self._compact_action.setChecked(enable)

        if save:
            try:
                cfg = load_cfg()
                cfg.setdefault("widget", {})["compact"] = enable
                if not enable:
                    cfg["widget"]["height"] = self._expanded_height
                CONFIG_PATH.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
            except Exception:
                pass

    def _toggle_sound(self, checked: bool) -> None:
        self._sound_enabled = bool(checked)
        try:
            cfg = load_cfg()
            cfg.setdefault("widget", {})["sound_enabled"] = self._sound_enabled
            CONFIG_PATH.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
        except Exception:
            pass
        # Small confirmation ping when turning it on
        if self._sound_enabled and "input" in self._sound_effects:
            try: self._sound_effects["input"].play()
            except Exception: pass

    def _set_opacity_custom(self) -> None:
        current = int(self.windowOpacity() * 100)
        pct, ok = QInputDialog.getInt(
            self, "Widget Opacity", "Opacity (%)", current, 20, 100, 5,
        )
        if ok:
            self._apply_opacity(pct, save=True)

    def _sync_menu_selections(self, strategy, mode):
        if strategy and strategy != self._current_strategy:
            for v, act in self._strategy_actions.items():
                act.setChecked(v == strategy)
            self._strat_menu.setTitle(f"Strategy:  {strategy}")
            self._current_strategy = strategy
        if mode and mode != self._current_mode:
            for v, act in self._mode_actions.items():
                act.setChecked(v == mode)
            self._mode_menu.setTitle(f"Mode:  {mode}")
            self._current_mode = mode

    def _show_help(self):
        self._help.show(); self._help.raise_(); self._help.activateWindow()

    def _show_usage(self):
        self._usage.refresh()
        self._usage.show(); self._usage.raise_(); self._usage.activateWindow()

    def _show_settings(self):
        self._settings.refresh(); self._settings.show(); self._settings.raise_(); self._settings.activateWindow()

    def _toggle_visibility(self):
        (self.hide if self.isVisible() else self.show)()
        if self.isVisible(): self.raise_()

    def _set_strategy(self, value):
        try:
            r = requests.post(server_url("/strategy"), json={"strategy": value}, timeout=2)
            if r.status_code == 200 and r.json().get("ok"):
                self._tray.showMessage("CC-Beeper-Win", f"Strategy: {value}", QSystemTrayIcon.MessageIcon.Information, 1800)
        except Exception as e:
            self._tray.showMessage("CC-Beeper-Win", f"strategy failed: {e}", QSystemTrayIcon.MessageIcon.Warning, 2500)

    def _set_mode(self, mode):
        try:
            r = requests.post(server_url("/mode"), json={"mode": mode}, timeout=2)
            if r.status_code == 200 and r.json().get("ok"):
                self._tray.showMessage("CC-Beeper-Win", f"Mode: {mode.upper()}", QSystemTrayIcon.MessageIcon.Information, 1800)
        except Exception as e:
            self._tray.showMessage("CC-Beeper-Win", f"mode failed: {e}", QSystemTrayIcon.MessageIcon.Warning, 2500)

    def _clear_session_trust(self):
        try:
            for c in (requests.get(server_url("/trust"), timeout=1).json() or {}).get("session", []):
                requests.post(server_url("/trust/remove"), json={"category": c}, timeout=1)
        except Exception:
            pass


# ==========================================================================
# Entry point
# ==========================================================================

def main() -> int:
    import logging, traceback
    log_path = ROOT / "widget.log"
    logging.basicConfig(filename=str(log_path), level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    def _excepthook(etype, value, tb):
        logging.error("uncaught exception:\n%s", "".join(traceback.format_exception(etype, value, tb)))
    sys.excepthook = _excepthook

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    w = BeeperWidget(); w.show()
    logging.info("widget started (glass HUD)")
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
