"""Codex CLI driver. No Claude parser, state, configuration, or sender imports.

The screen grammar was captured from Codex CLI 0.159.3 in Windows Terminal.
Only a live composer/footer or a live picker identifies a Codex surface;
an old startup banner in shell scrollback is insufficient.
"""
from __future__ import annotations

import ctypes
import hashlib
import re
import time
from ctypes import wintypes
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import uiautomation as auto

TAIL_CHARS = 24000
_FOOTER = re.compile(
    r"^\s*(?P<model>[\w][\w./:-]*)\s+"
    r"(?P<effort>none|minimal|low|medium|high|xhigh|max|ultra)\s*[·•]", re.I)
_OLD_FOOTER = re.compile(r"\d+%\s+context\s+left", re.I)
_SHORTCUTS = re.compile(r"^\s*\? for shortcuts\b", re.I)
_WARNINGS = re.compile(r"^\s*⚠\s+\d+\s+warnings?\s*[·•]\s*f2\s+to\s+view\s*$", re.I)
_BUSY = re.compile(r"^[•◦●○✦✧]\s+.+\(.*\besc to interrupt\)", re.I)
_TIME = re.compile(r"try again at\s+(?:([A-Za-z]{3,9}\s+\d{1,2}(?:st|nd|rd|th)?,?\s+\d{4})\s+)?"
                   r"(\d{1,2}):(\d{2})\s*([AP]M)?(?:\s+on\s+([^.!\n]+))?", re.I)
_MENU = re.compile(r"\b(?:enter|esc|s)\s+(?:to\s+)?(?:select|confirm|back|cancel|default|session)\b", re.I)
_ERROR = re.compile(r"^\s*■\s+(.+)", re.M)
# Native CLI 0.159.3 uses » for the empty Ultra composer (captured in
# Windows Terminal); submitted turns and other levels use ›.
_USER = re.compile(r"^\s*[›»]\s+", re.M)
_EMPTY = {"Ask Codex to do anything", "", "Use /skills to list available skills"}


def normalized(text: str) -> str:
    return "\n".join(line.rstrip() for line in (text or "").replace("\r", "").splitlines())


@dataclass(frozen=True)
class Screen:
    identified: bool = False
    ready: bool = False
    running: bool = False
    draft: bool = False
    blocked: bool = False
    model: str = ""
    effort: str = ""
    error_kind: str = ""
    error_id: str = ""
    reset_utc: datetime | None = None
    completion_id: str = ""
    permission: bool = False


def _reset_time(message: str, now: datetime) -> datetime | None:
    m = _TIME.search(message)
    if not m:
        return None
    before_date, hour, minute, ampm, after_date = m.groups()
    date_text = before_date or after_date
    hour, minute = int(hour), int(minute)
    if minute > 59 or (ampm and not 1 <= hour <= 12) or (not ampm and hour > 23):
        return None
    if ampm:
        hour = hour % 12 + (12 if ampm.upper() == "PM" else 0)
    local_now = now.astimezone()
    day = local_now.date()
    if date_text:
        cleaned = re.sub(r"(?<=\d)(st|nd|rd|th)\b", "", date_text.strip(), flags=re.I).replace(",", "")
        parsed = None
        for fmt in ("%B %d %Y", "%b %d %Y", "%d %b %Y", "%d %B %Y",
                    "%Y-%m-%d", "%B %d", "%b %d", "%d %b", "%d %B"):
            try:
                value = datetime.strptime(cleaned, fmt)
                year = value.year if "%Y" in fmt else local_now.year
                parsed = value.replace(year=year).date()
                if "%Y" not in fmt and parsed < day - timedelta(days=180):
                    parsed = parsed.replace(year=year + 1)
                break
            except ValueError:
                continue
        if parsed is None:
            return None
        day = parsed
    # The native renderer adds a date for a different day. A same-day time
    # already in the past is due, not a fresh wait until tomorrow.
    return datetime(day.year, day.month, day.day, hour, minute).astimezone(timezone.utc)


