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
#
# BUMP THIS ONLY IN THE RELEASE COMMIT — never at the start of a dev cycle.
# The updater compares version strings alone, so a locally-built dev exe that
# already carries the upcoming number is a trap: `is_newer("1.0.16", "1.0.16")`
# is False, so that build reports "up to date" forever and NEVER updates to the
# real release. It happened — a dev exe built 2026-07-25 claimed v1.0.16 a day
# before v1.0.16 shipped, and would have silently kept running unreviewed code.
# A "-dev" suffix does not help: parse_version() strips it, so 1.0.17-dev and
# 1.0.17 compare equal. Leave this at the LAST RELEASED version while
# developing; release.yml refuses to publish if it disagrees with the tag.
APP_VERSION = "2.0.1"


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
#     You've hit your session limit · resets H:MMam (Asia/Shanghai)
#     /up·grade or /usage-credits to finish what you're working on.
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
#
# The follow-up wording is a moving target — Anthropic has shipped
# `/extra-usage`, then renamed it `/usage-credits`, with the tail either
# "to finish what you're working on" or "to increase your usage limit". So
# after "/upgrade" we accept ANY of the known continuations (either slash
# command, or either stable tail phrase). The separator glyph between "limit"
# and "resets" also varies (middle dot ·, bullet operator ∙, dot operator ⋅,
# en/em dash), so the class enumerates all of them.
LIMIT_RE = re.compile(
    r"You['’]ve hit your (?:\w+\s+)?limit\s*[·•‧․∙⋅⸱\-–—]?\s*"
    r"resets\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)\s*\(([^)]+)\)"
    r"[\s\S]{0,400}?"
    r"/upgrade\b[^\n]{0,80}?"
    r"(?:/extra[-\s]?usage|/usage[-\s]?credits"
    r"|increase\s+your\s+usage\s+limit|finish\s+what\s+you)",
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
# the fixed sentence. Node's generic "API Error: fetch fai·led" is the same
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

# Server-side truncation: a response is cut off partway through streaming,
# so the turn ends with a partial answer that 'continue' can resume. Two
# leading wordings seen so far — the 500-class "Server-error" one, and a
# later "Response-stalled" one — both closing with the same "…may be
# incomplete" footer sentence (plus a `✻ Sautéed for 1m 6s`-style completion
# marker). Unlike ECONNRESET this is NOT a connectivity failure: the request
# reached Anthropic, but the server erred / stalled partway through streaming,
# so the turn stops early. Typing 'continue' makes Claude resume from where it
# cut off. These arrive in bursts during overload, so we treat them like the
# other network-stuck states — keep poking 'continue' every retry_interval
# until a turn completes cleanly (the tail anchor clears the marker once a
# full response scrolls it out of range).
# The regex matches either known leading phrase OR — as a forward-compat net
# for future re-wordings — any `API Error` line ending in that shared footer.
# It uses \s+ / . (and the prose above avoids the verbatim strings) so this
# source can't self-trigger in a watched terminal.
SERVER_ERROR_RE = re.compile(
    r"API\s+Error:\s*(?:"
    r"Server\s+error\s+mid.response"        # 500-class truncation
    r"|Response\s+stalled\s+mid.stream"     # later stream-stall wording
    r"|[\s\S]{0,80}?The\s+response\s+above\s+may\s+be\s+incomplete"
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

    # Multi-tab warn-once flag (background tabs can't be watched).
    tabs_warned: bool = False


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


def list_tab_titles(window_ctrl) -> list:
    """Titles of ALL tabs in a WT window, read from its TabView's
    TabItemControls (shallow — the tab strip sits at depth 3, well above
    the terminal content subtree).

    Windows Terminal only exposes the ACTIVE tab's TermControl in the UIA
    tree — background tabs' content physically cannot be read. Callers use
    this list to detect (and warn about) sessions the watchdog can't see.
    Returns [] on any failure or when the structure doesn't match.
    """
    def _find_tabview(c, depth):
        if depth > 4:
            return None
        try:
            if (c.ClassName or "") == "Microsoft.UI.Xaml.Controls.TabView":
                return c
        except Exception:
            return None
        try:
            for ch in c.GetChildren():
                r = _find_tabview(ch, depth + 1)
                if r is not None:
                    return r
        except Exception:
            pass
        return None

    out = []
    try:
        tv = _find_tabview(window_ctrl, 0)
        if tv is None:
            return out
        for ch in tv.GetChildren():
            try:
                for item in ch.GetChildren():
                    if item.ControlTypeName == "TabItemControl":
                        out.append(item.Name or "")
            except Exception:
                pass
    except Exception:
        pass
    return out


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

# The "Switch model?" dialog needs a LOOSER allowance than the limit picker.
# Both are modals, but this one is detected in order to press Enter *on it*
# during a Fable recovery, and Claude Code stacks its full footer below the
# modal — input box, border rules, multi-line status bar, hints — every line
# padded to the terminal width, so the 1500 used for the limit picker is far
# too tight here — an OPEN dialog would look stale and stall the recovery.
#
# 4000 rather than the 6000 network allowance, because the two failure modes
# are NOT equally bad:
#   too small → the dialog isn't seen, <esc> fires and cancels it, <confirm>
#     times out, and the turn resumes on the old model. Wasteful, but it stays
#     inside the recovery and FABLE_MAX_RUNS stops it repeating.
#   too large → a dialog the USER dismissed a while ago still matches, so
#     <confirm> presses Enter into their input box and submits whatever they
#     had half-typed as a prompt. That has side effects in their session and
#     cannot be undone.
# Prefer the recoverable failure. ~20 padded rows at 200 columns clears a
# normal footer; FABLE_CONFIRM_MAX_S bounds the stall if it ever doesn't.
SWITCH_POST_MATCH_TAIL = 4000


def parse_limit_message(
    text: str, pattern=None
) -> tuple[int, int, str, str] | None:
    """Return (hour_12, minute, ampm, tz_name) from the *latest* limit line.

    Returns None if no match. The match must be near the end of the buffer —
    a stale limit message further back means Claude has already moved on.
    `minute` is 0 when the message omits minutes (e.g. `resets 11pm`).

    `pattern` may be a user-supplied compiled regex from the Advanced dialog
    (see TRIGGER_SPECS); it MUST expose the same four groups as LIMIT_RE
    (hour, minute, am/pm, tz) — `compile_trigger_patterns` enforces that
    before the override ever reaches here.
    """
    rx = pattern or LIMIT_RE
    matches = list(rx.finditer(text))
    if not matches:
        return None
    m = matches[-1]
    if len(text) - m.end() > MAX_POST_MATCH_TAIL:
        return None
    try:
        hour = int(m.group(1))
        minute = int(m.group(2)) if m.group(2) else 0
        ampm = (m.group(3) or "").lower()
        tz = (m.group(4) or "").strip()
    except (IndexError, ValueError):
        # A user override that compiled but yields non-numeric / missing
        # groups must not kill the tick — treat it as "no limit seen".
        return None
    return hour, minute, ampm, tz


# Claude Code's live spinner, e.g. "✽ Swirling… (2m 0s · ↓ 5.0k tokens)".
# The verb is randomised and the suffix varies, so anchor on the part that
# doesn't move: an elapsed clock followed by a middot, inside parentheses.
# The status bar's own parenthesised times ("(4h 37m / 5h)") use a slash, not
# a middot, and the finished-turn line ("Brewed for 3m 2s · …") has no
# parentheses — neither can match. Second alternative is the older wording,
# split so this source file cannot suppress a real banner when someone reads
# it inside a watched terminal.
RUNNING_RE = re.compile(
    r"\((?:\s*\d+\s*[hms])+\s*·" r"|esc" r"\s+to\s+interrupt", re.I)


RUNNING_TAIL_CHARS = 2000


def session_running(text: str) -> bool:
    """True if Claude Code is streaming a turn RIGHT NOW.

    The spinner is redrawn just above the input box, so only the very bottom
    of the buffer says anything about the present; a spinner further up is
    scrollback from a turn that has already finished.
    """
    return RUNNING_RE.search(str(text or "")[-RUNNING_TAIL_CHARS:]) is not None


def session_running_after(text: str, pos: int) -> bool:
    """True if the session is streaming a turn *after* offset `pos`.

    An error banner only means "stuck" while nothing has run since. Once
    Claude Code is streaming again the banner is just scrollback, and resending
    'continue' does not unstick anything — it queues another prompt that lands
    when the turn ends. An 80-minute outage put eight of them into one session
    that way.
    """
    return RUNNING_RE.search(text, pos) is not None


# Claude Code stamps every safeguard block with its own request id, a few
# lines under the notice. It is the only exact way to tell one block from the
# next: the TUI redraws at a fixed layout, so a fresh block lands at the same
# distance from the tail as the one it replaced, and a purely positional test
# cannot see it. Measured live — a block 2s after a recovery finished went
# unnoticed for the rest of the session because both sat 1924 chars up.
REQUEST_ID_RE = re.compile(r"Request\s+ID:\s*(\S+)", re.I)


def fable_refusal_id(text: str, pattern=None):
    """Request id belonging to the most recent safeguard notice, or None.

    Only ids that appear AFTER the notice count; one further up belongs to an
    older block. Returns None when the notice has no id in view yet, which is
    normal for a few hundred milliseconds while the block is still printing —
    callers must treat None as "no new information", never as "new block".
    """
    rx = pattern or FABLE_REFUSAL_RE
    matches = list(rx.finditer(text))
    if not matches:
        return None
    m = REQUEST_ID_RE.search(text, matches[-1].end())
    return m.group(1) if m else None


def parse_retry_exhausted(text: str, pattern=None) -> bool:
    """True if the most recent network-retry banner shows N == total (e.g.
    `attempt 10/10`) and the banner sits near the tail of the buffer.

    A retry banner buried far up in scrollback (e.g. from a previous outage
    the user already cleared manually) is ignored.

    `pattern` may be a user-supplied compiled regex; it MUST expose the two
    numeric groups (attempt, total) that RETRY_RE does.
    """
    rx = pattern or RETRY_RE
    matches = list(rx.finditer(text))
    if not matches:
        return False
    m = matches[-1]
    if len(text) - m.end() > NETWORK_POST_MATCH_TAIL:
        return False
    if session_running_after(text, m.end()):
        return False
    try:
        n, total = int(m.group(1)), int(m.group(2))
    except (IndexError, ValueError, TypeError):
        return False
    return n >= total > 0


def parse_econnreset_stuck(text: str, pattern=None) -> bool:
    """True if a bare `API Error: Unable to connect to API (E...)` (any
    errno / UND_ERR_* code) or the generic fetch-fai·led one appears near the
    tail of the buffer — i.e. Claude is currently stuck on a network error
    without the usual retry banner. Tail-anchored just like
    `parse_retry_exhausted`, so stale errors from a previous outage don't
    retrigger.
    """
    rx = pattern or ECONNRESET_RE
    matches = list(rx.finditer(text))
    if not matches:
        return False
    m = matches[-1]
    if len(text) - m.end() > NETWORK_POST_MATCH_TAIL:
        return False
    if session_running_after(text, m.end()):
        return False
    return True


def parse_server_error_stuck(text: str, pattern=None) -> bool:
    """True if a server-side truncation marker sits near the tail of the
    buffer — the response was cut off mid-stream (the 500 "Server-error"
    wording or the later "Response-stalled" one, both closing with the shared
    "…may be incomplete" footer), so Claude's last turn ended early and needs
    a 'continue' to resume. Tail-anchored exactly like `parse_econnreset_stuck`
    so a stale marker from an earlier, already-resolved outage doesn't
    retrigger.
    """
    rx = pattern or SERVER_ERROR_RE
    matches = list(rx.finditer(text))
    if not matches:
        return False
    m = matches[-1]
    if len(text) - m.end() > NETWORK_POST_MATCH_TAIL:
        return False
    if session_running_after(text, m.end()):
        return False
    return True


# Fable refusal-recovery (opt-in GUI feature). When Fable's safety guardrails
# block a turn, Claude Code stalls here (CC auto-switch off) with a safeguard
# notice that names Fable and says it can't respond with that model. The GUI's
# Advanced feature detects this per opted-in window and drives a recovery
# (switch to a fallback model, continue, then switch back to Fable after a
# delay). The default pattern is user-overridable in the Advanced dialog; its
# `\s+` usage also keeps this source from self-matching in a watched terminal.
# Model-agnostic on purpose. The phrase is what identifies a safeguard block;
# the model in front of it is whatever is current, and that changes — Opus 4.8
# retired and Opus 5 shipped inside this project's lifetime, so naming one
# model here means the next one is simply not detected. `\w+` covers any
# family and version ("Fable 5's", "Opus 6's", something not invented yet).
# The `\s+` runs also keep this source from matching itself in a watched
# terminal, which is why they must survive any edit here.
DEFAULT_FABLE_PATTERN = (
    r"\b\w[\w.]{1,20}\s*[\d.]*\s*[’']?s?\s+safeguards\s+flagged"
    r"|can[’']?t\s+respond\s+to\s+this\s+request\s+with\s+\w[\w.]{1,20}"
)
FABLE_REFUSAL_RE = re.compile(DEFAULT_FABLE_PATTERN, re.IGNORECASE)


# Claude Code's status bar names the active model in square brackets near the
# very bottom of the viewport. Only used to REPORT what a recovery ended on —
# never to drive keystrokes — so a missed read costs a log line, nothing more.
# Written with \s so this source can't match its own pattern in a watched
# terminal, and the family list is open-ended enough to survive renames.
# Matched by STRUCTURE, not by a list of model names. The status line renders
# the active model in brackets followed by a context bar and a percentage, and
# that shape is stable while the names are not: Opus 4.8 was retired and Opus 5
# arrived within this project's lifetime. Enumerating families means the next
# rename silently turns the drift check into a no-op — the same wording drift
# that has broken detection here before. Bracketed tokens that are NOT the
# status line (build tags like the ones a statusline plugin prints) lack the
# bar-and-percentage tail and are ignored. Bar glyphs are written as escapes so
# this source cannot match its own pattern in a watched terminal.
MODEL_BAR_RE = re.compile(
    r"\[\s*([^\]\n]{1,32}?)\s*\]\s+[█▓▒░]{2,}\s*\d{1,3}\s*%"
)

# Fallback for a customised status line with no bar: the known families. Kept
# only as a second chance — the structural match above is the primary path.
MODEL_FAMILY_RE = re.compile(
    r"\[\s*((?:Fable|Opus|Sonnet|Haiku|Mythos)[^\]\n]{0,24}?)\s*\]",
    re.IGNORECASE,
)


def current_model(text: str):
    """Model label shown in the status bar, or None if it isn't visible.

    Returns what the bar actually says (e.g. the family plus its version), so
    a log line can name it exactly. Takes the LAST occurrence: the bar is
    redrawn at the bottom, so the most recent one is the live state.
    """
    matches = MODEL_BAR_RE.findall(text or "")
    if matches:
        return matches[-1]
    matches = MODEL_FAMILY_RE.findall(text or "")
    return matches[-1] if matches else None


def fable_refusal_distance(text: str, pattern=None):
    """Chars between the END of the latest safeguard notice and the end of the
    buffer, or None if there is no in-range notice.

    Callers use this to tell a NEW block from the one they already handled.
    The notice lingers in scrollback long after a recovery finishes (measured:
    tens of minutes), so "is a notice visible" cannot answer that question. The
    distance can only GROW as the session prints more, so a match that is
    suddenly CLOSER to the tail is a fresh notice — which matters because the
    recovery ends by retrying the very message that was flagged, making an
    immediate re-block the likeliest next event.
    """
    rx = pattern or FABLE_REFUSAL_RE
    matches = list(rx.finditer(text))
    if not matches:
        return None
    dist = len(text) - matches[-1].end()
    return dist if dist <= NETWORK_POST_MATCH_TAIL else None


def parse_fable_refusal(text: str, pattern=None) -> bool:
    """True if a Fable safety-guardrail block sits near the tail of the buffer.
    `pattern` may be a user-compiled re.Pattern from the Advanced dialog;
    defaults to FABLE_REFUSAL_RE. Tail-anchored like the other network parsers
    so a stale notice further up scrollback doesn't retrigger."""
    return fable_refusal_distance(text, pattern) is not None


# The "Switch model?" confirmation dialog Claude Code shows when switching to a
# model whose cache differs (e.g. `/model fable` from a cached Opus). Option 1
# (the affirmative "Yes, switch…" line) is pre-selected, so a plain Enter
# confirms it. We anchor on the affirmative + negative option PAIR — not the
# far-above "Switch model?" header, which the padded multi-line description
# pushes ~900 chars away on a real (full-width) terminal. The recovery WAITS
# for this and confirms it BEFORE sending 'continue' — a blind Enter fired
# before the dialog renders lands in the input box and the continue then runs
# on the old model.
# NOTE: the two option strings are deliberately NOT written out together
# anywhere in this file. This source is routinely displayed inside a watched
# terminal, and a verbatim pair here would make the tool detect a dialog that
# isn't there and press Enter into whatever the user was typing — the same
# de-fanging convention the limit / network patterns above follow.
SWITCH_MODEL_RE = re.compile(
    r"Yes,\s+switch\s+to[\s\S]{0,300}?No,\s+go\s+back",
    re.IGNORECASE,
)


# The picker Claude Code shows WITH the safeguard notice ("Session paused"):
# a numbered pair whose first option offers to switch to a fallback model and
# whose second offers to edit the prompt and retry on the blocked one. The two
# option strings are deliberately NOT reproduced verbatim here — writing them
# out on adjacent lines makes THIS FILE match the pattern below, so merely
# viewing the source in a watched terminal would look like an open picker and
# earn a blind Enter. (Same de-fanging convention as the patterns above; the
# test file splits the literals with string concatenation for this reason.)
#
# Option 1 is pre-selected, so a bare Enter accepts the model switch — the
# session recovers without typing /model at all. This is the real recovery
# path; the /model + "Switch model?" dance is only needed to go BACK to the
# blocked model afterwards.
# Anchored on the option PAIR, and deliberately not naming either model: the
# fallback model name changes with every release. Written with \s+ so this
# source can't self-match in a watched terminal.
FABLE_PICKER_RE = re.compile(
    r"Switch\s+to\s+[\s\S]{0,120}?Edit\s+prompt\s+and\s+retry",
    re.IGNORECASE,
)


def parse_fable_picker(text: str, pattern=None) -> bool:
    """True if the safeguard picker is open at the tail of the buffer, i.e. a
    bare Enter accepts the pre-selected "Switch to <fallback>" option.
    Tail-anchored like the other modals so a stale one further up scrollback
    can't make us press Enter into the input box."""
    rx = pattern or FABLE_PICKER_RE
    matches = list(rx.finditer(text))
    if not matches:
        return False
    m = matches[-1]
    if len(text) - m.end() > SWITCH_POST_MATCH_TAIL:
        return False
    return True


def parse_switch_model_prompt(text: str, pattern=None) -> bool:
    """True if the 'Switch model?' Yes/No dialog is open at the tail of the
    buffer (a plain Enter confirms the pre-selected 'Yes, switch to …').
    Tail-anchored so a stale one further up scrollback doesn't retrigger."""
    rx = pattern or SWITCH_MODEL_RE
    matches = list(rx.finditer(text))
    if not matches:
        return False
    m = matches[-1]
    if len(text) - m.end() > SWITCH_POST_MATCH_TAIL:
        return False
    return True


def parse_limit_prompt(text: str, pattern=None, limit_pattern=None) -> bool:
    """True if the interactive limit picker is currently open at the tail of
    the buffer — the "What do you want to do?" modal whose first option waits
    for the li·mit to reset. (Spelled with a · here for the same reason as the
    pattern above: the verbatim phrases would make THIS file read as an open
    picker in a watched terminal, and this detector presses Enter.)

    Two guards against pressing Enter into a session that already moved on:
      * tight tail anchor (PROMPT_POST_MATCH_TAIL) — the picker is a modal
        drawn at the very bottom when open;
      * if the regular limit banner appears AFTER the picker text, the picker
        was already confirmed — don't press Enter again.

    `limit_pattern` overrides the banner regex used by that second guard, so
    a user who customises the limit banner keeps the guard working.
    """
    rx = pattern or LIMIT_PROMPT_RE
    matches = list(rx.finditer(text))
    if not matches:
        return False
    m = matches[-1]
    if len(text) - m.end() > PROMPT_POST_MATCH_TAIL:
        return False
    banner = list((limit_pattern or LIMIT_RE).finditer(text))
    if banner and banner[-1].start() >= m.end():
        return False
    return True


def parse_oauth_expired(text: str, pattern=None) -> bool:
    """True if the session is dead on an expired OAuth token near the tail.
    'continue' cannot fix this — callers should warn the user instead."""
    rx = pattern or OAUTH_EXPIRED_RE
    matches = list(rx.finditer(text))
    if not matches:
        return False
    m = matches[-1]
    return len(text) - m.end() <= NETWORK_POST_MATCH_TAIL


# ---------------------------------------------------------------------------
# User-editable trigger patterns
# ---------------------------------------------------------------------------
#
# Anthropic re-words these banners without warning (see the /extra-usage →
# /usage-credits rename, and the separator-glyph churn in LIMIT_RE), and each
# rename silently stops auto-continue from firing until the tool ships a new
# build. Exposing the patterns in the GUI's Advanced dialog lets a user fix a
# wording change themselves the same day.
#
# `groups` is the number of capture groups the parser INDEXES INTO. An
# override with fewer groups would raise mid-tick, so compile_trigger_patterns
# rejects it up front and the built-in default stays in force. Defaults are
# read back off the compiled objects (`.pattern`) so there is exactly one
# source of truth for each regex.
TRIGGER_SPECS = [
    {
        "key": "limit",
        "label": "Rate-limit banner",
        "default": LIMIT_RE.pattern,
        "groups": 4,
        "help": "The “you’ve hit your limit · resets …” line. "
                "Must keep 4 capture groups in order: hour, minute, am/pm, "
                "timezone — they schedule the resume.",
    },
    {
        "key": "limit_prompt",
        "label": "Limit picker (modal)",
        "default": LIMIT_PROMPT_RE.pattern,
        "groups": 0,
        "help": "The interactive “What do you want to do?” chooser. "
                "Matching it presses Enter to accept “Stop and wait”.",
    },
    {
        "key": "retry",
        "label": "Network retry exhausted",
        "default": RETRY_RE.pattern,
        "groups": 2,
        "help": "The “Retrying in Ns · attempt N/M” banner. Must keep "
                "2 numeric capture groups: attempt and total.",
    },
    {
        "key": "econnreset",
        "label": "Connection error",
        "default": ECONNRESET_RE.pattern,
        "groups": 0,
        "help": "Bare API connection failures (errno / fetch failed) with no "
                "retry banner.",
    },
    {
        "key": "server_error",
        "label": "Truncated / stalled response",
        "default": SERVER_ERROR_RE.pattern,
        "groups": 0,
        "help": "Server-side mid-stream truncation — the turn ended early "
                "and ‘continue’ resumes it.",
    },
    {
        "key": "oauth",
        "label": "Dead session (login needed)",
        "default": OAUTH_EXPIRED_RE.pattern,
        "groups": 0,
        "help": "Expired OAuth token. Matching it only WARNS — no keys are "
                "ever sent, since ‘continue’ cannot fix it.",
    },
    {
        "key": "fable",
        "label": "Fable safeguard block",
        "default": DEFAULT_FABLE_PATTERN,
        "groups": 0,
        "help": "Starts the Fable refusal-recovery (only when that feature is "
                "enabled above).",
    },
    {
        "key": "fable_picker",
        "label": "Safeguard picker",
        "default": FABLE_PICKER_RE.pattern,
        "groups": 0,
        # Help text avoids quoting both option strings together — see the
        # de-fanging note above FABLE_PICKER_RE.
        "help": "The two-option chooser shown with the safeguard notice "
                "(“Session paused”). Option 1 offers the fallback model and "
                "is pre-selected, so Enter alone performs the whole "
                "recovery — no /model needed.",
    },
    {
        "key": "switch_model",
        "label": "“Switch model?” dialog",
        "default": SWITCH_MODEL_RE.pattern,
        "groups": 0,
        "help": "The Yes/No confirmation shown when changing model with "
                "/model. The recovery waits for this before pressing Enter.",
    },
]

TRIGGER_DEFAULTS = {s["key"]: s["default"] for s in TRIGGER_SPECS}


def compile_trigger_patterns(overrides) -> tuple[dict, list]:
    """Compile user pattern overrides into {key: re.Pattern}.

    Returns (patterns, errors). Only keys that differ from the built-in
    default AND validate appear in `patterns`; everything else is omitted so
    callers fall back to the module-level default by passing None. `errors` is
    a list of human-readable strings for the GUI to surface — a bad override
    is REJECTED (default stays in force) rather than silently disabling a
    trigger, because a trigger that never fires is the failure mode this
    feature exists to prevent.
    """
    patterns, errors = {}, []
    specs = {s["key"]: s for s in TRIGGER_SPECS}
    for key, raw in (overrides or {}).items():
        spec = specs.get(key)
        if spec is None:
            continue
        src = str(raw or "").strip()
        if not src or src == spec["default"]:
            continue                      # unchanged → use the built-in
        try:
            rx = re.compile(src, re.IGNORECASE)
        except re.error as e:
            errors.append(f"{spec['label']}: invalid regex ({e}); "
                          f"keeping the built-in pattern")
            continue
        if rx.groups < spec["groups"]:
            errors.append(
                f"{spec['label']}: needs {spec['groups']} capture group(s) "
                f"but has {rx.groups}; keeping the built-in pattern")
            continue
        if rx.search("") is not None:
            # A pattern that can match nothing (".*", "x*", "foo|") matches at
            # every position, including a zero-width one at the very end — so
            # the tail anchor always passes and EVERY window looks stuck.
            # Left in, it would type 'continue' into every Claude session on
            # a loop, so reject it rather than let it through.
            errors.append(
                f"{spec['label']}: matches empty text, so it would fire on "
                f"every window; keeping the built-in pattern")
            continue
        patterns[key] = rx
    return patterns, errors


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
            auto.SendKeys(sendkeys_literal(line) + "{Enter}", interval=0.02)
        return True
    finally:
        time.sleep(0.15)
        if saved_fg and saved_fg != target_hwnd:
            _set_foreground_hwnd(saved_fg)


def sendkeys_literal(text) -> str:
    """Escape text so uiautomation.SendKeys types it VERBATIM.

    SendKeys reads `{...}` as key syntax, so an unescaped brace doesn't just
    type the wrong thing — it RAISES (`{` → ValueError, `{task}` → TypeError).
    That exception used to escape send_text_lines and abort the whole tick,
    which meant every window enumerated after the offending one silently
    stopped being watched. Recovery steps are free text typed by the user in
    the Advanced dialog, so braces are entirely plausible ("continue with the
    {plan}"). Only braces are special here — %, ^, + and parentheses are
    literal to SendKeys unless they follow a modifier key spec.
    """
    # Single pass on purpose: chained str.replace would re-escape the braces
    # the first replacement just emitted ("{" → "{{}" → "{{{}}").
    out = []
    for ch in str(text):
        out.append("{{}" if ch == "{" else "{}}" if ch == "}" else ch)
    return "".join(out)


def send_keys(window_ctrl, keyspec: str, dry_run: bool = False) -> bool:
    """Activate the window's TermControl and send a raw uiautomation SendKeys
    spec (e.g. "{Esc}") with NO trailing Enter. Same foreground-safety and
    return contract as send_text_lines — used for interrupt keys like ESC that
    surface the "Switch model?" dialog without waiting for the turn to end."""
    if not keyspec:
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
        for _ in range(2):
            try:
                window_ctrl.SetActive()
            except Exception:
                pass
            try:
                term.SetFocus()
            except Exception:
                pass
            time.sleep(0.25)
            if not target_hwnd or _get_foreground_hwnd() == target_hwnd:
                break
        if target_hwnd and _get_foreground_hwnd() != target_hwnd:
            return False
        auto.SendKeys(str(keyspec), interval=0.02)
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

        # Multi-tab warning: WT exposes only the ACTIVE tab's content, so
        # Claude sessions in background tabs are invisible to the watchdog.
        tabs = list_tab_titles(w)
        if len(tabs) > 1:
            if not st.tabs_warned:
                others = [t for t in tabs if t != title]
                print(f"[tabs] {title!r}: this window has {len(tabs)} tabs — "
                      f"only the ACTIVE tab can be watched; "
                      f"currently invisible: {others!r}. "
                      f"Open each Claude session in its own window "
                      f"(drag the tab out of the tab bar).", flush=True)
                st.tabs_warned = True
        else:
            st.tabs_warned = False

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
        # Three flavors of "stuck / cut off" we treat identically:
        #   a) retry banner at attempt N/N — retries exhausted
        #   b) bare `API Error: ... (E...)` / `fetch failed` — no banner
        #   c) a server-side truncation — the "Server-error"/"Response-
        #      stalled" mid-stream wordings; 'continue' resumes the partial turn
        stuck_reason = None
        if tail:
            if parse_retry_exhausted(tail):
                stuck_reason = "retry-exhausted"
            elif parse_econnreset_stuck(tail):
                stuck_reason = "econnreset"
            elif parse_server_error_stuck(tail):
                stuck_reason = "server-error"
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
