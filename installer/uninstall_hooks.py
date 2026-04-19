"""Convenience wrapper: python uninstall_hooks.py == python install_hooks.py --uninstall"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def main() -> int:
    here = Path(__file__).resolve().parent
    return subprocess.call([sys.executable, str(here / "install_hooks.py"), "--uninstall"])


if __name__ == "__main__":
    raise SystemExit(main())
