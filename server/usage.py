"""Aggregate Claude Code usage across all local transcripts.

Walks every JSONL under `~/.claude/projects/` (or `$CLAUDE_CONFIG_DIR`),
extracts the `usage` block from every assistant record, and totals
tokens into rolling time windows (1 h / 5 h / 24 h / this-week /
all-time) plus per-project breakdowns.

Works universally — Max / Team / Pro / API. Max users mostly care about
the 5-hour rolling window (their rate-limit reset cadence); API users
can translate token counts to dollars with their plan's pricing.
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger("cc-beeper.usage")

# mtime-keyed cache of already-parsed transcripts. Key = absolute path.
_CACHE: dict[str, dict[str, Any]] = {}

# Windows we report on. Values are "seconds ago from now" to anchor at
# lookup time (so 'today' and 'this_week' respect local midnight).
_WINDOW_LABELS = {
    "last_1h":  "Last 1 Hour",
    "last_5h":  "Last 5 Hours (Max Rate-Limit Window)",
    "today":    "Today (Since Midnight Local)",
    "this_week":"This Week (Since Monday Local)",
    "all":      "All-Time",
}


def _parse_ts(ts: str) -> float | None:
    """ISO-8601 string → Unix timestamp (seconds since epoch)."""
    if not ts:
        return None
    try:
        if ts.endswith("Z"):
            ts = ts[:-1] + "+00:00"
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        return None


def _window_thresholds() -> dict[str, float]:
    """Unix timestamp lower-bound for each window; records with ts >=
    threshold are included in that window."""
    now = time.time()
    now_dt = datetime.now()
    midnight = now_dt.replace(hour=0, minute=0, second=0, microsecond=0)
    monday = midnight - timedelta(days=now_dt.weekday())
    return {
        "last_1h":   now - 3600,
        "last_5h":   now - 5 * 3600,
        "today":     midnight.timestamp(),
        "this_week": monday.timestamp(),
        "all":       0.0,
    }


def _parse_transcript_records(path: Path) -> list[tuple[float, dict[str, Any]]]:
    """Return [(timestamp, usage_dict)] for every assistant record with a
    usage block in this transcript. Non-assistant records are ignored."""
    out: list[tuple[float, dict[str, Any]]] = []
    try:
        with path.open(encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if not isinstance(rec, dict):
                    continue
                if rec.get("type") != "assistant":
                    continue
                ts = _parse_ts(rec.get("timestamp") or "")
                if ts is None:
                    continue
                msg = rec.get("message") or {}
                usage = msg.get("usage") if isinstance(msg, dict) else None
                if not isinstance(usage, dict):
                    continue
                out.append((ts, usage))
    except Exception as e:
        log.debug("usage parse failed for %s: %s", path, e)
    return out


def _claude_projects_dir() -> Path:
    """Where Claude Code keeps transcripts. Respects $CLAUDE_CONFIG_DIR."""
    cfg = os.environ.get("CLAUDE_CONFIG_DIR")
    base = Path(cfg) if cfg else (Path.home() / ".claude")
    return base / "projects"


def _empty_bucket() -> dict[str, Any]:
    return {
        "input": 0, "output": 0,
        "cache_read": 0, "cache_write": 0,
        "turns": 0, "session_ids": set(),
    }


def aggregate_usage(projects_dir: Path | None = None) -> dict[str, Any]:
    """Return the full usage rollup."""
    if projects_dir is None:
        projects_dir = _claude_projects_dir()
    now = time.time()
    thresholds = _window_thresholds()

    totals: dict[str, dict[str, Any]] = {w: _empty_bucket() for w in thresholds}
    by_project: dict[str, dict[str, Any]] = {}

    if not projects_dir.exists():
        return {
            "generated_at": now,
            "projects_dir": str(projects_dir),
            "projects_dir_exists": False,
            "windows": _serialise(totals),
            "by_project": [],
        }

    files_seen = 0
    for path in projects_dir.rglob("*.jsonl"):
        if not path.is_file():
            continue
        files_seen += 1
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue

        # Skip pathological files.
        try:
            size = path.stat().st_size
        except OSError:
            continue
        if size > 300 * 1024 * 1024:
            continue

        cache_key = str(path)
        cached = _CACHE.get(cache_key)
        if cached and cached.get("mtime") == mtime:
            records = cached["records"]
        else:
            records = _parse_transcript_records(path)
            _CACHE[cache_key] = {"mtime": mtime, "records": records}

        project = path.parent.name
        session_id = path.stem

        pbucket = by_project.setdefault(project, _empty_bucket())

        for ts, usage in records:
            ipt = int(usage.get("input_tokens") or 0)
            out = int(usage.get("output_tokens") or 0)
            cr  = int(usage.get("cache_read_input_tokens") or 0)
            cw  = int(usage.get("cache_creation_input_tokens") or 0)

            pbucket["input"] += ipt
            pbucket["output"] += out
            pbucket["cache_read"] += cr
            pbucket["cache_write"] += cw
            pbucket["turns"] += 1
            pbucket["session_ids"].add(session_id)

            for wname, thresh in thresholds.items():
                if ts >= thresh:
                    tb = totals[wname]
                    tb["input"] += ipt
                    tb["output"] += out
                    tb["cache_read"] += cr
                    tb["cache_write"] += cw
                    tb["turns"] += 1
                    tb["session_ids"].add(session_id)

    # Top-N projects by total tokens (input + output, ignoring cache).
    project_list: list[dict[str, Any]] = []
    for name, bucket in by_project.items():
        row = dict(bucket)
        row["project"] = name
        row["sessions"] = len(bucket["session_ids"])
        row.pop("session_ids", None)
        project_list.append(row)
    project_list.sort(
        key=lambda r: r.get("input", 0) + r.get("output", 0),
        reverse=True,
    )

    return {
        "generated_at": now,
        "projects_dir": str(projects_dir),
        "projects_dir_exists": True,
        "files_scanned": files_seen,
        "windows": _serialise(totals),
        "window_labels": _WINDOW_LABELS,
        "by_project": project_list[:20],   # cap so the payload stays small
    }


def _serialise(totals: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Convert the sessions set to a count + return a plain-dict shape."""
    out: dict[str, dict[str, Any]] = {}
    for w, bucket in totals.items():
        row = dict(bucket)
        row["sessions"] = len(bucket["session_ids"])
        row.pop("session_ids", None)
        out[w] = row
    return out
