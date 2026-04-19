"""Install CC-Beeper-Win hooks into ~/.claude/settings.json.

Adds entries that POST each hook payload to the local server.
Our entries are tagged with the marker "cc-beeper-win" in the command
string so we can safely remove/update them later without touching
user-owned hooks.

Usage:
    python install_hooks.py            # install
    python install_hooks.py --dry-run  # preview
    python install_hooks.py --uninstall # remove only our entries
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

SETTINGS = Path.home() / ".claude" / "settings.json"
MARKER = "cc-beeper-win"

EVENTS = [
    ("SessionStart", "sessionstart"),
    ("UserPromptSubmit", "userpromptsubmit"),
    ("PreToolUse", "pretooluse"),
    ("PostToolUse", "posttooluse"),
    ("Stop", "stop"),
    ("StopFailure", "stopfailure"),
    ("Notification", "notification"),
    ("SessionEnd", "sessionend"),
]


def hook_command(endpoint: str, port_file: Path) -> str:
    # Read the port written by the running server; fall back to 19222.
    # Git Bash / MSYS `$$` returns the cygwin PID, not the Windows PID,
    # so psutil can't find it. /proc/$$/winpid gives the real Win32 PID.
    port_file_posix = str(port_file).replace("\\", "/")
    return (
        "bash -c '"
        f"PORT=$(cat \"{port_file_posix}\" 2>/dev/null || echo 19222); "
        "WINPID=$(cat /proc/$$/winpid 2>/dev/null || echo $$); "
        "PWINPID=$(cat /proc/$PPID/winpid 2>/dev/null || echo $PPID); "
        "curl -sS --max-time 120 "
        "-H \"Content-Type: application/json\" "
        "-H \"X-CC-SHELL-PID: $WINPID\" "
        "-H \"X-CC-PPID: $PWINPID\" "
        "--data-binary @- "
        f"http://127.0.0.1:$PORT/{endpoint} 2>/dev/null || true; "
        f"# {MARKER}"
        "'"
    )


def load_settings() -> dict:
    if not SETTINGS.exists():
        return {}
    return json.loads(SETTINGS.read_text(encoding="utf-8"))


def backup_settings() -> Path:
    backup = SETTINGS.with_suffix(".json.ccbeeper.bak")
    shutil.copy2(SETTINGS, backup)
    return backup


def is_our_hook(entry: dict) -> bool:
    for h in entry.get("hooks", []):
        cmd = h.get("command", "")
        if MARKER in cmd:
            return True
    return False


def strip_our_hooks(settings: dict) -> dict:
    hooks = settings.setdefault("hooks", {})
    for event, _ in EVENTS:
        arr = hooks.get(event, [])
        hooks[event] = [e for e in arr if not is_our_hook(e)]
        if not hooks[event]:
            hooks.pop(event, None)
    return settings


def add_our_hooks(settings: dict, port_file: Path) -> dict:
    hooks = settings.setdefault("hooks", {})
    for event, endpoint in EVENTS:
        arr = hooks.setdefault(event, [])
        arr.append(
            {
                "hooks": [
                    {
                        "type": "command",
                        "command": hook_command(endpoint, port_file),
                        "timeout": 120,
                    }
                ]
            }
        )
    return settings


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--uninstall", action="store_true")
    args = ap.parse_args()

    if not SETTINGS.exists():
        print(f"ERROR: {SETTINGS} does not exist. Install Claude Code first.", file=sys.stderr)
        return 1

    port_file = Path(__file__).resolve().parents[1] / ".port"

    settings = load_settings()
    settings = strip_our_hooks(settings)
    if not args.uninstall:
        settings = add_our_hooks(settings, port_file)

    rendered = json.dumps(settings, indent=2, ensure_ascii=False)

    if args.dry_run:
        print(rendered)
        return 0

    backup = backup_settings()
    print(f"backed up settings.json -> {backup}")
    SETTINGS.write_text(rendered + "\n", encoding="utf-8")
    action = "uninstalled" if args.uninstall else "installed"
    print(f"cc-beeper-win hooks {action}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
