"""CC-Beeper-Win smart launcher.

Idempotent: ensures the hook server + widget are both running, then exits.
Safe to run many times — won't spawn duplicates. This is the script the
taskbar / Start Menu / desktop shortcuts target.
"""
from __future__ import annotations

import os
import pathlib
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent
PORT_FILE = ROOT / ".port"

# pythonw silences stdout/stderr so child processes don't flash a console.
PYTHONW = pathlib.Path(sys.executable).parent / "pythonw.exe"
if not PYTHONW.exists():
    PYTHONW = pathlib.Path("pythonw.exe")  # fall back to PATH

# Windows-specific: detached & no console window
CREATE_FLAGS = 0x00000008 | 0x08000000  # DETACHED_PROCESS | CREATE_NO_WINDOW


def _port() -> int:
    try:
        return int(PORT_FILE.read_text().strip())
    except Exception:
        return 19222


def server_alive() -> bool:
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{_port()}/health", timeout=1)
        return True
    except (urllib.error.URLError, ConnectionError, TimeoutError):
        return False
    except Exception:
        return False


def widget_running() -> bool:
    try:
        import psutil  # type: ignore
    except ImportError:
        return False
    widget_path = str(ROOT / "widget.py").lower()
    for p in psutil.process_iter(["name", "cmdline"]):
        try:
            name = (p.info.get("name") or "").lower()
            if "python" not in name:
                continue
            cmd = " ".join(p.info.get("cmdline") or []).lower()
            if "widget.py" in cmd:
                return True
        except Exception:
            continue
    return False


def start_process(script: pathlib.Path) -> None:
    subprocess.Popen(
        [str(PYTHONW), str(script)],
        cwd=str(ROOT),
        creationflags=CREATE_FLAGS,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def main() -> int:
    os.chdir(str(ROOT))
    if not server_alive():
        start_process(ROOT / "server" / "server.py")
        # Wait up to 5 s for the server to come up before launching the widget.
        for _ in range(20):
            time.sleep(0.25)
            if server_alive():
                break
    if not widget_running():
        start_process(ROOT / "widget.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
