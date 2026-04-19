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
    QApplication, QDialog, QFrame, QHBoxLayout, QInputDialog, QLabel,
    QLineEdit, QMainWindow, QMenu, QMessageBox, QPushButton, QScrollArea,
    QSizePolicy, QSystemTrayIcon, QVBoxLayout, QWidget,
    QGraphicsOpacityEffect, QGraphicsDropShadowEffect, QProgressBar,
)

ROOT = Path(__file__).resolve().parent
ASSETS = ROOT / "assets"
CONFIG_PATH = ROOT / "config.json"
PORT_FILE = ROOT / ".port"
POLL_MS = 500

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
# Glass HUD palette  (warm light, matches Apple-style liquid glass reference)
# --------------------------------------------------------------------------

BG_RGBA         = (246, 243, 238, 225)   # translucent warm off-white
HIGHLIGHT_RGBA  = (255, 255, 255, 90)    # top-edge inner highlight
BORDER_RGBA     = (255, 255, 255, 120)   # outer hairline
TEXT_TITLE      = "#1F2430"
TEXT_SUBTITLE   = "#60667A"
TEXT_META       = "#7A8299"
BAR_TRACK       = "#d7d2c8"
BAR_FILL        = "#1F2430"
CORNER_RADIUS   = 22


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

        path = QPainterPath()
        path.addRoundedRect(r, CORNER_RADIUS, CORNER_RADIUS)

        # Base translucent fill with a subtle top-to-bottom gradient
        g = QLinearGradient(r.topLeft(), r.bottomLeft())
        g.setColorAt(0.0, QColor(*[c if i < 3 else BG_RGBA[3] + 10 for i, c in enumerate(BG_RGBA)]))
        g.setColorAt(1.0, QColor(*BG_RGBA))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(g))
        p.drawPath(path)

        # Inner top highlight (light strip 1–6 px below the top border)
        hl_rect = QRectF(r.left() + 4, r.top() + 2, r.width() - 8, 14)
        hl_path = QPainterPath()
        hl_path.addRoundedRect(hl_rect, CORNER_RADIUS - 6, CORNER_RADIUS - 6)
        hg = QLinearGradient(hl_rect.topLeft(), hl_rect.bottomLeft())
        hg.setColorAt(0.0, QColor(*HIGHLIGHT_RGBA))
        hg.setColorAt(1.0, QColor(255, 255, 255, 0))
        p.setBrush(QBrush(hg))
        p.drawPath(hl_path)

        # Hairline outer border
        p.setPen(QPen(QColor(*BORDER_RGBA), 1))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPath(path)


