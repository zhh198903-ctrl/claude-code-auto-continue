"""
Auto-continue Claude Code after the 5-hour limit hits.

What it does:
  * Polls every Windows Terminal window's scrollback via UI Automation.
  * Detects the "You've hit your limit · resets <H>[:<MM>]<am|pm> (<TZ>)"
    message (both Anthropic wordings of the /upgrade follow-up line).
  * Parses the reset time, waits until that moment (plus a small buffer),
    then types `continue` + Enter into that exact window.

Safe-by-default:
  * --dry-run prints what it would do without sending keystrokes.
  * Per-window cooldown prevents double-sending after we already pressed
    continue for the same limit hit.
  * Restores foreground window after sending keys (minimizes focus theft).

Usage examples:
  python auto_continue.py                # watch all WT windows, live
  python auto_continue.py --dry-run      # detect + log only
  python auto_continue.py --interval 20  # poll every 20s (default 30)
  python auto_continue.py --match peak   # only windows whose title contains "peak"
"""

from __future__ import annotations

import argparse
import ctypes
import io
import re
import sys
import time
from ctypes import wintypes
from dataclasses import dataclass
from datetime import datetime, timedelta

import pytz
import uiautomation as auto


def _force_utf8_console() -> None:
    """Switch stdout/stderr to UTF-8 so WT titles with CJK don't blow up."""
    try:
        sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
        sys.stderr.reconfigure(encoding="utf-8", line_buffering=True)
    except Exception:
        try:
            sys.stdout = io.TextIOWrapper(
                sys.stdout.buffer, encoding="utf-8", line_buffering=True
            )
            sys.stderr = io.TextIOWrapper(
                sys.stderr.buffer, encoding="utf-8", line_buffering=True
            )
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Patterns
# ---------------------------------------------------------------------------

# Matches the full two-line Anthropic limit message. Anthropic has shipped
# more than one wording; both are accepted:
#
#     You've hit your limit · resets 11pm (Asia/Shanghai)
#     /upgrade or /extra-usage to finish what you're working on.
#
#     You've hit your limit · resets 2:50pm (Asia/Shanghai)
#     /upgrade to increase your usage limit.
#
# The reset time may or may not carry minutes (`11pm` vs `2:50pm`).
# Requiring the /upgrade follow-up line is what tells us this is a real
# rate-limit hit and not e.g. our own test output or this script's source
# code visible in the user's scrollback. The DOTALL `[\s\S]{0,400}` lets the
# two lines be separated by terminal padding/whitespace.
LIMIT_RE = re.compile(
    r"You['’]ve hit your limit\s*[·•‧․\-]?\s*"
    r"resets\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)\s*\(([^)]+)\)"
    r"[\s\S]{0,400}?"
    r"/upgrade\s+(?:or\s+/extra[-\s]?usage"
    r"|to\s+increase\s+your\s+usage\s+limit)",
    re.IGNORECASE,
)

# How many trailing chars of the scrollback we scan each tick. Plenty for the
# message to appear, small enough that re.search stays cheap.
SCAN_TAIL_CHARS = 8000


# ---------------------------------------------------------------------------
# Per-window state
# ---------------------------------------------------------------------------


@dataclass
class WindowState:
    title: str
    # Reset moment in UTC for the currently-detected limit hit. None = no
    # limit currently detected.
    pending_reset_utc: datetime | None = None
    # Last time we sent "continue" to this window. Used to suppress
    # re-detection: the limit message stays in scrollback after we continue,
    # so we'd otherwise loop. We ignore detections for `cooldown_seconds`
    # after sending.
    last_sent_utc: datetime | None = None
    # Logged-once flags so we don't spam the console.
    logged_detection: bool = False


# ---------------------------------------------------------------------------
# UI Automation helpers
# ---------------------------------------------------------------------------


def find_terminal_windows():
    """Return list of WindowControl for Windows Terminal top-level windows."""
    out = []
    for w in auto.GetRootControl().GetChildren():
        cls = w.ClassName or ""
        if "CASCADIA" in cls.upper():
            out.append(w)
    return out


def find_termcontrol(window_ctrl, depth=0, _limit=10):
    """Locate the TermControl (actual terminal surface) inside a WT window."""
    if depth > _limit:
        return None
    try:
        if (window_ctrl.ClassName or "") == "TermControl":
            return window_ctrl
    except Exception:
        pass
    try:
        for c in window_ctrl.GetChildren():
            r = find_termcontrol(c, depth + 1, _limit)
            if r:
                return r
    except Exception:
        pass
    return None


