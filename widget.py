"""CC-Beeper-Win — tabbed always-on-top pixel-art bedroom widget.

Single widget with a tab strip along the top — one tab per Claude Code
session. The tab for a session flashes when that session needs attention.
Clicking a tab switches the widget to that session's view. Meter at the
bottom shows context % and token totals for the active tab.

Clicking the sprite (no pending) focuses that session's terminal window.
Clicking the sprite (with pending) opens the 4-way approval popup.
Right-click tray icon for strategy/mode, trust settings, quit.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import requests
from PySide6.QtCore import Qt, QTimer, QPoint, QPropertyAnimation, QEasingCurve, QRect, QSize
from PySide6.QtGui import QAction, QGuiApplication, QIcon, QPainter, QPixmap, QRadialGradient, QColor
from PySide6.QtWidgets import (
    QApplication, QDialog, QFrame, QHBoxLayout, QLabel, QMainWindow, QMenu,
    QPushButton, QScrollArea, QSizePolicy, QSystemTrayIcon, QVBoxLayout,
    QWidget, QGraphicsOpacityEffect, QProgressBar,
)

ROOT = Path(__file__).resolve().parent
ASSETS = ROOT / "assets"
CONFIG_PATH = ROOT / "config.json"
PORT_FILE = ROOT / ".port"
POLL_MS = 500

STATE_TO_SPRITE = {
    "snoozing":        "snoozing.png",
    "working":         "working.png",
    "done":            "done.png",
    "awaiting_input":  "input.png",
    "error":           "error.png",
    "allow":           "allow.png",
    "input":           "input.png",
    "listening":       "listening.png",
    "recap":           "listening.png",
}
ATTENTION_STATES = {"allow", "input", "error", "awaiting_input"}
STATE_COLOR = {
    "snoozing": "#4b5563",
    "working":  "#7BAFFF",
    "done":     "#57d9a3",
    "error":    "#ff5e5e",
    "allow":    "#ffb74d",
    "input":    "#c084fc",
    "listening":"#77C9EB",
    "recap":    "#77C9EB",
}


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


def short_label(first_task: str, cwd: str, session_id: str, limit: int = 16) -> str:
    """Tab label: prefer the session's first user prompt (truncated at a
    word boundary), fall back to cwd basename, fall back to session id."""
    if first_task:
        t = first_task.strip().replace("\n", " ")
        if len(t) <= limit:
            return t
        # truncate at last space within budget
        head = t[:limit]
        sp = head.rfind(" ")
        if sp >= 8:
            head = head[:sp]
        return head.rstrip(",.;:") + "…"
    if cwd:
        base = os.path.basename(cwd.replace("\\", "/").rstrip("/"))
        if base:
            return base[:limit]
    return (session_id or "?")[:6]


def fmt_tokens(n: int) -> str:
    if n < 1000:
        return f"{n}"
    if n < 1_000_000:
        return f"{n/1000:.1f}k"
    return f"{n/1_000_000:.2f}M"


# ---------------------------------------------------------------------------
# Stylesheets
# ---------------------------------------------------------------------------

BUTTON_CSS = """
QPushButton {
    background: rgba(30, 38, 52, 240);
    color: #F2F5FA;
    border: 1px solid #487CFF;
    border-radius: 4px;
    font-family: Consolas, 'Courier New', monospace;
    font-size: 11px; font-weight: 600;
    padding: 6px 8px; text-align: left;
}
QPushButton:hover { background: rgba(72, 124, 255, 200); color: #0B1020; }
QPushButton#deny { border-color: #FF5E5E; color: #FFBDBD; }
QPushButton#deny:hover { background: rgba(255, 94, 94, 220); color: #1A0B0B; }
"""

CARD_CSS = """
QFrame#card {
    background: rgba(10, 14, 22, 235);
    border: 1px solid #487CFF;
    border-radius: 6px;
}
QLabel#title { color: #F2F5FA; font-family: Consolas, monospace; font-size: 11px; font-weight: 700; padding: 4px 6px 0 6px; }
QLabel#meta  { color: #9FEED9; font-family: Consolas, monospace; font-size: 10px; padding: 0 6px; }
QLabel#summary { color: #FAC496; font-family: Consolas, monospace; font-size: 10px; padding: 4px 6px; }
QLabel#reason  { color: #FFBDBD; font-family: Consolas, monospace; font-size: 10px; padding: 0 6px 4px 6px; }
"""

TAB_CSS = """
QWidget#tabbar { background: rgba(8, 12, 20, 245); padding: 0; }
/* Notebook-folder style: rounded top only, slight negative right-margin
   so adjacent tabs overlap a little. */
