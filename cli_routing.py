"""GUI boundary: a Codex terminal never enters the existing Claude watcher."""
import auto_continue as claude
import codex_cli as codex


def _is_codex(window):
    return codex.inspect_screen(codex.read_text(window) or "").identified


def claude_windows():
    return [window for window in claude.find_terminal_windows() if not _is_codex(window)]


def claude_window(hwnd):
    window = claude.terminal_window_from_handle(hwnd)
    return None if window is not None and _is_codex(window) else window
