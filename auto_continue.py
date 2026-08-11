"""
Detection and keystroke primitives for the Auto-Continue watchdog.

This module owns everything the GUI watcher (gui.py — the loop the shipped
exe runs) needs to observe and act on Claude Code sessions:

  * UI Automation readers for Windows Terminal windows and their scrollback.
  * The detection patterns and parsers: the 5-hour limit banner, network-stuck
    states, safeguard blocks, the live-turn spinner, the model status bar.
  * Checked keystroke senders (foreground-verified; they refuse to type
    rather than land keys in the wrong window).

There is deliberately NO watcher loop here. One used to exist — a CLI
sibling of gui.py's tick — and every detection change then had to be made
and verified twice; more than once only one copy was fixed. Run the GUI
(Auto-Continue.pyw) instead; it does everything the CLI did and more.
"""

from __future__ import annotations

import ctypes
import hashlib
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
APP_VERSION = "2.0.13"


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
# `weekly limit`, `daily usage limit` (up to ~3 qualifier words), or a
# hyphenated/numeric one like `5-hour limit`, etc. The reset time may or may
# not carry minutes (`Hpm` vs `H:MMpm`). Requiring the /upgrade follow-up
# line is what tells us this is a real rate-limit hit and not e.g. our own
# test output or this script's source code visible in the user's scrollback.
# The DOTALL `[\s\S]{0,400}` lets the two lines be separated by terminal
# padding/whitespace.
#
# The follow-up wording is a moving target — Anthropic has shipped
# `/extra-usage`, then renamed it `/usage-credits`, with the tail either
# "to finish what you're working on" or "to increase your usage limit". So
# after "/upgrade" we accept ANY of the known continuations (either slash
# command, or either stable tail phrase). The separator glyph between "limit"
# and "resets" also varies (middle dot ·, bullet operator ∙, dot operator ⋅,
# en/em dash), so the class enumerates all of them.
LIMIT_RE = re.compile(
    r"You['’]ve hit your (?:[\w-]+\s+){0,3}limit\s*[·•‧․∙⋅⸱\-–—]?\s*"
    r"resets\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)\s*\(([^)]+)\)"
    r"[\s\S]{0,400}?"
    r"(?:"
    r"/upgrade\b[^\n]{0,80}?"
    r"(?:/extra[-\s]?usage|/usage[-\s]?credits"
    r"|increase\s+your\s+usage\s+limit|finish\s+what\s+you)"
    # Seen live 2026-08-11: the follow-up line offered API billing and carried
    # no /upgrade at all, so demanding that word cost the window its entire
    # resume. The picker was answered and then nothing happened — a banner
    # that does not parse schedules no reset, and says nothing about it.
    r"|/log\s*in\b[^\n]{0,80}?(?:usage.billed|API\s+usage)"
    r")",
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
    # Detection only. Nothing is ever typed in response to this picker: its
    # other option is PAID extra usage, and Enter takes whatever is
    # highlighted. Matching it only marks the window as waiting on a human,
    # so the pattern stays broad — a stricter one would miss a picker and
    # leave the tool silent about a window that has stopped.
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
#     inside the recovery and the per-window resume budget stops it repeating.
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


# A detector can only tell an application-printed banner from the user
# TYPING that same phrase into the composer if it looks at WHERE the match
# sits, not just what it says — e.g. asking for help with "API Error: Unable
# to conn·ect to API (ECONN·RESET)" in one's own message reads identically
# to the real banner. (De-fanged with · like the pattern comments above, so
# this comment cannot self-trigger the very detector it documents.) Sampled
# from a live Claude Code TUI
# (2026-08): the composer/input-box line always renders with '>' as its
# first character, followed by a non-breaking space (U+00A0) — `repr()`
# shows it as `'>\xa0                    '`.
#
# The NBSP is the load-bearing part of the prefix, not the '>' alone: the
# limit picker, safeguard picker and "Switch model?" dialog all print their
# pre-selected option with a leading "> " of their own (e.g. "> 1. Switch to
# ..."), but with a PLAIN space (U+0020) — never NBSP. Treating a plain space
# as equivalent would make this helper misfire on that legitimate app output
# and hide a real, open picker.
#
# The GLYPH, unlike the space, varies: ASCII '>' in some builds and U+276F
# in others — both seen on this machine within one afternoon. Matching only
# '>' made a line that genuinely held a half-typed message ("delete the
# folder", in a session that had just offered to delete one) read as an
# empty box; an empty box is what licenses pressing Enter, which would have
# SUBMITTED it. Both glyphs are accepted; the NBSP still does the
# separating. No other TUI chrome is guessed at, since guessing wrong there
# risks hiding a REAL banner instead.
_COMPOSER_LINE_RE = re.compile("^[>\u276f]\xa0")


def _is_composer_line(text: str, pos: int) -> bool:
    """True if `pos` falls on a line the user is actively composing (see
    _COMPOSER_LINE_RE), i.e. NOT a line Claude Code itself printed.

    Used by detectors where a false positive causes an unwanted keystroke —
    typing 'continue' would corrupt the user's half-written message, and the
    picker/dialog detectors send a bare Enter, which would SUBMIT it.
    """
    line_start = text.rfind("\n", 0, pos) + 1
    line_end = text.find("\n", pos)
    if line_end == -1:
        line_end = len(text)
    return _COMPOSER_LINE_RE.match(text[line_start:line_end]) is not None


def parse_retry_exhausted(text: str, pattern=None) -> bool:
    """True if the most recent network-retry banner shows N == total (e.g.
    `attempt 10/10`) and the banner sits near the tail of the buffer.

    A retry banner buried far up in scrollback (e.g. from a previous outage
    the user already cleared manually) is ignored.

    `pattern` may be a user-supplied compiled regex; it MUST expose the two
    numeric groups (attempt, total) that RETRY_RE does.

    A match sitting on a composer line (see `_is_composer_line`) is dropped —
    that's the user typing the phrase, not a real banner — so 'continue'
    never lands mid-message.
    """
    rx = pattern or RETRY_RE
    matches = [m for m in rx.finditer(text)
               if not _is_composer_line(text, m.start())]
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

    A match on a composer line (see `_is_composer_line`) is dropped — e.g. a
    user asking for help by typing the exact error text reads identically to
    the real banner otherwise, and would get 'continue' typed into it.
    """
    rx = pattern or ECONNRESET_RE
    matches = [m for m in rx.finditer(text)
               if not _is_composer_line(text, m.start())]
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

    A match on a composer line (see `_is_composer_line`) is dropped — the
    user typing this text is not a real banner.
    """
    rx = pattern or SERVER_ERROR_RE
    matches = [m for m in rx.finditer(text)
               if not _is_composer_line(text, m.start())]
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
    can't make us press Enter into the input box.

    A match on a composer line (see `_is_composer_line`) is dropped — the
    user typing this text is not an open picker, and a bare Enter would
    SUBMIT whatever else they'd typed."""
    rx = pattern or FABLE_PICKER_RE
    matches = [m for m in rx.finditer(text)
               if not _is_composer_line(text, m.start())]
    if not matches:
        return False
    m = matches[-1]
    if len(text) - m.end() > SWITCH_POST_MATCH_TAIL:
        return False
    return True


# A per-model quota running dry is a DIFFERENT problem from the 5-hour limit,
# and has a different answer. The 5-hour limit stops every model, so waiting
# for the reset is the only move. A weekly per-model allowance stops one model
# while the others still work — so the remaining tasks can simply be finished
# somewhere else, and waiting days for the reset is pure waste.
#
# Written from the shape of Claude Code's other limit banners rather than from
# a captured sample: the machine this was built on had its Fable allowance
# refilled, so the real wording will not appear for days. That is exactly why
# this pattern is EDITABLE in Advanced -> Triggers with a live test box — when
# the real banner shows up, paste it in and adjust, no new build required.
#
# Deliberately narrow: it demands a model name AND a limit/quota word AND a
# reset clause. A looser rule would fire on any terminal that merely discusses
# a limit, and this one changes which model your session runs on.
# The \s+ separators (rather than literal spaces) keep this source from
# matching its own pattern when the watchdog reads a terminal showing it.
MODEL_QUOTA_RE = re.compile(
    r"(?:"
    r"\b(?:fable|opus|sonnet|haiku)\b[\w\s.\-]{0,40}?"
    r"(?:limit|quota)"
    r"|(?:limit|quota)\s+for\s+[\w\s.\-]{0,20}?"
    r"\b(?:fable|opus|sonnet|haiku)\b"
    r")"
    r"[\s\S]{0,200}?"
    r"reset[s]?\b",
    re.IGNORECASE,
)

# How far above the tail a quota banner may sit and still count as current.
# Same reasoning as the safeguard notice: once a session has scrolled on, an
# old banner is history, not a live block.
QUOTA_POST_MATCH_TAIL = 3000


def parse_model_quota(text: str, pattern=None) -> bool:
    """True if a per-model quota banner is live at the tail of the buffer.

    Tail-anchored and composer-aware for the same reasons as the other
    detectors: a banner scrolled far up is stale, and text on the input line
    is something the user typed, not something Claude Code printed.
    """
    rx = pattern or MODEL_QUOTA_RE
    matches = [m for m in rx.finditer(text)
               if not _is_composer_line(text, m.start())]
    if not matches:
        return False
    return len(text) - matches[-1].end() <= QUOTA_POST_MATCH_TAIL


def parse_switch_model_prompt(text: str, pattern=None) -> bool:
    """True if the 'Switch model?' Yes/No dialog is open at the tail of the
    buffer (a plain Enter confirms the pre-selected 'Yes, switch to …').
    Tail-anchored so a stale one further up scrollback doesn't retrigger.

    A match on a composer line (see `_is_composer_line`) is dropped for the
    same reason as `parse_fable_picker` — a bare Enter here SUBMITS the
    user's half-typed message."""
    rx = pattern or SWITCH_MODEL_RE
    matches = [m for m in rx.finditer(text)
               if not _is_composer_line(text, m.start())]
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

    A match on a composer line (see `_is_composer_line`) is dropped — the
    user typing this text is not an open picker, and a bare Enter would
    SUBMIT whatever else they'd typed.
    """
    rx = pattern or LIMIT_PROMPT_RE
    matches = [m for m in rx.finditer(text)
               if not _is_composer_line(text, m.start())]
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
# A Claude Code chooser in general: it pauses the session and waits for a
# selection. Two shapes matter and both are covered by "the selected option
# is marked, and the options are numbered":
#
#     > 1. Do the thing            <- selection marker + first option
#       2. Do something else
#
# Deliberately NOT `\s` after the marker: the composer line is a '>' followed
# by U+00A0, and `\s` matches that, which would let a half-typed message read
# as a chooser. A plain space or tab is the chooser's own marker.
#
# The examples in this comment are de-fanged (the marker and the digit are
# separated by a word) so that reading this file in a watched terminal cannot
# make the watchdog press Enter.
# The row prefix: indentation, optionally a box rule, more indentation.
# Kept explicit rather than a loose `.{0,8}` so that ordinary prose ending
# in "> 1." cannot pose as a chooser.
_ROW = r"[^\S\n]{0,6}[\u2502\u2503\u2551|]?[^\S\n]{0,4}"
CHOOSER_RE = re.compile(
    r"(?m)^" + _ROW + r"[>\u276f][ \t]+1[.)]\s+\S[\s\S]{0,2000}?"
    r"^" + _ROW + r"2[.)]\s+\S")

# Claude Code's tool-permission prompt. It is a chooser like any other, but
# answering it AUTHORISES an action — an edit, a command — so it gets its own
# switch rather than riding along with the harmless ones. The tells are
# stable across its variants: the question, and the two option wordings that
# only ever appear on permission prompts.
PERMISSION_RE = re.compile(
    r"Do\s+you\s+want\s+to\s+\w+"
    r"|allow\s+all\s+\w+\s+during\s+this\s+session"
    r"|and\s+don['\u2019]t\s+ask\s+again"
    r"|tell\s+Claude\s+what\s+to\s+do\s+differently",
    re.IGNORECASE,
)


# How far above the tail a chooser may sit and still count as open. Bigger
# than the limit picker's allowance because what Claude Code draws BELOW a
# chooser varies: the input box and status bar always, and often a todo list
# as well. Measured live at 1556 characters on a session with seven tasks
# listed, which the 1500 shared with the limit picker missed by fifty-six —
# and a missed chooser is a session that waits until a human notices.
CHOOSER_POST_MATCH_TAIL = 4000


def _chooser_match(text: str, pattern=None):
    """The last chooser on screen, if one is genuinely showing.

    Shared by both parsers below. A chooser far up the scrollback is one the
    user already answered, and a match on the composer line is the user
    typing something that looks like one.
    """
    rx = pattern or CHOOSER_RE
    matches = list(rx.finditer(text or ""))
    if not matches:
        return None
    m = matches[-1]
    if len(text) - m.end() > CHOOSER_POST_MATCH_TAIL:
        return None
    if _is_composer_line(text, m.start()):
        return None
    # An OPEN chooser has replaced the input box — measured on a live prompt
    # caught while it waited: below the options there was only the
    # "Esc to cancel" hint and no composer at all. So a composer line after
    # the match means this is not open: either the session already answered
    # it and the box came back, or the text is a chooser QUOTED in
    # conversation. The second case is not hypothetical — a reply that
    # quoted a permission prompt verbatim got that quotation answered, twice.
    for line in text[m.end():].splitlines():
        if _COMPOSER_LINE_RE.match(line):
            return None
    return m


def _permission_context(text: str, m) -> str:
    """The text a permission prompt's tells actually live in.

    The chooser match itself is a poor place to look: the question sits on
    the line ABOVE the selection marker, and the giveaway option wordings
    ("and don't ask again") fall past the match's non-greedy end. So widen to
    a window around it — enough for the question and the full option list,
    not so much that an unrelated earlier permission prompt in the scrollback
    makes an ordinary chooser look dangerous.
    """
    return text[max(0, m.start() - 300):min(len(text), m.end() + 400)]


def parse_chooser_prompt(text: str, pattern=None, perm_pattern=None) -> bool:
    """True if a chooser is waiting that is NOT a permission prompt.

    Answering this kind of chooser only picks an option the session already
    offered; the session was going to sit there until someone did. Permission
    prompts are excluded here on purpose — they are gated by their own
    switch, because accepting one authorises work rather than merely
    unblocking it.
    """
    m = _chooser_match(text, pattern)
    if m is None:
        return False
    prx = perm_pattern or PERMISSION_RE
    return not prx.search(_permission_context(text, m))


def parse_permission_prompt(text: str, pattern=None, perm_pattern=None) -> bool:
    """True if the chooser waiting on screen is a tool-permission prompt.

    Separate from parse_chooser_prompt so the two can never be enabled by
    accident together: this one says yes to running a command or editing a
    file, which is only appropriate when the user has decided that this
    session may act unattended.
    """
    m = _chooser_match(text, pattern)
    if m is None:
        return False
    prx = perm_pattern or PERMISSION_RE
    return bool(prx.search(_permission_context(text, m)))


def chooser_signature(text: str, pattern=None):
    """A short stable id for the chooser currently on screen, or None.

    Enter must be pressed ONCE per chooser. "The pattern still matches" is
    not evidence the chooser is still open — an answered modal's text stays
    in the scrollback, and treating that as "still open" once fired eight
    Enters at one dialog, each submitting whatever was in the input box.
    Comparing this id against the last one answered is what makes it once.
    """
    m = _chooser_match(text, pattern)
    if m is None:
        return None
    return hashlib.sha1(
        m.group(0).encode("utf-8", "replace")).hexdigest()[:12]


def composer_has_draft(text: str) -> bool:
    """True if the user has something half-typed in the input box.

    Every auto-answer path presses a bare Enter, and Enter with a draft in
    the box SUBMITS the draft. That is the whole risk being guarded against,
    and it exists only when a composer is on screen AND has text in it.

    An ABSENT composer is not a draft. Measured on a live session: while one
    of Claude Code's choosers is open it replaces the input box entirely —
    the screen ends with the "Enter to select / Tab to navigate / Esc to
    cancel" hint and no composer line exists. Treating that as "might be a
    draft" refused to act on every real chooser, i.e. on the only case the
    feature is for, while stand-ins that printed a composer line kept the
    tests green.

    An unreadable buffer is still "no", because nothing about it is known.
    """
    if not text:
        return False
    for line in text.splitlines():
        if _COMPOSER_LINE_RE.match(line):
            return bool(line[1:].strip())
    return False


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
        "key": "chooser",
        "label": "Any chooser (auto-answer)",
        "default": CHOOSER_RE.pattern,
        "groups": 0,
        "help": "A numbered chooser the session is waiting on. Only used "
                "when “Answer choosers” is enabled; Enter accepts the "
                "pre-selected first option.",
    },
    {
        "key": "permission",
        "label": "Tool-permission prompt",
        "default": PERMISSION_RE.pattern,
        "groups": 0,
        "help": "Tells a permission request apart from an ordinary chooser. "
                "It is EXCLUDED from “Answer choosers” and only answered "
                "when the separate permission switch is on.",
    },
    {
        "key": "model_quota",
        "label": "Model quota exhausted",
        "default": MODEL_QUOTA_RE.pattern,
        "groups": 0,
        "help": "A per-model allowance running dry (not the 5-hour limit). "
                "Only acted on when “Switch models when the quota runs out” "
                "is ticked. The shipped default is written from the shape of "
                "the other banners, not from a captured sample — when you see "
                "the real one, paste it into the test box below and adjust.",
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
        # is_dst=True resolves the AMBIGUOUS hour — the one that repeats when
        # clocks fall back — to its FIRST occurrence. pytz's default picks
        # the second, which would park the window for an extra hour on a
        # reset that had already happened. Erring early is the cheaper
        # mistake: the fire path re-checks that the limit banner is still
        # current before typing, so a too-early wake-up is skipped, while a
        # too-late one is an hour of the session sitting idle. The flag is
        # ignored for every unambiguous time, i.e. all but one hour a year.
        target_local = tz.localize(target_naive, is_dst=True)
    except Exception:
        # Non-existent local time (spring-forward gap): shift an hour.
        target_local = tz.localize(target_naive + timedelta(hours=1),
                                   is_dst=True)
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
        #
        # A zero/unreadable target_hwnd (NativeWindowHandle raised or was
        # falsy) means the foreground check CANNOT be performed — that must
        # be treated as "not verified", i.e. failure, not as an automatic
        # pass. The loop below still runs (SetActive/SetFocus are attempted
        # regardless) but never breaks early on a zero handle, and the
        # post-loop guard always fires when the handle is unreadable so
        # unverified typing never happens.
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
            if target_hwnd and _get_foreground_hwnd() == target_hwnd:
                break
        if _get_foreground_hwnd() != target_hwnd:
            return False
        for i, line in enumerate(lines):
            if i > 0:
                # Slash commands like /effort apply synchronously but Claude
                # Code's TUI needs a beat to render the resulting state
                # before the next "continue" submission lands.
                time.sleep(0.6)
                # ...and re-check we still own the foreground. The check
                # above covers the FIRST line only; a focus steal during
                # that pause would put the rest of a multi-line sequence
                # (e.g. /model then continue) into whatever took over.
                # Stopping mid-sequence is recoverable — the caller retries
                # — while typing into the wrong app is not.
                if _get_foreground_hwnd() != target_hwnd:
                    return False
            try:
                auto.SendKeys(sendkeys_literal(line) + "{Enter}",
                              interval=0.02)
            except Exception:
                # A throw here used to escape into the watcher's per-window
                # loop, which has no guard of its own outside the Fable
                # block — so one window's failed send stopped every window
                # after it from being looked at that tick, with a single
                # generic 'tick error' line as the only symptom. Report the
                # failure instead; callers already treat False as "retry
                # next tick". (This happened once for real, with braces in
                # free-typed text; sendkeys_literal escapes those now, but
                # the containment must not depend on having predicted every
                # input.)
                return False
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
        # Same "unreadable handle means unverified, not verified" rule as
        # send_text_lines — see the comment there. A zero target_hwnd must
        # never cause an early break/pass; the post-loop check always fires.
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
            if target_hwnd and _get_foreground_hwnd() == target_hwnd:
                break
        if _get_foreground_hwnd() != target_hwnd:
            return False
        try:
            auto.SendKeys(str(keyspec), interval=0.02)
        except Exception:
            return False        # same containment as send_text_lines
        return True
    finally:
        time.sleep(0.15)
        if saved_fg and saved_fg != target_hwnd:
            _set_foreground_hwnd(saved_fg)
