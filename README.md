# claude-code-auto-continue

A Windows GUI watchdog that detects when [Claude Code](https://github.com/anthropics/claude-code) running in Windows Terminal hits the 5-hour usage limit, waits for the reset moment, then automatically types `continue` into that exact terminal — letting you walk away from a long session and come back to a still-progressing conversation.

Optionally also sends `/effort <level>` first, configurable per window.

> **Windows-only.** Uses Win32 UI Automation to read terminal scrollback and `SendInput` for keystroke delivery. Tested on Windows 11 with Windows Terminal + PowerShell.

## Download

**[⬇️ Auto-Continue.exe (latest release)](https://github.com/zhh198903-ctrl/claude-code-auto-continue/releases/latest)** — single-file Windows executable, ~55 MB, no Python or dependencies required. Double-click to run.

Prefer source? See [Install](#install) below.

## How it works

1. **Detect** — Polls every Windows Terminal window's scrollback via UI Automation. When the literal Anthropic limit message appears (both lines: `You've hit your limit · resets <H>[:<MM>]<am|pm> (<TZ>)` followed by the `/upgrade …` line), the row turns yellow and a countdown starts. Both Anthropic wordings of that follow-up line are recognized — `/upgrade or /extra-usage to finish what you're working on.` and `/upgrade to increase your usage limit.` — and the reset time may carry minutes (`2:50pm`) or not (`11pm`).
2. **Schedule** — Parses the reset time in the named timezone, computes the next occurrence in UTC, adds a small buffer (default 20 s past the reset).
3. **Fire** — At fire time, brings the target Windows Terminal window forward, sends `/effort <level>` + Enter (if configured for that row, plus an extra Enter to confirm the "Change effort level?" dialog), then `continue` + Enter. Restores the previously-focused window so the keystrokes don't yank you out of what you were doing.

The watcher runs on a Qt worker thread so UIA reads (which take 100 ms+) never block the UI.

## Install

### Option A — Prebuilt exe (recommended)

Grab **`Auto-Continue.exe`** from the [latest release](https://github.com/zhh198903-ctrl/claude-code-auto-continue/releases/latest) and double-click it. That's it.

### Option B — From source

```powershell
git clone https://github.com/zhh198903-ctrl/claude-code-auto-continue.git
cd claude-code-auto-continue
pip install -r requirements.txt
```

To build your own exe from source: `python -m PyInstaller Auto-Continue.spec --clean --noconfirm` → output at `dist/Auto-Continue.exe`.

## Run

```powershell
python gui.py
```

Or double-click **`Auto-Continue.pyw`** — the `.pyw` extension launches under `pythonw.exe`, so no extra console window appears alongside the GUI.

There's also a CLI runner:

```powershell
python auto_continue.py --dry-run   # detect + log only
python auto_continue.py             # live, all WT windows
python auto_continue.py --interval 20 --buffer 30 --match peak
```

## GUI features

- **Live table** of every Windows Terminal window — title, status, reset time, countdown, last sent, per-row effort dropdown, action buttons.
- **Status colors** — yellow = ⏳ Waiting, orange = ▶ Sending, green = ✓ Sent (5 s flash), gray = Cooldown, light gray = Excluded.
- **Action buttons per row:**
  - **Now** — fire `continue` immediately (bypasses the timer; respects the per-row effort setting).
  - **Skip** — cancel a pending continue for this row.
  - **Exclude** — never watch this window again (persisted across restarts).
  - **Clear cooldown** — reset the 15-min post-send suppression so the row can re-detect immediately (useful for testing).
- **Effort dropdown per row** — `(none)` / `max` / `xhigh` / `high` / `medium` / `low` / `auto`. When set, the fire sequence becomes `/effort <level>` + Enter → wait 0.6 s → blank Enter (confirms the "Change effort level?" dialog) → wait 0.6 s → `continue` + Enter. Persisted per stable window title.
- **Dry-run** — detect, schedule, and log everything but never actually press keys. Useful for sanity-checking before going live.
- **Keep awake** — calls `SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_AWAYMODE_REQUIRED)` so Windows Modern Standby doesn't kill the watcher process during a long unattended wait.
- **Minimize to system tray** — minimize button hides the window to the right-side tray; X button fully exits. Right-click the tray icon for **Show window** / **Quit**.
- **DPI awareness** set to per-monitor v2 before any Qt or UIA DLL loads, so high-DPI displays render correctly without a startup warning.
- **Per-window cooldown** — 15 minutes after a successful fire, the row's detection is suppressed so the lingering limit message in scrollback doesn't trigger a re-fire.

All settings (poll interval, buffer, dry-run, keep-awake, excluded windows, per-window effort) persist via `QSettings` (Windows registry under `HKCU\Software\auto_continue\gui`).

## Architecture

```
auto_continue.py    Shared core + standalone CLI runner
                    - find_terminal_windows()   enumerate Windows Terminal hwnds
                    - read_terminal_text()      UIA TextPattern → full scrollback
                    - parse_limit_message()     two-line regex with stale-buffer guard
                    - next_reset_datetime()     pytz-aware next occurrence
                    - send_continue() / send_text_lines()
                                                SetActive + SetFocus + auto.SendKeys

gui.py              PyQt6 front-end
                    - Watcher (QObject on QThread)  poll loop, signal-based
                    - MainWindow                    table + log + tray + settings

Auto-Continue.pyw   Double-click launcher (no console window)

test_parse.py       Regex unit tests (8 samples)
```

## Detection robustness

The regex requires **both lines** of the Anthropic limit message and rejects matches buried more than ~4000 chars deep in scrollback. This avoids:

- Source files containing the literal string (e.g. test fixtures, this README).
- Past limit messages still in scrollback that the user already handled.
- Random text that happens to contain "You've hit your limit" but not the `/upgrade` follow-up.

If a real Anthropic message format change breaks detection, edit `LIMIT_RE` in `auto_continue.py`.

## Foreground rights / SendKeys notes

When the GUI fires keystrokes, it:

1. Saves the currently foreground window handle.
2. `SetActive` on the target Windows Terminal window, then `SetFocus` on its TermControl.
3. Waits 250 ms for the focus change to land.
4. `auto.SendKeys("continue{Enter}", interval=0.02)` → Win32 `SendInput`.
5. Restores the original foreground window.

This works because the GUI is an interactive Qt app the user launched — it has Windows foreground rights. Subprocesses spawned without a UI cannot reliably take foreground; tests of the same code path from a headless launcher fail silently. If you need fully headless operation, run the GUI as a normal interactive app and let it be the foreground-rights holder.

## Known limitations

- **Windows-only.** UIA + SendInput + Modern Standby calls are all Win32-specific.
- **Windows Terminal required.** ConHost classic console isn't enumerated (the regex would still match its scrollback if it were, but the Watcher's `find_terminal_windows()` filters by the WT class `CASCADIA_HOSTING_WINDOW_CLASS`).
- **The 15-min cooldown** is hard-coded. After a fire, the row won't re-detect for 15 minutes even if a new limit message appears (rare in practice — Anthropic limits don't fire that fast back-to-back).
- **`/effort max`** triggers a confirmation dialog that we auto-confirm with an extra Enter. Other levels also trigger this dialog (verified May 2026), so the extra Enter is always sent when an effort level is configured.
- **Modern Standby** can throttle background Python processes even with keep-awake enabled in some power profiles. If the watcher dies overnight while keep-awake was on, file an issue with the Windows event log around the death time.

## License

MIT — see `LICENSE`.
