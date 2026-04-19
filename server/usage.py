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


def _parse_transcript_records(path: Path) -> list[tuple[float, str, dict[str, Any]]]:
    """Return [(timestamp, model_family, usage_dict)] for every assistant
    record with a usage block. Non-assistant records are ignored."""
    out: list[tuple[float, str, dict[str, Any]]] = []
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
                if not isinstance(msg, dict):
                    continue
                usage = msg.get("usage")
                if not isinstance(usage, dict):
                    continue
                family = _model_family(msg.get("model") or "")
                out.append((ts, family, usage))
    except Exception as e:
        log.debug("usage parse failed for %s: %s", path, e)
    return out


def _model_family(raw: str) -> str:
    """Coarse bucket for the model id. Matches Anthropic's usage-page
    groupings: 'opus' / 'sonnet' / 'haiku' / 'other'."""
    s = (raw or "").lower()
    if "opus" in s:    return "opus"
    if "sonnet" in s:  return "sonnet"
    if "haiku" in s:   return "haiku"
    return "other"


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
        "oldest_ts": None,
        "newest_ts": None,
    }


def _by_family_empty() -> dict[str, dict[str, int]]:
    return {f: {"input": 0, "output": 0, "turns": 0}
            for f in ("opus", "sonnet", "haiku", "other")}