def inspect_screen(text: str, now: datetime | None = None) -> Screen:
    text = normalized(text)
    now = now or datetime.now(timezone.utc)
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        return Screen()
    bottom = lines[-8:]
    # The default alternate-screen UI puts a shortcuts row below the model.
    # Inline (--no-alt-screen) omits it. Only that native footer continuation
    # may follow a model footer; a shell prompt invalidates old scrollback.
    footer_index = len(lines) - 1
    for _ in range(2):
        if footer_index > 0 and (_SHORTCUTS.match(lines[footer_index]) or _WARNINGS.match(lines[footer_index])):
            footer_index -= 1
        else:
            break
    footer_line = lines[footer_index]
    footer = _FOOTER.match(footer_line)
    menu = bool(_MENU.search(lines[-1]))
    header = bool(re.search(r"^\s*>_ OpenAI Codex\s*\(v[\d.]+", text, re.M))
    picker = any(re.match(r"\s*(Select Model and Effort|Select Reasoning Level for|Advanced Reasoning|Update Model Permissions)\b", line)
                 or re.fullmatch(r"\s*Enable full access\?\s*", line) for line in lines[-20:])
    # A native command approval replaces both the composer and the startup
    # header in a long transcript. Identify its own heading, selected option
    # and confirmation footer, not arbitrary assistant text mentioning it.
    permission = bool(menu and re.fullmatch(r"\s*Press enter to confirm or esc to cancel\s*", lines[-1], re.I)
        and any(re.fullmatch(r"\s*Would you like to run the following command\?\s*", line) for line in lines[-20:])
        and any(re.match(r"\s*[›»]\s+\d+\.\s+", line) for line in lines[-8:]))
    prompts = list(_USER.finditer(text))
    old_footer = bool(_OLD_FOOTER.search(lines[-1]))
    identified = bool((footer or old_footer) and prompts) or bool((header or picker) and menu) or permission
    if not identified:
        return Screen()
    running = any(_BUSY.match(line.strip()) for line in bottom)
    blocked = menu or permission
    ready = bool(prompts and (footer or old_footer) and not blocked)
    draft = False
    if ready:
        remainder = text[prompts[-1].end():]
        input_lines = []
        for line in remainder.splitlines():
            if _FOOTER.match(line) or _OLD_FOOTER.search(line):
                break
            if line.strip():
                input_lines.append(line.strip())
        draft = " ".join(input_lines) not in _EMPTY
    kind = event = ""
    reset = None
    errors = list(_ERROR.finditer(text))
    if errors and not running:
        error = errors[-1]
        # The last prompt is the empty composer; another submitted prompt
        # below this error, or a completed turn below it, makes it history.
        after = text[error.end():]
        subsequent = list(_USER.finditer(after))
        complete_after = re.search(r"^\s*Worked for .+\s[•·]\s+\d{1,2}:\d{2}", after, re.M)
        if len(subsequent) <= 1 and not complete_after:
            end = error.end() + (subsequent[0].start() if subsequent else len(after))
            message = " ".join(text[error.start():end].split())
            lower = message.casefold().replace("’", "'")
            if "usage limit" in lower or "usage_limit_reached" in lower:
                kind, reset = "quota", _reset_time(message, now)
            elif any(word in lower for word in ("401", "403", "unauthorized", "authentication",
                       "token expired", "sign in", "billing", "insufficient_quota", "credits")):
                kind = "attention"
            elif any(word in lower for word in ("stream disconnected", "error sending request",
                       "error decoding response", "connection", "timed out", "reconnecting",
                       "503", "502", "504", "500", "stream error", "failed to connect")):
                kind = "network"
            else:
                kind = "attention"
            event = hashlib.sha256(" ".join(text[:end].split()).encode()).hexdigest()
    completions = list(re.finditer(r"^\s*Worked for .+\s[•·]\s+\d{1,2}:\d{2}", text, re.M))
    completion = hashlib.sha256(" ".join(text[:completions[-1].end()].split()).encode()).hexdigest() if completions else ""
    return Screen(identified, ready, running, draft, blocked,
                  footer.group("model") if footer else "",
                  footer.group("effort") if footer else "", kind, event, reset, completion, permission)


def terminal_handles() -> list[int]:
    handles = []
    u32 = ctypes.windll.user32
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def visit(hwnd, _):
        buf = ctypes.create_unicode_buffer(256)
        u32.GetClassNameW(hwnd, buf, 256)
        if buf.value == "CASCADIA_HOSTING_WINDOW_CLASS" and u32.IsWindowVisible(hwnd):
            handles.append(int(hwnd))
        return True

    if not u32.EnumWindows(callback_type(visit), 0):
        raise OSError("Codex terminal enumeration failed")
    return handles


def window_from_handle(hwnd):
    h = ctypes.c_void_p(int(hwnd))
    u32 = ctypes.windll.user32
    if not u32.IsWindow(h) or not u32.IsWindowVisible(h):
        return None
    buf = ctypes.create_unicode_buffer(256)
    u32.GetClassNameW(h, buf, 256)
    return auto.ControlFromHandle(int(hwnd)) if buf.value == "CASCADIA_HOSTING_WINDOW_CLASS" else None


def window_exists(hwnd):
    return bool(ctypes.windll.user32.IsWindow(ctypes.c_void_p(int(hwnd))))


