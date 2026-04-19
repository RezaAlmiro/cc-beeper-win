"""Trust store + mode-based default decisions.

Trust is two-layered:
1. Mode matrix — the current mode (strict/relaxed/trusted/yolo) defines a
   default decision for each category bucket.
2. Explicit trust — categories the user has opted into (per-session or
   persistent via trust.json).

The resolver's final decision is the most permissive of the two, but the
security layer (security.py) can override to "ask" or "deny" regardless.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Literal

Decision = Literal["allow", "ask", "deny"]

# Mode matrix: maps category prefix -> default decision.
# Lookup is prefix-based, longest match wins.
MODE_MATRIX: dict[str, dict[str, Decision]] = {
    "strict": {
        # strict asks for everything except pure observability
        "Read": "ask",
        "Meta": "allow",
    },
    "relaxed": {
        "Read": "allow",
        "Meta": "allow",
        "Bash:read": "allow",
        "Bash:git-read": "allow",
        "MCP:read": "allow",
    },
    "trusted": {
        "Read": "allow",
        "Meta": "allow",
        "Bash:read": "allow",
        "Bash:git-read": "allow",
        "Bash:git-write-local": "allow",
        "MCP:read": "allow",
        "Write:project": "allow",
        "Agent:explore": "allow",
    },
    "yolo": {
        "Read": "allow",
        "Meta": "allow",
        "Bash:read": "allow",
        "Bash:git-read": "allow",
        "Bash:git-write-local": "allow",
        "Bash:network": "allow",
        "Bash:install": "allow",
        "Bash:other": "allow",
        "MCP:read": "allow",
        "MCP:write": "allow",
        "Write:project": "allow",
        "Agent:explore": "allow",
        "Agent:general-purpose": "allow",
    },
}


class TrustStore:
    """Persistent + in-memory trust list."""

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        self._session_categories: set[str] = set()
        self._persistent: set[str] = set()
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self._persistent = set(data.get("trusted_categories", []))
        except Exception:
            self._persistent = set()

    def _save(self) -> None:
        data = {"trusted_categories": sorted(self._persistent)}
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def is_trusted(self, category: str) -> bool:
        with self._lock:
            return category in self._session_categories or category in self._persistent

    def add_session(self, category: str) -> None:
        with self._lock:
            self._session_categories.add(category)

    def add_persistent(self, category: str) -> None:
        with self._lock:
            self._persistent.add(category)
            self._save()

    def remove(self, category: str) -> None:
        with self._lock:
            self._session_categories.discard(category)
            self._persistent.discard(category)
            self._save()

    def list_all(self) -> dict[str, list[str]]:
        with self._lock:
            return {
                "session": sorted(self._session_categories),
                "persistent": sorted(self._persistent),
            }


def mode_decision(mode: str, category: str) -> Decision:
    """Look up the default decision for a category under the current mode."""
    matrix = MODE_MATRIX.get(mode, MODE_MATRIX["strict"])
    # longest-prefix match
    best: Decision = "ask"
    best_len = -1
    for prefix, decision in matrix.items():
        if category == prefix or category.startswith(prefix + ":"):
            if len(prefix) > best_len:
                best = decision
                best_len = len(prefix)
    return best


def resolve(mode: str, category: str, trust: TrustStore) -> tuple[Decision, str]:
    """Return (decision, reason) for a category. Does NOT run security checks —
    those are layered on top in server.py."""
    if trust.is_trusted(category):
        return "allow", "trusted category"
    decision = mode_decision(mode, category)
    return decision, f"mode={mode}"
