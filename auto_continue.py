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

# Bumped on every shipped build so the running version is visible in the GUI
# title bar (and thus in the Windows Terminal window title the watchdog reads).
APP_VERSION = "1.0.4"


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
# several wordings; all are accepted:
#
#     You've hit your limit · resets 11pm (Asia/Shanghai)
#     /upgrade or /extra-usage to finish what you're working on.
#
#     You've hit your limit · resets 2:50pm (Asia/Shanghai)
#     /upgrade to increase your usage limit.
#
#     You've hit your session limit · resets 12am (Asia/Shanghai)
#     /upgrade to increase your usage limit.
#
# The leading "hit your ..." may say `limit`, `session limit`, `usage limit`,
# `weekly limit`, etc. The reset time may or may not carry minutes (`11pm`
# vs `2:50pm`). Requiring the /upgrade follow-up line is what tells us this
# is a real rate-limit hit and not e.g. our own test output or this script's
# source code visible in the user's scrollback. The DOTALL `[\s\S]{0,400}`
# lets the two lines be separated by terminal padding/whitespace.
LIMIT_RE = re.compile(
    r"You['’]ve hit your (?:\w+\s+)?limit\s*[·•‧․\-]?\s*"
    r"resets\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)\s*\(([^)]+)\)"
    r"[\s\S]{0,400}?"
    r"/upgrade\s+(?:or\s+/extra[-\s]?usage"
    r"|to\s+increase\s+your\s+usage\s+limit)",
    re.IGNORECASE,
)

# Network-retry banner shown when Claude Code can't reach the API:
#     Retrying in 0s · attempt 7/10
# When attempt count hits N/N, Claude has exhausted its automatic retries
# and the user normally has to type 'continue' to resume after the network
# is back. We mimic that. Anchoring on the "Retrying in <n>s" prefix avoids
# matching unrelated occurrences of "attempt N/M" in normal output.
RETRY_RE = re.compile(
    r"Retrying\s+in\s+\d+\s*s\s*[·•‧․\-]?\s*"
    r"attempt\s+(\d+)\s*/\s*(\d+)",
    re.IGNORECASE,
)

# Bare-error variant — sometimes Claude Code prints the API error directly
# without the "Retrying in Ns · attempt N/M" banner (e.g. when a tool call's
# result can't be POSTed back and there's still a shell tool running):
#     API Error: Unable to connect to API (ECONNRESET)
# Treat this the same as retry-exhausted: keep poking 'continue' until the
# connection comes back. The literal "(ECONNRESET)" with parens is distinctive
# enough that we won't false-match prose. To avoid matching this script's own
# source / docstrings in scrollback, we require the leading "API Error:" prefix.
ECONNRESET_RE = re.compile(
    r"API\s+Error:\s*Unable\s+to\s+connect\s+to\s+API\s*\(ECONNRESET\)",
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
    # The (hour_12, minute, ampm, tz) tuple that produced pending_reset_utc.
    # We compare against this on each detect tick, not against the computed
    # UTC moment: once the wall-clock target has passed, re-parsing the same
    # lingering message would roll it forward to "tomorrow at the same
    # time", which would push out a pending that's about to fire.
    pending_reset_key: tuple | None = None
    # Last time we sent "continue" to this window. Used to suppress
    # re-detection: the limit message stays in scrollback after we continue,
    # so we'd otherwise loop. We ignore detections for `cooldown_seconds`
    # after sending.
    last_sent_utc: datetime | None = None
    # Logged-once flags so we don't spam the console.
    logged_detection: bool = False

    # Network-retry exhaustion (attempt N/N) state. Independent of the
    # rate-limit fields above — both can be active at the same time.
    retry_last_sent_utc: datetime | None = None
    retry_logged: bool = False


# ---------------------------------------------------------------------------
# UI Automation helpers
# ---------------------------------------------------------------------------


def find_terminal_windows():
    """Return list of WindowControl for Windows Terminal top-level windows.

    Every UIA access is guarded: a flaky child window that raises a COMError
    (e.g. EVENT_E_ALL_SUBSCRIBERS_FAILED) is skipped rather than aborting the
    whole enumeration — otherwise one bad window would blind the watchdog to
    every other terminal that tick.
    """
    out = []
    try:
        children = auto.GetRootControl().GetChildren()
    except Exception:
        return out
    for w in children:
        try:
            cls = w.ClassName or ""
        except Exception:
            continue
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


def parse_retry_exhausted(text: str) -> bool:
    """True if the most recent network-retry banner shows N == total (e.g.
    `attempt 10/10`) and the banner sits near the tail of the buffer.

    A retry banner buried far up in scrollback (e.g. from a previous outage
    the user already cleared manually) is ignored.
    """
    matches = list(RETRY_RE.finditer(text))
    if not matches:
        return False
    m = matches[-1]
    if len(text) - m.end() > MAX_POST_MATCH_TAIL:
        return False
    n, total = int(m.group(1)), int(m.group(2))
    return n >= total > 0


def parse_econnreset_stuck(text: str) -> bool:
    """True if `API Error: Unable to connect to API (ECONNRESET)` appears
    near the tail of the buffer — i.e. Claude is currently stuck on a
    network error without the usual retry banner. Tail-anchored just like
    `parse_retry_exhausted`, so stale ECONNRESET messages from a previous
    outage don't retrigger.
    """
    matches = list(ECONNRESET_RE.finditer(text))
    if not matches:
        return False
    m = matches[-1]
    if len(text) - m.end() > MAX_POST_MATCH_TAIL:
        return False
    return True


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
          f"buffer={args.buffer}s, retry_interval={args.retry_interval}s, "
          f"dry_run={args.dry_run}, match={args.match!r}", flush=True)

    retry_interval = timedelta(seconds=args.retry_interval)

    while True:
        try:
            tick(states, args, cooldown, buffer, retry_interval)
        except KeyboardInterrupt:
            print("\n[exit] interrupted by user.", flush=True)
            return
        except Exception as e:
            # Don't let a transient UIA hiccup take the watchdog down.
            print(f"[warn] tick error: {type(e).__name__}: {e}", flush=True)
        time.sleep(args.interval)


