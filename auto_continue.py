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
APP_VERSION = "1.0.9"


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
#     You've hit your limit · resets Hpm (Asia/Shanghai)
#     /up·grade or /extra-usage to finish what you're working on.
#
#     You've hit your limit · resets H:MMpm (Asia/Shanghai)
#     /up·grade to increase your usage limit.
#
#     You've hit your session limit · resets Ham (Asia/Shanghai)
#     /up·grade to increase your usage limit.
#
# (The examples above are deliberately de-fanged — digits replaced with H/MM
# and a · inside "/upgrade" — so that viewing THIS file inside a watched
# terminal can never false-trigger the detector.)
#
# The leading "hit your ..." may say `limit`, `session limit`, `usage limit`,
# `weekly limit`, etc. The reset time may or may not carry minutes (`Hpm`
# vs `H:MMpm`). Requiring the /upgrade follow-up line is what tells us this
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
# Some builds print the long form instead:
#     Retrying in 8 seconds… (attempt 7/10)
# When attempt count hits N/N, Claude has exhausted its automatic retries
# and the user normally has to type 'continue' to resume after the network
# is back. We mimic that. Anchoring on the "Retrying in <n>s" prefix avoids
# matching unrelated occurrences of "attempt N/M" in normal output. The
# `[^\n]{0,30}?` gap keeps both parts on the same line while allowing any
# separator glyphs (`· `, `… (`, `- `).
RETRY_RE = re.compile(
    r"Retrying\s+in\s+\d+\s*s(?:ec(?:ond)?s?)?\b"
    r"[^\n]{0,30}?"
    r"attempt\s+(\d+)\s*/\s*(\d+)",
    re.IGNORECASE,
)

# Bare-error variant — sometimes Claude Code prints the API error directly
# without the retry banner (e.g. when a tool call's result can't be POSTed
# back and there's still a shell tool running):
#     API Error: Unable to connect to API (ECONN·RESET)
# The parenthesized code varies with the underlying failure — ECONN·RESET,
# ETIMED·OUT, ECONN·REFUSED, ENOT·FOUND, EAI_·AGAIN, EHOST·UNREACH, undici's
# UND_·ERR_* family — so we accept any parenthesized E*/UND_ERR_* code after
# the fixed sentence. Node's generic "API Error: fetch failed" is the same
# stuck-until-continue state and is accepted too. (Examples de-fanged with ·
# so viewing this file in a watched terminal can't false-trigger.)
# Treat all of these the same as retry-exhausted: keep poking 'continue'
# until the connection comes back. Requiring the leading "API Error:" prefix
# avoids matching prose / logs that merely mention an errno.
ECONNRESET_RE = re.compile(
    r"API\s+Error:\s*(?:"
    r"Unable\s+to\s+connect\s+to\s+API\s*"
    r"\(\s*(?:E[A-Z0-9_]{2,}|UND_ERR_[A-Z_]+)\s*\)"
    r"|fetch\s+failed"
    r")",
    re.IGNORECASE,
)

# Interactive limit picker (newer Claude Code builds). When the limit hits
# mid-turn, instead of printing the banner directly Claude Code first shows
# a modal choice:
#
#     What do you want to do?
#     > 1. Stop and wait for li·mit to reset
#       2. Upgrade your plan
#     Enter to confirm - Esc to cancel
#
# Option 1 is pre-selected; a bare Enter confirms it, after which the regular
# "You've hit your limit - resets ..." banner appears and the normal pending
# flow takes over. We detect the picker and press Enter for the user.
# (Example above de-fanged with a · inside "limit" for the same reason as the
# other patterns.)
LIMIT_PROMPT_RE = re.compile(
    r"What\s+do\s+you\s+want\s+to\s+do\?"
    r"[\s\S]{0,300}?"
    r"Stop\s+and\s+wait\s+for\s+limit\s+to\s+reset"
    r"[\s\S]{0,400}?"
    r"Enter\s+to\s+confirm",
    re.IGNORECASE,
)

