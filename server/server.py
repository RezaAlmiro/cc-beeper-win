"""CC-Beeper-Win local hook server.

Listens on 127.0.0.1:19222-19230 for Claude Code hook HTTP POSTs.

v2c features:
  * Per-session state (one tab per session in the widget)
  * Transcript parsing: model auto-detect, context-window %, token totals
  * Category classifier + regex + Gemini security layer
  * Persistent + session trust
  * Blocking PreToolUse gate with widget-side 4-way approval ladder
  * Terminal HWND resolved at hook time for click-through focus
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

# Optional .env loading for the (optional) Gemini classifier key.
# Looks in the project root first, then its parent directory. Missing file
# is fine — Gemini is disabled by default.
try:
    from dotenv import load_dotenv
    _project_root = Path(__file__).resolve().parents[1]
    for candidate in (_project_root / ".env", _project_root.parent / ".env"):
        if candidate.exists():
            load_dotenv(candidate)
            break
except ImportError:
    pass

sys.path.insert(0, str(Path(__file__).resolve().parent))
from classify import classify  # noqa: E402
from trust import TrustStore, mode_decision  # noqa: E402
from security import regex_flags, gemini_classify, GEMINI_CHECK_CATEGORIES  # noqa: E402
from stats import parse_transcript, first_user_prompt, latest_assistant_narrative  # noqa: E402
from usage import aggregate_usage  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[logging.FileHandler(ROOT / "server.log", encoding="utf-8"), logging.StreamHandler()],
)
log = logging.getLogger("cc-beeper.server")


def load_config() -> dict[str, Any]:
    with CONFIG_PATH.open(encoding="utf-8") as f:
        return json.load(f)


def find_free_port(start: int, end: int) -> int:
    for port in range(start, end + 1):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise RuntimeError(f"no free port in range {start}-{end}")


app = FastAPI(title="CC-Beeper-Win")
CFG = load_config()
TRUST = TrustStore(ROOT / CFG.get("trusted_categories_file", "trust.json"))

# Pending user-decisions, awaited by the blocked PreToolUse handler.
PENDING: dict[str, dict[str, Any]] = {}
PENDING_LOCK = asyncio.Lock()
PENDING_TIMEOUT_S = 300

# Session-keyed state. Each session has:
#   - task, cwd, transcript_path, model, stats, ts, stopped_at
#   - state: snoozing|working|done|error|allow|input|listening
#   - tool, category, message, expires_at
#   - cc_shell_pid, cc_ppid, terminal_hwnd
# A session can have AT MOST ONE pending request at a time (stored in PENDING).
SESSIONS: dict[str, dict[str, Any]] = {}

# Transcript stats cache: session_id -> {stats_dict, mtime, fetched_at}
STATS_CACHE: dict[str, dict[str, Any]] = {}
STATS_MIN_REFRESH_S = 1.5

# Sessions the user has explicitly closed via the widget. Future hook
# payloads with these IDs are silently dropped so the tab doesn't come
# back on the next tool call. In-memory only — cleared on server restart.
DISMISSED_SESSIONS: set[str] = set()


def _now() -> float:
    return time.time()


def _session(session_id: str) -> dict[str, Any]:
    return SESSIONS.setdefault(session_id, {
        "state": "snoozing",
        "tool": None,
        "category": None,
        "message": None,
        "expires_at": 0.0,
        "ts": _now(),
        "awaiting_input": False,   # True = Claude asked a question and is waiting on user
    })


def _set_state(session_id: str, state: str, *, tool: str | None = None,
               category: str | None = None, message: str | None = None,
               ttl_s: float = 0.0) -> None:
    s = _session(session_id)
    s["state"] = state
    s["tool"] = tool
    s["category"] = category
    s["message"] = message
    s["expires_at"] = _now() + ttl_s if ttl_s > 0 else 0.0
    s["ts"] = _now()


async def safe_json(request: Request) -> dict[str, Any]:
    raw = await request.body()
    if not raw:
        return {}
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("utf-8", errors="replace")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"_raw": text[:500]}


def _is_dismissed(session_id: str) -> bool:
    return session_id in DISMISSED_SESSIONS


# ---------------------------------------------------------------------------
# Terminal HWND resolution
# ---------------------------------------------------------------------------

# Process-name priority for picking which ancestor hosts the visible UI.
# Lower number = higher priority. True terminal emulators with their own
# visible top-level window come first; shells/console-hosts come last as
# fallback (their "windows" are often zero-size pseudo-console helpers).
TERMINAL_PROC_PRIORITY: dict[str, int] = {
    "WindowsTerminal.exe": 0,
    "wt.exe":              0,
    "WezTerm.exe":         0,
    "wezterm-gui.exe":     0,
    "alacritty.exe":       0,
    "mintty.exe":          0,
    "Hyper.exe":           0,
    "ConEmu64.exe":        1,
    "ConEmuC64.exe":       1,
    "Cmder.exe":           1,
    "powershell.exe":      3,
    "pwsh.exe":            3,
    "cmd.exe":             3,
    "bash.exe":            3,
    "git-bash.exe":        3,
    "conhost.exe":         5,
    "OpenConsole.exe":     5,
}

# Window classes we always reject (hidden console helpers).
HIDDEN_WINDOW_CLASSES = {
    "PseudoConsoleWindow",
    "ConsoleWindowClass",  # traditional cmd/pwsh console
}

# Window classes that belong to a real terminal emulator UI. When we see
# one of these we accept the window even if Windows has cloaked it offscreen
# (rect at -25600,-25600 happens when a WT window is minimised).
TERMINAL_UI_CLASSES = {
    "CASCADIA_HOSTING_WINDOW_CLASS",   # Windows Terminal
    "org.wezfurlong.wezterm",          # WezTerm
    "Alacritty",                        # Alacritty
    "mintty",                           # mintty / Git Bash
    "ConEmuMainClass",                  # ConEmu
    "Hyper",                            # Hyper
}


def _claude_pid_for_pid(pid: int | None) -> int | None:
    """Walk the ancestor chain looking for a claude.exe / node (Claude Code
    CLI) process. That's the long-lived PID we use to judge whether the
    session is still alive."""
    if not pid:
        return None
    try:
        import psutil  # type: ignore
    except Exception:
        return None
    try:
        p = psutil.Process(pid)
    except Exception:
        return None
    CLAUDE_NAMES = {"claude.exe", "claude-code.exe"}
    for ancestor in [p] + list(p.parents()):
        try:
            name = ancestor.name()
        except Exception:
            continue
        if name in CLAUDE_NAMES:
            return ancestor.pid
    return None


def _enumerate_terminal_ui_windows() -> list[tuple[int, str, str]]:
    """All visible top-level windows whose class is a known terminal UI.
    Returns [(hwnd, class, title)]. Used as a fallback when the process
    tree doesn't reach a terminal emulator (e.g. Windows Terminal
    re-parents its shells to explorer.exe, hiding the WT ancestor)."""
    try:
        import win32gui  # type: ignore
    except Exception:
        return []
    hits: list[tuple[int, str, str]] = []

    def enum(hwnd: int, _: Any) -> None:
        if not win32gui.IsWindowVisible(hwnd):
            return
        try:
            cls = win32gui.GetClassName(hwnd) or ""
        except Exception:
            return
        if cls not in TERMINAL_UI_CLASSES:
            return
        hits.append((hwnd, cls, win32gui.GetWindowText(hwnd) or ""))
    try:
        win32gui.EnumWindows(enum, None)
    except Exception:
        return []
    return hits


def _terminal_hwnd_by_title(cwd_hint: str, task_hint: str) -> int | None:
    """Pick a terminal-UI window whose title best matches the session's cwd
    basename or first-prompt keywords. Returns None if no reasonable match."""
    windows = _enumerate_terminal_ui_windows()
    if not windows:
        return None
    if len(windows) == 1:
        return windows[0][0]

    hints: list[str] = []
    if cwd_hint:
        base = cwd_hint.replace("\\", "/").rstrip("/").split("/")[-1].lower()
        if base:
            hints.append(base)
    for w in (task_hint or "").split():
        w = w.strip("/,.:;'\"").lower()
        if len(w) >= 4:
            hints.append(w)

    scored: list[tuple[int, int]] = []  # (score, hwnd)
    for hwnd, _cls, title in windows:
        lt = title.lower()
        score = 0
        # Strong signal: Claude Code sets the WT tab title to "… Claude Code"
        if "claude code" in lt:
            score += 20
        elif "claude" in lt:
            score += 10
        for h in hints:
            if h and h in lt:
                score += 5
        scored.append((score, hwnd))
    scored.sort(reverse=True)
    best_score, best_hwnd = scored[0]
    # If no window had any meaningful match, return None so the UI can say
    # "terminal not found" rather than focusing an unrelated WT tab.
    return best_hwnd if best_score > 0 else None


def _terminal_hwnd_for_pid(pid: int | None) -> int | None:
    if not pid:
        return None
    try:
        import psutil  # type: ignore
        import win32gui  # type: ignore
        import win32process  # type: ignore
    except Exception:
        return None
    try:
        p = psutil.Process(pid)
    except Exception:
        return None

    # Walk ancestors collecting (priority, pid) for every terminal-ish proc.
    candidates: list[tuple[int, int]] = []
    for ancestor in [p] + list(p.parents()):
        try:
            name = ancestor.name()
        except Exception:
            continue
        pr = TERMINAL_PROC_PRIORITY.get(name)
        if pr is not None:
            candidates.append((pr, ancestor.pid))
    if not candidates:
        # Process tree didn't reach a terminal — common when WT re-parents
        # its shells to explorer.exe. Fall back to class-based enumeration.
        return None
    candidates.sort(key=lambda c: c[0])

    # Enumerate real top-level windows for each candidate PID, in priority
    # order. Pick the first that looks like a legitimate visible terminal.
    for _, tpid in candidates:
        hits: list[int] = []

        def enum(hwnd: int, _: Any) -> None:
            if not win32gui.IsWindowVisible(hwnd):
                return
            try:
                _, wpid = win32process.GetWindowThreadProcessId(hwnd)
            except Exception:
                return
            if wpid != tpid:
                return
            try:
                cls = win32gui.GetClassName(hwnd) or ""
            except Exception:
                cls = ""
            if cls in HIDDEN_WINDOW_CLASSES:
                return
            # Known terminal-UI classes: accept unconditionally. Windows can
            # cloak a minimised WT window to (-25600, -25600) with a tiny
            # rect, and it's still the correct HWND to SetForegroundWindow.
            if cls in TERMINAL_UI_CLASSES:
                hits.append(hwnd)
                return
            # Everything else: require a realistic size and reject tool
            # windows / owned popups.
            try:
                l, t, r, b = win32gui.GetWindowRect(hwnd)
            except Exception:
                return
            if (r - l) < 80 or (b - t) < 40:
                return
            try:
                import win32con  # type: ignore
                ex_style = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
                if ex_style & win32con.WS_EX_TOOLWINDOW:
                    return
            except Exception:
                pass
            hits.append(hwnd)

        try:
            win32gui.EnumWindows(enum, None)
        except Exception:
            continue
        if hits:
            return hits[0]
    return None


# ---------------------------------------------------------------------------
# Stats helpers
# ---------------------------------------------------------------------------

def _session_stats(session_id: str) -> dict[str, Any]:
    s = SESSIONS.get(session_id) or {}
    tp = s.get("transcript_path")
    if not tp:
        return {}
    try:
        mtime = os.path.getmtime(tp)
    except OSError:
        return {}
    cache = STATS_CACHE.get(session_id)
    now = _now()
    if cache and cache.get("mtime") == mtime and (now - cache.get("fetched_at", 0)) < STATS_MIN_REFRESH_S:
        return cache["stats"]
    stats = parse_transcript(tp)
    if stats is None:
        return cache.get("stats", {}) if cache else {}
    d = stats.to_dict()
    STATS_CACHE[session_id] = {"stats": d, "mtime": mtime, "fetched_at": now}
    return d


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/health")
async def health() -> dict[str, Any]:
    return {
        "ok": True,
        "mode": CFG.get("mode"),
        "strategy": CFG.get("decision_strategy"),
        "sessions": len(SESSIONS),
        "trust": TRUST.list_all(),
    }


def _is_session_alive(s: dict[str, Any]) -> bool:
    """Is the Claude Code process that owns this session still running?
    Use the resolved claude.exe PID (long-lived) — not the hook's shell/ppid
    which are short-lived bash-c PIDs that vanish between hooks.
    Returns True if we can't tell (don't prune on uncertainty)."""
    pid = s.get("cc_claude_pid")
    if not pid:
        # We haven't been able to resolve a claude.exe ancestor yet — keep
        # the session; we'll sweep once we can actually disprove liveness.
        return True
    try:
        import psutil  # type: ignore
    except Exception:
        return True
    try:
        return psutil.pid_exists(int(pid))
    except Exception:
        return True


# Sessions whose Claude process exited this long ago (seconds) are swept.
# Short because once the tab is closed, there's no point keeping it visible.
DEAD_SESSION_TTL_S = 8


def _sweep_dead_sessions() -> list[str]:
    """Remove sessions whose host Claude process has exited. Returns the
    list of session IDs that were removed."""
    now = _now()
    removed: list[str] = []
    for sid, s in list(SESSIONS.items()):
        if _is_session_alive(s):
            # reset dead-first-seen marker if process came back (shouldn't,
            # but be safe)
            s.pop("_dead_first_seen_at", None)
            continue
        first = s.get("_dead_first_seen_at")
        if first is None:
            s["_dead_first_seen_at"] = now
            continue
        if (now - first) >= DEAD_SESSION_TTL_S:
            removed.append(sid)
            SESSIONS.pop(sid, None)
            STATS_CACHE.pop(sid, None)
    return removed


@app.get("/sessions")
async def sessions_endpoint() -> dict[str, Any]:
    now = _now()
    swept = _sweep_dead_sessions()
    if swept:
        log.info("swept dead sessions: %s", [s[:8] for s in swept])

    # Opportunistic HWND re-resolution: if any live session is missing a
    # terminal_hwnd, retry via PID walk AND via title-based window match.
    # Lets click-to-focus / slash-commands recover after a hook that
    # couldn't resolve the first time.
    for s in SESSIONS.values():
        if s.get("terminal_hwnd"):
            continue
        h = (
            _terminal_hwnd_for_pid(s.get("cc_claude_pid"))
            or _terminal_hwnd_by_title(s.get("cwd", ""), s.get("first_task", ""))
        )
        if h:
            s["terminal_hwnd"] = h

    out: list[dict[str, Any]] = []
    for sid, s in SESSIONS.items():
        # auto-revert transient states
        if s.get("expires_at") and now > s["expires_at"]:
            # Don't kill snoozing-revert if still pending
            has_pending = any(p["session_id"] == sid for p in PENDING.values())
            if not has_pending:
                s["state"] = "snoozing"
                s["expires_at"] = 0.0
        pending_here = [
            {
                "id": rid,
                "tool": p["tool"],
                "category": p["category"],
                "summary": p["summary"],
                "reason": p.get("reason", ""),
                "age_s": round(now - p["created_at"], 1),
            }
            for rid, p in PENDING.items() if p.get("session_id") == sid
        ]
        first_task = s.get("first_task") or ""
        if not first_task and s.get("transcript_path"):
            # Session started before our hook captured the prompt — pull it
            # from the transcript so tabs get a real label immediately.
            first_task = first_user_prompt(s["transcript_path"])
            if first_task:
                s["first_task"] = first_task
        # Pull Claude's in-flight narrative text from the transcript so
        # the widget ticker can surface "thinking" between tool calls.
        narrative = ""
        if s.get("transcript_path"):
            try:
                narrative = latest_assistant_narrative(s["transcript_path"], max_chars=350)
            except Exception:
                narrative = ""

        out.append({
            "session_id": sid,
            "cwd": s.get("cwd", ""),
            "task": (s.get("task") or "")[:200],
            "first_task": first_task,
            "custom_name": s.get("custom_name", ""),
            "cc_claude_pid": s.get("cc_claude_pid"),
            "cc_shell_pid": s.get("cc_shell_pid"),
            "recent_tools": s.get("recent_tools", []),
            "current_turn_steps": s.get("current_turn_steps", []),
            "narrative": narrative,
            "turn_start_ts": s.get("turn_start_ts"),
            "state": s.get("state", "snoozing"),
            "tool": s.get("tool"),
            "category": s.get("category"),
            "message": s.get("message"),
            "ts": s.get("ts"),
            "stopped_at": s.get("stopped_at"),
            "terminal_hwnd": s.get("terminal_hwnd"),
            "pending": pending_here,
            "stats": _session_stats(sid),
        })
    out.sort(key=lambda r: r.get("ts") or 0, reverse=True)
    return {
        "mode": CFG.get("mode"),
        "strategy": CFG.get("decision_strategy"),
        "sessions": out,
    }


@app.post("/resolve")
async def resolve(request: Request) -> dict[str, Any]:
    body = await safe_json(request)
    rid = body.get("request_id", "")
    decision = body.get("decision", "").lower()
    scope = body.get("scope", "once").lower()
    if rid not in PENDING:
        return {"ok": False, "error": "unknown request_id"}
    if decision not in {"allow", "deny"}:
        return {"ok": False, "error": "decision must be allow|deny"}
    entry = PENDING[rid]
    category = entry["category"]
    if decision == "allow":
        if scope == "session":
            TRUST.add_session(category)
        elif scope == "persistent":
            TRUST.add_persistent(category)
    entry["decision"] = decision
    entry["scope"] = scope
    entry["event"].set()
    log.info("resolve %s+%s cat=%s", decision, scope, category)
    return {"ok": True, "category": category, "scope": scope}


@app.post("/mode")
async def set_mode(request: Request) -> dict[str, Any]:
    body = await safe_json(request)
    new_mode = body.get("mode", "").strip().lower()
    if new_mode not in {"strict", "relaxed", "trusted", "yolo"}:
        return {"ok": False, "error": f"unknown mode {new_mode!r}"}
    CFG["mode"] = new_mode
    CONFIG_PATH.write_text(json.dumps(CFG, indent=2), encoding="utf-8")
    log.info("mode switched to %s", new_mode)
    return {"ok": True, "mode": new_mode}


@app.post("/strategy")
async def set_strategy(request: Request) -> dict[str, Any]:
    body = await safe_json(request)
    new_strat = body.get("strategy", "").strip().lower()
    if new_strat not in {"observer", "assist", "auto"}:
        return {"ok": False, "error": f"unknown strategy {new_strat!r}"}
    CFG["decision_strategy"] = new_strat
    CONFIG_PATH.write_text(json.dumps(CFG, indent=2), encoding="utf-8")
    log.info("strategy switched to %s", new_strat)
    return {"ok": True, "strategy": new_strat}


@app.get("/trust")
async def trust_list() -> dict[str, Any]:
    return TRUST.list_all()


@app.get("/usage")
async def usage_rollup() -> dict[str, Any]:
    """Aggregate token usage across EVERY local Claude Code transcript.
    Results are cached per-transcript by mtime so repeat calls are cheap.
    Returns totals for 1h / 5h / today / this-week / all-time plus a
    top-20 per-project breakdown."""
    return aggregate_usage()


@app.post("/trust/add")
async def trust_add(request: Request) -> dict[str, Any]:
    body = await safe_json(request)
    category = body.get("category", "")
    scope = body.get("scope", "session")
    if not category:
        return {"ok": False, "error": "missing category"}
    if scope == "persistent":
        TRUST.add_persistent(category)
    else:
        TRUST.add_session(category)
    return {"ok": True, "trust": TRUST.list_all()}


@app.post("/trust/remove")
async def trust_remove(request: Request) -> dict[str, Any]:
    body = await safe_json(request)
    category = body.get("category", "")
    TRUST.remove(category)
    return {"ok": True, "trust": TRUST.list_all()}


@app.post("/session/dismiss")
async def session_dismiss(request: Request) -> dict[str, Any]:
    """User clicked Close Tab in the widget. Mark this session as
    dismissed so future hook payloads for it are silently dropped —
    the tab stays closed. Dismissal is in-memory only; a server
    restart forgets every dismissal."""
    body = await safe_json(request)
    sid = str(body.get("session_id") or "")
    if not sid:
        return {"ok": False, "error": "missing session_id"}
    DISMISSED_SESSIONS.add(sid)
    SESSIONS.pop(sid, None)
    STATS_CACHE.pop(sid, None)
    log.info("session %s dismissed by user", sid[:8])
    return {"ok": True, "dismissed": len(DISMISSED_SESSIONS)}


@app.post("/session/undismiss")
async def session_undismiss(request: Request) -> dict[str, Any]:
    """Undo a previous dismiss so the session's hooks start showing up
    again. If Claude is still running, the tab will reappear on the
    very next hook fire."""
    body = await safe_json(request)
    sid = str(body.get("session_id") or "")
    if sid:
        DISMISSED_SESSIONS.discard(sid)
        return {"ok": True, "dismissed": len(DISMISSED_SESSIONS)}
    return {"ok": False, "error": "missing session_id"}


@app.get("/session/dismissed")
async def session_dismissed_list() -> dict[str, Any]:
    return {"dismissed": sorted(DISMISSED_SESSIONS)}


@app.post("/session/name")
async def session_name(request: Request) -> dict[str, Any]:
    """Attach a user-chosen display name to a session. Persisted only in
    memory — lives as long as the session does."""
    body = await safe_json(request)
    sid = body.get("session_id", "")
    name = str(body.get("name") or "").strip()[:80]
    if sid not in SESSIONS:
        return {"ok": False, "error": "unknown session"}
    if name:
        SESSIONS[sid]["custom_name"] = name
    else:
        SESSIONS[sid].pop("custom_name", None)
    log.info("session %s renamed to %r", sid[:8], name or "(cleared)")
    return {"ok": True, "session_id": sid, "custom_name": name}


# ---------------------------------------------------------------------------
# Hook event handlers
# ---------------------------------------------------------------------------

def _apply_hook_metadata(session_id: str, body: dict[str, Any], request: Request) -> dict[str, Any] | None:
    """Extract cwd, transcript_path, and PIDs from hook body + headers,
    resolve the terminal HWND (once per session). Returns None if the
    session is dismissed — callers must early-return without acting."""
    if _is_dismissed(session_id):
        return None
    s = _session(session_id)
    s["ts"] = _now()

    cwd_val = str(body.get("cwd") or s.get("cwd", ""))
    if cwd_val:
        s["cwd"] = cwd_val
    tp = body.get("transcript_path")
    if tp:
        s["transcript_path"] = str(tp)

    # Parent PIDs from hook headers (WINPID conversion already done)
    shell_hdr = request.headers.get("x-cc-shell-pid") or request.headers.get("X-CC-SHELL-PID")
    ppid_hdr = request.headers.get("x-cc-ppid") or request.headers.get("X-CC-PPID")
    shell_pid = int(shell_hdr) if shell_hdr and shell_hdr.isdigit() else None
    ppid = int(ppid_hdr) if ppid_hdr and ppid_hdr.isdigit() else None
    if shell_pid:
        s["cc_shell_pid"] = shell_pid
    if ppid:
        s["cc_ppid"] = ppid

    # Resolve the long-lived claude.exe ancestor PID first — we use it as
    # a stable seed for any subsequent terminal-HWND re-resolution. The
    # shell_pid/ppid we get from the hook are ephemeral bash-c PIDs that
    # die as soon as curl returns, so they're useless for deciding "is
    # this session still alive a minute from now?". claude.exe lives for
    # the entire session, so that's what the sweep tracks.
    if not s.get("cc_claude_pid"):
        claude_pid = _claude_pid_for_pid(shell_pid) or _claude_pid_for_pid(ppid)
        if claude_pid:
            s["cc_claude_pid"] = claude_pid

    # Terminal HWND — re-resolve if missing OR if the stored handle no
    # longer points at a real window (user closed + reopened the tab).
    hwnd = s.get("terminal_hwnd")
    if hwnd:
        try:
            import win32gui  # type: ignore
            if not win32gui.IsWindow(int(hwnd)):
                hwnd = None
                s.pop("terminal_hwnd", None)
        except Exception:
            pass
    if not hwnd:
        hwnd = (
            _terminal_hwnd_for_pid(shell_pid)
            or _terminal_hwnd_for_pid(ppid)
            or _terminal_hwnd_for_pid(s.get("cc_claude_pid"))
            # Final fallback: pick a terminal-class window by title match.
            # Useful when the shell was reparented to explorer.exe and the
            # process tree no longer reaches a terminal emulator.
            or _terminal_hwnd_by_title(s.get("cwd", ""), s.get("first_task", ""))
        )
        if hwnd:
            s["terminal_hwnd"] = hwnd
    return s


@app.post("/userpromptsubmit")
async def user_prompt_submit(request: Request) -> JSONResponse:
    body = await safe_json(request)
    session_id = str(body.get("session_id", "unknown"))
    s = _apply_hook_metadata(session_id, body, request)
    if s is None:  # dismissed
        return JSONResponse({})
    prompt = str(body.get("prompt") or body.get("message") or "")
    if prompt:
        s["task"] = prompt[:2000]
        # Keep the very first prompt as the session's stable display name.
        if not s.get("first_task"):
            from stats import _clean_prompt  # local import to avoid cycle
            s["first_task"] = _clean_prompt(prompt)
    # New prompt arriving clears any "awaiting user reply" flag AND resets
    # the per-turn step tracker — we're about to start a new turn.
    s["awaiting_input"] = False
    s["current_turn_steps"] = []
    s["turn_start_ts"] = _now()
    _set_state(session_id, "working", message=prompt[:120])
    log.info("UserPromptSubmit session=%s prompt_len=%d", session_id[:8], len(prompt))
    return JSONResponse({})


def _summarize(tool: str, tool_input: dict[str, Any]) -> str:
    if tool == "Bash":
        return str(tool_input.get("command") or "")[:220]
    if tool in {"Write", "Edit", "NotebookEdit"}:
        return str(tool_input.get('file_path') or tool_input.get('notebook_path') or "")[:220]
    if tool == "Read":
        return str(tool_input.get("file_path") or "")[:220]
    if tool in {"Grep", "Glob"}:
        return str(tool_input.get("pattern") or tool_input.get("query") or "")[:220]
    try:
        return json.dumps(tool_input, ensure_ascii=False)[:220]
    except Exception:
        return str(tool_input)[:220]


async def _await_user_decision(
    session_id: str, tool_name: str, tool_input: dict[str, Any],
    category: str, reason: str,
) -> tuple[str, str]:
    request_id = str(uuid.uuid4())
    event = asyncio.Event()
    entry = {
        "tool": tool_name,
        "tool_input": tool_input,
        "category": category,
        "session_id": session_id,
        "summary": _summarize(tool_name, tool_input),
        "reason": reason,
        "created_at": _now(),
        "event": event,
        "decision": None,
        "scope": None,
    }
    async with PENDING_LOCK:
        PENDING[request_id] = entry
    _set_state(session_id, "allow", tool=tool_name, category=category,
               message=reason, ttl_s=PENDING_TIMEOUT_S)
    try:
        await asyncio.wait_for(event.wait(), timeout=PENDING_TIMEOUT_S)
        return entry["decision"] or "ask", entry["scope"] or "once"
    except asyncio.TimeoutError:
        return "ask", "timeout"
    finally:
        async with PENDING_LOCK:
            PENDING.pop(request_id, None)
        still_pending = any(p.get("session_id") == session_id for p in PENDING.values())
        if not still_pending:
            _set_state(session_id, "working")


@app.post("/pretooluse")
async def pretooluse(request: Request) -> JSONResponse:
    body = await safe_json(request)
    tool_name = body.get("tool_name") or "?"
    tool_input = body.get("tool_input") or {}
    session_id = str(body.get("session_id") or "unknown")

    if _apply_hook_metadata(session_id, body, request) is None:  # dismissed
        return JSONResponse({})

    category = classify(tool_name, tool_input)
    flags = regex_flags(tool_name, tool_input)
    strategy = CFG.get("decision_strategy", "assist")
    sec_cfg = CFG.get("security", {})
    mode = CFG.get("mode", "relaxed")

    # If Claude is about to call AskUserQuestion, it's queueing a
    # follow-up for the user — flag this so when Stop fires we know to
    # render the "done, but awaiting your reply" state instead of plain done.
    if tool_name == "AskUserQuestion":
        SESSIONS.setdefault(session_id, {})["awaiting_input"] = True

    # Keep a rolling list of recent tool calls so the widget's ticker
    # line can scroll "Reading foo.py → Editing bar.py → Bash git status".
    sess = SESSIONS.get(session_id)
    if sess is not None:
        recent = sess.setdefault("recent_tools", [])
        summary = _summarize(tool_name, tool_input)
        blurb = f"{tool_name}: {summary}" if summary else tool_name
        recent.append(blurb[:140])
        sess["recent_tools"] = recent[-8:]  # keep last 8

        # Per-turn step tracker — reset on UserPromptSubmit, appended on
        # each PreToolUse, mark-done on PostToolUse. Used by the widget
        # ticker to render "Step 3 ⏳ Editing widget.py" style progress.
        steps = sess.setdefault("current_turn_steps", [])
        steps.append({
            "tool": tool_name,
            "summary": summary[:140],
            "status": "running",
            "started_at": _now(),
            "finished_at": None,
        })
        sess["current_turn_steps"] = steps[-50:]   # cap for very long turns

    _set_state(
        session_id,
        "working" if not flags else "allow",
        tool=tool_name, category=category,
        message=("; ".join(flags) if flags else None),
        ttl_s=30.0 if flags else 0.0,
    )
    log.info("PreToolUse session=%s tool=%s cat=%s flags=%s strategy=%s",
             session_id[:8], tool_name, category, flags or "-", strategy)

    if (
        tool_name == "Bash"
        and sec_cfg.get("safety_net_block_catastrophic", True)
        and "irreversible destructive command" in flags
    ):
        joined = "; ".join(flags)
        log.warning("safety-net BLOCK tool=%s reason=%s", tool_name, joined)
        _set_state(session_id, "error", tool=tool_name, category=category,
                   message=joined, ttl_s=10.0)
        return JSONResponse({"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": f"cc-beeper-win safety net: {joined}",
        }})

    if strategy == "observer":
        return JSONResponse({})

    if not flags and TRUST.is_trusted(category):
        return JSONResponse({"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "permissionDecisionReason": "cc-beeper: trusted category",
        }})

    base_decision = mode_decision(mode, category)
    gemini_reason = ""
    if (
        base_decision == "allow"
        and not flags
        and sec_cfg.get("gemini_enabled", False)
        and category in GEMINI_CHECK_CATEGORIES
    ):
        task = (SESSIONS.get(session_id) or {}).get("task", "")
        verdict, why = gemini_classify(
            task, tool_name, tool_input,
            timeout=float(sec_cfg.get("gemini_timeout_s", 4.0)),
        )
        if verdict == "block":
            return JSONResponse({"hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": f"cc-beeper Gemini blocked: {why}",
            }})
        if verdict == "warn":
            base_decision = "ask"
            gemini_reason = why

    if base_decision == "allow" and not flags:
        return JSONResponse({"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "permissionDecisionReason": f"cc-beeper: mode={mode}",
        }})

    if strategy == "auto":
        return JSONResponse({})

    reason_bits: list[str] = []
    if flags:
        reason_bits.append("; ".join(flags))
    if gemini_reason:
        reason_bits.append(gemini_reason)
    if not reason_bits:
        reason_bits.append(f"mode={mode} requires approval for {category}")
    reason = " | ".join(reason_bits)

    decision, scope = await _await_user_decision(
        session_id, tool_name, tool_input, category, reason,
    )
    log.info("user decision session=%s cat=%s -> %s (%s)", session_id[:8], category, decision, scope)

    if decision == "allow":
        return JSONResponse({"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "permissionDecisionReason": f"user approved via widget ({scope})",
        }})
    if decision == "deny":
        return JSONResponse({"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": "user denied via widget",
        }})
    return JSONResponse({})


@app.post("/posttooluse")
async def posttooluse(request: Request) -> JSONResponse:
    body = await safe_json(request)
    session_id = str(body.get("session_id") or "unknown")
    if _apply_hook_metadata(session_id, body, request) is None:
        return JSONResponse({})
    tool_name = body.get("tool_name") or "?"
    tool_input = body.get("tool_input") or {}
    category = classify(tool_name, tool_input)
    # Mark the most recent running step as done (for the ticker).
    sess = SESSIONS.get(session_id)
    if sess is not None:
        steps = sess.get("current_turn_steps") or []
        for step in reversed(steps):
            if step.get("status") == "running" and step.get("tool") == tool_name:
                step["status"] = "done"
                step["finished_at"] = _now()
                break
    # AskUserQuestion completed means the user has already answered → no
    # longer awaiting input.
    if tool_name == "AskUserQuestion":
        s = SESSIONS.setdefault(session_id, {})
        s["awaiting_input"] = False
    if CFG.get("security", {}).get("auto_learn_from_posttooluse"):
        if not TRUST.is_trusted(category):
            TRUST.add_session(category)
    return JSONResponse({})


@app.post("/notification")
async def notification(request: Request) -> JSONResponse:
    body = await safe_json(request)
    session_id = str(body.get("session_id") or "unknown")
    if _apply_hook_metadata(session_id, body, request) is None:
        return JSONResponse({})
    ntype = str(body.get("notification_type") or "").lower()
    msg = str(body.get("message") or ntype or "Needs your attention")
    log.info("Notification session=%s type=%s: %s", session_id[:8], ntype or "?", msg[:80])

    # Claude Code fires Notification in several cases:
    #   permission_prompt  — real "approve this tool" prompt → red flash
    #   elicitation_dialog — Claude is asking the user a question → green flash
    #   idle_prompt        — CC is idle waiting for next user prompt → quiet
    #   auth_success       — benign auth event → quiet
    IDLE_TYPES = {"idle_prompt", "auth_success"}
    if ntype in IDLE_TYPES:
        cur = (SESSIONS.get(session_id) or {}).get("state")
        if cur != "done" and cur != "awaiting_input":
            _set_state(session_id, "snoozing", message=msg[:120])
        return JSONResponse({})

    if ntype == "elicitation_dialog":
        s = SESSIONS.setdefault(session_id, {})
        s["awaiting_input"] = True
        _set_state(session_id, "awaiting_input", message=msg[:120])
        return JSONResponse({})

    # permission_prompt or any other non-idle type — treat as red-flash input.
    _set_state(session_id, "input", message=msg[:120], ttl_s=45.0)
    return JSONResponse({})


@app.post("/stop")
async def stop(request: Request) -> JSONResponse:
    body = await safe_json(request)
    session_id = str(body.get("session_id") or "unknown")
    s = _apply_hook_metadata(session_id, body, request)
    if s is None:
        return JSONResponse({})
    s["stopped_at"] = _now()
    log.info("Stop session=%s awaiting=%s", session_id[:8], bool(s.get("awaiting_input")))
    # Two kinds of "turn finished":
    #   - awaiting_input: Claude used AskUserQuestion or fired elicitation_dialog
    #     in this turn → flashing green (turn done, but Claude needs your answer
    #     to continue)
    #   - plain done: Claude finished and is waiting for your next free-form
    #     prompt → steady green
    if s.get("awaiting_input"):
        _set_state(session_id, "awaiting_input",
                   message="Turn finished — Claude is waiting on your reply")
    else:
        _set_state(session_id, "done",
                   message="Turn finished — ready for next prompt")
    return JSONResponse({})


@app.post("/sessionstart")
async def session_start(request: Request) -> JSONResponse:
    """Claude Code fires SessionStart right after `claude` boots, before
    the user types anything. Register the tab immediately so the widget
    shows the session the moment CC is open, not only after the first
    prompt."""
    body = await safe_json(request)
    session_id = str(body.get("session_id") or "unknown")
    s = _apply_hook_metadata(session_id, body, request)
    if s is None:  # dismissed by user
        return JSONResponse({})
    # Idle/ready state — Claude is booted and waiting for your first prompt.
    _set_state(session_id, "snoozing",
               message="Ready — waiting for first prompt")
    log.info("SessionStart session=%s cwd=%s", session_id[:8], s.get("cwd", ""))
    return JSONResponse({})


@app.post("/sessionend")
async def session_end(request: Request) -> JSONResponse:
    """Claude Code fires SessionEnd on a clean /exit or equivalent.
    Remove the session immediately so the widget drops its tab."""
    body = await safe_json(request)
    session_id = str(body.get("session_id") or "unknown")
    SESSIONS.pop(session_id, None)
    STATS_CACHE.pop(session_id, None)
    log.info("SessionEnd session=%s (removed)", session_id[:8])
    return JSONResponse({})


@app.post("/stopfailure")
async def stop_failure(request: Request) -> JSONResponse:
    body = await safe_json(request)
    session_id = str(body.get("session_id") or "unknown")
    if _apply_hook_metadata(session_id, body, request) is None:
        return JSONResponse({})
    log.info("StopFailure session=%s: %s", session_id[:8], body.get("error", ""))
    _set_state(session_id, "error", message=str(body.get("error", "Unknown"))[:120], ttl_s=10.0)
    return JSONResponse({})


def main() -> None:
    start, end = CFG.get("port_range", [19222, 19230])
    port = find_free_port(start, end)
    (ROOT / ".port").write_text(str(port), encoding="utf-8")
    log.info(
        "cc-beeper-win listening on 127.0.0.1:%d (mode=%s, strategy=%s, trust=%d)",
        port, CFG.get("mode"), CFG.get("decision_strategy"),
        len(TRUST.list_all()["persistent"]),
    )
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning", access_log=False)


if __name__ == "__main__":
    main()
