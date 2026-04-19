"""Transcript JSONL parsing: token totals, context usage, model detection.

Claude Code writes a transcript file per session with one JSON line per
message. Assistant messages carry a `usage` block like:
    {"input_tokens": 1234, "output_tokens": 567, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 89}
and a `model` field like "claude-opus-4-7-20260401[1m]".

We compute:
- session totals: sum of input / output / cache_write / cache_read
- context in use: input_tokens of the most recent assistant message (this is
  Claude's rolling context — the model is reading this many tokens right now)
- model + context window size
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger("cc-beeper.stats")

MODEL_LIMITS = [
    # Match order matters — more specific patterns first.
    # Opus 4.7 defaults to 1M (the common Claude Code config); the [1m]
    # suffix isn't always present in transcript records. Users on the base
    # 200K tier can override via config.
    (re.compile(r"claude-opus-4-7.*\[1m\]", re.I), 1_000_000, "Opus 4.7 (1M)"),
    (re.compile(r"claude-opus-4-7.*legacy", re.I),   200_000, "Opus 4.7"),
    (re.compile(r"claude-opus-4-7", re.I),         1_000_000, "Opus 4.7 (1M)"),
    (re.compile(r"claude-opus-4-6", re.I),           200_000, "Opus 4.6"),
    (re.compile(r"claude-sonnet-4-6", re.I),         200_000, "Sonnet 4.6"),
    (re.compile(r"claude-sonnet-4-5", re.I),         200_000, "Sonnet 4.5"),
    (re.compile(r"claude-haiku-4-5", re.I),          200_000, "Haiku 4.5"),
    (re.compile(r"claude-3-5-sonnet", re.I),         200_000, "Sonnet 3.5"),
    (re.compile(r"claude-3-5-haiku", re.I),          200_000, "Haiku 3.5"),
]
DEFAULT_LIMIT = 200_000


def resolve_model(raw: str) -> tuple[int, str]:
    for pat, limit, label in MODEL_LIMITS:
        if pat.search(raw):
            return limit, label
    return DEFAULT_LIMIT, (raw or "unknown")


@dataclass
class SessionStats:
    model_raw: str = ""
    model_label: str = "?"
    context_limit: int = DEFAULT_LIMIT
    current_context: int = 0     # input_tokens of most recent turn
    total_input: int = 0
    total_output: int = 0
    total_cache_read: int = 0
    total_cache_write: int = 0
    turns: int = 0

    @property
    def context_pct(self) -> float:
        return 0.0 if not self.context_limit else min(100.0, 100.0 * self.current_context / self.context_limit)

    def to_dict(self) -> dict[str, Any]:
        return {
            "model_raw": self.model_raw,
            "model_label": self.model_label,
            "context_limit": self.context_limit,
            "current_context": self.current_context,
            "context_pct": round(self.context_pct, 1),
            "total_input": self.total_input,
            "total_output": self.total_output,
            "total_cache_read": self.total_cache_read,
            "total_cache_write": self.total_cache_write,
            "turns": self.turns,
        }


def _find_usage(obj: Any) -> dict[str, Any] | None:
    """Usage block can live at the top level, inside `message.usage`, or
    inside nested shapes depending on transcript format."""
    if isinstance(obj, dict):
        if "usage" in obj and isinstance(obj["usage"], dict):
            return obj["usage"]
        for v in obj.values():
            found = _find_usage(v)
            if found:
                return found
    elif isinstance(obj, list):
        for v in obj:
            found = _find_usage(v)
            if found:
                return found
    return None


def _find_model(obj: Any) -> str:
    if isinstance(obj, dict):
        m = obj.get("model")
        if isinstance(m, str) and m:
            return m
        for v in obj.values():
            m = _find_model(v)
            if m:
                return m
    elif isinstance(obj, list):
        for v in obj:
            m = _find_model(v)
            if m:
                return m
    return ""


_XML_TAG_RE = re.compile(r"<[^>]+>")
_CMD_NAME_RE = re.compile(r"<command-name>\s*/?([^<]+?)\s*</command-name>", re.I)
_CMD_ARGS_RE = re.compile(r"<command-args>\s*([^<]*)\s*</command-args>", re.I)


def _clean_prompt(text: str) -> str:
    """Normalize a user prompt for use as a session label.
    - If wrapped in slash-command XML, return "cmd args"
    - Otherwise strip tags and collapse whitespace, truncate to 200 chars"""
    if not text:
        return ""
    cmd = _CMD_NAME_RE.search(text)
    if cmd:
        name = cmd.group(1).strip()
        args_m = _CMD_ARGS_RE.search(text)
        args = (args_m.group(1).strip() if args_m else "")[:120]
        return f"/{name}{(' ' + args) if args else ''}"[:200]
    # strip XML-ish tags
    cleaned = _XML_TAG_RE.sub(" ", text)
    cleaned = " ".join(cleaned.split())
    return cleaned[:200]


def first_user_prompt(path: str | Path) -> str:
    """Return the first user message text from a transcript JSONL, or "".
    Skips command wrappers / tool results / system reminders and returns
    the first message that looks like a real user ask."""
    p = Path(path)
    if not p.exists() or not p.is_file():
        return ""
    try:
        with p.open(encoding="utf-8", errors="replace") as f:
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
                role = (
                    rec.get("role")
                    or (rec.get("message") or {}).get("role")
                    or ""
                )
                if role != "user":
                    continue
                msg = rec.get("message") or rec
                content = msg.get("content") if isinstance(msg, dict) else None
                text = ""
                if isinstance(content, str):
                    text = content
                elif isinstance(content, list):
                    parts: list[str] = []
                    for blk in content:
                        if isinstance(blk, dict) and blk.get("type") == "text":
                            parts.append(str(blk.get("text") or ""))
                        elif isinstance(blk, str):
                            parts.append(blk)
                    text = " ".join(parts)
                text = text.strip()
                if not text:
                    continue
                # Skip tool-results / system-reminders / meta-command framing
                if text.startswith("<tool_use_") or text.startswith("<tool_result>"):
                    continue
                cleaned = _clean_prompt(text)
                if cleaned:
                    return cleaned
    except Exception:
        pass
    return ""


MAX_TRANSCRIPT_BYTES = 200 * 1024 * 1024   # 200 MB — transcripts this big mean something's wrong


def parse_transcript(path: str | Path) -> SessionStats | None:
    p = Path(path)
    if not p.exists() or not p.is_file():
        return None
    # Guard against a hook payload handing us a pathologically large file —
    # transcript_path comes from Claude's hook body, so a local attacker
    # could in theory point us at a 10 GB file and block the event loop.
    try:
        if p.stat().st_size > MAX_TRANSCRIPT_BYTES:
            log.warning("transcript %s too large (%d bytes) — skipping", p, p.stat().st_size)
            return None
    except OSError:
        return None
    stats = SessionStats()
    last_usage: dict[str, Any] | None = None
    latest_model: str = ""
    try:
        with p.open(encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                # Only sum assistant messages (they carry usage)
                role = ""
                if isinstance(rec, dict):
                    role = str(rec.get("role") or rec.get("message", {}).get("role") or "")
                if role and role != "assistant":
                    continue
                usage = _find_usage(rec)
                if usage:
                    stats.turns += 1
                    stats.total_input += int(usage.get("input_tokens") or 0)
                    stats.total_output += int(usage.get("output_tokens") or 0)
                    stats.total_cache_read += int(usage.get("cache_read_input_tokens") or 0)
                    stats.total_cache_write += int(usage.get("cache_creation_input_tokens") or 0)
                    last_usage = usage
                model = _find_model(rec)
                if model:
                    latest_model = model
    except Exception as e:
        log.warning("transcript parse error for %s: %s", p, e)
        return None

    if last_usage:
        stats.current_context = (
            int(last_usage.get("input_tokens") or 0)
            + int(last_usage.get("cache_read_input_tokens") or 0)
            + int(last_usage.get("cache_creation_input_tokens") or 0)
        )
    if latest_model:
        stats.model_raw = latest_model
        stats.context_limit, stats.model_label = resolve_model(latest_model)
    # Claude Code on Opus 4.7 can be running the 1M context variant without
    # the "[1m]" suffix in the transcript. If we observe a context that
    # exceeds the detected limit, auto-promote to the 1M tier.
    if stats.current_context > stats.context_limit and stats.context_limit < 1_000_000:
        stats.context_limit = 1_000_000
        if stats.model_label and "(1M)" not in stats.model_label:
            stats.model_label = f"{stats.model_label} (1M)"
    return stats