# Dead-session state 'continue' can NOT fix — surfaced to the user instead
# of being poked at. Tail-anchored like the network patterns.
OAUTH_EXPIRED_RE = re.compile(
    r"OAuth\s+token\s+has\s+expired|Please\s+run\s+/login",
    re.IGNORECASE,
)

# How many trailing chars of the scrollback we scan each tick. Plenty for the
# message to appear, small enough that re.search stays cheap.
SCAN_TAIL_CHARS = 10000


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
    # Key of the last limit message we already fired for (or that the user
    # skipped). Prevents the same still-visible message from re-arming a
    # bogus "tomorrow at the same time" pending once the cooldown expires.
    # Cleared as soon as a tick sees no limit message (it scrolled away).
    fired_key: tuple | None = None
    # Logged-once flags so we don't spam the console.
    logged_detection: bool = False

    # Network-retry exhaustion (attempt N/N) state. Independent of the
    # rate-limit fields above — both can be active at the same time.
    retry_last_sent_utc: datetime | None = None
    retry_logged: bool = False

    # Interactive limit-picker ("What do you want to do?") bookkeeping.
    prompt_last_sent_utc: datetime | None = None
    prompt_logged: bool = False

    # OAuth-expired warn-once flag ('continue' can't fix that state).
    oauth_logged: bool = False


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


def init_uia_thread() -> None:
    """Initialize UI Automation on the CURRENT thread. Call once, first thing,
    on any non-main thread that will touch uiautomation.

    uiautomation builds a single global IUIAutomation COM client on the first
    thread that uses it, and that client only returns a *live* view of the
    desktop tree when its thread has a properly initialized (STA) COM
    apartment. The GUI runs the watcher on a Qt worker thread; without this
    call that thread gets an uninitialized/implicit apartment and keeps
    seeing the snapshot of windows that existed when it started — so a
    terminal opened *after* the watchdog launched is never detected.

    `InitializeUIAutomationInCurrentThread()` is `comtypes.CoInitializeEx()`
    (STA by default). Idempotent enough to call once per thread; errors are
    swallowed because the main-thread/CLI path is already COM-initialized by
    comtypes at import.
    """
    try:
        auto.InitializeUIAutomationInCurrentThread()
    except Exception:
        pass


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
    """Return the tail of the visible+scrollback text of the active tab's
    TermControl (SCAN_TAIL_CHARS characters — all any caller scans).

    Fast path: collapse the document range to its end, extend the start back
    SCAN_TAIL_CHARS characters, and fetch only that slice — instead of
    marshalling the ENTIRE scrollback (potentially megabytes with WT's
    10k-line history) over cross-process COM every tick for every window.
    Falls back to the full-document read if the provider rejects the range
    surgery. `waitTime=0` skips uiautomation's default 0.5s post-op sleep.
    """
    term = find_termcontrol(window_ctrl)
    if term is None:
        return None
    try:
        tp = term.GetTextPattern()
        if not tp:
            return None
        try:
            tr = tp.DocumentRange
            tr.MoveEndpointByRange(
                auto.TextPatternRangeEndpoint.Start, tr,
                auto.TextPatternRangeEndpoint.End, waitTime=0,
            )
            tr.MoveEndpointByUnit(
                auto.TextPatternRangeEndpoint.Start,
                auto.TextUnit.Character, -SCAN_TAIL_CHARS, waitTime=0,
            )
            text = tr.GetText(-1)
            if text:
                return text
        except Exception:
            pass
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
#
# 6000, same as the network allowance below and for the same reason (learned
# the hard way in d3b7239 for ECONNRESET): Claude Code pads every line to the
# full window width and stacks a tall footer below the message — input box,
# border rules, multi-line status bar, hints, often a `※ recap:` block. On a
# wide terminal that footer alone can run past 4000 chars, which would make a
# *current* limit message look "stale" and never get auto-continued.
MAX_POST_MATCH_TAIL = 6000

# Network-stuck messages (retry banner / bare ECONNRESET) use the same
# allowance. Must stay below SCAN_TAIL_CHARS so the match is actually within
# the scanned slice.
NETWORK_POST_MATCH_TAIL = 6000

