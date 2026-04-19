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

import requests
from PySide6.QtCore import Qt, QTimer, QPoint, QPropertyAnimation, QEasingCurve, QRect, QSize, QEvent, QRectF
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
A glass HUD that tracks your Claude Code sessions. One session visible
at a time; arrow buttons cycle through them. Right-click the title for
rename and session-specific slash commands.
</p>

<h3 style="color:#1F2430">State dot (top-right)</h3>
<ul style="color:#1F2430; line-height:1.5">
  <li><b style="color:#ff7a7a">red</b> — Claude is working</li>
  <li><b style="color:#FFB74D">amber</b> — tool approval pending; click the centre button</li>
  <li><b style="color:#4CD98D">green</b> — turn done, ready for your next prompt</li>
  <li><b style="color:#7CE0A8">soft green</b> — turn done but Claude asked a follow-up, waiting on your reply</li>
</ul>

<h3 style="color:#1F2430">Strategy · Mode · Trust</h3>
<p style="color:#60667A">All tuned from the tray icon's right-click menu.
Strategy is who decides permissions (Assist / Observer / Auto). Mode sets
how lenient auto-allow is (Strict / Relaxed / Trusted / YOLO). Trust
stores the categories you approved with "allow forever".</p>

<h3 style="color:#1F2430">Slash commands (token hygiene)</h3>
<p style="color:#60667A">The /compact button in the HUD summarises + shrinks the context for the active session. Right-click the title for /clear, /cost, /model, /resume. Disabled while the state dot is red.</p>
<ul style="color:#1F2430; line-height:1.5">
  <li><b>/compact</b> around 60% context = most efficient</li>
  <li><b>/clear</b> between unrelated tasks</li>
  <li>Cache reads cost ~10% of fresh reads — huge cache numbers are fine</li>
</ul>

<h3 style="color:#1F2430">Shortcuts</h3>
<ul style="color:#1F2430; line-height:1.5">
  <li>Click the sprite → focus that session's terminal / approve pending</li>
  <li>Drag the panel body → move widget</li>
  <li>Drag any edge → resize</li>
  <li>Tray icon click → show/hide widget</li>
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
        root = QVBoxLayout(container)
        root.setContentsMargins(16, 12, 16, 12)
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
        self.btn_rename = QPushButton("✎ rename"); self.btn_rename.setObjectName("smallBtn")
        self.btn_prev   = QPushButton("◀");        self.btn_prev.setObjectName("iconBtn")
        self.btn_action = QPushButton("●");        self.btn_action.setObjectName("iconBtn")
        self.btn_next   = QPushButton("▶");        self.btn_next.setObjectName("iconBtn")
        self.btn_compact = QPushButton("⇣ /compact"); self.btn_compact.setObjectName("smallBtn")
        self.btn_rename.clicked.connect(self._rename_active)
        self.btn_prev.clicked.connect(lambda: self._cycle_session(-1))
        self.btn_action.clicked.connect(self._on_action_click)
        self.btn_next.clicked.connect(lambda: self._cycle_session(+1))
        self.btn_compact.clicked.connect(lambda: self._send_cmd("/compact"))
        btns.addWidget(self.btn_rename)
        btns.addStretch()
        btns.addWidget(self.btn_prev)
        btns.addWidget(self.btn_action)
        btns.addWidget(self.btn_next)
        btns.addStretch()
        btns.addWidget(self.btn_compact)
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
        self._current_strategy: str | None = None
        self._current_mode: str | None = None
        self._tray = self._build_tray()

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

        s = self._active_session()
        if not s:
            self._render_empty(data.get("strategy"), data.get("mode"))
            return
        self._render_session(s)

    def _render_empty(self, strategy, mode):
        self.lbl_title.setText("— no active sessions —")
        self.lbl_subtitle.setText(f"strategy {strategy or '?'}   ·   mode {mode or '?'}")
        self._set_state_dot("snoozing")
        self.ctx_bar.setValue(0); self.lbl_ctx_used.setText(""); self.lbl_ctx_left.setText("")
        self.lbl_meta.setText("open a Claude Code session and its tab will appear here")
        self.btn_action.setText("●"); self.btn_action.setProperty("accent", ""); self.btn_action.style().unpolish(self.btn_action); self.btn_action.style().polish(self.btn_action)
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

        # Action button label + colour depending on what's going on
        if has_pending:
            self.btn_action.setText("APPROVE?"); self.btn_action.setProperty("accent", "amber")
        elif state == "awaiting_input":
            self.btn_action.setText("REPLY"); self.btn_action.setProperty("accent", "green")
        elif state == "working":
            self.btn_action.setText("working…"); self.btn_action.setProperty("accent", "red")
        elif state == "done":
            self.btn_action.setText("done"); self.btn_action.setProperty("accent", "green")
        else:
            self.btn_action.setText("focus"); self.btn_action.setProperty("accent", "")
        self.btn_action.style().unpolish(self.btn_action); self.btn_action.style().polish(self.btn_action)

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
            f"in {fmt_tokens(stats.get('total_input', 0))}   ·   "
            f"out {fmt_tokens(stats.get('total_output', 0))}   ·   "
            f"cache {fmt_tokens(stats.get('total_cache_read', 0))}"
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
        menu = QMenu()
        show_act = QAction("Show / Hide widget", self); show_act.triggered.connect(self._toggle_visibility); menu.addAction(show_act)
        menu.addSeparator()
        self._strat_menu = menu.addMenu("Strategy:  …")
        for label, value in (
            ("Assist  (widget decides)  [default]", "assist"),
            ("Observer  (never override Claude)",   "observer"),
            ("Auto  (headless rules + Gemini)",     "auto"),
        ):
            a = QAction(label, self); a.setCheckable(True)
            a.triggered.connect(lambda _=False, v=value: self._set_strategy(v))
            self._strat_menu.addAction(a); self._strategy_actions[value] = a
        self._mode_menu = menu.addMenu("Mode:  …")
        for m, descr in (
            ("strict",  "ask for everything except reads"),
            ("relaxed", "reads + git-read fly; writes + network ask  [default]"),
            ("trusted", "+ project writes and local git operations"),
            ("yolo",    "auto-allow almost everything (safety net still on)"),
        ):
            a = QAction(f"{m.upper()}  —  {descr}", self); a.setCheckable(True)
            a.triggered.connect(lambda _=False, mode=m: self._set_mode(mode))
            self._mode_menu.addAction(a); self._mode_actions[m] = a
        menu.addSeparator()
        h = QAction("Help…", self); h.triggered.connect(self._show_help); menu.addAction(h)
        s = QAction("Manage trust…", self); s.triggered.connect(self._show_settings); menu.addAction(s)
        c = QAction("Clear session trust", self); c.triggered.connect(self._clear_session_trust); menu.addAction(c)
        menu.addSeparator()
        q = QAction("Quit widget", self); q.triggered.connect(QApplication.instance().quit); menu.addAction(q)
        tray.setContextMenu(menu)
        tray.activated.connect(lambda reason: self._toggle_visibility() if reason == QSystemTrayIcon.ActivationReason.Trigger else None)
        tray.show()
        return tray

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
