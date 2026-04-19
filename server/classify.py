"""Classify a PreToolUse payload into a stable category key.

The category string is the unit of trust — when a user approves a tool call,
future calls in the same category are auto-approved (per mode rules or
explicit trust).

Category format: "<Bucket>:<Shape>"
  Bucket ∈ {Read, Write, Bash, MCP, Agent, Meta}
  Shape is a coarse tag for that bucket.

Examples:
  Read tool                     -> "Read:file"
  Grep tool                     -> "Read:search"
  Write/Edit in a project file  -> "Write:project"
  Write/Edit to .env or ~/.ssh  -> "Write:config"
  Bash "git status"             -> "Bash:git-read"
  Bash "git push --force"       -> "Bash:destructive"
  Bash "rm -rf foo"             -> "Bash:destructive"
  Bash "npm install"            -> "Bash:install"
  Bash "curl https://…"         -> "Bash:network"
  Bash "ls" / "cat" / "pwd"     -> "Bash:read"
  mcp__…__get_… / search_…      -> "MCP:read"
  mcp__…__send_… / create_…     -> "MCP:write"
  Agent (Explore)               -> "Agent:explore"
  Agent (other)                 -> "Agent:spawn"
"""
from __future__ import annotations

import re
from typing import Any

CONFIG_PATH_PATTERNS = [
    re.compile(r"\.env(\.|$)"),
    re.compile(r"/\.ssh/"),
    re.compile(r"\.aws/credentials"),
    re.compile(r"/\.config/gws/credentials"),
    re.compile(r"/\.claude/(settings|credentials|\.env)"),
    re.compile(r"client_secret", re.I),
    re.compile(r"token_cache", re.I),
]

DESTRUCTIVE_BASH = [
    re.compile(r"\brm\s+(-[rRf]+\s+)?(/|~|\*)", re.I),
    re.compile(r"\brmdir\s+/s", re.I),
    re.compile(r"\bdel\s+/s", re.I),
    re.compile(r"\bgit\s+push\s+(-f|--force|--force-with-lease\s+--force)"),
    re.compile(r"\bgit\s+reset\s+--hard\b"),
    re.compile(r"\bgit\s+clean\s+-[fxd]+\b"),
    re.compile(r"\bdrop\s+(table|database|schema)\b", re.I),
    re.compile(r"\btruncate\s+table\b", re.I),
    re.compile(r">\s*/dev/sd[a-z]", re.I),
    re.compile(r"\bmkfs", re.I),
]

NETWORK_BASH = [
    re.compile(r"\bcurl\b"),
    re.compile(r"\bwget\b"),
    re.compile(r"\bssh\b"),
    re.compile(r"\bscp\b"),
    re.compile(r"\brsync\b.*::"),
]

INSTALL_BASH = [
    re.compile(r"\bnpm\s+(install|i|add)\b"),
    re.compile(r"\bpip\s+install\b"),
    re.compile(r"\bpnpm\s+(install|add)\b"),
    re.compile(r"\byarn\s+(install|add)\b"),
    re.compile(r"\bapt(-get)?\s+install\b"),
    re.compile(r"\bbrew\s+install\b"),
    re.compile(r"\bchoco\s+install\b"),
    re.compile(r"\bwinget\s+install\b"),
    re.compile(r"\bcargo\s+install\b"),
]

GIT_READ = re.compile(r"^\s*git\s+(status|log|diff|show|branch|remote|blame|rev-parse|describe|config\s+--get)\b")
GIT_WRITE_LOCAL = re.compile(r"^\s*git\s+(add|commit|stash|checkout|switch|merge|rebase|pull|fetch|tag)\b")
GIT_PUBLISH = re.compile(r"^\s*git\s+push\b")

SAFE_BASH_CMDS = {
    "ls", "dir", "pwd", "cd", "cat", "head", "tail", "wc", "which", "where",
    "echo", "printf", "date", "whoami", "hostname", "env", "set", "ps",
    "netstat", "ipconfig", "ifconfig", "ping", "nslookup",
    "python", "python3", "node", "deno", "ruby", "go", "rustc", "cargo",
    "tsc", "tsx", "pytest", "uvicorn", "fastapi",
}


def _first_word(cmd: str) -> str:
    cmd = cmd.strip()
    for token in cmd.split():
        # strip leading redirects, env assignments, etc.
        if "=" in token and token.split("=", 1)[0].isidentifier():
            continue
        return token.lstrip("/\\").split("/")[-1].split("\\")[-1].lower()
    return ""


def _touches_config_path(text: str) -> bool:
    return any(p.search(text) for p in CONFIG_PATH_PATTERNS)


def classify(tool_name: str, tool_input: dict[str, Any]) -> str:
    tn = tool_name or ""

    # MCP tools — bucket by verb prefix in the method name
    if tn.startswith("mcp__"):
        # mcp__<server>__<method>
        method = tn.split("__", 2)[-1].lower()
        if re.match(r"^(get|list|search|read|fetch|describe|status|resolve|download|show|find|query|history|agenda|triage)(_|$)", method):
            return f"MCP:read:{tn}"
        if re.match(r"^(send|create|update|delete|remove|move|add|insert|post|comment|reply|share|import|export|upload|invite|set|generate|merge|resize|commit|cancel|start|schedule|trigger)(_|$)", method):
            return f"MCP:write:{tn}"
        return f"MCP:other:{tn}"

    # Read-only Claude tools
    if tn in {"Read", "Grep", "Glob", "LS", "WebFetch", "WebSearch", "NotebookRead", "TaskList", "TaskGet", "TaskOutput", "ListMcpResourcesTool", "ReadMcpResourceTool"}:
        return "Read:file" if tn in {"Read", "NotebookRead"} else "Read:search"

    # Write-y Claude tools
    if tn in {"Write", "Edit", "NotebookEdit"}:
        path = str(tool_input.get("file_path") or tool_input.get("notebook_path") or "")
        if _touches_config_path(path.replace("\\", "/")):
            return "Write:config"
        return "Write:project"

    # Task / plan management
    if tn in {"TaskCreate", "TaskUpdate", "TaskStop"}:
        return "Meta:task"

    # Subagent launches
    if tn == "Agent":
        subtype = str(tool_input.get("subagent_type") or "general").lower()
        return "Agent:explore" if subtype == "explore" else f"Agent:{subtype}"

    # Bash — look at the command
    if tn == "Bash":
        cmd = str(tool_input.get("command") or "")

        # config path touches via cat/echo/sed/etc
        if _touches_config_path(cmd):
            return "Bash:config-touch"

        # destructive always wins
        if any(p.search(cmd) for p in DESTRUCTIVE_BASH):
            return "Bash:destructive"

        # git special-casing
        if GIT_READ.search(cmd):
            return "Bash:git-read"
        if GIT_PUBLISH.search(cmd):
            return "Bash:git-publish"
        if GIT_WRITE_LOCAL.search(cmd):
            return "Bash:git-write-local"

        if any(p.search(cmd) for p in INSTALL_BASH):
            return "Bash:install"
        if any(p.search(cmd) for p in NETWORK_BASH):
            return "Bash:network"

        first = _first_word(cmd)
        if first in SAFE_BASH_CMDS:
            return "Bash:read"

        return "Bash:other"

    # Misc
    if tn in {"BashOutput", "KillShell", "SlashCommand"}:
        return f"Meta:{tn.lower()}"

    return f"Other:{tn}"