# The interactive limit picker is a modal drawn at the very bottom of the
# screen — when it is OPEN, little besides padding and the "Enter to confirm"
# footer can follow it. A tight allowance keeps us from pressing Enter long
# after the user dismissed the picker themselves (a bare Enter would submit
# whatever they have typed in the input box).
PROMPT_POST_MATCH_TAIL = 1500


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
    if len(text) - m.end() > NETWORK_POST_MATCH_TAIL:
        return False
    n, total = int(m.group(1)), int(m.group(2))
    return n >= total > 0


def parse_econnreset_stuck(text: str) -> bool:
    """True if a bare `API Error: Unable to connect to API (E...)` (any
    errno / UND_ERR_* code) or `API Error: fetch failed` appears near the
    tail of the buffer — i.e. Claude is currently stuck on a network error
    without the usual retry banner. Tail-anchored just like
    `parse_retry_exhausted`, so stale errors from a previous outage don't
    retrigger.
    """
    matches = list(ECONNRESET_RE.finditer(text))
    if not matches:
        return False
    m = matches[-1]
    if len(text) - m.end() > NETWORK_POST_MATCH_TAIL:
        return False
    return True


def parse_limit_prompt(text: str) -> bool:
    """True if the interactive limit picker ("What do you want to do?" /
    "Stop and wait for limit to reset" / "Enter to confirm") is currently
    open at the tail of the buffer.

    Two guards against pressing Enter into a session that already moved on:
      * tight tail anchor (PROMPT_POST_MATCH_TAIL) — the picker is a modal
        drawn at the very bottom when open;
      * if the regular limit banner appears AFTER the picker text, the picker
        was already confirmed — don't press Enter again.
    """
    matches = list(LIMIT_PROMPT_RE.finditer(text))
    if not matches:
        return False
    m = matches[-1]
    if len(text) - m.end() > PROMPT_POST_MATCH_TAIL:
        return False
    banner = list(LIMIT_RE.finditer(text))
    if banner and banner[-1].start() >= m.end():
        return False
    return True


def parse_oauth_expired(text: str) -> bool:
    """True if the session is dead on an expired OAuth token near the tail.
    'continue' cannot fix this — callers should warn the user instead."""
    matches = list(OAUTH_EXPIRED_RE.finditer(text))
    if not matches:
        return False
    m = matches[-1]
    return len(text) - m.end() <= NETWORK_POST_MATCH_TAIL


# Timezone ABBREVIATIONS Anthropic sometimes prints instead of IANA names.
# Mapped to a representative IANA zone so DST is handled for the region
# (e.g. "EDT" seen in summer still computes a correct winter reset).
TZ_ABBREV = {
    "UTC": "UTC", "GMT": "Etc/GMT",
    "EST": "America/New_York", "EDT": "America/New_York",
    "CST": "America/Chicago", "CDT": "America/Chicago",
    "MST": "America/Denver", "MDT": "America/Denver",
    "PST": "America/Los_Angeles", "PDT": "America/Los_Angeles",
    "BST": "Europe/London",
    "CET": "Europe/Paris", "CEST": "Europe/Paris",
    "JST": "Asia/Tokyo", "KST": "Asia/Seoul", "IST": "Asia/Kolkata",
    "AEST": "Australia/Sydney", "AEDT": "Australia/Sydney",
}