def tick(states: dict[int, WindowState], args,
         cooldown: timedelta, buffer: timedelta,
         retry_interval: timedelta) -> None:
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

        # 0. Network-retry exhaustion runs first and is independent of the
        # rate-limit pending/cooldown state. If the API is unreachable in
        # the middle of a 5h wait, we still want to keep re-sending
        # 'continue' to unstick Claude once the connection comes back.
        # Two flavors of "stuck on network" we treat identically:
        #   a) `Retrying in 0s · attempt 10/10` — retries exhausted
        #   b) `API Error: Unable to connect to API (ECONNRESET)` — bare error
        tail_for_retry = None
        text = read_terminal_text(w)
        if text:
            tail_for_retry = text[-SCAN_TAIL_CHARS:]
        stuck_reason = None
        if tail_for_retry:
            if parse_retry_exhausted(tail_for_retry):
                stuck_reason = "retry-exhausted"
            elif parse_econnreset_stuck(tail_for_retry):
                stuck_reason = "econnreset"
        if stuck_reason:
            if (st.retry_last_sent_utc is None
                    or now_utc - st.retry_last_sent_utc >= retry_interval):
                if not st.retry_logged:
                    print(f"[{stuck_reason}] {title!r}: network stuck; "
                          f"sending 'continue' (will resend every "
                          f"{int(retry_interval.total_seconds())}s "
                          f"until recovery)", flush=True)
                    st.retry_logged = True
                ok = send_continue(w, dry_run=args.dry_run)
                if ok:
                    st.retry_last_sent_utc = now_utc
            # Don't also run rate-limit logic this tick — network is dead.
            continue
        else:
            if st.retry_last_sent_utc is not None:
                print(f"[retry-recovered] {title!r}: network banner gone, "
                      f"clearing retry state", flush=True)
                st.retry_last_sent_utc = None
                st.retry_logged = False

        # 1. Detect latest limit message and update pending if it changed.
        # Skipped during cooldown so the still-visible old message doesn't
        # retrigger immediately after we just sent continue. Done *before*
        # the fire check so a new limit (different reset time) appearing
        # while we're already waiting on an older one correctly replaces
        # the pending target — otherwise we'd fire at the stale time and
        # miss the new one.
        in_cooldown = (st.last_sent_utc is not None
                       and now_utc - st.last_sent_utc < cooldown)
        if tail_for_retry and not in_cooldown:
            parsed = parse_limit_message(tail_for_retry)
            if parsed and parsed != st.pending_reset_key:
                # New limit message (different reset tuple). Replace pending.
                hour_12, minute, ampm, tz_name = parsed
                new_reset = next_reset_datetime(
                    hour_12, minute, ampm, tz_name
                )
                old = st.pending_reset_utc
                st.pending_reset_utc = new_reset
                st.pending_reset_key = parsed
                if old is None:
                    print(
                        f"[detect] {title!r}\n"
                        f"          limit hit; resets "
                        f"{hour_12}:{minute:02d}{ampm} ({tz_name})\n"
                        f"          will fire continue at "
                        f"{fmt_dt_local(new_reset + buffer)}",
                        flush=True,
                    )
                else:
                    print(
                        f"[update] {title!r}\n"
                        f"          reset shifted: "
                        f"{fmt_dt_local(old + buffer)} → "
                        f"{fmt_dt_local(new_reset + buffer)}",
                        flush=True,
                    )
                st.logged_detection = True
            elif parsed is None:
                # No current limit message. Don't clear pending — the message
                # may have just scrolled off — but reset the log flag so a
                # fresh detection on the next tick re-logs.
                st.logged_detection = False

        # 2. Fire pending if its time has come.
        if st.pending_reset_utc is not None:
            fire_at = st.pending_reset_utc + buffer
            if now_utc >= fire_at:
                print(f"[fire] {now_utc.astimezone():%H:%M:%S} → sending "
                      f"'continue' to {title!r}", flush=True)
                ok = send_continue(w, dry_run=args.dry_run)
                if ok:
                    st.last_sent_utc = now_utc
                    st.pending_reset_utc = None
                    st.pending_reset_key = None
                    st.logged_detection = False
                else:
                    # Couldn't send (no TermControl?). Push the next attempt
                    # one interval out so we don't spin.
                    st.pending_reset_utc = now_utc + timedelta(
                        seconds=args.interval
                    )

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
    p.add_argument("--retry-interval", type=int, default=30,
                   help="When the network-retry banner shows attempt N/N "
                        "(retries exhausted), resend 'continue' every N "
                        "seconds until Claude responds (default 30).")
    p.add_argument("--match", type=str, default="",
                   help="Only watch WT windows whose title contains this "
                        "substring (case-insensitive). Empty = all.")
    p.add_argument("--dry-run", action="store_true",
                   help="Detect and log, but don't actually send keystrokes.")
    return p.parse_args(argv)


if __name__ == "__main__":
    _force_utf8_console()
    run(parse_args())