def read_terminal_text(window_ctrl) -> str | None:
    """Return the visible+scrollback text of the active tab's TermControl."""
    term = find_termcontrol(window_ctrl)
    if term is None:
        return None
    try:
        tp = term.GetTextPattern()
        if not tp:
            return None
        return tp.DocumentRange.GetText(-1)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Reset-time parsing
# ---------------------------------------------------------------------------


# Maximum number of characters that may appear *after* the limit-message
# match. If the post-match tail is longer than this, the message is buried in
# old scrollback (e.g. logs of a previous limit hit, or this script's own
# source viewed in the terminal) — ignore it.
MAX_POST_MATCH_TAIL = 4000


def parse_limit_message(text: str) -> tuple[int, int, str, str] | None:
    """Return (hour_12, minute, ampm, tz_name) from the *latest* limit line.

    Returns None if no match. The match must be near the end of the buffer —
    a stale limit message further back means Claude has already moved on.
    `minute` is 0 when the message omits minutes (e.g. `resets 11pm`).
    """
    matches = list(LIMIT_RE.finditer(text))
    if not matches:
        return None
    m = matches[-1]
    if len(text) - m.end() > MAX_POST_MATCH_TAIL:
        return None
    minute = int(m.group(2)) if m.group(2) else 0
    return int(m.group(1)), minute, m.group(3).lower(), m.group(4).strip()


def next_reset_datetime(hour_12: int, minute: int, ampm: str,
                        tz_name: str) -> datetime:
    """
    Compute the next wall-clock occurrence of `hour_12:minute am/pm` in
    `tz_name`, return it as an aware UTC datetime.
    """
    try:
        tz = pytz.timezone(tz_name)
    except pytz.UnknownTimeZoneError:
        # Fall back to local time. Better than crashing.
        tz = pytz.timezone("Asia/Shanghai")

    hour_24 = hour_12 % 12 + (12 if ampm == "pm" else 0)
    now_local = datetime.now(tz)
    target_local = now_local.replace(
        hour=hour_24, minute=minute, second=0, microsecond=0
    )
    if target_local <= now_local:
        target_local += timedelta(days=1)
    return target_local.astimezone(pytz.UTC)


# ---------------------------------------------------------------------------
# Sending keystrokes
# ---------------------------------------------------------------------------


_user32 = ctypes.windll.user32
_user32.GetForegroundWindow.restype = wintypes.HWND


def _get_foreground_hwnd() -> int:
    return int(_user32.GetForegroundWindow() or 0)


def _set_foreground_hwnd(hwnd: int) -> None:
    if hwnd:
        try:
            _user32.SetForegroundWindow(hwnd)
        except Exception:
            pass


def send_continue(window_ctrl, dry_run: bool = False) -> bool:
    """Activate the window's TermControl and type 'continue' + Enter.

    Returns True on success (or simulated success in dry-run), False if the
    target has no TermControl. Callers are responsible for any logging —
    this function intentionally does not write to stdout because callers
    may be running in a thread whose stdout codec can't encode the WT
    title's unicode glyphs.
    """
    return send_text_lines(window_ctrl, ["continue"], dry_run=dry_run)


def send_text_lines(window_ctrl, lines, dry_run: bool = False) -> bool:
    """Activate the window's TermControl, then type each line followed by
    Enter, with a small pause between consecutive submissions so Claude
    Code can fully process slash commands before the next message arrives.

    Same caveats as send_continue: returns True/False, no stdout output.
    """
    if not lines:
        return True
    term = find_termcontrol(window_ctrl)
    if term is None:
        return False

    if dry_run:
        return True

    saved_fg = _get_foreground_hwnd()
    try:
        try:
            window_ctrl.SetActive()
        except Exception:
            pass
        try:
            term.SetFocus()
        except Exception:
            pass
        # Tiny pause so the focus change actually lands before keystrokes.
        time.sleep(0.25)
        for i, line in enumerate(lines):
            if i > 0:
                # Slash commands like /effort apply synchronously but Claude
                # Code's TUI needs a beat to render the resulting state
                # before the next "continue" submission lands.
                time.sleep(0.6)
            auto.SendKeys(str(line) + "{Enter}", interval=0.02)
        return True
    finally:
        time.sleep(0.15)
        if saved_fg and saved_fg != int(window_ctrl.NativeWindowHandle or 0):
            _set_foreground_hwnd(saved_fg)


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------