def next_reset_datetime(hour_12: int, minute: int, ampm: str,
                        tz_name: str) -> datetime:
    """
    Compute the next wall-clock occurrence of `hour_12:minute am/pm` in
    `tz_name`, return it as an aware UTC datetime.

    DST-correct: the target is built as a NAIVE local datetime and then
    localized with `tz.localize()`, so a reset that lands on the other side
    of a DST transition gets that side's UTC offset. (The old
    `aware.replace(hour=...)` approach kept *today's* fixed offset — the
    classic pytz pitfall — and was an hour off across transitions.)
    """
    hour_24 = hour_12 % 12 + (12 if ampm == "pm" else 0)
    name = (tz_name or "").strip()
    try:
        tz = pytz.timezone(TZ_ABBREV.get(name.upper(), name))
    except pytz.UnknownTimeZoneError:
        # Unknown zone string. The reset time is rendered in the *user's*
        # local zone, so the machine's own zone is the best guess — far
        # better than a hardcoded region. Fixed current offset (no DST
        # lookahead), fine for a <24h horizon fallback.
        local_tz = datetime.now().astimezone().tzinfo
        now_local = datetime.now(local_tz)
        target = now_local.replace(
            hour=hour_24, minute=minute, second=0, microsecond=0
        )
        if target <= now_local:
            target += timedelta(days=1)
        return target.astimezone(pytz.UTC)

    now_naive = datetime.now(tz).replace(tzinfo=None)
    target_naive = now_naive.replace(
        hour=hour_24, minute=minute, second=0, microsecond=0
    )
    if target_naive <= now_naive:
        target_naive += timedelta(days=1)
    try:
        target_local = tz.localize(target_naive)
    except Exception:
        # Non-existent local time (spring-forward gap): shift an hour.
        target_local = tz.localize(target_naive + timedelta(hours=1))
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

    try:
        target_hwnd = int(window_ctrl.NativeWindowHandle or 0)
    except Exception:
        target_hwnd = 0

    saved_fg = _get_foreground_hwnd()
    try:
        # Two attempts to bring the target to the foreground, then VERIFY it
        # actually got there. SendKeys types into whatever has focus — if
        # SetForegroundWindow silently failed (Windows denies it while the
        # user is actively typing elsewhere), the old code injected
        # 'continue' into the user's editor/browser. Refusing to type and
        # returning False is strictly better: callers retry next tick.
        for _ in range(2):
            try:
                window_ctrl.SetActive()
            except Exception:
                pass
            try:
                term.SetFocus()
            except Exception:
                pass
            # Tiny pause so the focus change actually lands.
            time.sleep(0.25)
            if not target_hwnd or _get_foreground_hwnd() == target_hwnd:
                break
        if target_hwnd and _get_foreground_hwnd() != target_hwnd:
            return False
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
        if saved_fg and saved_fg != target_hwnd:
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

        try:
            title = w.Name or f"<hwnd {hwnd}>"
        except Exception:
            # A UIA COMError reading the title must not abort the whole
            # tick (it would blind the watchdog to every later window).
            title = f"<hwnd {hwnd}>"
        if args.match and args.match.lower() not in title.lower():
            continue

        st = states.setdefault(hwnd, WindowState(title=title))
        st.title = title  # title can change as user switches tabs

        # Read the scrollback tail once; every detector shares this view.
        text = read_terminal_text(w)
        tail = text[-SCAN_TAIL_CHARS:] if text else ""

        # -1. Dead-session states 'continue' can't fix: warn once.
        if tail and parse_oauth_expired(tail):
            if not st.oauth_logged:
                print(f"[oauth] {title!r}: OAuth token expired — "
                      f"auto-continue can't fix this; run /login there",
                      flush=True)
                st.oauth_logged = True
        else:
            st.oauth_logged = False

        # 0. Interactive limit picker ("What do you want to do?"). Newer
        # Claude Code builds show this modal INSTEAD of the limit banner;
        # option 1 ("Stop and wait for limit to reset") is pre-selected, so
        # a bare Enter confirms it and makes the regular banner (with the
        # reset time) appear — which the normal flow below then picks up.
        # Does NOT touch last_sent_utc: the banner that appears right after
        # must be detected immediately, not swallowed by the cooldown.
        if tail and parse_limit_prompt(tail):
            if (st.prompt_last_sent_utc is None
                    or now_utc - st.prompt_last_sent_utc >= retry_interval):
                if not st.prompt_logged:
                    print(f"[limit-prompt] {title!r}: limit picker open; "
                          f"pressing Enter to confirm "
                          f"'Stop and wait for limit to reset'", flush=True)
                    st.prompt_logged = True
                ok = send_text_lines(w, [""], dry_run=args.dry_run)
                if ok:
                    st.prompt_last_sent_utc = now_utc
            # Modal open — nothing else can be current this tick.
            continue
        elif st.prompt_logged or st.prompt_last_sent_utc is not None:
            st.prompt_logged = False
            st.prompt_last_sent_utc = None

        # 0.5. Network-retry exhaustion runs next and is independent of the
        # rate-limit pending/cooldown state. If the API is unreachable in
        # the middle of a 5h wait, we still want to keep re-sending
        # 'continue' to unstick Claude once the connection comes back.
        # Two flavors of "stuck on network" we treat identically:
        #   a) retry banner at attempt N/N — retries exhausted
        #   b) bare `API Error: ... (E...)` / `fetch failed` — no banner
        stuck_reason = None
        if tail:
            if parse_retry_exhausted(tail):
                stuck_reason = "retry-exhausted"
            elif parse_econnreset_stuck(tail):
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
                    # If a rate-limit pending elapsed during the outage,
                    # this 'continue' doubles as its fire — otherwise we'd
                    # send a second, redundant continue right after
                    # recovery.
                    if (st.pending_reset_utc is not None
                            and now_utc >= st.pending_reset_utc + buffer):
                        st.fired_key = st.pending_reset_key
                        st.pending_reset_utc = None
                        st.pending_reset_key = None
                        st.last_sent_utc = now_utc
                        st.logged_detection = False
            # Don't also run rate-limit logic this tick — network is dead.
            continue
        else:
            if st.retry_last_sent_utc is not None or st.retry_logged:
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
        # miss the new one. `fired_key` blocks the OTHER stale case: a
        # message we already fired for, still visible after the cooldown,
        # must not re-arm a bogus "tomorrow at the same time" pending.
        in_cooldown = (st.last_sent_utc is not None
                       and now_utc - st.last_sent_utc < cooldown)
        if tail and not in_cooldown:
            parsed = parse_limit_message(tail)
            if (parsed and parsed != st.pending_reset_key
                    and parsed != st.fired_key):
                # New limit message (different reset tuple). Replace pending.
                hour_12, minute, ampm, tz_name = parsed
                new_reset = None
                try:
                    new_reset = next_reset_datetime(
                        hour_12, minute, ampm, tz_name
                    )
                except Exception as e:
                    print(f"[warn] reset calc failed for {title!r}: "
                          f"{type(e).__name__}: {e}", flush=True)
                if new_reset is not None:
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
                # fresh detection on the next tick re-logs, and forget the
                # fired key (the handled message is gone; any future match
                # is genuinely new).
                st.logged_detection = False
                st.fired_key = None

        # 2. Fire pending if its time has come.
        if st.pending_reset_utc is not None:
            fire_at = st.pending_reset_utc + buffer
            if now_utc >= fire_at:
                # Re-verify the message is still current — if the user
                # already continued manually, the session has moved on and
                # injecting a redundant 'continue' would start an unwanted
                # turn. Same staleness window as detection (6000 chars
                # covers a wide-terminal footer; a genuinely blocked session
                # prints nothing else below the banner).
                if tail and parse_limit_message(tail) is None:
                    print(f"[skip] {title!r}: limit message gone before "
                          f"fire; assuming handled manually", flush=True)
                    st.fired_key = st.pending_reset_key
                    st.pending_reset_utc = None
                    st.pending_reset_key = None
                    st.logged_detection = False
                    continue
                print(f"[fire] {now_utc.astimezone():%H:%M:%S} → sending "
                      f"'continue' to {title!r}", flush=True)
                ok = send_continue(w, dry_run=args.dry_run)
                if ok:
                    st.last_sent_utc = now_utc
                    st.fired_key = st.pending_reset_key
                    st.pending_reset_utc = None
                    st.pending_reset_key = None
                    st.logged_detection = False
                else:
                    # Couldn't send (no TermControl / foreground denied).
                    # Retry on the next tick (mirrors the GUI path).
                    st.pending_reset_utc = now_utc

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