# ==========================================================================
# The big circular state button — custom-painted so we can animate per state
# ==========================================================================

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
            pen = QPen(QColor("#5a3b00"), 4, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
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

BUTTON_CSS = """
QPushButton#iconBtn {
    background: rgba(255, 255, 255, 150);
    color: #1F2430;
    border: 1px solid rgba(255, 255, 255, 200);
    border-radius: 18px;
    font-family: 'Segoe UI', sans-serif;
    font-size: 13px; font-weight: 700;
    padding: 4px 10px;
}
QPushButton#iconBtn:hover {
    background: rgba(255, 255, 255, 220);
    border: 1px solid #1F2430;
}
QPushButton#iconBtn[accent="red"]   { background: #FF5E5E; color: white; border: 1px solid #ff2e2e; }
QPushButton#iconBtn[accent="green"] { background: #4CD98D; color: #062615; border: 1px solid #2fa35a; }
QPushButton#iconBtn[accent="amber"] { background: #FFB74D; color: #2a1d00; border: 1px solid #c78a2a; }

/* Matching-size circular prev/next arrows. */
QPushButton#arrowCircle {
    min-width: 36px; max-width: 36px; min-height: 36px; max-height: 36px;
    border-radius: 18px;
    border: 1px solid rgba(255, 255, 255, 220);
    background: rgba(255, 255, 255, 170);
    color: #1F2430;
    font-family: 'Segoe UI', sans-serif;
    font-size: 14px; font-weight: 800;
}
QPushButton#arrowCircle:hover { border: 1px solid #1F2430; background: rgba(255,255,255,230); }

/* Browser-style session tabs integrated into the top of the glass panel.
   Tabs fill nearly the full height of the strip so labels are legible;
   the active tab body blends into the panel below. Top-edge stripe is
   state-coloured. */
QPushButton.miniTab {
    background: rgba(255, 255, 255, 110);
    color: #1F2430;
    border: 1px solid rgba(0, 0, 0, 40);
    border-bottom: none;
    border-top-left-radius: 10px;
    border-top-right-radius: 10px;
    border-bottom-left-radius: 0;
    border-bottom-right-radius: 0;
    font-family: 'Segoe UI', sans-serif;
    font-size: 12px; font-weight: 600;
    /* Light top padding so the label sits right under the coloured stripe;
       heavier bottom padding gives the tab its full height. */
    padding: 3px 14px 12px 14px;
    min-height: 28px;
}
QPushButton.miniTab:hover {
    background: rgba(255, 255, 255, 180);
}
QPushButton.miniTab[active="true"] {
    background: rgba(255, 255, 255, 245);
    color: #0B1020;
    border: 1px solid rgba(0, 0, 0, 75);
    border-bottom: none;
    font-weight: 800;
    padding: 4px 14px 14px 14px;
}
/* State-colour stripe on the top edge — 3 px for inactive, 4 px for active.
   Keys mirror the action-circle's state names exactly. */
QPushButton.miniTab[stateKey="idle"]     { border-top: 3px solid #9AA3B2; }
QPushButton.miniTab[stateKey="done"]     { border-top: 3px solid #4CD98D; }
QPushButton.miniTab[stateKey="working"]  { border-top: 3px solid #FFB74D; }
QPushButton.miniTab[stateKey="input"]    { border-top: 3px solid #3DA1FF; }
QPushButton.miniTab[stateKey="approval"] { border-top: 3px solid #FF7A00; }
QPushButton.miniTab[stateKey="error"]    { border-top: 3px solid #8B0000; }
QPushButton.miniTab[active="true"][stateKey="idle"]     { border-top: 4px solid #9AA3B2; }
QPushButton.miniTab[active="true"][stateKey="done"]     { border-top: 4px solid #4CD98D; }
QPushButton.miniTab[active="true"][stateKey="working"]  { border-top: 4px solid #FFB74D; }
QPushButton.miniTab[active="true"][stateKey="input"]    { border-top: 4px solid #3DA1FF; }
QPushButton.miniTab[active="true"][stateKey="approval"] { border-top: 4px solid #FF7A00; }
QPushButton.miniTab[active="true"][stateKey="error"]    { border-top: 4px solid #8B0000; }

/* Playlist / session-picker button on the far left of the tab strip.
   Same visual weight as a tab so the strip reads as one continuous row. */
QPushButton#playlistBtn {
    background: rgba(255, 255, 255, 150);
    color: #1F2430;
    border: 1px solid rgba(0, 0, 0, 40);
    border-bottom: none;
    border-top-left-radius: 10px;
    border-top-right-radius: 10px;
    border-bottom-left-radius: 0;
    border-bottom-right-radius: 0;
    font-family: 'Segoe UI', sans-serif;
    font-size: 15px; font-weight: 800;
    padding: 6px 12px;
    min-height: 28px;
    min-width: 30px;
}
QPushButton#playlistBtn:hover { background: rgba(255, 255, 255, 220); }

QPushButton#smallBtn {
    background: transparent; color: #1F2430;
    border: none; font-family: 'Segoe UI', sans-serif;
    font-size: 12px; font-weight: 600; padding: 2px 6px;
    border-radius: 10px;
}
QPushButton#smallBtn:hover { background: rgba(255, 255, 255, 200); }

QLabel#title {
    color: #1F2430; font-family: 'Segoe UI', sans-serif;
    font-size: 16px; font-weight: 700;
}
QLabel#subtitle {
    color: #60667A; font-family: 'Segoe UI', sans-serif;
    font-size: 11px; font-weight: 500;
}
QLabel#meta {
    color: #7A8299; font-family: 'Segoe UI', sans-serif;
    font-size: 10px; font-weight: 500;
}
QLabel#stateDot {
    border-radius: 11px; min-width: 22px; min-height: 22px;
    max-width: 22px; max-height: 22px;
}
QLabel#ctxTime {
    color: #1F2430; font-family: 'Segoe UI', sans-serif;
    font-size: 11px; font-weight: 600;
}
QProgressBar#ctxBar {
    background: #d7d2c8; border: none; border-radius: 4px; height: 8px;
    text-align: center; color: transparent;
}
QProgressBar#ctxBar::chunk { background: #1F2430; border-radius: 4px; }
QProgressBar#ctxBar[state="hot"]::chunk  { background: #ff8a4d; }
QProgressBar#ctxBar[state="crit"]::chunk { background: #ff2e2e; }
"""


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
            QFrame#card {
                background: rgba(248, 246, 242, 240);
                border: 1px solid rgba(255, 255, 255, 160);
                border-radius: 14px;
            }
            QLabel#title    { font-size: 13px; font-weight: 700; padding: 6px 8px 0; }
            QLabel#summary  { color: #FA6B2A; font-size: 11px; padding: 4px 8px; }
            QLabel#reason   { color: #C23A3A; font-size: 11px; padding: 0 8px 6px; }
            QLabel#meta     { font-size: 11px; padding: 0 8px; }
        """)
        self._on_resolve = on_resolve

        outer = QVBoxLayout(self); outer.setContentsMargins(0, 0, 0, 0)
        self.card = QFrame(self); self.card.setObjectName("card")
        outer.addWidget(self.card)

        v = QVBoxLayout(self.card); v.setContentsMargins(8, 8, 8, 8); v.setSpacing(4)
        self.lbl_title = QLabel("Claude wants to run:"); self.lbl_title.setObjectName("title")
        self.lbl_meta = QLabel(""); self.lbl_meta.setObjectName("meta")
        self.lbl_summary = QLabel(""); self.lbl_summary.setObjectName("summary"); self.lbl_summary.setWordWrap(True)
        self.lbl_reason = QLabel(""); self.lbl_reason.setObjectName("reason"); self.lbl_reason.setWordWrap(True)
        for w in (self.lbl_title, self.lbl_meta, self.lbl_summary, self.lbl_reason):
            v.addWidget(w)

        row = QVBoxLayout(); row.setSpacing(4)
        btn_once = QPushButton("✓  Allow once"); btn_once.setObjectName("iconBtn")
        btn_sess = QPushButton("✓✓  Allow for this session"); btn_sess.setObjectName("iconBtn"); btn_sess.setProperty("accent", "green")
        btn_fwd  = QPushButton("✓✓✓  Allow forever (this category)"); btn_fwd.setObjectName("iconBtn"); btn_fwd.setProperty("accent", "green")
        btn_deny = QPushButton("✗  Deny"); btn_deny.setObjectName("iconBtn"); btn_deny.setProperty("accent", "red")
        btn_once.clicked.connect(lambda: on_resolve("allow", "once"))
        btn_sess.clicked.connect(lambda: on_resolve("allow", "session"))
        btn_fwd.clicked.connect(lambda: on_resolve("allow", "persistent"))
        btn_deny.clicked.connect(lambda: on_resolve("deny", "once"))
        for b in (btn_once, btn_sess, btn_fwd, btn_deny):
            row.addWidget(b)
        v.addLayout(row)

        self.resize(340, 250)

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
  <li><b>State Dot (top-right)</b> — The Live Status Colour (See Below).</li>
  <li><b>Context Bar</b> — Shows How Much Of The Model's Context Window Is In Use. The Left Label Is Percent Used; The Right Label Is Tokens Remaining. Bar Turns Orange Above 60% And Red Above 85%.</li>
  <li><b>Meta Line</b> — Lifetime Totals For This Session: In (Input Tokens), Out (Output Tokens), Cache (Cached Reads).</li>
  <li><b>Action Circle (Centre Bottom)</b> — The Big Round Button Shows A Single-Letter Code Plus A Colour That Mirrors The State Dot. Click Does The Natural Thing For The Current State (Approve Pending, Focus Terminal, Etc.). Flashes On States That Need Your Attention.</li>
  <li><b>◀ / ▶ Arrows</b> — Cycle Through Active Sessions. Size Matches The State Circle So The Bottom Row Feels Balanced.</li>
  <li><b>Session Tabs (Top Of The Panel)</b> — Browser-Style Tabs Integrated Into The Top Edge Of The Glass Body. Each Tab Carries A 3-Pixel State-Coloured Stripe Along Its Top, So You Can See Every Session's Status At A Glance. The Active Tab Lifts Slightly, Has A Thicker Coloured Stripe, And Blends Into The Body Below. Click Any Tab To Switch. New Sessions Appear As New Tabs Automatically.</li>
  <li><b>☰ Playlist Button (Far Left Of The Tab Strip)</b> — Opens A Menu Listing Every Active Session With Its Coloured State Dot. A Third Way To Switch (Alongside Tabs And ◀ / ▶). Useful When You Have Many Tabs And Want A Compact Scroll-Able List.</li>
  <li><b>⇣ Commands ▾</b> — Dropdown With /compact, /clear, /cost, /model, /resume. Focuses The Session's Terminal And Types The Command.</li>
  <li><b>✎ Rename</b> — Opens A Small Text Dialog To Set A Custom Tab Name.</li>
</ul>

<h3 style="color:#1F2430">State — Dot + Action Circle</h3>
<p style="color:#60667A">The Top-Right Dot And The Big Centre-Bottom Circle Always Agree. The Circle Adds A Single-Letter Code Plus A Distinctive Animation Per State:</p>
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
<p style="color:#60667A">The Widget Refuses To Send A Slash Command While The State Dot Is Red — The Keystrokes Would Be Mixed Into Claude's Live Input Stream. Wait For Green.</p>

<h3 style="color:#1F2430">Token-Saving Tips (Without Hurting Output)</h3>
<ul style="color:#1F2430; line-height:1.6">
  <li>Watch The Context Bar. Hit <b>/compact</b> Around 60 % And You Rarely See Red.</li>
  <li><b>/clear</b> Between Unrelated Tasks — Don't Carry Setup From Task A Into Task B.</li>
  <li>Cache Reads Cost About 10 % Of A Fresh Read — Huge Cache Numbers In The Meta Line Are Usually Good, Not Waste.</li>
  <li>Drop To A Cheaper Model With <b>/model</b> For Routine Edits; Promote Back To Opus For Hard Reasoning.</li>
</ul>

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

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setWindowTitle("CC-Beeper-Win")
        self.setStyleSheet(BUTTON_CSS)

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
        # Zero top margin: tabs sit flush against the glass's top edge so
        # there's no dead "headroom" strip of glass above the tabs.
        root = QVBoxLayout(container)
        root.setContentsMargins(16, 0, 16, 12)
        root.setSpacing(8)

        # Integrated browser-style tab strip sitting right on top of the
        # glass body. Height matches the tabs' natural footprint (one
        # rowful of content, no extra padding), so there's no wasted
        # headroom. The ☰ playlist button on the left opens a full
        # session menu; tabs to the right of it take their natural widths
        # (like Windows Terminal) and grow as sessions are added.
        self.tabbar = QWidget(container)
        self.tabbar.setFixedHeight(36)
        self.tabbar.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.tabbar_layout = QHBoxLayout(self.tabbar)
        self.tabbar_layout.setContentsMargins(10, 4, 10, 0)
        self.tabbar_layout.setSpacing(2)

        self.btn_playlist = QPushButton("☰", self.tabbar)
        self.btn_playlist.setObjectName("playlistBtn")
        self.btn_playlist.setToolTip("Sessions — click to pick")
        self.btn_playlist.clicked.connect(self._show_playlist_menu)
        self.tabbar_layout.addWidget(self.btn_playlist)
        self.tabbar_layout.addStretch(1)
        root.addWidget(self.tabbar)
        self._tab_buttons: dict[str, QPushButton] = {}

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

        self.state_dot = QLabel(""); self.state_dot.setObjectName("stateDot")
        self.state_dot.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._set_state_dot("snoozing")
        top.addWidget(self.state_dot, 0, Qt.AlignmentFlag.AlignTop)
        root.addLayout(top)

        # Middle: context bar + time-style labels
        mid = QHBoxLayout(); mid.setSpacing(8)
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
        root.addLayout(mid)

        # Meta line (tokens in/out/cache)
        self.lbl_meta = QLabel(""); self.lbl_meta.setObjectName("meta")
        self.lbl_meta.setAlignment(Qt.AlignmentFlag.AlignCenter)
        root.addWidget(self.lbl_meta)

        # Bottom controls
        btns = QHBoxLayout(); btns.setSpacing(6)
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
        root.addLayout(btns)

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
        self._dock_to_corner()

        self._popup = ApprovalPopup(self._resolve)
        self._settings = TrustSettings()
        self._help = HelpDialog()
        self._strategy_actions: dict[str, QAction] = {}
        self._mode_actions: dict[str, QAction] = {}
        self._opacity_actions: dict[int, QAction] = {}
        self._current_strategy: str | None = None
        self._current_mode: str | None = None
        self._tray = self._build_tray()

        # Apply saved opacity (default 95%)
        self._apply_opacity(int(w_cfg.get("opacity_pct", 95)))

        # Right-click anywhere on the widget → main menu (tray menu reused).
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._show_main_menu)

        self._size_save_timer = QTimer(self)
        self._size_save_timer.setSingleShot(True); self._size_save_timer.setInterval(400)
        self._size_save_timer.timeout.connect(self._persist_size)

        self.setMouseTracking(True)
        container.setMouseTracking(True)
        QApplication.instance().installEventFilter(self)

        self._timer = QTimer(self); self._timer.timeout.connect(self._tick); self._timer.start(POLL_MS)

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

    # -- state rendering --------------------------------------------------

    def _load_pixmap(self, fname: str) -> QPixmap:
        if fname not in self._pixmap_cache:
            p = ASSETS / fname
            self._pixmap_cache[fname] = QPixmap(str(p)) if p.exists() else QPixmap()
        return self._pixmap_cache[fname]

    def _set_state_dot(self, state: str):
        color = STATE_COLOR.get(state, "#9AA3B2")
        self.state_dot.setStyleSheet(
            f"background: {color}; border-radius: 11px; min-width: 22px; min-height: 22px;"
            "border: 2px solid rgba(255,255,255,180);"
        )
        self.state_dot.setToolTip(state)

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

        s = self._active_session()
        if not s:
            self._render_empty(data.get("strategy"), data.get("mode"))
            return
        self._render_session(s)

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
                btn.clicked.connect(lambda _=False, x=sid: self._select_session(x))
                # Insert before the trailing stretch. Because ☰ is at index 0
                # and the stretch is last, new tabs land between them.
                self.tabbar_layout.insertWidget(self.tabbar_layout.count() - 1, btn)
                self._tab_buttons[sid] = btn
            label = session_label(s)[:22]
            btn.setText(label)
            btn.setProperty("stateKey", self._state_key(s))
            btn.setProperty("active", sid == self._active_sid)
            btn.style().unpolish(btn); btn.style().polish(btn)
            btn.setToolTip(
                f"session: {sid[:8]}\n"
                f"state:   {s.get('state','?')}\n"
                f"cwd:     {s.get('cwd','')}\n"
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

    def _render_empty(self, strategy, mode):
        self.lbl_title.setText("— No Active Sessions —")
        self.lbl_subtitle.setText(f"Strategy {(strategy or '?').title()}   ·   Mode {(mode or '?').title()}")
        self._set_state_dot("snoozing")
        self.ctx_bar.setValue(0); self.lbl_ctx_used.setText(""); self.lbl_ctx_left.setText("")
        self.lbl_meta.setText("Open A Claude Code Session And Its Tab Will Appear Here")
        self.btn_action.set_state("idle", "I", "Idle — No Active Sessions")
        pm = self._load_pixmap("snoozing.png")
        if not pm.isNull():
            self.sprite.setPixmap(pm.scaled(64, 64, Qt.AspectRatioMode.KeepAspectRatioByExpanding, Qt.TransformationMode.SmoothTransformation))


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
        self._set_state_dot(state)

        pending = s.get("pending") or []
        has_pending = bool(pending)

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

        # Sprite
        fname = STATE_TO_SPRITE.get(state, "snoozing.png")
        pm = self._load_pixmap(fname)
        if not pm.isNull():
            scaled = pm.scaled(64, 64, Qt.AspectRatioMode.KeepAspectRatioByExpanding, Qt.TransformationMode.SmoothTransformation)
            self.sprite.setPixmap(scaled)

        if has_pending and self._popup.isVisible():
            self._popup.show_for(pending[0], self.frameGeometry())
        if not has_pending and self._popup.isVisible():
            self._popup.hide()

    # -- actions ----------------------------------------------------------

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
        cmd_menu = menu.addMenu("Send slash command")
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
        r = QAction("Rename…", self); r.triggered.connect(self._rename_active); menu.addAction(r)
        menu.exec_(self.lbl_title.mapToGlobal(pos))

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

        menu.addSeparator()
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