QPushButton.sessionTab {
    background: rgba(170, 40, 40, 230);
    color: #ffeaea;
    border: 1px solid rgba(255, 255, 255, 40);
    border-bottom: none;
    border-top-left-radius: 9px;
    border-top-right-radius: 9px;
    border-bottom-left-radius: 0;
    border-bottom-right-radius: 0;
    font-family: Consolas, 'Courier New', monospace;
    font-size: 11px; font-weight: 700;
    padding: 6px 10px 10px 10px;
    margin-right: -6px;
    text-align: center;
}
/* Turn finished, nothing else expected — steady green. */
QPushButton.sessionTab[tabstate="done"] {
    background: rgba(40, 140, 70, 240);
    color: #e9fff0;
}
/* Turn finished but Claude asked a follow-up — green. */
QPushButton.sessionTab[tabstate="awaiting"] {
    background: rgba(60, 200, 110, 245);
    color: #062615;
}
/* Active tab: thicker white top border, sits visually on top. */
QPushButton.sessionTab[active="true"] {
    border: 2px solid #ffffff;
    border-bottom: none;
    padding: 6px 10px 12px 10px;
}
/* Pending approval — flashing bright red (overrides colour). */
QPushButton.sessionTab[flashing="true"][flash_color="red"] {
    background: rgba(255, 94, 94, 245);
    color: #0B1020;
}
/* Awaiting user reply — flashing bright green. */
QPushButton.sessionTab[flashing="true"][flash_color="green"] {
    background: rgba(95, 230, 140, 245);
    color: #062615;
}
"""

METER_CSS = """
QWidget#meter { background: rgba(8, 12, 20, 245); padding: 4px 6px 4px 6px; }
QLabel#metertext {
    color: #9FEED9; font-family: Consolas, monospace;
    font-size: 12px; font-weight: 600; padding: 2px 4px;
}
QProgressBar#ctxbar {
    background: rgba(40, 54, 78, 220);
    border: 1px solid rgba(255, 255, 255, 30);
    border-radius: 3px;
    text-align: center;
    color: #F2F5FA;
    font-family: Consolas, monospace;
    font-size: 11px;
    font-weight: 700;
}
QProgressBar#ctxbar::chunk { background: #487CFF; border-radius: 2px; }
QProgressBar#ctxbar[state="hot"]::chunk  { background: #ff7043; }
QProgressBar#ctxbar[state="crit"]::chunk { background: #ff2e2e; }
"""

SETTINGS_CSS = """
QDialog { background: #0B1020; }
QLabel#hdr    { color: #F2F5FA; font-family: Consolas, monospace; font-size: 13px; font-weight: 700; padding: 8px 4px 4px 4px; }
QLabel#sub    { color: #9FEED9; font-family: Consolas, monospace; font-size: 10px; padding: 0 4px 6px 4px; }
QLabel#cat    { color: #FAC496; font-family: Consolas, monospace; font-size: 11px; }
QLabel#empty  { color: #6b7280; font-family: Consolas, monospace; font-size: 10px; font-style: italic; padding: 4px; }
QLabel#helpSection {
    color: #F2F5FA; font-family: 'Segoe UI', sans-serif;
    font-size: 12px; padding: 2px 6px;
}
"""


HELP_TEXT = """\
<h2 style="color:#F2F5FA">CC-Beeper-Win — Help</h2>

<p style="color:#9FEED9">
The widget is a live tab strip, one tab per Claude Code session. Tabs
change colour to tell you what's going on; click the tab to switch the
view; click the sprite area to jump to that session's terminal (when
idle) or open the approval popup (when Claude is waiting on you).
</p>

<h3 style="color:#F2F5FA">Tab colours</h3>
<ul style="color:#F2F5FA; line-height:1.45">
  <li><b style="color:#ff9a9a">RED (steady)</b> — Claude is working on your prompt.</li>
  <li><b style="color:#ff4d4d">RED flashing</b> — Claude is asking permission for a tool call. Click the widget to Allow / Deny.</li>
  <li><b style="color:#57d9a3">GREEN (steady)</b> — Turn finished, nothing else expected. Free to send the next prompt.</li>
  <li><b style="color:#77e0a3">GREEN flashing</b> — Turn finished BUT Claude asked a follow-up question. Reply to continue.</li>
</ul>

<h3 style="color:#F2F5FA">Strategy</h3>
<p style="color:#9FEED9">Who actually decides on tool permissions:</p>
<ul style="color:#F2F5FA; line-height:1.45">
  <li><b>Assist</b> (default) — Widget is the permission UI. When Claude needs approval, the tab flashes and the 4-way popup lets you pick Allow once / session / forever / Deny. Approved categories are remembered so you don't keep re-answering the same question.</li>
  <li><b>Observer</b> — Widget only watches. Claude's own native permission prompt runs as normal in the terminal. No overrides. Use if you prefer CC's built-in allow-list UI.</li>
  <li><b>Auto</b> — Headless rules + optional Gemini Flash check. No widget popup. Only useful if you want lights-out operation with the safety layer still on.</li>
</ul>

<h3 style="color:#F2F5FA">Mode</h3>
<p style="color:#9FEED9">How lenient the auto-allow policy is — i.e. which
tool categories fly through without asking.</p>
<ul style="color:#F2F5FA; line-height:1.45">
  <li><b>Strict</b> — Ask for everything except Read-type tools.</li>
  <li><b>Relaxed</b> (default) — Read + search + git-read fly. Writes, installs, network, MCP writes ask.</li>
  <li><b>Trusted</b> — Adds project Writes and local git operations to the auto-allow list.</li>
  <li><b>YOLO</b> — Almost everything flies except: writes to config/credentials paths, and catastrophic destructive commands (rm -rf /, git push --force, etc.) which the Safety Net always blocks regardless of mode.</li>
</ul>

