"""Positive provider routing at the GUI boundary; unknown terminals are held."""
import re

import auto_continue as claude
import codex_cli as codex

_providers = {}


def classify_text(text):
    """Identify the live surface, never infer Claude from a failed Codex read.

    Claude's composer uses U+276F; Codex uses U+203A. A shell prompt after
    either UI invalidates that historical surface. This filter leaves Claude's
    parser, settings and keyboard driver untouched.
    """
    if not text:
        return "unknown"
    lines = [line.rstrip() for line in text.replace("\r", "").splitlines() if line.strip()]
    if not lines:
        return "unknown"
    tail = "\n".join(lines[-80:])
    if re.match(r"\s*(?:PS\s+.*>|(?:[A-Za-z]:\\[^\n]*>)|\w+@[^\n]*[$#])", lines[-1]):
        return "unknown"
    if codex.inspect_screen(text).identified:
        return "codex"
    # An unrecognised Codex layout still must not enter the Claude watcher.
    if re.search(r"^\s*›(?:\s|$)", tail, re.M):
        return "codex" if re.search(r"OpenAI Codex|Ask Codex|Select Model and Effort", text) else "unknown"
    composer = bool(re.search(r"^\s*[│┃|]?\s*❯(?:\s|$)", tail, re.M))
    footer = bool(re.search(r"(?i)(?:shift\+tab|accept edits|bypass permissions|auto mode|plan mode|\? for shortcuts)", "\n".join(lines[-8:])))
    composer_box = bool(re.search(r"^[─━]{8,}", tail, re.M))
    if composer and (footer or composer_box):
        return "claude"
    # Claude modals replace the composer. Require its own banner as well as
    # the modal hint, instead of treating a generic chooser as a Claude UI.
    banner = bool(re.search(r"^\s*[│┃|]?\s*Claude Code\b", text, re.M))
    if banner and re.search(r"(?i)(?:Enter to (?:select|confirm)|Esc to cancel|shift\+tab to approve)", "\n".join(lines[-5:])):
        return "claude"
    return "unknown"


def provider(window):
    try:
        identity = classify_text(codex.read_text(window))
    except Exception:
        identity = "unknown"
    try:
        _providers[int(window.NativeWindowHandle)] = identity
    except Exception:
        pass
    return identity


def claude_row_allowed(hwnd):
    # Claude retains a missing HWND's state for transient UIA failures. The
    # GUI must not label that retained row as Claude after a provider switch.
    return _providers.get(int(hwnd), "claude") == "claude"


def claude_windows():
    return [window for window in claude.find_terminal_windows() if provider(window) == "claude"]


def claude_window(hwnd):
    window = codex.window_from_handle(hwnd)
    return window if window is not None and provider(window) == "claude" else None