def term_control(window, depth=0):
    if depth > 12:
        return None
    if window.ClassName == "TermControl":
        return window
    for child in window.GetChildren():
        found = term_control(child, depth + 1)
        if found is not None:
            return found
    return None


def read_text(window) -> str | None:
    try:
        term = term_control(window)
        if term is None:
            return None
        pattern = term.GetTextPattern()
        if not pattern:
            return None
        span = pattern.DocumentRange
        try:
            span.MoveEndpointByRange(auto.TextPatternRangeEndpoint.Start, span,
                                     auto.TextPatternRangeEndpoint.End, waitTime=0)
            span.MoveEndpointByUnit(auto.TextPatternRangeEndpoint.Start,
                                    auto.TextUnit.Character, -TAIL_CHARS, waitTime=0)
            return span.GetText(-1)
        except Exception:
            return pattern.DocumentRange.GetText(-1)[-TAIL_CHARS:]
    except Exception:
        return None


def send_prompt(window, prompt: str, dry_run=False) -> bool:
    """Re-read the Codex surface and verify focus before typing anything."""
    screen = inspect_screen(read_text(window) or "")
    if not screen.ready or screen.running or screen.draft or screen.blocked:
        return False
    if not prompt.strip() or "\n" in prompt or "\r" in prompt:
        return False
    if dry_run:
        return True
    u32 = ctypes.windll.user32
    u32.GetForegroundWindow.restype = wintypes.HWND
    previous = u32.GetForegroundWindow()
    target = int(window.NativeWindowHandle or 0)
    if not target:
        return False
    try:
        term = term_control(window)
        if term is None:
            return False
        for _ in range(2):
            window.SetActive()
            term.SetFocus()
            time.sleep(.25)
            if u32.GetForegroundWindow() == target:
                break
        if u32.GetForegroundWindow() != target:
            return False
        # A user can type while focus is being acquired. Verify again at
        # the last possible moment instead of appending to their draft.
        current = inspect_screen(read_text(window) or "")
        if not current.ready or current.running or current.draft or current.blocked:
            return False
        auto.SendKeys(prompt, charMode=True, interval=.02)
        if u32.GetForegroundWindow() != target:
            return False
        auto.SendKeys("{Enter}")
        return True
    except Exception:
        return False
    finally:
        if previous and previous != target and u32.GetForegroundWindow() == target:
            u32.SetForegroundWindow(previous)


def send_menu_keys(window, keys, expected_heading, expected_rows=None) -> bool:
    """Keys are allowed only in the specific picker opened by this driver."""
    u32 = ctypes.windll.user32
    u32.GetForegroundWindow.restype = wintypes.HWND
    previous = u32.GetForegroundWindow()
    target = int(window.NativeWindowHandle or 0)
    try:
        term = term_control(window)
        if term is None:
            return False
        # Windows Terminal can reject the first focus transfer when the
        # previous picker step restored another window. Use the same bounded
        # acquisition as prompt submission, then recheck this exact picker.
        for _ in range(2):
            window.SetActive()
            term.SetFocus()
            time.sleep(.25)
            if u32.GetForegroundWindow() == target:
                break
        text = normalized(read_text(window) or "")
        if u32.GetForegroundWindow() != target or expected_heading not in text or not _MENU.search("\n".join(text.splitlines()[-4:])):
            return False
        if expected_rows is not None and picker_rows(text, expected_heading) != expected_rows:
            return False
        auto.SendKeys(keys)
        return True
    except Exception:
        return False
    finally:
        if previous and previous != target and u32.GetForegroundWindow() == target:
            u32.SetForegroundWindow(previous)


def picker_rows(text, heading):
    section = normalized(text).rsplit(heading, 1)[-1]
    return [(int(m.group(2)), m.group(3).strip(), bool(m.group(1)))
            for m in re.finditer(r"^\s*(›\s*)?(\d+)\.\s+(.+)$", section, re.M)]


def _wait_picker(window, headings, seconds=3):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        text = normalized(read_text(window) or "")
        for heading in headings:
            if heading in text and _MENU.search("\n".join(text.splitlines()[-4:])):
                return text, heading
        time.sleep(.1)
    return "", ""


def _select_picker(window, text, heading, name, final=False):
    rows = picker_rows(text, heading)
    selected = next((number for number, _, active in rows if active), None)
    target = next((number for number, label, _ in rows if
                   label.casefold().startswith(name.casefold()) and
                   (len(label) == len(name) or not label[len(name)].isalnum())), None)
    if selected is None or target is None:
        return False
    # Native CLI offers an explicit session-only key; Enter here would
    # update the user's default Codex configuration, which we never do.
    steps = "{Down}" * max(0, target - selected) + "{Up}" * max(0, selected - target)
    if not final:
        return send_menu_keys(window, steps + "{Enter}", heading, rows)
    if steps and not send_menu_keys(window, steps, heading, rows):
        return False
    time.sleep(.2)
    current = normalized(read_text(window) or "")
    updated = picker_rows(current, heading)
    if "s session" not in current.splitlines()[-1] or not any(number == target and active for number, _, active in updated):
        return False
    return send_menu_keys(window, "s", heading, updated)


