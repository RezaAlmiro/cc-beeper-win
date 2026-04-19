# CC-Beeper-Win — Features & Roadmap

A complete inventory of what's shipped, plus a backlog of improvements
ranked by "how useful if it were free." Not a commitment to build any of
these — just the design surface to pick from.

---

## Part 1. Current Features

### Core architecture
- Local FastAPI hook server, binds first free port in `127.0.0.1:19222-19230`
- Every Claude Code hook is wired: `SessionStart`, `UserPromptSubmit`,
  `PreToolUse`, `PostToolUse`, `Notification`, `Stop`, `StopFailure`, `SessionEnd`
- PySide6 glass-HUD widget (frameless, always-on-top, translucent)
- Config persistence in `config.json`; persistent trust in `trust.json` (gitignored)
- Git Bash / MSYS cygwin-PID to Win32-PID bridging via `/proc/$$/winpid`
  so `psutil` can walk the process tree

### Session management
- Per-session state keyed by `session_id`
- Browser-style tab strip integrated into the top edge of the glass
- Equal-width auto-shrink tabs (Windows-Terminal behaviour)
- Three ways to navigate: click a tab, ◀ / ▶ arrows, ☰ playlist dropdown
- **Sticky Close Tab** — dismissed sessions don't come back on the next
  hook fire; cleared only by restarting the server
- **Rename Tab** — custom display name persisted for the life of the session
- First user prompt auto-used as tab label (slash-command wrappers cleaned)
- Liveness sweep: sessions whose `claude.exe` ancestor PID is dead for >8 s
  are removed automatically
- Per-tab right-click menu: rename / slash commands / export stats / close

### State visualisation
- **State badge** (top-right pill) spells the state word — `IDLE`, `WORKING`,
  `DONE`, `INPUT`, `APPROVE`, `ERROR` — in the matching colour
- **Action circle** (bottom-centre) shows a one-letter code with distinct animation per state:
  - **I** white, steady — Idle
  - **D** green, steady — Done
  - **W** amber with a rotating clock-sweep arc — Working
  - **IN** blue, soft pulsing flash — Input Needed
  - **A** red with a pulsing red halo ring outside the circle — Approval Pending
  - **E** dark crimson, fast hard flash — Error
- Tab coloured stripe along its top edge, keyed to the same palette
- **News-ticker crawler** between title and context bar — scrolls the
  current tool and the last 8 tool calls; sits still if the text fits

### Decision strategies (who decides on permissions)
- **Assist** (default) — widget is the permission UI with the 4-way ladder
- **Observer** — widget only watches; Claude's native permission prompt runs
- **Auto** — headless rules + optional Gemini classifier, no popup

### Permission modes (auto-allow ladder)
- **Strict** — ask for everything except pure reads
- **Relaxed** (default) — reads + git-read fly; writes + network ask
- **Trusted** — also auto-allows project writes and local git operations
- **YOLO** — allow almost everything (safety net still blocks catastrophic ops)
- Tool-call classifier bucketises every call into a stable category
  (`Bash:git-read`, `Write:config`, `MCP:write:…`, etc.)
- Mode matrix maps each category → decision

### 4-way approval ladder
- Large popup on pending; generous spacing, summary in a peach highlight
  box, reason in italic red, 32 px action buttons
- **Allow Once** — this single call goes through
- **Allow For This Session** — category remembered until widget restart
- **Allow Forever** — written to `trust.json`, survives reboots
- **Deny** — hard-denied back to Claude with a reason
- **Manage Trust** dialog — view + remove any approval (session or persistent)

### Safety layer
- Regex fast-path on every `PreToolUse`:
  - Prompt-injection phrasing ("ignore all previous instructions", …)
  - Credential paths (`.ssh/id_rsa`, `.env`, `credentials.json`, …)
  - Irreversible commands (`rm -rf /`, `git push --force`, `drop table`, `mkfs`)
- **Safety net** — Bash-only catastrophic commands get hard-denied regardless
  of mode or trust
- **Optional Gemini Flash classifier** — when enabled, risky auto-allows
  get a ~300 ms "does this serve the stated task?" check; can downgrade
  to "ask" or hard-deny

### Context & token monitoring
- Transcript parsing (JSONL) with 200 MB file-size guard
- Model auto-detect (Opus 4.7 auto-promotes to 1M tier)
- Context progress bar (green < 60 % / orange 60–85 % / red > 85 %)
- Live lifetime totals: input / output / cache-read / cache-write / turns
- **Export Session Stats to .txt** — full report with identity, token
  totals, cache-hit rate, output/input ratio, auto-generated insights
  (cache efficiency, context pressure, output-heavy vs read-heavy,
  long-run flag) and token-hygiene tips tailored to the session

### Slash commands
- **⇣ Commands ▾** dropdown (and tab right-click menu) sends:
  - `/compact` — summarise + shrink context
  - `/clear` — reset conversation
  - `/cost` — print token usage
  - `/model` — switch model mid-session
  - `/resume` — resume an earlier session