<h3 style="color:#F2F5FA">Manage trust…</h3>
<p style="color:#9FEED9">View and remove any categories you've approved. Persistent approvals survive restarts; session approvals are forgotten when you quit the widget.</p>

<h3 style="color:#F2F5FA">Shortcuts</h3>
<ul style="color:#F2F5FA; line-height:1.45">
  <li>Single-click sprite → focus that session's terminal window / approve pending</li>
  <li>Drag sprite → reposition widget</li>
  <li>Click tray icon → show/hide widget</li>
  <li>Right-click tray icon → menu (you're here)</li>
</ul>

<p style="color:#6b7280; font-size:10px; padding-top:8px">
Repo: <a href="https://github.com/RezaAlmiro/cc-beeper-win" style="color:#9FEED9">github.com/RezaAlmiro/cc-beeper-win</a>
</p>
"""


class HelpDialog(QDialog):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("CC-Beeper-Win — Help")
        self.setWindowFlags(self.windowFlags() | Qt.WindowType.WindowStaysOnTopHint)
        self.setStyleSheet(BUTTON_CSS + SETTINGS_CSS)
        self.resize(560, 620)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 10, 10, 10); outer.setSpacing(6)
        area = QScrollArea(); area.setWidgetResizable(True); area.setFrameShape(QScrollArea.Shape.NoFrame)
        inner = QWidget()
        inner.setStyleSheet("background: rgba(16, 22, 32, 240); border: 1px solid #2a3348; border-radius: 4px;")
        inner_layout = QVBoxLayout(inner); inner_layout.setContentsMargins(12, 10, 12, 10); inner_layout.setSpacing(0)
        body = QLabel(HELP_TEXT)
        body.setObjectName("helpSection")
        body.setTextFormat(Qt.TextFormat.RichText)
        body.setWordWrap(True)
        body.setOpenExternalLinks(True)
        inner_layout.addWidget(body)
        area.setWidget(inner)
        outer.addWidget(area, stretch=1)
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.hide)
        outer.addWidget(close_btn)


# ---------------------------------------------------------------------------
# Halo + approval popup + trust settings (mostly unchanged)
# ---------------------------------------------------------------------------

class HaloWindow(QWidget):
    def __init__(self, color_hex: str) -> None:
        super().__init__()
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowTransparentForInput
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self._color = QColor(color_hex)
        self._opacity_fx = QGraphicsOpacityEffect(self)
        self._opacity_fx.setOpacity(0.0)
        self.setGraphicsEffect(self._opacity_fx)
        self._anim: QPropertyAnimation | None = None

    def paintEvent(self, event) -> None:
        rect = self.rect()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        grad = QRadialGradient(rect.center(), max(rect.width(), rect.height()) / 2)
        inner = QColor(self._color); inner.setAlphaF(0.85)
        mid = QColor(self._color); mid.setAlphaF(0.45)
        outer = QColor(self._color); outer.setAlphaF(0.0)
        grad.setColorAt(0.0, inner); grad.setColorAt(0.55, mid); grad.setColorAt(1.0, outer)
        p.fillRect(rect, grad)

    def anchor_to(self, widget_geom: QRect, extra: int) -> None:
        self.setGeometry(widget_geom.adjusted(-extra, -extra, extra, extra))

    def start_pulse(self) -> None:
        self.stop_pulse()
        self.show()
        anim = QPropertyAnimation(self._opacity_fx, b"opacity", self)
        anim.setDuration(1100)
        anim.setStartValue(0.0); anim.setKeyValueAt(0.5, 1.0); anim.setEndValue(0.0)
        anim.setLoopCount(-1); anim.setEasingCurve(QEasingCurve.Type.InOutSine)
        anim.start()
        self._anim = anim

    def stop_pulse(self) -> None:
        if self._anim is not None:
            self._anim.stop(); self._anim = None
        self._opacity_fx.setOpacity(0.0)
        self.hide()


class ApprovalPopup(QWidget):
    def __init__(self, on_resolve) -> None:
        super().__init__()
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setStyleSheet(BUTTON_CSS + CARD_CSS)
        self._on_resolve = on_resolve

        outer = QVBoxLayout(self); outer.setContentsMargins(0, 0, 0, 0)
        self.card = QFrame(self); self.card.setObjectName("card")
        outer.addWidget(self.card)

        v = QVBoxLayout(self.card); v.setContentsMargins(8, 8, 8, 8); v.setSpacing(4)
        self.lbl_title = QLabel("Claude wants to run:"); self.lbl_title.setObjectName("title")
        self.lbl_meta = QLabel("");    self.lbl_meta.setObjectName("meta")
        self.lbl_summary = QLabel(""); self.lbl_summary.setObjectName("summary"); self.lbl_summary.setWordWrap(True)
        self.lbl_reason = QLabel("");  self.lbl_reason.setObjectName("reason");   self.lbl_reason.setWordWrap(True)
        for w in (self.lbl_title, self.lbl_meta, self.lbl_summary, self.lbl_reason):
            v.addWidget(w)

        btn_once = QPushButton("✓  Allow once")
        btn_session = QPushButton("✓✓  Allow for this session")
        btn_forever = QPushButton("✓✓✓  Allow forever (this category)")
        btn_deny = QPushButton("✗  Deny"); btn_deny.setObjectName("deny")
        btn_once.clicked.connect(lambda: on_resolve("allow", "once"))
        btn_session.clicked.connect(lambda: on_resolve("allow", "session"))
        btn_forever.clicked.connect(lambda: on_resolve("allow", "persistent"))
        btn_deny.clicked.connect(lambda: on_resolve("deny", "once"))
        for b in (btn_once, btn_session, btn_forever, btn_deny):
            v.addWidget(b)

        self.resize(320, 240)

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


class TrustSettings(QDialog):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("CC-Beeper-Win — Trust settings")
        self.setWindowFlags(self.windowFlags() | Qt.WindowType.WindowStaysOnTopHint)
        self.setStyleSheet(BUTTON_CSS + SETTINGS_CSS)
        self.resize(460, 520)

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
            b = QPushButton(label)
            if "Clear ALL" in label:
                b.setObjectName("deny")
            b.clicked.connect(cb)
            row.addWidget(b)
        outer.addLayout(row)

    def _hdr(self, t: str) -> QLabel:
        l = QLabel(t); l.setObjectName("hdr"); return l

    def _sub(self, t: str) -> QLabel:
        l = QLabel(t); l.setObjectName("sub"); l.setWordWrap(True); return l

    def _scroll(self) -> QScrollArea:
        area = QScrollArea(); area.setWidgetResizable(True); area.setFrameShape(QScrollArea.Shape.NoFrame)
        inner = QWidget()
        inner.setStyleSheet("background: rgba(16, 22, 32, 240); border: 1px solid #2a3348; border-radius: 4px;")
        layout = QVBoxLayout(inner); layout.setContentsMargins(6, 6, 6, 6); layout.setSpacing(4); layout.addStretch()
        area.setWidget(inner)
        return area

    def _populate(self, area: QScrollArea, categories: list[str]) -> None:
        layout = area.widget().layout()
        while layout.count() > 1:
            item = layout.takeAt(0)
            w = item.widget() if item else None
            if w is not None:
                w.deleteLater()
        if not categories:
            empty = QLabel("— none —"); empty.setObjectName("empty")
            layout.insertWidget(layout.count() - 1, empty); return
        for cat in categories:
            row_w = QWidget(); row = QHBoxLayout(row_w); row.setContentsMargins(4, 2, 4, 2); row.setSpacing(6)
            lbl = QLabel(cat); lbl.setObjectName("cat")
            lbl.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
            rm = QPushButton("✗ Remove"); rm.setObjectName("deny")
            rm.clicked.connect(lambda _=False, c=cat: self._remove(c))
            row.addWidget(lbl); row.addWidget(rm)
            layout.insertWidget(layout.count() - 1, row_w)

    def refresh(self) -> None:
        try:
            data = requests.get(server_url("/trust"), timeout=1.5).json()
        except Exception:
            data = {"session": [], "persistent": []}
        self._populate(self.persistent_area, data.get("persistent", []))
        self._populate(self.session_area, data.get("session", []))

    def showEvent(self, event) -> None:
        self.refresh(); super().showEvent(event)

    def _remove(self, c: str) -> None:
        try: requests.post(server_url("/trust/remove"), json={"category": c}, timeout=2)
        except Exception: pass
        self.refresh()

    def _clear_session(self) -> None:
        try:
            for c in (requests.get(server_url("/trust"), timeout=1.5).json() or {}).get("session", []):
                requests.post(server_url("/trust/remove"), json={"category": c}, timeout=1)
        except Exception: pass
        self.refresh()

    def _clear_all(self) -> None:
        try:
            d = requests.get(server_url("/trust"), timeout=1.5).json() or {}
            for c in d.get("session", []) + d.get("persistent", []):
                requests.post(server_url("/trust/remove"), json={"category": c}, timeout=1)
        except Exception: pass
        self.refresh()


# ---------------------------------------------------------------------------
# Session tab
# ---------------------------------------------------------------------------

class SessionTab(QPushButton):
    def __init__(self, session_id: str, parent=None) -> None:
        super().__init__(parent)
        self.session_id = session_id
        self.setProperty("class", "sessionTab")
        self.setProperty("active", False)
        self.setProperty("flashing", False)
        self.setProperty("tabstate", "working")
        self.setProperty("flash_color", "red")
        self.setMinimumHeight(18)
        self.setMaximumHeight(22)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self._flash_anim: QPropertyAnimation | None = None
        self._fx = QGraphicsOpacityEffect(self)
        self._fx.setOpacity(1.0)
        self.setGraphicsEffect(self._fx)

    def _repolish(self) -> None:
        self.style().unpolish(self); self.style().polish(self)

    def set_active(self, active: bool) -> None:
        self.setProperty("active", bool(active))
        self._repolish()

    def set_tabstate(self, state: str) -> None:
        if state == "done":
            tabstate = "done"
        elif state == "awaiting_input":
            tabstate = "awaiting"
        else:
            tabstate = "working"
        self.setProperty("tabstate", tabstate)
        self._repolish()

    def set_flashing(self, flash: bool, color: str = "red") -> None:
        self.setProperty("flashing", bool(flash))
        self.setProperty("flash_color", color)
        self._repolish()
        if flash:
            if self._flash_anim is None:
                a = QPropertyAnimation(self._fx, b"opacity", self)
                a.setDuration(650); a.setStartValue(1.0); a.setKeyValueAt(0.5, 0.4); a.setEndValue(1.0)
                a.setLoopCount(-1); a.setEasingCurve(QEasingCurve.Type.InOutSine); a.start()
                self._flash_anim = a
        else:
            if self._flash_anim is not None:
                self._flash_anim.stop(); self._flash_anim = None
            self._fx.setOpacity(1.0)


# ---------------------------------------------------------------------------
# Widget
# ---------------------------------------------------------------------------

class BeeperWidget(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.cfg = load_cfg()
        w_cfg = self.cfg.get("widget", {})
        self.w_px = int(w_cfg.get("width", 180))
        self.h_px = int(w_cfg.get("height", 120))  # slightly taller to fit tabs+meter

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setWindowTitle("CC-Beeper-Win")
        self.setStyleSheet(BUTTON_CSS + CARD_CSS + TAB_CSS + METER_CSS)

        container = QWidget(self); container.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setCentralWidget(container)
        root = QVBoxLayout(container); root.setContentsMargins(0, 0, 0, 0); root.setSpacing(0)

        # --- tab bar ---
        self.tabbar = QWidget(container); self.tabbar.setObjectName("tabbar")
        self.tabbar.setFixedHeight(34)
        self.tabbar_layout = QHBoxLayout(self.tabbar)
        self.tabbar_layout.setContentsMargins(6, 2, 6, 0); self.tabbar_layout.setSpacing(0)
        # Tabs use margin-right: -6px for notebook-folder overlap; equal stretch.
        root.addWidget(self.tabbar)

        # --- sprite ---
        self.sprite = QLabel(container)
        self.sprite.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.sprite.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.sprite.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        root.addWidget(self.sprite, stretch=1)

        # Red border overlay (absolute-positioned)
        self.border_overlay = QWidget(container)
        self.border_overlay.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        border_color = w_cfg.get("border_color", "#ff2e2e")
        self.border_overlay.setStyleSheet(
            f"background: transparent; border: 2px solid {border_color}; border-radius: 6px;"
        )
        self._border_opacity_fx = QGraphicsOpacityEffect(self.border_overlay)
        self._border_opacity_fx.setOpacity(0.0)
        self.border_overlay.setGraphicsEffect(self._border_opacity_fx)
        self._border_anim: QPropertyAnimation | None = None

        # --- meter ---
        self.meter = QWidget(container); self.meter.setObjectName("meter"); self.meter.setFixedHeight(56)
        meter_lay = QVBoxLayout(self.meter)
        meter_lay.setContentsMargins(6, 4, 6, 4); meter_lay.setSpacing(3)
        self.ctx_bar = QProgressBar(self.meter); self.ctx_bar.setObjectName("ctxbar")
        self.ctx_bar.setMinimum(0); self.ctx_bar.setMaximum(100); self.ctx_bar.setValue(0)
        self.ctx_bar.setFixedHeight(22); self.ctx_bar.setTextVisible(True)
        self.ctx_bar.setFormat("ctx —")
        self.tok_lbl = QLabel(""); self.tok_lbl.setObjectName("metertext")
        self.tok_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        meter_lay.addWidget(self.ctx_bar)
        meter_lay.addWidget(self.tok_lbl)
        root.addWidget(self.meter)

        # --- halo, state, tabs ---
        halo_color = w_cfg.get("halo_color", "#ff5e5e")
        self.halo = HaloWindow(halo_color)
        self._halo_extra = int(w_cfg.get("halo_radius", 8))

        self._tabs: dict[str, SessionTab] = {}
        self._active_session: str | None = None
        self._sessions_snapshot: dict[str, dict[str, Any]] = {}
        self._pixmap_cache: dict[str, QPixmap] = {}
        self._dragging = False
        self._drag_offset = QPoint(); self._drag_moved = False

        self.resize(self.w_px, self.h_px)
        self._dock_corner = w_cfg.get("corner", "bottom-right")
        self._dock_margin = int(w_cfg.get("margin", 16))
        self._dock_to_corner()

        self._popup = ApprovalPopup(self._resolve)
        self._settings = TrustSettings()
        self._help = HelpDialog()
        # Tray-menu selection state — must exist before _build_tray runs.
        self._strategy_actions: dict[str, QAction] = {}
        self._mode_actions: dict[str, QAction] = {}
        self._current_strategy: str | None = None
        self._current_mode: str | None = None
        self._tray = self._build_tray()

        self._timer = QTimer(self); self._timer.timeout.connect(self._tick); self._timer.start(POLL_MS)

    # -- layout -----------------------------------------------------------

    def _dock_to_corner(self) -> None:
        scr = QGuiApplication.primaryScreen().availableGeometry()
        corner = self._dock_corner; margin = self._dock_margin
        x = scr.right() - self.width() - margin if "right" in corner else scr.left() + margin
        y = scr.bottom() - self.height() - margin if "bottom" in corner else scr.top() + margin
        self.move(x, y)
        self.halo.anchor_to(self.frameGeometry(), self._halo_extra)
        self.border_overlay.setGeometry(self.centralWidget().rect())

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self.border_overlay.setGeometry(self.centralWidget().rect())
        self.halo.anchor_to(self.frameGeometry(), self._halo_extra)

    def moveEvent(self, event) -> None:
        super().moveEvent(event)
        self.halo.anchor_to(self.frameGeometry(), self._halo_extra)

    # -- sprite + state ---------------------------------------------------

    def _load_pixmap(self, fname: str) -> QPixmap:
        if fname not in self._pixmap_cache:
            p = ASSETS / fname
            self._pixmap_cache[fname] = QPixmap(str(p)) if p.exists() else QPixmap()
        return self._pixmap_cache[fname]

    def _tick(self) -> None:
        try:
            r = requests.get(server_url("/sessions"), timeout=0.8)
            data = r.json() if r.status_code == 200 else {}
        except requests.RequestException:
            data = {}
        # Keep the tray menu's tick marks in sync with the server
        self._sync_menu_selections(data.get("strategy"), data.get("mode"))

        sessions = data.get("sessions", [])
        snapshot = {s["session_id"]: s for s in sessions}
        self._sessions_snapshot = snapshot

        self._refresh_tabs(sessions)

        if not sessions:
            self._render_empty(data.get("strategy"), data.get("mode"))
            return

        # Pick active session: user-chosen if still present, otherwise the
        # most-recent or any pending session.
        active = self._active_session if self._active_session in snapshot else None
        if active is None:
            # prefer any with pending
            for s in sessions:
                if s.get("pending"):
                    active = s["session_id"]; break
            if active is None:
                active = sessions[0]["session_id"]
            self._active_session = active
            for sid, tab in self._tabs.items():
                tab.set_active(sid == active)

        self._render_session(snapshot[active])

    def _refresh_tabs(self, sessions: list[dict[str, Any]]) -> None:
        # Remove tabs for gone sessions
        current_ids = {s["session_id"] for s in sessions}
        for sid in list(self._tabs.keys()):
            if sid not in current_ids:
                tab = self._tabs.pop(sid)
                self.tabbar_layout.removeWidget(tab)
                tab.deleteLater()
                if self._active_session == sid:
                    self._active_session = None
        # Add/update tabs
        for s in sessions:
            sid = s["session_id"]
            label = short_label(s.get("first_task", ""), s.get("cwd", ""), sid)
            tab = self._tabs.get(sid)
            if tab is None:
                tab = SessionTab(sid, self.tabbar)
                tab.clicked.connect(lambda _=False, x=sid: self._select_session(x))
                # equal stretch factor → every tab gets the same width
                self.tabbar_layout.addWidget(tab, 1)
                self._tabs[sid] = tab
            state = s.get("state", "working")
            tab.setText(label)
            tab.setToolTip(self._tab_tooltip(s))
            tab.set_tabstate(state)
            has_pending = bool(s.get("pending"))
            # Flash colour depends on what kind of attention: permission
            # prompts / tool approvals → red; Claude asking a follow-up
            # question → green. "done" alone is NOT flashing.
            if has_pending or state in {"allow", "input", "error"}:
                tab.set_flashing(True, color="red")
            elif state == "awaiting_input":
                tab.set_flashing(True, color="green")
            else:
                tab.set_flashing(False)

    def _tab_tooltip(self, s: dict[str, Any]) -> str:
        lines = [
            f"session: {s['session_id'][:8]}",
        ]
        ft = s.get("first_task", "")
        if ft:
            lines.append(f"topic: {ft[:120]}")
        lines.append(f"cwd: {s.get('cwd','')}")
        lines.append(f"state: {s.get('state','?')}")
        stats = s.get("stats") or {}
        if stats:
            lines.append(f"model: {stats.get('model_label','?')}")
            lines.append(f"ctx: {stats.get('context_pct','?')}%  ({stats.get('current_context',0):,}/{stats.get('context_limit',0):,})")
            lines.append(f"in: {stats.get('total_input',0):,}  out: {stats.get('total_output',0):,}")
        return "\n".join(lines)

    def _select_session(self, sid: str) -> None:
        if sid not in self._sessions_snapshot:
            return
        self._active_session = sid
        for other_sid, tab in self._tabs.items():
            tab.set_active(other_sid == sid)
        self._render_session(self._sessions_snapshot[sid])

    def _render_empty(self, strategy: str | None, mode: str | None) -> None:
        pm = self._load_pixmap("snoozing.png")
        if not pm.isNull():
            h = max(40, self.height() - self.tabbar.height() - self.meter.height() - 6)
            scaled = pm.scaled(self.width() - 8, h,
                Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                Qt.TransformationMode.SmoothTransformation)
            self.sprite.setPixmap(scaled)
        self.sprite.setToolTip(f"no active sessions\nstrategy: {strategy or '?'}\nmode: {mode or '?'}")
        self._stop_alert()
        self.ctx_bar.setFormat("no session")
        self.ctx_bar.setValue(0)
        self.tok_lbl.setText("")

    def _render_session(self, s: dict[str, Any]) -> None:
        state = s.get("state", "snoozing")
        fname = STATE_TO_SPRITE.get(state, "snoozing.png")
        pm = self._load_pixmap(fname)
        if not pm.isNull():
            h = max(40, self.height() - self.tabbar.height() - self.meter.height() - 6)
            scaled = pm.scaled(self.width() - 8, h,
                Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                Qt.TransformationMode.SmoothTransformation)
            self.sprite.setPixmap(scaled)

        self.sprite.setToolTip(self._sprite_tooltip(s))

        pending = s.get("pending") or []
        has_pending = bool(pending)
        if has_pending:
            self._start_alert()
            if self._popup.isVisible():
                self._popup.show_for(pending[0], self.frameGeometry())
        else:
            self._stop_alert()
            if self._popup.isVisible():
                self._popup.hide()

        stats = s.get("stats") or {}
        self._render_meter(stats)

    def _sprite_tooltip(self, s: dict[str, Any]) -> str:
        lines = [
            f"session: {s['session_id'][:8]}",
            f"state: {s.get('state','?')}",
        ]
        if s.get("tool"):
            lines.append(f"tool: {s['tool']}")
        if s.get("category"):
            lines.append(f"category: {s['category']}")
        if s.get("cwd"):
            lines.append(f"cwd: {s['cwd']}")
        lines.append("click to focus terminal / approve")
        return "\n".join(lines)

    def _render_meter(self, stats: dict[str, Any]) -> None:
        if not stats:
            self.ctx_bar.setFormat("ctx —")
            self.ctx_bar.setValue(0)
            self.tok_lbl.setText("")
            self.ctx_bar.setProperty("state", "")
            self.ctx_bar.style().unpolish(self.ctx_bar); self.ctx_bar.style().polish(self.ctx_bar)
            return
        pct = stats.get("context_pct", 0.0) or 0.0
        cur = stats.get("current_context", 0) or 0
        lim = stats.get("context_limit", 0) or 0
        label = stats.get("model_label", "?")
        self.ctx_bar.setValue(int(min(100, pct)))
        self.ctx_bar.setFormat(f"{label}  {pct:.0f}%  {fmt_tokens(cur)}/{fmt_tokens(lim)}")
        if pct > 85:
            self.ctx_bar.setProperty("state", "crit")
        elif pct > 60:
            self.ctx_bar.setProperty("state", "hot")
        else:
            self.ctx_bar.setProperty("state", "")
        self.ctx_bar.style().unpolish(self.ctx_bar); self.ctx_bar.style().polish(self.ctx_bar)
        self.tok_lbl.setText(
            f"in {fmt_tokens(stats.get('total_input',0))}  ·  "
            f"out {fmt_tokens(stats.get('total_output',0))}  ·  "
            f"cache {fmt_tokens(stats.get('total_cache_read',0))}"
        )

    # -- alert animations -------------------------------------------------

    def _start_alert(self) -> None:
        self.halo.anchor_to(self.frameGeometry(), self._halo_extra)
        self.halo.start_pulse()
        if self._border_anim is not None:
            self._border_anim.stop()
        anim = QPropertyAnimation(self._border_opacity_fx, b"opacity", self)
        anim.setDuration(900)
        anim.setStartValue(0.0); anim.setKeyValueAt(0.5, 1.0); anim.setEndValue(0.0)
        anim.setLoopCount(-1); anim.setEasingCurve(QEasingCurve.Type.InOutSine); anim.start()
        self._border_anim = anim

    def _stop_alert(self) -> None:
        self.halo.stop_pulse()
        if self._border_anim is not None:
            self._border_anim.stop(); self._border_anim = None
        self._border_opacity_fx.setOpacity(0.0)

    # -- drag + click -----------------------------------------------------

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._dragging = True; self._drag_moved = False
            self._drag_offset = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()

    def mouseMoveEvent(self, event) -> None:
        if self._dragging:
            self._drag_moved = True
            self.move(event.globalPosition().toPoint() - self._drag_offset)
            event.accept()

    def mouseReleaseEvent(self, event) -> None:
        if self._dragging and not self._drag_moved and event.button() == Qt.MouseButton.LeftButton:
            self._handle_click()
        self._dragging = False

    def _handle_click(self) -> None:
        sid = self._active_session
        if not sid:
            return
        s = self._sessions_snapshot.get(sid) or {}
        pending = s.get("pending") or []
        if pending:
            self._popup.show_for(pending[0], self.frameGeometry())
        else:
            self._focus_terminal(s.get("terminal_hwnd"))

    def _focus_terminal(self, hwnd: int | None) -> None:
        """Raise the terminal window. SetForegroundWindow on its own is
        rate-limited by Windows (it'll just flash the taskbar button if the
        calling process isn't already foreground). We work around that with
        the standard input-attach / alt-tap trick so the window actually
        comes to the front."""
        if not hwnd:
            return
        try:
            import ctypes
            from ctypes import wintypes
            import win32gui  # type: ignore
            import win32con  # type: ignore
            import win32process  # type: ignore

            user32 = ctypes.windll.user32
            hwnd_i = int(hwnd)

            # If minimized, restore first.
            if win32gui.IsIconic(hwnd_i):
                win32gui.ShowWindow(hwnd_i, win32con.SW_RESTORE)

            # Trick #1: tap Alt to give our thread the right to change foreground.
            user32.keybd_event(0x12, 0, 0, 0)        # Alt down
            user32.keybd_event(0x12, 0, 0x0002, 0)   # Alt up

            # Trick #2: attach our input queue to the target's thread. Once
            # attached Windows treats our thread as same-foreground and the
            # SetForegroundWindow actually lands.
            cur_tid = ctypes.windll.kernel32.GetCurrentThreadId()
            target_tid, _ = win32process.GetWindowThreadProcessId(hwnd_i)
            attached = False
            try:
                if target_tid and target_tid != cur_tid:
                    if user32.AttachThreadInput(target_tid, cur_tid, True):
                        attached = True
                win32gui.BringWindowToTop(hwnd_i)
                win32gui.ShowWindow(hwnd_i, win32con.SW_SHOW)
                try:
                    win32gui.SetForegroundWindow(hwnd_i)
                except Exception:
                    pass
                try:
                    win32gui.SetFocus(hwnd_i)
                except Exception:
                    pass
                # Last resort: SwitchToThisWindow (undocumented but reliable).
                try:
                    user32.SwitchToThisWindow(wintypes.HWND(hwnd_i), True)
                except Exception:
                    pass
            finally:
                if attached:
                    user32.AttachThreadInput(target_tid, cur_tid, False)
        except Exception:
            pass

    # -- resolve ----------------------------------------------------------

    def _resolve(self, decision: str, scope: str) -> None:
        sid = self._active_session
        if not sid:
            self._popup.hide(); return
        s = self._sessions_snapshot.get(sid) or {}
        pending = s.get("pending") or []
        if not pending:
            self._popup.hide(); return
        rid = pending[0].get("id")
        try:
            requests.post(server_url("/resolve"),
                          json={"request_id": rid, "decision": decision, "scope": scope},
                          timeout=2)
        except Exception as e:
            self._tray.showMessage("CC-Beeper-Win", f"resolve error: {e}", QSystemTrayIcon.MessageIcon.Warning, 2500)
        finally:
            self._popup.hide()

    # -- tray -------------------------------------------------------------

    def _build_tray(self) -> QSystemTrayIcon:
        icon_path = ASSETS / "snoozing.png"
        icon = QIcon(str(icon_path)) if icon_path.exists() else QIcon()
        tray = QSystemTrayIcon(icon, self); tray.setToolTip("CC-Beeper-Win")
        self._tray_menu = QMenu()
        menu = self._tray_menu

        show_act = QAction("Show / Hide widget", self); show_act.triggered.connect(self._toggle_visibility)
        menu.addAction(show_act)
        menu.addSeparator()

        # Strategy submenu — items are checkable so the current selection
        # shows a tick next to it.
        self._strat_menu = menu.addMenu("Strategy:  …")
        for label, value in (
            ("Assist  (widget decides)  [default]", "assist"),
            ("Observer  (never override Claude)",   "observer"),
            ("Auto  (headless rules + Gemini)",     "auto"),
        ):
            a = QAction(label, self)
            a.setCheckable(True)
            a.triggered.connect(lambda _=False, v=value: self._set_strategy(v))
            self._strat_menu.addAction(a)
            self._strategy_actions[value] = a

        self._mode_menu = menu.addMenu("Mode:  …")
        for m, descr in (
            ("strict",  "ask for everything except reads"),
            ("relaxed", "reads + git-read fly; writes + network ask  [default]"),
            ("trusted", "+ project writes and local git operations"),
            ("yolo",    "auto-allow almost everything (safety net still on)"),
        ):
            a = QAction(f"{m.upper()}  —  {descr}", self)
            a.setCheckable(True)
            a.triggered.connect(lambda _=False, mode=m: self._set_mode(mode))
            self._mode_menu.addAction(a)
            self._mode_actions[m] = a

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

    def _sync_menu_selections(self, strategy: str | None, mode: str | None) -> None:
        """Reflect the server-side current strategy/mode in the tray menu
        (tick marks + submenu titles). Called on every poll."""
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

    def _show_help(self) -> None:
        self._help.show(); self._help.raise_(); self._help.activateWindow()

    def _toggle_visibility(self) -> None:
        (self.hide if self.isVisible() else self.show)()
        if self.isVisible(): self.raise_()

    def _set_strategy(self, value: str) -> None:
        try:
            r = requests.post(server_url("/strategy"), json={"strategy": value}, timeout=2)
            if r.status_code == 200 and r.json().get("ok"):
                self._tray.showMessage("CC-Beeper-Win", f"Strategy: {value}", QSystemTrayIcon.MessageIcon.Information, 1800)
        except Exception as e:
            self._tray.showMessage("CC-Beeper-Win", f"strategy switch failed: {e}", QSystemTrayIcon.MessageIcon.Warning, 2500)

    def _set_mode(self, mode: str) -> None:
        try:
            r = requests.post(server_url("/mode"), json={"mode": mode}, timeout=2)
            if r.status_code == 200 and r.json().get("ok"):
                self._tray.showMessage("CC-Beeper-Win", f"Mode: {mode.upper()}", QSystemTrayIcon.MessageIcon.Information, 1800)
        except Exception as e:
            self._tray.showMessage("CC-Beeper-Win", f"mode switch failed: {e}", QSystemTrayIcon.MessageIcon.Warning, 2500)

    def _show_settings(self) -> None:
        self._settings.refresh(); self._settings.show(); self._settings.raise_(); self._settings.activateWindow()

    def _clear_session_trust(self) -> None:
        try:
            for c in (requests.get(server_url("/trust"), timeout=1).json() or {}).get("session", []):
                requests.post(server_url("/trust/remove"), json={"category": c}, timeout=1)
        except Exception: pass


def main() -> int:
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    w = BeeperWidget(); w.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