def apply_session_options(window, model="", effort="") -> bool:
    """Drive native /model pickers, changing this session only."""
    initial = inspect_screen(read_text(window) or "")
    if not initial.ready or initial.running or initial.draft or initial.blocked:
        return False
    wanted_model = model or initial.model
    wanted_effort = effort or initial.effort
    if (not model or model.casefold() == initial.model.casefold()) and (not effort or effort == initial.effort):
        return True
    if not send_prompt(window, "/model"):
        return False
    text, heading = _wait_picker(window, ["Select Model and Effort"])
    if not heading or not _select_picker(window, text, heading, wanted_model):
        if heading:
            send_menu_keys(window, "{Esc}", heading)
        return False
    text, heading = _wait_picker(window, ["Select Reasoning Level"])
    names = {"low": "Low", "medium": "Medium", "high": "High", "xhigh": "Extra high",
             "max": "Max", "ultra": "Ultra"}
    if wanted_effort in ("max", "ultra") and heading:
        if not _select_picker(window, text, heading, "More reasoning"):
            return False
        text, heading = _wait_picker(window, ["Advanced Reasoning"])
    if not heading or wanted_effort not in names or not _select_picker(window, text, heading, names[wanted_effort], final=True):
        # Back out of only the pickers we opened. Unrelated dialogs are held.
        for _ in range(2):
            if heading:
                send_menu_keys(window, "{Esc}", heading)
            text, heading = _wait_picker(window, ["Select Model and Effort", "Select Reasoning Level", "Advanced Reasoning"], seconds=.3)
        return False
    end = time.monotonic() + 4
    while time.monotonic() < end:
        current = inspect_screen(read_text(window) or "")
        if current.ready and current.model.casefold() == wanted_model.casefold() and current.effort == wanted_effort:
            return True
        time.sleep(.15)
    return False


PERMISSION_NAMES = {"full-access": "Full Access", "approve-for-me": "Approve for me",
                    "ask-for-approval": "Ask for approval"}


def _permission_current(rows, name):
    pattern = r"^" + re.escape(name) + r"(?: \(non-admin sandbox\))? \(current\)"
    return any(re.match(pattern, label) for _, label, _ in rows)


def apply_session_permissions(window, mode):
    """Use the captured CLI 0.160 permission picker; verify its current row."""
    if mode not in PERMISSION_NAMES or not send_prompt(window, "/permissions"):
        return False
    heading = "Update Model Permissions"
    text, found = _wait_picker(window, [heading])
    if not found:
        return False
    name = PERMISSION_NAMES[mode]
    rows = picker_rows(text, heading)
    if _permission_current(rows, name):
        return send_menu_keys(window, "{Esc}", heading, rows)
    if not _select_picker(window, text, heading, name):
        send_menu_keys(window, "{Esc}", heading, rows)
        return False
    if mode == "full-access":
        text, found = _wait_picker(window, ["Enable full access?"])
        if not found or not _select_picker(window, text, found, "Yes, continue anyway"):
            return False
    deadline = time.monotonic() + 4
    while time.monotonic() < deadline:
        current = inspect_screen(read_text(window) or "")
        if current.ready and not (current.blocked or current.draft or current.running):
            break
        time.sleep(.1)
    else:
        return False
    if not send_prompt(window, "/permissions"):
        return False
    text, found = _wait_picker(window, [heading])
    if not found:
        return False
    rows = picker_rows(text, heading)
    applied = _permission_current(rows, name)
    closed = send_menu_keys(window, "{Esc}", heading, rows)
    return applied and closed


def approve_permission(window):
    """Approve this exact native command request, never an unrelated chooser."""
    text = normalized(read_text(window) or "")
    if not inspect_screen(text).permission:
        return False
    heading = "Would you like to run the following command?"
    rows = picker_rows(text, heading)
    selected = next((label for _, label, active in rows if active), "")
    # A user already moving through this prompt takes priority over the
    # automatic default. The once-only approval must remain the active row.
    if not selected.startswith("Yes, proceed (y)"):
        return False
    if not send_menu_keys(window, "y", heading, rows):
        return False
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if not inspect_screen(read_text(window) or "").permission:
            return True
        time.sleep(.1)
    return False
