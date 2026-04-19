# CC-Beeper-Win

A Windows companion widget for [Claude Code](https://docs.claude.com/en/docs/claude-code).
Inspired by the macOS-only [vecartier/cc-beeper](https://github.com/vecartier/cc-beeper) —
this is a from-scratch Windows port with per-session tabs, an approval
ladder, context + token meters, and a small pixel-art pet bedroom.

> Built in one long Claude Code session as a worked example of using CC to
> design and iterate on a real desktop tool — hooks, process-tree tricks,
> an async approval queue, transcript parsing, and a PySide6 UI, all
> shaped through back-and-forth with the model. Feel free to fork,
> hack, or rebuild for your own setup.

![screenshot placeholder]()

## What it does

- **Floating always-on-top widget** (180×120, drag anywhere) that lives in
  the corner of your screen and reacts live to your Claude Code session.
- **Tab strip**, one tab per active Claude Code session. Click a tab to
  switch view. Tabs go **red** by default (work in progress) and turn
  **green** the moment a turn finishes, so a glance tells you which
  sessions are still running and which are waiting on your reply.
- **Click-to-focus terminal**. Clicking the sprite brings the terminal
  tab that hosts the active Claude session back to the foreground —
  resolved at hook time via the process tree so it works even with
  multiple Windows Terminal tabs open.
- **4-way approval ladder**. When Claude wants permission for a tool, the
  widget flashes red (halo + border pulse) and a small popup offers:
  - *Allow once*
  - *Allow for this session*
  - *Allow forever (this category)*
  - *Deny*

  Scopes are remembered — "allow forever" categories survive restarts
  via `trust.json`, and a **Manage trust…** dialog lets you view and
  remove any approval later.
- **Context + token meter**. Reads the session transcript directly. Shows
  model (auto-detected), current context usage as a bar (green / orange /
  red), and lifetime input / output / cache-read totals.
- **Three strategies** (tray menu):
  - `assist` — widget is the permission UI (default)
  - `observer` — never override Claude; just monitor
  - `auto` — headless rules + optional Gemini classifier, no popup
- **Four modes** govern what's auto-allowed without prompting: `strict`,
  `relaxed`, `trusted`, `yolo`.
- **Safety net** — catastrophic Bash commands (`rm -rf /`,
  `git push --force`, `drop table`, `mkfs`…) are always hard-denied at
  the hook, even in YOLO mode.
- **Optional Gemini Flash classifier** — if you set `GEMINI_API_KEY` and
  flip `security.gemini_enabled` in `config.json`, risky auto-allowed
  tool calls get a ~300 ms sanity check ("does this tool call plausibly
  serve the stated task?") and get downgraded to a prompt if they don't.

## Requirements

- Windows 10/11
- Python 3.10+
- Claude Code CLI installed (`claude` on PATH)
- Git Bash (comes with Git for Windows) — used to execute the hook
  commands that Claude Code stores in `~/.claude/settings.json`

## Install

```bash
git clone https://github.com/<you>/cc-beeper-win.git
cd cc-beeper-win
pip install -r requirements.txt
python installer/install_hooks.py
```

The installer writes HTTP-hook entries into `~/.claude/settings.json`
alongside any existing hooks. Every entry is tagged with `cc-beeper-win`
in the command string so it can be cleanly uninstalled later. Your
`settings.json` is backed up to `settings.json.ccbeeper.bak` first.

## Run

```bash
start_all.bat          # launches the hook server + widget in the background
```

or manually:

```bash
pythonw server/server.py    # local HTTP server on 127.0.0.1:19222-19230
pythonw widget.py           # the tabbed widget
```

Verify the server is alive:

```bash
curl http://127.0.0.1:19222/health
# {"ok":true, "mode":"relaxed", ...}
```

Open a new Claude Code session (`claude` in a terminal). A new tab will
appear on the widget as soon as your first prompt fires. Reject or
approve its permission prompts with the widget's four buttons.

## Uninstall

```bash
python installer/uninstall_hooks.py   # remove just the cc-beeper-win entries
# or
python installer/install_hooks.py --uninstall
```

Your other settings.json hooks are untouched.

## Configuration

Everything lives in `config.json` at the project root. Defaults are
safe. Relevant knobs:

```jsonc
{
  "decision_strategy": "assist",      // assist | observer | auto
  "mode": "relaxed",                  // strict | relaxed | trusted | yolo
  "security": {
    "gemini_enabled": false,
    "gemini_timeout_s": 4.0,
    "safety_net_block_catastrophic": true
  },
  "widget": {
    "width": 220, "height": 160,
    "corner": "bottom-right", "margin": 16,
    "halo_radius": 8,
    "halo_color": "#ff5e5e",
    "border_color": "#ff2e2e"
  }
}
```

Your approved "allow forever" categories live in `trust.json` (which
this repo ships empty). Delete that file to reset.

## Architecture

```
claude  ──hook fires──>  bash curl  ──POST──>  FastAPI server  ──state──>  widget
                                                     │
                                                     └── optional: Gemini classifier
```

- `server/server.py` — hook endpoints (`/pretooluse`, `/stop`, …), per-session
  state, pending-request queue, terminal HWND resolution.
- `server/classify.py` — tool-call → category classifier
  (e.g. `Bash:git-read`, `Write:config`, `MCP:write:…`).
- `server/security.py` — regex fast-path + optional Gemini sanity check.
- `server/stats.py` — transcript JSONL parser; model + token + context math.
- `server/trust.py` — persistent + session trust store with 4 modes.
- `widget.py` — PySide6 widget, tab strip, meter, halo, approval popup,
  trust settings dialog, system tray.
- `installer/install_hooks.py` — reversibly injects hook entries into
  `~/.claude/settings.json`.

## Security notes

- The hook server binds to `127.0.0.1` only — no external network
  exposure. There is no auth on the local API, which is the standard
  localhost threat model: any process already on your machine could POST
  to `/resolve` and auto-approve a pending request. Don't run this on a
  shared machine or a hostile environment.
- The optional Gemini Flash classifier is **off by default**. When you
  enable it (`security.gemini_enabled: true` + `GEMINI_API_KEY` in `.env`),
  each risky tool call's `tool_input` + your session's latest user prompt
  are POSTed to Google's Gemini API. Leave it off if you work on sensitive
  code.
- The safety net hard-denies obviously catastrophic Bash commands
  (`rm -rf /`, `git push --force`, `drop table`, `mkfs`) even in YOLO mode.
  Toggle with `security.safety_net_block_catastrophic`.
- The installer backs up `~/.claude/settings.json` to
  `settings.json.ccbeeper.bak` before modifying it, and all injected hook
  entries are tagged with `cc-beeper-win` for clean reversal. Your other
  hooks are never touched.

## License

MIT — see [LICENSE](LICENSE).

## Acknowledgements

- Original concept: [vecartier/cc-beeper](https://github.com/vecartier/cc-beeper) (macOS, Swift 6).
- Pixel-art sprites were generated with Gamma + ideogram-v3-turbo.
- Built interactively via Claude Code.