def fmt_dt_local(dt_utc: datetime) -> str:
    return dt_utc.astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")


def run(args) -> None:
    states: dict[int, WindowState] = {}
    # Cooldown: after we send continue, don't re-detect for this long. The
    # limit message lingers in the scrollback even after the user is unblocked.
    cooldown = timedelta(minutes=15)
    # Buffer added to the parsed reset time. Anthropic's reset is on the hour;
    # giving them a few seconds of grace avoids hitting the gate at HH:00:00.
    buffer = timedelta(seconds=args.buffer)

    print(f"[boot] auto_continue running; interval={args.interval}s, "
          f"buffer={args.buffer}s, dry_run={args.dry_run}, "
          f"match={args.match!r}", flush=True)

    while True:
        try:
            tick(states, args, cooldown, buffer)
        except KeyboardInterrupt:
            print("\n[exit] interrupted by user.", flush=True)
            return
        except Exception as e:
            # Don't let a transient UIA hiccup take the watchdog down.
            print(f"[warn] tick error: {type(e).__name__}: {e}", flush=True)
        time.sleep(args.interval)


def tick(states: dict[int, WindowState], args,
         cooldown: timedelta, buffer: timedelta) -> None:
    now_utc = datetime.now(pytz.UTC)
    windows = find_terminal_windows()

    seen_hwnds: set[int] = set()
    for w in windows:
        try:
            hwnd = int(w.NativeWindowHandle or 0)
        except Exception:
            hwnd = 0
        if not hwnd:
            continue
        seen_hwnds.add(hwnd)

        title = w.Name or f"<hwnd {hwnd}>"
        if args.match and args.match.lower() not in title.lower():
            continue

        st = states.setdefault(hwnd, WindowState(title=title))
        st.title = title  # title can change as user switches tabs

        # 1. If a continue is already pending, check whether time has come.
        if st.pending_reset_utc is not None:
            fire_at = st.pending_reset_utc + buffer
            if now_utc >= fire_at:
                print(f"[fire] {now_utc.astimezone():%H:%M:%S} → sending "
                      f"'continue' to {title!r}", flush=True)
                ok = send_continue(w, dry_run=args.dry_run)
                if ok:
                    st.last_sent_utc = now_utc
                    st.pending_reset_utc = None
                    st.logged_detection = False
                else:
                    # Couldn't send (no TermControl?). Push the next attempt
                    # one interval out so we don't spin.
                    st.pending_reset_utc = now_utc + timedelta(
                        seconds=args.interval
                    )
            continue  # don't re-detect on the same tick

        # 2. Cooldown after recent send: skip detection.
        if st.last_sent_utc and now_utc - st.last_sent_utc < cooldown:
            continue

        # 3. Read terminal and look for limit message.
        text = read_terminal_text(w)
        if not text:
            continue
        tail = text[-SCAN_TAIL_CHARS:]
        parsed = parse_limit_message(tail)
        if not parsed:
            st.logged_detection = False
            continue

        hour_12, minute, ampm, tz_name = parsed
        reset_utc = next_reset_datetime(hour_12, minute, ampm, tz_name)
        st.pending_reset_utc = reset_utc
        if not st.logged_detection:
            print(
                f"[detect] {title!r}\n"
                f"          limit hit; resets {hour_12}:{minute:02d}{ampm} "
                f"({tz_name})\n"
                f"          will fire continue at "
                f"{fmt_dt_local(reset_utc + buffer)}",
                flush=True,
            )
            st.logged_detection = True

    # Clean up state for windows that closed.
    for hwnd in list(states):
        if hwnd not in seen_hwnds:
            del states[hwnd]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="Auto-continue Claude Code after the 5h limit."
    )
    p.add_argument("--interval", type=int, default=30,
                   help="Polling interval in seconds (default 30).")
    p.add_argument("--buffer", type=int, default=20,
                   help="Extra seconds to wait past reset time (default 20).")
    p.add_argument("--match", type=str, default="",
                   help="Only watch WT windows whose title contains this "
                        "substring (case-insensitive). Empty = all.")
    p.add_argument("--dry-run", action="store_true",
                   help="Detect and log, but don't actually send keystrokes.")
    return p.parse_args(argv)


if __name__ == "__main__":
    _force_utf8_console()
    run(parse_args())