- Confirmation dialog before each send
- Widget focuses the session's terminal, waits 220 ms for the foreground
  change, then simulates keystrokes via the `keyboard` module
- Refuses to fire while the state is red (would mix with Claude's live input)

### Terminal focus
- Click the sprite (or the action circle when not pending) → brings that
  session's exact terminal window forward
- HWND resolved at hook time by walking the process tree, with a
  window-title fallback (matches "Claude Code" / cwd basename) for cases
  where Windows Terminal re-parents its shells to `explorer.exe`
- `AttachThreadInput` + `SwitchToThisWindow` combo so `SetForegroundWindow`
  actually raises the window instead of just flashing the taskbar

### Audio cues
- Bell-synth WAVs generated on first run, saved to `assets/sounds/`:
  - **Approve** — E5 → C5 descending ding-dong (attention)
  - **Input** — A4 → C♯5 → E5 rising arpeggio (question)
  - **Done** — C5 → E5 → G5 rising major triad (completion)
- Fires on state transition only (not every poll)
- 1.5 s cooldown per session per cue
- Toggled via right-click → Sound Cues; setting persists

### Widget chrome & layout
- **Resize from all four edges + four corners** — cursor hints the
  direction, native `QWindow.startSystemResize` kicks in
- **Opacity control** — 60 / 75 / 85 / 95 / 100 % presets + custom slider,
  persisted
- **Drag the panel body** to move
- **Corner docking** — bottom-right default, configurable
- Size + position + opacity all persisted across restarts

### Menus & help
- **Right-click anywhere on the widget** → full main menu
- Tray icon with the same menu
- Strategy / Mode submenus with active-selection tick marks and inline
  descriptions ("Strict — ask for everything except reads", etc.)
- Opacity / Sound Cues / Help / Manage Trust / Clear Session Trust /
  Quit Widget
- **Help dialog** — full anatomy walkthrough with state-badge pills
  rendered inline, right-click behaviours documented

### Installation & deployment
- `installer/install_hooks.py` — adds hook entries to
  `~/.claude/settings.json` tagged with `cc-beeper-win`; auto-backs-up
  before writing
- `installer/uninstall_hooks.py` — clean removal of just our entries
- `install_launcher.bat` — generates `.ico` from a sprite, drops
  shortcuts on Desktop + in Start Menu; user can right-click → Pin to
  taskbar
- `install_autostart.bat` — adds a shortcut to Windows Startup folder
- `start_all.bat` / `start_widget.bat` / `start_server.bat` — one-click
  relaunch flows for server-and-widget, widget-only, or server-only
- `launcher.pyw` — idempotent combined launcher used by the shortcuts;
  ping-checks `/health` first, starts server only if needed, then widget

### Licence & distribution
- MIT, copyright 2026 RezaAlmiro
- Public repo: `github.com/RezaAlmiro/cc-beeper-win`
- Zero hardcoded secrets, zero PII, assets' EXIF stripped
- 3,790+ LOC Python, ~36 files total

---

## Part 2. Possible Improvements

Grouped roughly by cost-vs-value. Each item is one sentence — expand
before building.

### Interaction polish (easy wins)
- **Global hotkeys** — Alt+A to approve pending, Alt+D to deny, Alt+T to
  focus terminal, Alt+M to mute sound; match CC-Beeper Mac's scheme
- **Keyboard shortcut for ladder** — 1 / 2 / 3 / 4 mapped to the four
  buttons in the approval popup
- **Ctrl+Tab / Ctrl+Shift+Tab** to cycle sessions from keyboard
- **Hover-preview a tab** — tooltip currently shows static metadata;
  could show the last 3 tool calls live
- **Click the ticker** to pause / scroll-to-history / copy the current line

### Visual / UX
- **True backdrop blur** via Windows 11 DWM acrylic or mica — right now
  it's a semi-opaque fake; real blur would show the wallpaper through
- ~~**Dark-glass theme**~~ — **shipped.** Right-click → Theme → Light /
  Dark. Palette swap is live (no restart) and persists in `config.json`.
  Scope: main widget chrome (glass, text, tabs, ticker, context bar,
  action circle idle, buttons, badge). Approval popup and dialogs
  stay light by design; UsageDialog stays dark by design.
- ~~**Compact mode**~~ — **shipped.** Right-click → **Compact Mode**
  collapses to a 72 px single-row heartbeat view (sprite + title +
  state badge + ticker). Tab strip, context meter, token line and
  bottom button row hide. Expanded height is remembered and restored
  on toggle-off. Persists in `config.json` under `widget.compact`.
- ~~**Custom sprite pack**~~ — **shipped.** `_load_pixmap` checks
  `assets/custom/<fname>` first, falls back to the bundled asset.
  Folder is gitignored so user packs stay local. Filenames are
  documented in README.
- ~~**State-transition animations**~~ — **sprite crossfade shipped.**
  150 ms opacity crossfade (75 ms fade-out → swap → 75 ms fade-in,
  `InOutQuad` easing) when the sprite changes. No-op on same-state
  polls so it only fires on real state transitions. Halo / action-
  circle colour cross-fade still outstanding.
- ~~**Multi-monitor awareness**~~ — **shipped.** Position (x / y /
  screen name) is saved on every drag, restored on launch. Falls back
  to "screen containing the stored point" if the remembered monitor
  was unplugged, and to `_dock_to_corner` if no screen contains it.
  Position is clamped within the target screen so a resolution change
  never spawns off-screen.
- ~~**DPI-aware scaling**~~ — **shipped.** Sprites are now rendered
  at `logical_px × devicePixelRatio` physical pixels with the
  devicePixelRatio tag set, so they stay crisp on 125 / 150 / 200 %
  monitors instead of Qt upscaling a 64-pixel image to the physical
  target (which blurs). Picked up automatically when the widget
  moves between monitors with different DPIs (re-queried each tick).

### Feature depth
- **Cost tracking** — per-session dollar estimate using token counts ×
  model pricing; aggregate total per day
- **Sparkline chart** — in-widget mini-chart of cumulative tokens over
  time or tools-per-minute
- **Cross-session aggregates** — "Today: 4 sessions, 2.1 M tokens,
  12 approvals" in the help dialog or a stats panel
- **Session timeline persistence** — optional JSONL of every state
  transition for later review
- **Notification history** — in-widget scrollback of the last N approvals
  / denials / errors with timestamps
- **Approve-all-like-this** — after approving a Write once, quick button
  "Allow all Write to this path" instead of a whole category
- **Auto-compact threshold** — "compact automatically at 80 %" toggle

### Trust & safety
- **Per-project trust** — tie categories to `cwd` so a different project
  isn't affected by approvals made in this one
- **Custom safety-net patterns** — user-defined regex list on top of the
  built-in catastrophic-command list
- **Rate-limit categories** — "ask every 10th time" mid-way between
  once and session
- **Audit log** — append-only record of every approval decision (who,
  what, when, scope)
- **Trust export/import** — YAML sharable with a team so everyone
  auto-allows the same baseline

### Performance
- **Incremental transcript parsing** — track file byte-offset per
  session, only parse newly appended lines instead of re-reading the
  full JSONL each poll
- **WebSocket push** — replace 500 ms polling with server-sent updates;
  eliminates most client CPU
- **One-process deployment** — Qt event loop hosting FastAPI inside the
  same process (no separate server + widget)
- **Single-file .exe** — PyInstaller build + installer `.msi`;
  eliminates the "install Python first" step for non-devs

### Platform expansion
- **macOS port** — PySide6 is already cross-platform; `_focus_terminal`
  and Windows-Terminal-specific plumbing would need NSWorkspace /
  AXUIElement equivalents
- **Linux port** — same story, replace `win32gui` with X11 / Wayland
  primitives
- **Merge with `vecartier/cc-beeper`** — could share the hook-payload
  contract via a tiny shared JSON spec

### Integrations
- **Telegram bot mirror** — forward every approval popup to your phone;
  reply "y" / "n" to resolve remotely. (You already have a Telegram
  bot stack in `~/.config/gws`; would drop in cleanly.)
- **Phone push** — ntfy.sh / Pushover hook when a session goes red
- **Slack webhook** — team visibility of who's approving what
- **VS Code / Cursor / JetBrains extensions** — show CC session state
  in the IDE's status bar

### Admin / fleet (multi-user teams)
- **Named config profiles** — "Work safe", "Home YOLO", one-click swap
- **Centralised policy config** — org admin sets the baseline trust
  list, modes, safety net, ships it as a YAML

### Developer experience
- **Pytest harness** — mock hook payloads, assert state transitions
- **GitHub Actions CI** — lint, test, package on every push
- **Auto-updater** — check GitHub releases on launch; self-patch
- **Homebrew / winget / scoop** packaging
- **`.pyz` zipapp** — single-file executable-without-install

### Voice & accessibility
- **TTS read-out** — optional speech for "Claude wants to run `rm`" so
  you can work away from the screen. Gemini 2.5 Flash TTS in the
  workspace is already free; natural drop-in.
- **Voice-command approval** — "approve all" / "deny this" via Whisper
- **Screen-reader labels** — every widget has a proper accessible name
- **Colour-blind-safe palette** — alternate colour set for deutan /
  protan / tritan users

### Observability
- **`/metrics` endpoint** — Prometheus-style scrape target
- **CSV / JSON bulk export** — last N sessions in one archive
- **Daily digest email** — total tokens, biggest cache wins, cost
  estimate, tools-used breakdown

---

## Where to start

If I were picking two features to ship next, they'd be:

1. **Global hotkeys + ladder number-keys** — highest usability win for
   lowest build cost. People who live at the keyboard will love it.
2. **Telegram bot mirror** — you already have the stack; makes the
   widget useful even when you're away from the desk. Closes the loop
   with the infrastructure you've already built.

Everything else is incremental polish.