def aggregate_usage(projects_dir: Path | None = None) -> dict[str, Any]:
    """Return the full usage rollup."""
    if projects_dir is None:
        projects_dir = _claude_projects_dir()
    now = time.time()
    thresholds = _window_thresholds()

    totals: dict[str, dict[str, Any]] = {w: _empty_bucket() for w in thresholds}
    by_project: dict[str, dict[str, Any]] = {}
    by_family_week: dict[str, dict[str, int]] = _by_family_empty()
    by_family_5h: dict[str, dict[str, int]] = _by_family_empty()

    # Hour-of-day + day-of-week tokens across ALL time (calendar analytics)
    hour_tokens: dict[int, int] = {h: 0 for h in range(24)}
    hour_turns:  dict[int, int] = {h: 0 for h in range(24)}
    dow_tokens:  dict[int, int] = {d: 0 for d in range(7)}
    dow_turns:   dict[int, int] = {d: 0 for d in range(7)}
    # Daily series for the last 14 days (string date → tokens)
    daily_tokens: dict[str, int] = {}
    daily_turns:  dict[str, int] = {}
    cutoff_14d = now - 14 * 24 * 3600
    # Per-session duration tracking (for avg-session-length insight)
    session_bounds: dict[str, list[float]] = {}   # session_id → [min_ts, max_ts]

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

        for ts, family, usage in records:
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
                    if tb["oldest_ts"] is None or ts < tb["oldest_ts"]:
                        tb["oldest_ts"] = ts
                    if tb["newest_ts"] is None or ts > tb["newest_ts"]:
                        tb["newest_ts"] = ts

            # Per-model-family rollups for the two UI-visible windows
            if ts >= thresholds["this_week"]:
                fb = by_family_week[family]
                fb["input"] += ipt; fb["output"] += out; fb["turns"] += 1
            if ts >= thresholds["last_5h"]:
                fb = by_family_5h[family]
                fb["input"] += ipt; fb["output"] += out; fb["turns"] += 1

            # Calendar analytics — when do YOU work on Claude?
            dt = datetime.fromtimestamp(ts)
            tok = ipt + out
            hour_tokens[dt.hour] += tok
            hour_turns[dt.hour] += 1
            dow_tokens[dt.weekday()] += tok
            dow_turns[dt.weekday()] += 1
            if ts >= cutoff_14d:
                key = dt.strftime("%Y-%m-%d")
                daily_tokens[key] = daily_tokens.get(key, 0) + tok
                daily_turns[key]  = daily_turns.get(key, 0) + 1

            # Per-session bounds for duration insights
            sb = session_bounds.setdefault(session_id, [ts, ts])
            if ts < sb[0]: sb[0] = ts
            if ts > sb[1]: sb[1] = ts

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

    # --- Insight derivations ----------------------------------------------

    # Peak hour = hour-of-day bucket with the most tokens (all-time)
    peak_hour = max(hour_tokens, key=hour_tokens.get) if any(hour_tokens.values()) else None
    total_hour_tokens = sum(hour_tokens.values()) or 1
    peak_hour_share = (hour_tokens.get(peak_hour, 0) / total_hour_tokens) if peak_hour is not None else 0

    # Off-peak = contiguous run of 3 lowest-usage hours
    if any(hour_tokens.values()):
        hours_sorted = sorted(range(24), key=lambda h: hour_tokens[h])
        off_peak_hours = sorted(hours_sorted[:3])
    else:
        off_peak_hours = []

    # Busiest day
    busy_dow = max(dow_tokens, key=dow_tokens.get) if any(dow_tokens.values()) else None
    total_dow_tokens = sum(dow_tokens.values()) or 1
    busy_dow_share = (dow_tokens.get(busy_dow, 0) / total_dow_tokens) if busy_dow is not None else 0

    # Efficiency
    all_bucket = totals.get("all") or {}
    total_input = all_bucket.get("input", 0)
    total_cache_r = all_bucket.get("cache_read", 0)
    total_output = all_bucket.get("output", 0)
    total_turns = all_bucket.get("turns", 0)
    total_served = total_input + total_cache_r + all_bucket.get("cache_write", 0)
    cache_hit_pct = (100 * total_cache_r / total_served) if total_served else 0
    out_in_ratio = (total_output / total_input) if total_input else 0

    # Average session duration (minutes)
    durations = [(e - s) / 60.0 for s, e in session_bounds.values() if e > s]
    avg_session_min = (sum(durations) / len(durations)) if durations else 0
    median_session_min = 0.0
    if durations:
        sd = sorted(durations)
        median_session_min = sd[len(sd) // 2]

    # 14-day daily series (fill gaps with 0)
    daily_series: list[dict[str, Any]] = []
    today_dt = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    for i in range(13, -1, -1):
        day = today_dt - timedelta(days=i)
        k = day.strftime("%Y-%m-%d")
        daily_series.append({
            "date": k,
            "dow": day.strftime("%a"),
            "tokens": int(daily_tokens.get(k, 0)),
            "turns":  int(daily_turns.get(k, 0)),
        })

    # Reset-time computations — approximate. 5h window is rolling.
    next_5h_reset = None
    oldest_5h = totals.get("last_5h", {}).get("oldest_ts")
    if oldest_5h:
        next_5h_reset = oldest_5h + 5 * 3600
    _now_dt = datetime.now()
    _mon = _now_dt.replace(hour=0, minute=0, second=0, microsecond=0) \
        - timedelta(days=_now_dt.weekday())
    next_week_reset = (_mon + timedelta(days=7)).timestamp()

    return {
        "generated_at": now,
        "projects_dir": str(projects_dir),
        "projects_dir_exists": True,
        "files_scanned": files_seen,
        "windows": _serialise(totals),
        "window_labels": _WINDOW_LABELS,
        "by_project": project_list[:20],
        "by_family_week": by_family_week,
        "by_family_5h": by_family_5h,
        "next_5h_reset": next_5h_reset,
        "next_week_reset": next_week_reset,
        # --- analytics for the insights dashboard ---
        "hour_tokens": [int(hour_tokens[h]) for h in range(24)],
        "hour_turns":  [int(hour_turns[h]) for h in range(24)],
        "dow_tokens":  [int(dow_tokens[d]) for d in range(7)],
        "dow_turns":   [int(dow_turns[d]) for d in range(7)],
        "daily_series": daily_series,
        "insights": {
            "peak_hour": peak_hour,
            "peak_hour_share": peak_hour_share,
            "off_peak_hours": off_peak_hours,
            "busiest_dow": busy_dow,
            "busiest_dow_share": busy_dow_share,
            "cache_hit_pct": cache_hit_pct,
            "output_input_ratio": out_in_ratio,
            "avg_session_minutes": avg_session_min,
            "median_session_minutes": median_session_min,
            "total_sessions": len(session_bounds),
            "total_turns": total_turns,
        },
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


# -- Public helper: defaults for optional user-set token caps -------------
# These are rough eyeball numbers for Max-20x / Max-5x / Pro; the user can
# override in config.json under widget.usage_limits.
DEFAULT_LIMITS = {
    "session_5h":    1_000_000,   # tokens per 5-hour rolling window
    "weekly_all":   10_000_000,   # tokens per week, all models combined
    "weekly_sonnet":10_000_000,   # Sonnet-only quota
    "weekly_opus":   3_000_000,   # Opus-only quota
}

