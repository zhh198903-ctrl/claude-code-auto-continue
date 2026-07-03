"""
GUI for the Claude Code 5h auto-continue watchdog.

Layout:
  ┌─ header ────────────────────────────────────────────────────────────┐
  │ ● Running   [Stop]   [✓] dry-run   poll 30s   buffer 20s          │
  ├─ window table ─────────────────────────────────────────────────────┤
  │ Title              Status      Reset      Countdown  Action        │
  │ ⠂ peak-pulse…      ⏳ Waiting   23:00      02:15:42   [Now] [Skip]  │
  │ ⠐ move-resources…  Idle        —          —          [Exclude]     │
  │ …                                                                  │
  ├─ log ──────────────────────────────────────────────────────────────┤
  │ 10:35:42  [detect] limit hit on 'move-resources…' → resets 11pm    │
  │ …                                                                  │
  └────────────────────────────────────────────────────────────────────┘

The watching loop runs on a QThread so UIA reads (which can take 100ms+
per terminal) never block the UI. The worker emits a snapshot dict each
tick; the main thread rebuilds the table from that snapshot.

Three user actions per row:
  * Now      — fire `continue` immediately, skipping the timer.
  * Skip     — clear the pending limit for this row (e.g. user already
               handled it manually). Cooldown still applies.
  * Exclude  — never watch this window again (per-session, persisted).

Persisted settings (QSettings → registry on Windows): poll interval,
buffer seconds, dry-run flag, excluded window titles.
"""

from __future__ import annotations

import html
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

# IMPORTANT: set per-monitor v2 DPI awareness BEFORE any DLL that might set
# its own awareness (uiautomation/UIAutomationCore.dll, Qt). Windows allows
# the awareness to be set exactly once per process — whichever DLL gets there
# first wins, and Qt's later attempt then fails with "access denied" and
# triggers the warning the user sees. We do it ourselves at the earliest
# possible moment so Qt's preferred mode actually sticks.
def _set_dpi_awareness() -> None:
    import ctypes
    # Per-Monitor v2 (Windows 10 1703+). Constant is -4 cast to a context.
    DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = ctypes.c_void_p(-4)
    try:
        ok = ctypes.windll.user32.SetProcessDpiAwarenessContext(
            DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2
        )
        if ok:
            return
    except (AttributeError, OSError):
        pass
    # Fallback for older Windows: SetProcessDpiAwareness (Win 8.1+).
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PER_MONITOR
    except (AttributeError, OSError):
        pass

_set_dpi_awareness()

import pytz
from PyQt6.QtCore import (
    QEvent, QObject, QSettings, QThread, QTimer, QUrl, Qt, pyqtSignal, pyqtSlot,
)
from PyQt6.QtGui import QAction, QColor, QDesktopServices, QFont
from PyQt6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QHBoxLayout,
    QHeaderView, QLabel, QMainWindow, QMenu, QMessageBox, QPlainTextEdit,
    QPushButton, QSpinBox, QStyle, QSystemTrayIcon, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

from auto_continue import (
    APP_VERSION, LIMIT_RE, SCAN_TAIL_CHARS, find_terminal_windows,
    find_termcontrol, init_uia_thread, next_reset_datetime,
    parse_econnreset_stuck, parse_limit_message, parse_limit_prompt,
    parse_oauth_expired, parse_retry_exhausted,
    read_terminal_text, send_continue, send_text_lines,
)
import updater


# Effort levels offered in the per-window dropdown, matching Claude Code's
# own `/effort` slider (low → … → ultracode). The first sentinel means
# "don't send /effort, just continue as-is". `ultracode` (xhigh + workflows)
# is newer — sessions on Claude Code older than 4.7 won't have it, in which
# case `/effort ultracode` is simply ignored by that session.
EFFORT_NONE = ""
EFFORT_LEVELS = [EFFORT_NONE, "low", "medium", "high", "xhigh", "max",
                 "ultracode"]
EFFORT_LABEL = {
    EFFORT_NONE: "(none)",
    "low": "low",
    "medium": "medium",
    "high": "high",
    "xhigh": "xhigh",
    "max": "max",
    "ultracode": "ultracode",
}

# Model levels offered in the per-window dropdown, matching Claude Code's
# `/model` picker (Default / Sonnet / Haiku). The first sentinel means
# "don't send /model, leave the session on whatever model it's already on".
MODEL_NONE = ""
MODEL_LEVELS = [MODEL_NONE, "default", "sonnet", "haiku"]
MODEL_LABEL = {
    MODEL_NONE: "(none)",
    "default": "default",
    "sonnet": "sonnet",
    "haiku": "haiku",
}


def title_key(title: str) -> str:
    """Strip the leading WT spinner glyph + whitespace so the per-window
    settings (effort, exclusion) survive title churn while a session is
    actively running."""
    import re as _re
    return _re.sub(r"^[^\w]+", "", title or "").strip()


# ===========================================================================
# Watcher (background worker)
# ===========================================================================

# UI-facing window status codes. Kept short because they go in a table cell.
ST_IDLE = "idle"          # Window seen, no limit detected.
ST_PENDING = "pending"    # Limit detected, waiting until reset time.
ST_FIRING = "firing"      # In the middle of sending keys.
ST_SENT = "sent"          # Just sent — short-lived display state.
ST_COOLDOWN = "cooldown"  # Inside post-send cooldown window.
ST_EXCLUDED = "excluded"  # User chose to ignore this window.
ST_RETRY = "retry"        # Network retries exhausted, resending continue.
ST_PROMPT = "prompt"      # Limit picker open, confirming with Enter.


@dataclass
class _WState:
    """Internal per-window state, kept inside the watcher thread only."""
    hwnd: int
    title: str
    status: str = ST_IDLE
    reset_utc: Optional[datetime] = None
    # The (hour_12, minute, ampm, tz) tuple that produced reset_utc. We
    # compare against this when re-parsing the scrollback so a still-visible
    # message whose wall-clock target has just passed doesn't get "rolled
    # forward" by next_reset_datetime and push out a pending that's about
    # to fire.
    reset_key: Optional[tuple] = None
    last_sent_utc: Optional[datetime] = None
    sent_flash_until: Optional[datetime] = None  # show "sent" for ~5s
    # Key of the last limit message we already fired for (or the user
    # skipped). Prevents the same still-visible message from re-arming a
    # bogus "tomorrow" pending after the cooldown expires. Cleared once a
    # tick sees no limit message at all.
    fired_key: Optional[tuple] = None
    # Network-retry exhaustion bookkeeping. Independent of the rate-limit
    # fields above.
    retry_last_sent_utc: Optional[datetime] = None
    retry_active: bool = False
    # Interactive limit-picker ("What do you want to do?") bookkeeping.
    prompt_last_sent_utc: Optional[datetime] = None
    prompt_active: bool = False
    # OAuth-expired warn-once flag ('continue' can't fix that state).
    oauth_logged: bool = False


class Watcher(QObject):
    """
    Background worker. Runs `tick()` every `interval` seconds in its own
    thread. Emits a full snapshot list each tick — the GUI just rerenders.
    """

    # snapshot is a list[dict] — see _make_snapshot for the schema.
    snapshot = pyqtSignal(list)
    log = pyqtSignal(str, str)  # (level, message). level ∈ {info,warn,err,fire}
    running_changed = pyqtSignal(bool)

    def __init__(self):
        super().__init__()
        self._states: dict[int, _WState] = {}
        self._excluded_titles: set[str] = set()
        # Per-window effort override. Key is the *stable* part of the WT
        # title (leading spinner glyph stripped), value is one of
        # {max,xhigh,high,medium,low,ultracode}. Missing/empty = no override.
        self._effort_overrides: dict[str, str] = {}
        # Per-window model override, same keying. Value ∈ {default,sonnet,
        # haiku}. Missing/empty = leave the session's current model alone.
        self._model_overrides: dict[str, str] = {}

        self._interval = 30
        self._buffer = 20
        self._retry_interval = 30
        self._dry_run = False

        # User commands accumulated between ticks. Each maps hwnd → True.
        self._cmd_fire_now: set[int] = set()
        self._cmd_skip: set[int] = set()

        self._running = False
        # Use a QTimer that lives on this object's owning thread (the worker
        # thread) once moveToThread happens. Started/stopped via slots.
        self._timer: Optional[QTimer] = None

    # ---- thread entry ----------------------------------------------------

    @pyqtSlot()
    def thread_started(self) -> None:
        # Must run before the first find_terminal_windows() so this worker
        # thread builds the UIA client in a live (STA) apartment — otherwise
        # it only ever sees windows that existed when it started.
        init_uia_thread()
        self._timer = QTimer()
        self._timer.setSingleShot(False)
        self._timer.setInterval(self._interval * 1000)
        self._timer.timeout.connect(self._tick_safely)

    # ---- config from main thread ----------------------------------------

    @pyqtSlot(int)
    def set_interval(self, seconds: int) -> None:
        self._interval = max(5, int(seconds))
        if self._timer is not None:
            self._timer.setInterval(self._interval * 1000)

    @pyqtSlot(int)
    def set_buffer(self, seconds: int) -> None:
        self._buffer = max(0, int(seconds))

    @pyqtSlot(int)
    def set_retry_interval(self, seconds: int) -> None:
        self._retry_interval = max(5, int(seconds))

    @pyqtSlot(bool)
    def set_dry_run(self, on: bool) -> None:
        self._dry_run = bool(on)

    @pyqtSlot(list)
    def set_excluded(self, titles: list) -> None:
        # Normalized via title_key so exclusion survives the WT spinner
        # glyph and other leading-junk title churn (same keying as the
        # effort/model overrides).
        self._excluded_titles = {title_key(str(t)) for t in titles}

    @pyqtSlot(dict)
    def set_effort_overrides(self, overrides: dict) -> None:
        # Filter out empty / "(none)" entries so the worker only stores
        # actionable overrides.
        self._effort_overrides = {
            str(k): str(v) for k, v in overrides.items() if v
        }

    @pyqtSlot(dict)
    def set_model_overrides(self, overrides: dict) -> None:
        self._model_overrides = {
            str(k): str(v) for k, v in overrides.items() if v
        }

    # ---- user actions per row -------------------------------------------

    @pyqtSlot(int)
    def cmd_fire_now(self, hwnd: int) -> None:
        self._cmd_fire_now.add(int(hwnd))
        # Run a tick right away so the user sees the effect without waiting
        # for the next timer fire.
        self._tick_safely()

    @pyqtSlot(int)
    def cmd_skip(self, hwnd: int) -> None:
        self._cmd_skip.add(int(hwnd))
        self._tick_safely()

    @pyqtSlot(int, str)
    def cmd_exclude(self, hwnd: int, title: str) -> None:
        self._excluded_titles.add(title_key(title))
        self._tick_safely()

    @pyqtSlot(str)
    def cmd_unexclude(self, title: str) -> None:
        self._excluded_titles.discard(title_key(title))
        self._tick_safely()

    @pyqtSlot(int)
    def cmd_clear_cooldown(self, hwnd: int) -> None:
        st = self._states.get(int(hwnd))
        if st is not None:
            st.last_sent_utc = None
            st.sent_flash_until = None
            st.status = ST_IDLE
            self.log.emit("info", f"cooldown cleared for {st.title!r}")
        self._tick_safely()

    # ---- start / stop ---------------------------------------------------

    @pyqtSlot()
    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self.running_changed.emit(True)
        self.log.emit("info",
                      f"watcher started (interval {self._interval}s, "
                      f"buffer {self._buffer}s, "
                      f"{'DRY-RUN' if self._dry_run else 'live'})")
        if self._timer is not None:
            self._timer.start()
        # Run an immediate first tick so the table populates.
        self._tick_safely()

    @pyqtSlot()
    def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        if self._timer is not None:
            self._timer.stop()
        self.running_changed.emit(False)
        self.log.emit("info", "watcher stopped")

    # ---- core tick ------------------------------------------------------

    def _tick_safely(self) -> None:
        # Guard: per-row command slots (fire-now / skip / exclude / …) call
        # this unconditionally — a full detection+send tick must never run
        # while the watcher is Stopped.
        if not self._running:
            return
        try:
            self._tick()
        except Exception as e:
            self.log.emit("err", f"tick error: {type(e).__name__}: {e}")

    def _tick(self) -> None:
        now = datetime.now(pytz.UTC)
        cooldown = timedelta(minutes=15)
        buffer = timedelta(seconds=self._buffer)
        retry_interval = timedelta(seconds=self._retry_interval)

        windows = find_terminal_windows()
        seen: set[int] = set()

        for w in windows:
            try:
                hwnd = int(w.NativeWindowHandle or 0)
            except Exception:
                continue
            if not hwnd:
                continue
            seen.add(hwnd)

            try:
                title = w.Name or f"<hwnd {hwnd}>"
            except Exception:
                # A UIA COMError reading the title shouldn't drop the whole
                # tick — fall back to an hwnd label and keep going.
                title = f"<hwnd {hwnd}>"
            st = self._states.setdefault(hwnd, _WState(hwnd=hwnd, title=title))
            st.title = title

            # Excluded windows never get processed. Keyed via title_key so
            # exclusion survives the WT spinner glyph / title churn.
            if title_key(title) in self._excluded_titles:
                st.status = ST_EXCLUDED
                # Queued row commands are meaningless for an excluded row —
                # drop them so they can't fire much later (or into an
                # unrelated window after Windows recycles the HWND).
                self._cmd_fire_now.discard(hwnd)
                self._cmd_skip.discard(hwnd)
                continue

            # --- per-row commands accumulated since last tick ---
            if hwnd in self._cmd_skip:
                self._cmd_skip.discard(hwnd)
                if st.reset_utc is not None:
                    self.log.emit("info",
                                  f"skipped pending continue for {title!r}")
                    # Remember the skipped key: the message is still visible
                    # in the scrollback and must not instantly re-arm in the
                    # detection step below — that would make Skip a no-op.
                    st.fired_key = st.reset_key or st.fired_key
                st.reset_utc = None
                st.reset_key = None
                st.status = ST_IDLE

            force_fire = hwnd in self._cmd_fire_now
            if force_fire:
                self._cmd_fire_now.discard(hwnd)

            # 0. Fading "sent" flash.
            if st.sent_flash_until and now >= st.sent_flash_until:
                st.sent_flash_until = None
                st.status = (
                    ST_COOLDOWN
                    if st.last_sent_utc and now - st.last_sent_utc < cooldown
                    else ST_IDLE
                )

            # 0.5. Read scrollback once per tick so every detector below
            # shares the same view of the terminal.
            text = read_terminal_text(w)
            tail = text[-SCAN_TAIL_CHARS:] if text else ""
            dr = "[dry-run] " if self._dry_run else ""

            # 0.52. Dead-session states 'continue' can't fix: warn once.
            if tail and parse_oauth_expired(tail):
                if not st.oauth_logged:
                    self.log.emit(
                        "warn",
                        f"OAuth token expired on {title!r} — auto-continue "
                        f"can't fix this; run /login in that session"
                    )
                    st.oauth_logged = True
            else:
                st.oauth_logged = False

            # 0.55. Interactive limit picker ("What do you want to do?").
            # Newer Claude Code builds show this modal INSTEAD of the limit
            # banner; option 1 ("Stop and wait for limit to reset") is
            # pre-selected, so a bare Enter confirms it and makes the
            # regular banner (with the reset time) appear — which the flow
            # below then picks up on the next tick. Does NOT touch
            # last_sent_utc: the banner must not be swallowed by cooldown.
            if tail and parse_limit_prompt(tail):
                if (force_fire
                        or st.prompt_last_sent_utc is None
                        or now - st.prompt_last_sent_utc >= retry_interval):
                    first = not st.prompt_active
                    if first:
                        self.log.emit(
                            "warn",
                            f"limit picker open on {title!r}; confirming "
                            f"'Stop and wait for limit to reset'"
                        )
                        st.prompt_active = True
                    self.log.emit(
                        "fire" if first else "info",
                        f"{dr}pressing Enter (limit picker) → {title!r}"
                    )
                    ok = send_text_lines(w, [""], dry_run=self._dry_run)
                    if ok:
                        st.prompt_last_sent_utc = now
                st.status = ST_PROMPT
                continue
            elif st.prompt_active or st.prompt_last_sent_utc is not None:
                st.prompt_active = False
                st.prompt_last_sent_utc = None
                if st.status == ST_PROMPT:
                    st.status = ST_IDLE

            # 0.6. Network-stuck path. Runs *before* the rate-limit logic and
            # ignores the cooldown — if the API is unreachable in the middle
            # of a 5h wait we still want to resend 'continue' every
            # retry_interval seconds until the connection comes back. Two
            # flavors are treated identically:
            #   a) retry banner at attempt N/N — retries exhausted
            #   b) bare `API Error: ... (E...)` / `fetch failed` — no banner
            stuck_reason = None
            if tail:
                if parse_retry_exhausted(tail):
                    stuck_reason = "network retries exhausted"
                elif parse_econnreset_stuck(tail):
                    stuck_reason = "network API error"
            if stuck_reason:
                if (force_fire
                        or st.retry_last_sent_utc is None
                        or now - st.retry_last_sent_utc >= retry_interval):
                    first = not st.retry_active
                    if first:
                        self.log.emit(
                            "warn",
                            f"{stuck_reason} on {title!r}; "
                            f"sending 'continue' every "
                            f"{self._retry_interval}s until recovery"
                        )
                        st.retry_active = True
                    # 'fire' (tray balloon) only for the FIRST resend of an
                    # outage — a long outage would otherwise pop a balloon
                    # every retry_interval seconds.
                    self.log.emit(
                        "fire" if first else "info",
                        f"{dr}resending 'continue' (retry path) → {title!r}"
                    )
                    ok = send_continue(w, dry_run=self._dry_run)
                    if ok:
                        st.retry_last_sent_utc = now
                        st.status = ST_RETRY
                        # If a rate-limit pending elapsed during the outage,
                        # this 'continue' doubles as its fire — otherwise
                        # we'd send a second, redundant continue right
                        # after recovery.
                        if (st.reset_utc is not None
                                and now >= st.reset_utc + buffer):
                            st.fired_key = st.reset_key
                            st.reset_utc = None
                            st.reset_key = None
                            st.last_sent_utc = now
                    else:
                        self.log.emit(
                            "warn",
                            f"retry send failed for {title!r}; "
                            f"will try again in {self._interval}s"
                        )
                        st.status = ST_RETRY
                else:
                    st.status = ST_RETRY
                continue
            else:
                if st.retry_active:
                    self.log.emit(
                        "info",
                        f"network error cleared on {title!r}; "
                        f"recovered"
                    )
                    st.retry_active = False
                    st.retry_last_sent_utc = None
                    if st.status == ST_RETRY:
                        st.status = ST_IDLE

            # 1. Detect rate-limit and update pending if it changed.
            # Runs *before* the fire decision so a NEW limit message with a
            # different reset time correctly supersedes an older pending
            # target. Skipped inside the post-send cooldown window because
            # the lingering old message would otherwise re-trigger right
            # after we sent continue.
            in_cooldown = (st.last_sent_utc is not None
                           and now - st.last_sent_utc < cooldown)
            if tail and not in_cooldown:
                parsed = parse_limit_message(tail)
                if (parsed and parsed != st.reset_key
                        and parsed != st.fired_key):
                    # `fired_key` blocks the stale case: a message we
                    # already fired for (or the user skipped), still visible
                    # after the cooldown, must not re-arm a bogus "tomorrow
                    # at the same time" pending.
                    hour_12, minute, ampm, tz_name = parsed
                    new_reset = None
                    try:
                        new_reset = next_reset_datetime(
                            hour_12, minute, ampm, tz_name
                        )
                    except Exception as e:
                        self.log.emit(
                            "err", f"reset calc failed for {title!r}: {e}"
                        )
                    if new_reset is not None:
                        old = st.reset_utc
                        st.reset_utc = new_reset
                        st.reset_key = parsed
                        local = (new_reset + buffer).astimezone()
                        if old is None:
                            self.log.emit(
                                "info",
                                f"limit on {title!r} → resets "
                                f"{hour_12}:{minute:02d}{ampm} ({tz_name}); "
                                f"will fire at "
                                f"{local:%Y-%m-%d %H:%M:%S %Z}"
                            )
                        else:
                            old_local = (old + buffer).astimezone()
                            self.log.emit(
                                "info",
                                f"limit on {title!r} reset shifted: "
                                f"{old_local:%Y-%m-%d %H:%M:%S} → "
                                f"{local:%Y-%m-%d %H:%M:%S}"
                            )
                elif parsed is None:
                    # No limit message anywhere near the tail: the handled
                    # message is gone, so any future match is genuinely new.
                    st.fired_key = None

            # 2. Pending? Decide whether to fire.
            if st.reset_utc is not None or force_fire:
                fire_at = (st.reset_utc or now) + buffer
                if force_fire or now >= fire_at:
                    # Re-verify the message is still current — if the user
                    # already continued manually, the session has moved on
                    # and a redundant 'continue' would start an unwanted
                    # turn. (Forced fires skip this check by design.)
                    if (not force_fire and tail
                            and parse_limit_message(tail) is None):
                        self.log.emit(
                            "info",
                            f"limit message gone on {title!r} before fire; "
                            f"assuming handled manually"
                        )
                        st.fired_key = st.reset_key
                        st.reset_utc = None
                        st.reset_key = None
                        st.status = ST_IDLE
                        continue
                    st.status = ST_FIRING
                    model = self._model_overrides.get(title_key(title), "")
                    effort = self._effort_overrides.get(title_key(title), "")
                    lines = []
                    if model:
                        lines.append(f"/model {model}")
                        # A direct `/model <name>` switches immediately. The
                        # trailing blank Enter confirms a dialog if one ever
                        # pops; on an empty input box it's a harmless no-op.
                        lines.append("")
                    if effort:
                        lines.append(f"/effort {effort}")
                        # Every effort level change pops a "Change effort
                        # level?" confirmation dialog with the default
                        # cursor on "Yes, switch to <level>". Sending a
                        # plain Enter selects it. (Verified for low/high/
                        # max — applies to all levels.)
                        lines.append("")
                    lines.append("continue")
                    self.log.emit(
                        "fire",
                        f"{dr}sending {lines} to {title!r}"
                        + (" (forced)" if force_fire else "")
                    )
                    ok = send_text_lines(w, lines, dry_run=self._dry_run)
                    if ok:
                        st.last_sent_utc = now
                        st.fired_key = st.reset_key
                        st.reset_utc = None
                        st.reset_key = None
                        st.status = ST_SENT
                        st.sent_flash_until = now + timedelta(seconds=5)
                        self.log.emit(
                            "info",
                            ("simulated (dry-run)" if self._dry_run else "sent")
                            + (f" /model {model} +" if model else "")
                            + (f" /effort {effort} +" if effort else "")
                            + f" continue → {title!r}"
                        )
                    else:
                        self.log.emit("warn",
                                      f"send failed for {title!r}; "
                                      f"will retry in {self._interval}s")
                        st.reset_utc = now  # immediate retry next tick
                        st.status = ST_PENDING
                else:
                    st.status = ST_PENDING
                continue

            # 3. No pending. Resolve status: cooldown vs idle.
            if in_cooldown and st.status != ST_SENT:
                st.status = ST_COOLDOWN
            elif st.status not in (ST_SENT, ST_FIRING):
                st.status = ST_IDLE

        # Drop closed windows, and queued row commands aimed at them (a
        # stale hwnd could be recycled by Windows for an unrelated window).
        for hwnd in list(self._states):
            if hwnd not in seen:
                del self._states[hwnd]
        self._cmd_fire_now &= seen
        self._cmd_skip &= seen

        # Snapshot for the GUI.
        self.snapshot.emit(self._make_snapshot())

    def _make_snapshot(self) -> list:
        out = []
        for st in self._states.values():
            out.append({
                "hwnd": st.hwnd,
                "title": st.title,
                "status": st.status,
                "reset_utc": st.reset_utc,
                "last_sent_utc": st.last_sent_utc,
                "retry_last_sent_utc": st.retry_last_sent_utc,
                "excluded": title_key(st.title) in self._excluded_titles,
                "model": self._model_overrides.get(title_key(st.title), ""),
                "effort": self._effort_overrides.get(title_key(st.title), ""),
            })
        # Stable ordering: retry (network down) / limit picker are the most
        # urgent, then rate-limit pending, then idle, then excluded.
        order = {ST_RETRY: 0, ST_PROMPT: 1, ST_FIRING: 2, ST_PENDING: 3,
                 ST_SENT: 4, ST_IDLE: 5, ST_COOLDOWN: 6, ST_EXCLUDED: 7}
        out.sort(key=lambda r: (order.get(r["status"], 9), r["title"].lower()))
        return out


# ===========================================================================
# Main window
# ===========================================================================

STATUS_LABEL = {
    ST_IDLE:     "Idle",
    ST_PENDING:  "⏳ Waiting",
    ST_FIRING:   "▶ Sending…",
    ST_SENT:     "✓ Sent",
    ST_COOLDOWN: "Cooldown",
    ST_EXCLUDED: "Excluded",
    ST_RETRY:    "⚠ Net retry",
    ST_PROMPT:   "⏎ Limit prompt",
}


def _current_color_scheme() -> "Qt.ColorScheme":
    """Return the OS-level color scheme (Light/Dark/Unknown).

    Falls back to Unknown when running under a Qt build that predates
    QStyleHints.colorScheme() (added in Qt 6.5). Unknown is treated as
    Light by `_make_palette`.
    """
    app = QApplication.instance()
    if app is None:
        return Qt.ColorScheme.Unknown
    try:
        return app.styleHints().colorScheme()
    except (AttributeError, RuntimeError):
        return Qt.ColorScheme.Unknown


def _make_palette(scheme: "Qt.ColorScheme") -> dict:
    """Color set tuned to the active OS color scheme.

    Cell backgrounds and log text need explicit colors because Qt's auto
    palette only adjusts widget chrome (base / window / button) — our
    hard-coded status bg + log text were pale-on-light, which collapses
    on a dark widget base. Dark mode uses deeper saturated bgs with
    near-white text so cells stay readable.
    """
    if scheme == Qt.ColorScheme.Dark:
        return {
            "status_bg": {
                ST_PENDING:  QColor("#5c4a00"),
                ST_FIRING:   QColor("#5c3500"),
                ST_SENT:     QColor("#1f4a2b"),
                ST_COOLDOWN: QColor("#3a3f44"),
                ST_EXCLUDED: QColor("#2a2d31"),
                ST_RETRY:    QColor("#5c1e1e"),
                ST_PROMPT:   QColor("#3d3160"),
            },
            "status_fg": QColor("#f1f3f5"),
            "log_fg": {
                "info": QColor("#dee2e6"),
                "warn": QColor("#ffd43b"),
                "err":  QColor("#ff8787"),
                "fire": QColor("#74c0fc"),
            },
            "dot_running": "#51cf66",
            "dot_stopped": "#868e96",
        }
    # Light or Unknown → original light palette.
    return {
        "status_bg": {
            ST_PENDING:  QColor("#fff3bf"),
            ST_FIRING:   QColor("#ffd8a8"),
            ST_SENT:     QColor("#b2f2bb"),
            ST_COOLDOWN: QColor("#dee2e6"),
            ST_EXCLUDED: QColor("#f8f9fa"),
            ST_RETRY:    QColor("#ffc9c9"),
            ST_PROMPT:   QColor("#e5dbff"),
        },
        "status_fg": QColor("#212529"),
        "log_fg": {
            "info": QColor("#212529"),
            "warn": QColor("#b07a00"),
            "err":  QColor("#c92a2a"),
            "fire": QColor("#1864ab"),
        },
        "dot_running": "#2f9e44",
        "dot_stopped": "#adb5bd",
    }


def _fmt_local(dt_utc: Optional[datetime]) -> str:
    if dt_utc is None:
        return "—"
    return dt_utc.astimezone().strftime("%H:%M %Z")


def _fmt_countdown(target_utc: Optional[datetime], now_utc: datetime) -> str:
    if target_utc is None:
        return "—"
    delta = target_utc - now_utc
    secs = int(delta.total_seconds())
    if secs <= 0:
        return "now"
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h:d}:{m:02d}:{s:02d}"
    return f"{m:d}:{s:02d}"


class Updater(QObject):
    """Background GitHub self-update worker, on its OWN QThread (separate from
    Watcher so a multi-minute download never stalls watchdog ticks).

    All network/disk work happens in the two slots; results come back to the
    main thread via signals. It never quits the app or swaps the exe — that's
    the main thread's job in MainWindow._on_update_ready.
    """

    update_available = pyqtSignal(dict)    # normalized release (carries _manual)
    no_update = pyqtSignal(str, bool)      # (current_version, manual?)
    check_failed = pyqtSignal(str, bool)   # (reason, manual?)
    progress = pyqtSignal(int)             # 0..100, or -1 = indeterminate
    download_failed = pyqtSignal(str)
    ready_to_install = pyqtSignal(str)     # path to the staged new exe

    @pyqtSlot(bool)
    def check(self, manual: bool) -> None:
        rel = updater.fetch_latest_release()
        if rel is None:
            self.check_failed.emit(
                "Could not reach GitHub (network / rate limit / no asset)",
                manual)
            return
        if updater.is_newer(rel.get("version", ""), APP_VERSION):
            rel["_manual"] = manual
            self.update_available.emit(rel)
        else:
            self.no_update.emit(APP_VERSION, manual)

    @pyqtSlot(dict)
    def download_and_stage(self, rel: dict) -> None:
        try:
            dest = updater.staged_exe_path()
            size = int(rel.get("asset_size") or 0)

            def cb(done: int, total: int) -> None:
                tot = total or size
                self.progress.emit(int(done * 100 / tot) if tot else -1)

            updater.download_asset(rel["asset_url"], dest,
                                   progress_cb=cb, total_hint=size)
            if not updater.verify_sha256(dest, rel.get("sha256")):
                try:
                    os.remove(dest)
                except OSError:
                    pass
                self.download_failed.emit("Integrity check failed (SHA256 mismatch)")
                return
            self.ready_to_install.emit(dest)
        except Exception as e:
            self.download_failed.emit(f"Download failed: {type(e).__name__}: {e}")


class MainWindow(QMainWindow):
    # Signals into the watcher (auto-connected via Qt::QueuedConnection
    # because the watcher lives on a different thread).
    sig_start = pyqtSignal()
    sig_stop = pyqtSignal()
    sig_set_interval = pyqtSignal(int)
    sig_set_buffer = pyqtSignal(int)
    sig_set_retry_interval = pyqtSignal(int)
    sig_set_dry_run = pyqtSignal(bool)
    sig_set_excluded = pyqtSignal(list)
    sig_fire_now = pyqtSignal(int)
    sig_skip = pyqtSignal(int)
    sig_exclude = pyqtSignal(int, str)
    sig_unexclude = pyqtSignal(str)
    sig_clear_cooldown = pyqtSignal(int)
    sig_set_effort_overrides = pyqtSignal(dict)
    sig_set_model_overrides = pyqtSignal(dict)
    # Into the updater (its own thread).
    sig_update_check = pyqtSignal(bool)      # manual?
    sig_update_download = pyqtSignal(dict)

    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"Auto-Continue v{APP_VERSION} · Claude Code")
        self.resize(960, 620)

        self.settings = QSettings("auto_continue", "gui")
        self._latest_snapshot: list = []
        # Must exist before _load_settings runs, because _load_settings
        # toggles widgets that fire valueChanged → _save_settings, which
        # reads this attribute. Empty list is the right default.
        self._excluded_titles: list = []
        # Per-window effort overrides keyed by stable title (see title_key).
        self._effort_overrides: dict = {}
        # Per-window model overrides, same keying.
        self._model_overrides: dict = {}
        # The release dict from the latest "update available" result, if any.
        self._pending_release: Optional[dict] = None
        # Theme-aware color set, plus a ring buffer of recent log entries
        # so they can be re-rendered when the user flips Win11 dark mode.
        # Must exist before _load_settings (which may emit log lines via
        # _apply_keep_awake) and before _build_ui (which doesn't read them
        # directly but is co-located for clarity).
        self._palette = _make_palette(_current_color_scheme())
        self._log_buffer: list = []  # list[tuple[ts, level, msg]]
        # Persistent activity log for overnight postmortems (the in-window
        # view is a 500-line ring buffer that dies with the process).
        # %LOCALAPPDATA%\auto_continue\activity.log, rotated at ~1 MB.
        self._log_path = os.path.join(
            os.environ.get("LOCALAPPDATA", os.path.expanduser("~")),
            "auto_continue", "activity.log",
        )
        try:
            os.makedirs(os.path.dirname(self._log_path), exist_ok=True)
        except OSError:
            self._log_path = None
        # Signature of the last rendered table so unchanged snapshots skip
        # the full rebuild (which would close an open dropdown every tick).
        self._last_render_sig: Optional[list] = None

        self._build_ui()
        self._build_worker()
        self._build_updater()
        self._load_settings()

        # React to OS-level theme flips. Safe to skip on older Qt builds.
        try:
            QApplication.instance().styleHints().colorSchemeChanged.connect(
                self._on_color_scheme_changed
            )
        except (AttributeError, RuntimeError):
            pass

        # Periodic 1s repaint just for the live countdown column.
        self._tick_timer = QTimer(self)
        self._tick_timer.setInterval(1000)
        self._tick_timer.timeout.connect(self._refresh_countdowns)
        self._tick_timer.start()

        # Auto-check for updates once per launch (silent unless a newer
        # version exists). Delayed ~5s so startup/worker init isn't blocked;
        # the check itself runs on the updater thread regardless.
        QTimer.singleShot(5000, lambda: self.sig_update_check.emit(False))

    # ---- UI construction -------------------------------------------------

    def _build_ui(self) -> None:
        central = QWidget()
        root = QVBoxLayout(central)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(8)

        # Header bar
        header = QHBoxLayout()

        self.status_dot = QLabel("●")
        # Initial color comes from the active palette; will be repainted
        # by _refresh_running_indicator() (called from __init__ tail and
        # whenever Start/Stop toggles or the theme flips).
        self.status_dot.setStyleSheet(
            f"color: {self._palette['dot_stopped']}; font-size: 16px;"
        )
        self.status_text = QLabel("Stopped")
        self.status_text.setStyleSheet("font-weight: bold;")

        self.start_btn = QPushButton("Start")
        self.start_btn.setMinimumWidth(80)
        self.start_btn.clicked.connect(self._toggle_running)

        self.dry_run_check = QCheckBox("Dry-run")
        self.dry_run_check.setToolTip(
            "Detect and log, but do not actually press keys."
        )
        self.dry_run_check.toggled.connect(
            lambda v: (self.sig_set_dry_run.emit(v), self._save_settings())
        )

        self.keep_awake_check = QCheckBox("Keep awake")
        self.keep_awake_check.setToolTip(
            "Prevent the system from going to sleep / Modern Standby. "
            "Required if you want auto-continue to fire while you're away "
            "from the keyboard for hours — Modern Standby kills background "
            "Python processes."
        )
        self.keep_awake_check.toggled.connect(
            lambda v: (self._apply_keep_awake(v), self._save_settings())
        )

        self.autostart_check = QCheckBox("Start on launch")
        self.autostart_check.setToolTip(
            "Begin watching automatically when the app opens — including "
            "the automatic relaunch after a self-update. Without this, "
            "protection lapses until you click Start."
        )
        self.autostart_check.toggled.connect(lambda _v: self._save_settings())

        self.interval_spin = QSpinBox()
        self.interval_spin.setRange(5, 600)
        self.interval_spin.setValue(30)
        self.interval_spin.setSuffix(" s")
        self.interval_spin.setToolTip("Polling interval")
        self.interval_spin.valueChanged.connect(
            lambda v: (self.sig_set_interval.emit(v), self._save_settings())
        )

        self.buffer_spin = QSpinBox()
        self.buffer_spin.setRange(0, 600)
        self.buffer_spin.setValue(20)
        self.buffer_spin.setSuffix(" s")
        self.buffer_spin.setToolTip("Extra delay past the reset hour")
        self.buffer_spin.valueChanged.connect(
            lambda v: (self.sig_set_buffer.emit(v), self._save_settings())
        )

        self.retry_spin = QSpinBox()
        self.retry_spin.setRange(5, 600)
        self.retry_spin.setValue(30)
        self.retry_spin.setSuffix(" s")
        self.retry_spin.setToolTip(
            "When Claude shows 'attempt 10/10' (network retries exhausted), "
            "resend 'continue' every N seconds until the connection comes "
            "back and Claude responds."
        )
        self.retry_spin.valueChanged.connect(
            lambda v: (self.sig_set_retry_interval.emit(v),
                       self._save_settings())
        )

        header.addWidget(self.status_dot)
        header.addWidget(self.status_text)
        header.addSpacing(6)
        header.addWidget(self.start_btn)
        header.addSpacing(12)
        header.addWidget(self.dry_run_check)
        header.addWidget(self.keep_awake_check)
        header.addWidget(self.autostart_check)
        header.addSpacing(12)
        header.addWidget(QLabel("poll"))
        header.addWidget(self.interval_spin)
        header.addWidget(QLabel("buffer"))
        header.addWidget(self.buffer_spin)
        header.addWidget(QLabel("retry"))
        header.addWidget(self.retry_spin)
        header.addStretch()
        self.check_updates_btn = QPushButton("Check updates")
        self.check_updates_btn.setToolTip(
            "Check GitHub for a newer Auto-Continue release")
        self.check_updates_btn.clicked.connect(
            lambda: self.sig_update_check.emit(True))
        header.addWidget(self.check_updates_btn)
        root.addLayout(header)

        # Update banner (hidden until an update is found).
        self.update_banner = QWidget()
        bl = QHBoxLayout(self.update_banner)
        bl.setContentsMargins(8, 4, 8, 4)
        self.update_label = QLabel("")
        self.update_now_btn = QPushButton("Update now")
        self.update_now_btn.clicked.connect(self._start_update_download)
        self.update_notes_btn = QPushButton("Release notes")
        self.update_notes_btn.clicked.connect(self._open_release_notes)
        self.update_close_btn = QPushButton("✕")
        self.update_close_btn.setFixedWidth(28)
        self.update_close_btn.setToolTip("Dismiss")
        self.update_close_btn.clicked.connect(
            lambda: self.update_banner.setVisible(False))
        bl.addWidget(self.update_label)
        bl.addStretch()
        bl.addWidget(self.update_now_btn)
        bl.addWidget(self.update_notes_btn)
        bl.addWidget(self.update_close_btn)
        self.update_banner.setVisible(False)
        self._apply_banner_theme()
        root.addWidget(self.update_banner)

        # Window table
        self.table = QTableWidget(0, 8)
        self.table.setHorizontalHeaderLabels(
            ["Window", "Status", "Reset", "Countdown", "Last sent",
             "Model", "Effort", "Action"]
        )
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers
        )
        self.table.setAlternatingRowColors(True)
        h = self.table.horizontalHeader()
        h.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for i in range(1, 8):
            h.setSectionResizeMode(i, QHeaderView.ResizeMode.ResizeToContents)
        root.addWidget(self.table, stretch=2)

        # Log view
        log_label = QLabel("Activity log")
        log_label.setStyleSheet("font-weight: bold; margin-top: 4px;")
        root.addWidget(log_label)

        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(500)
        self.log_view.setFont(QFont("Consolas", 9))
        root.addWidget(self.log_view, stretch=1)

        self.setCentralWidget(central)

        # System tray (optional minimize-to-tray)
        self._build_tray()

    def _build_tray(self) -> None:
        if not QSystemTrayIcon.isSystemTrayAvailable():
            self.tray = None
            return
        icon = self.style().standardIcon(QStyle.StandardPixmap.SP_ComputerIcon)
        self.tray = QSystemTrayIcon(icon, self)
        self.tray.setToolTip("Auto-Continue — Claude Code")
        self.tray.activated.connect(self._on_tray_activated)

        # Right-click context menu so the user can show the window or
        # quit the app without having to restore it first.
        menu = QMenu()
        show_act = QAction("Show window", self)
        show_act.triggered.connect(self._show_from_tray)
        check_act = QAction("Check for updates…", self)
        check_act.triggered.connect(lambda: self.sig_update_check.emit(True))
        quit_act = QAction("Quit", self)
        quit_act.triggered.connect(self._quit_from_tray)
        menu.addAction(show_act)
        menu.addAction(check_act)
        menu.addSeparator()
        menu.addAction(quit_act)
        self.tray.setContextMenu(menu)

        self.tray.show()
        # First-time tip so the user understands the app is still running
        # after they minimize it.
        self._notified_minimize_to_tray = False

    def _on_tray_activated(self, reason) -> None:
        # Single click and double click both restore. Context menu handled
        # separately by the menu itself.
        if reason in (QSystemTrayIcon.ActivationReason.Trigger,
                      QSystemTrayIcon.ActivationReason.DoubleClick):
            self._show_from_tray()

    def _show_from_tray(self) -> None:
        self.showNormal()
        self.activateWindow()
        self.raise_()

    def _quit_from_tray(self) -> None:
        # Mirrors the X-button path; closeEvent does the full shutdown.
        self.close()

    # Intercept Windows' minimize so it goes straight to the tray instead
    # of showing as a taskbar button.
    def changeEvent(self, event) -> None:
        if (event.type() == QEvent.Type.WindowStateChange
                and self.tray is not None
                and self.windowState() & Qt.WindowState.WindowMinimized):
            # Defer hide() until after Qt finishes processing the state
            # change, otherwise the window can flash visible briefly.
            QTimer.singleShot(0, self._hide_to_tray)
        super().changeEvent(event)

    def _hide_to_tray(self) -> None:
        # Restore normal state internally so the next show() doesn't pop
        # back as minimized, then hide the window entirely.
        self.setWindowState(self.windowState() & ~Qt.WindowState.WindowMinimized)
        self.hide()
        if not self._notified_minimize_to_tray:
            self._notified_minimize_to_tray = True
            self.tray.showMessage(
                "Auto-Continue",
                "Still running in the system tray. "
                "Right-click the tray icon to show or quit.",
                QSystemTrayIcon.MessageIcon.Information, 3500,
            )

    # ---- Worker plumbing -------------------------------------------------

    def _build_worker(self) -> None:
        self.worker_thread = QThread(self)
        self.worker = Watcher()
        self.worker.moveToThread(self.worker_thread)

        self.worker_thread.started.connect(self.worker.thread_started)

        # Forward UI commands → worker.
        self.sig_start.connect(self.worker.start)
        self.sig_stop.connect(self.worker.stop)
        self.sig_set_interval.connect(self.worker.set_interval)
        self.sig_set_buffer.connect(self.worker.set_buffer)
        self.sig_set_retry_interval.connect(self.worker.set_retry_interval)
        self.sig_set_dry_run.connect(self.worker.set_dry_run)
        self.sig_set_excluded.connect(self.worker.set_excluded)
        self.sig_fire_now.connect(self.worker.cmd_fire_now)
        self.sig_skip.connect(self.worker.cmd_skip)
        self.sig_exclude.connect(self.worker.cmd_exclude)
        self.sig_unexclude.connect(self.worker.cmd_unexclude)
        self.sig_clear_cooldown.connect(self.worker.cmd_clear_cooldown)
        self.sig_set_effort_overrides.connect(self.worker.set_effort_overrides)
        self.sig_set_model_overrides.connect(self.worker.set_model_overrides)

        # Receive snapshots and logs.
        self.worker.snapshot.connect(self._on_snapshot)
        self.worker.log.connect(self._append_log)
        self.worker.running_changed.connect(self._on_running_changed)

        self.worker_thread.start()

    # ---- Updater plumbing -----------------------------------------------

    def _build_updater(self) -> None:
        self.updater_thread = QThread(self)
        self.updater = Updater()
        self.updater.moveToThread(self.updater_thread)
        # MainWindow -> updater
        self.sig_update_check.connect(self.updater.check)
        self.sig_update_download.connect(self.updater.download_and_stage)
        # updater -> MainWindow
        self.updater.update_available.connect(self._on_update_available)
        self.updater.no_update.connect(self._on_no_update)
        self.updater.check_failed.connect(self._on_update_check_failed)
        self.updater.progress.connect(self._on_update_progress)
        self.updater.download_failed.connect(self._on_update_download_failed)
        self.updater.ready_to_install.connect(self._on_update_ready)
        self.updater_thread.start()

    def _apply_banner_theme(self) -> None:
        dark = _current_color_scheme() == Qt.ColorScheme.Dark
        bg, fg = ("#5c4a00", "#fff3bf") if dark else ("#fff3bf", "#5c4a00")
        self.update_banner.setStyleSheet(
            f"background:{bg}; border-radius:4px;")
        self.update_label.setStyleSheet(f"color:{fg}; font-weight:bold;")

    # ---- Update flow -----------------------------------------------------

    @pyqtSlot(dict)
    def _on_update_available(self, rel: dict) -> None:
        self._pending_release = rel
        ver = rel.get("version", "?")
        self.update_label.setText(f"🔄  New version v{ver} available")
        self.update_now_btn.setVisible(True)
        self.update_notes_btn.setVisible(True)
        frozen = updater.is_frozen()
        self.update_now_btn.setEnabled(frozen)
        self.update_now_btn.setToolTip(
            "Download, replace and restart" if frozen
            else "Run the packaged .exe to self-update (disabled in source mode)")
        self.update_banner.setVisible(True)
        self._append_log("info", f"update available: v{ver}")
        if self.tray is not None:
            self.tray.showMessage(
                "Auto-Continue update",
                f"Version {ver} is available. Open the window to update.",
                QSystemTrayIcon.MessageIcon.Information, 6000)

    @pyqtSlot(str, bool)
    def _on_no_update(self, cur: str, manual: bool) -> None:
        self._append_log("info", f"up to date (v{cur})")
        if manual and self.tray is not None:
            self.tray.showMessage(
                "Auto-Continue",
                f"You're on the latest version (v{cur}).",
                QSystemTrayIcon.MessageIcon.Information, 4000)

    @pyqtSlot(str, bool)
    def _on_update_check_failed(self, reason: str, manual: bool) -> None:
        self._append_log("warn", f"update check failed: {reason}")
        if manual and self.tray is not None:
            self.tray.showMessage(
                "Auto-Continue", f"Update check failed: {reason}",
                QSystemTrayIcon.MessageIcon.Warning, 4000)

    def _start_update_download(self) -> None:
        if not self._pending_release:
            return
        ver = self._pending_release.get("version", "?")
        self.update_now_btn.setEnabled(False)
        self.update_notes_btn.setVisible(False)
        self.update_label.setText(f"Downloading v{ver}…")
        self._append_log("info", f"downloading v{ver}…")
        if not self._pending_release.get("sha256"):
            # Surface the silent best-effort branch of verify_sha256: with
            # no published digest the download is only protected by HTTPS.
            self._append_log(
                "warn",
                "release has no sha256 digest — integrity check "
                "will be skipped"
            )
        self.sig_update_download.emit(dict(self._pending_release))

    @pyqtSlot(int)
    def _on_update_progress(self, pct: int) -> None:
        ver = (self._pending_release or {}).get("version", "?")
        if pct < 0:
            self.update_label.setText(f"Downloading v{ver}…")
        else:
            self.update_label.setText(f"Downloading v{ver}…  {pct}%")

    @pyqtSlot(str)
    def _on_update_download_failed(self, reason: str) -> None:
        self._append_log("err", reason)
        # Revert the banner to the actionable state so the user can retry.
        if self._pending_release:
            self._on_update_available(self._pending_release)

    @pyqtSlot(str)
    def _on_update_ready(self, path: str) -> None:
        ver = (self._pending_release or {}).get("version", "?")
        if not updater.is_frozen():
            self._append_log(
                "info", f"dev mode — staged + verified at {path} (skip swap)")
            self.update_label.setText(f"v{ver} downloaded (dev mode, no swap)")
            return
        resp = QMessageBox.question(
            self, "Update Auto-Continue",
            f"Update to v{ver} and restart now?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes)
        if resp != QMessageBox.StandardButton.Yes:
            # User declined — restore the actionable banner.
            if self._pending_release:
                self._on_update_available(self._pending_release)
            return
        try:
            self._append_log("info", f"installing v{ver} and restarting…")
            # Launch the detached swapper FIRST — if writing/launching the
            # swap .bat fails we bail out here with the watcher threads
            # still alive, instead of a dead-but-"Running" app.
            updater.stage_and_swap(path)
            self.sig_stop.emit()
            self._save_settings()
            if self.keep_awake_check.isChecked():
                self._apply_keep_awake(False)
            self._shutdown_thread(self.worker_thread)
            self._shutdown_thread(self.updater_thread)
            QApplication.instance().quit()          # clean exit -> lock drops
        except Exception as e:
            self._append_log("err", f"install failed: {type(e).__name__}: {e}")
            if self._pending_release:
                self._on_update_available(self._pending_release)

    def _open_release_notes(self) -> None:
        rel = self._pending_release or {}
        url = rel.get("html_url")
        if url:
            QDesktopServices.openUrl(QUrl(url))

    # ---- Settings persistence -------------------------------------------

    def _settings_int(self, key: str, default: int) -> int:
        """int(QSettings.value(...)) that survives a corrupt registry value
        instead of killing the (windowed, console-less) exe at startup."""
        try:
            return int(self.settings.value(key, default))
        except (TypeError, ValueError):
            return default

    def _load_settings(self) -> None:
        # Block widget signals so that programmatically populating the
        # controls doesn't trigger a chain of valueChanged → _save_settings
        # → sig_set_* before the worker is even ready.
        widgets = [self.interval_spin, self.buffer_spin, self.retry_spin,
                   self.dry_run_check, self.keep_awake_check,
                   self.autostart_check]
        for w in widgets:
            w.blockSignals(True)
        try:
            self.interval_spin.setValue(self._settings_int("interval", 30))
            self.buffer_spin.setValue(self._settings_int("buffer", 20))
            self.retry_spin.setValue(
                self._settings_int("retry_interval", 30)
            )
            self.dry_run_check.setChecked(
                self.settings.value("dry_run", False, type=bool)
            )
            self.keep_awake_check.setChecked(
                self.settings.value("keep_awake", False, type=bool)
            )
            self.autostart_check.setChecked(
                self.settings.value("autostart", True, type=bool)
            )
            excl = self.settings.value("excluded", [], type=list) or []
            # Normalize through title_key — migrates raw titles persisted
            # by older versions (exclusion used to break when the WT
            # spinner glyph changed).
            self._excluded_titles = sorted(
                {title_key(str(t)) for t in excl if title_key(str(t))}
            )
            # QSettings can't roundtrip arbitrary dicts, so we serialize
            # effort overrides as JSON.
            import json
            raw = self.settings.value("effort_overrides", "", type=str) or ""
            try:
                self._effort_overrides = json.loads(raw) if raw else {}
            except Exception:
                self._effort_overrides = {}
            raw_m = self.settings.value("model_overrides", "", type=str) or ""
            try:
                self._model_overrides = json.loads(raw_m) if raw_m else {}
            except Exception:
                self._model_overrides = {}
        finally:
            for w in widgets:
                w.blockSignals(False)

        # Now push the final config to the worker exactly once. Always emit
        # COPIES: a queued connection shares the same Python object across
        # threads, and the worker iterating a dict/list the GUI thread later
        # mutates would raise mid-slot.
        self.sig_set_interval.emit(self.interval_spin.value())
        self.sig_set_buffer.emit(self.buffer_spin.value())
        self.sig_set_retry_interval.emit(self.retry_spin.value())
        self.sig_set_dry_run.emit(self.dry_run_check.isChecked())
        self.sig_set_excluded.emit(list(self._excluded_titles))
        self.sig_set_effort_overrides.emit(dict(self._effort_overrides))
        self.sig_set_model_overrides.emit(dict(self._model_overrides))
        # Apply keep-awake state immediately if the user had it ON before.
        if self.keep_awake_check.isChecked():
            self._apply_keep_awake(True)
        # Resume watching automatically (also covers the relaunch after a
        # self-update — protection must not silently lapse).
        if self.autostart_check.isChecked():
            self.sig_start.emit()

    def _save_settings(self) -> None:
        import json
        self.settings.setValue("interval", self.interval_spin.value())
        self.settings.setValue("buffer", self.buffer_spin.value())
        self.settings.setValue("retry_interval", self.retry_spin.value())
        self.settings.setValue("dry_run", self.dry_run_check.isChecked())
        self.settings.setValue("keep_awake", self.keep_awake_check.isChecked())
        self.settings.setValue("autostart", self.autostart_check.isChecked())
        self.settings.setValue("excluded", list(self._excluded_titles))
        self.settings.setValue(
            "effort_overrides", json.dumps(self._effort_overrides)
        )
        self.settings.setValue(
            "model_overrides", json.dumps(self._model_overrides)
        )

    # ---- Slots -----------------------------------------------------------

    @pyqtSlot()
    def _toggle_running(self) -> None:
        if self.start_btn.text() == "Start":
            self.sig_start.emit()
        else:
            self.sig_stop.emit()

    @pyqtSlot(bool)
    def _on_running_changed(self, running: bool) -> None:
        if running:
            self.status_text.setText("Running")
            self.start_btn.setText("Stop")
        else:
            self.status_text.setText("Stopped")
            self.start_btn.setText("Start")
        self._refresh_running_indicator()

    def _refresh_running_indicator(self) -> None:
        running = self.start_btn.text() == "Stop"
        color = (self._palette["dot_running"] if running
                 else self._palette["dot_stopped"])
        self.status_dot.setStyleSheet(f"color: {color}; font-size: 16px;")

    @pyqtSlot(list)
    def _on_snapshot(self, rows: list) -> None:
        self._latest_snapshot = rows
        # Skip the full table rebuild when nothing user-visible changed —
        # rebuilding destroys the cell widgets, which closes any model/
        # effort dropdown the user has open and resets row selection.
        # (The countdown column is repainted by its own 1s timer.)
        sig = [(r["hwnd"], r["title"], r["status"], r["reset_utc"],
                r["last_sent_utc"], r["excluded"], r["model"], r["effort"])
               for r in rows]
        if sig == self._last_render_sig:
            return
        self._last_render_sig = sig
        self._render_table()

    @pyqtSlot(str, str)
    def _append_log(self, level: str, message: str) -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        # Keep a parallel buffer (capped to match the QPlainTextEdit's
        # 500-block limit) so a theme flip can re-render past lines in
        # the new color set.
        self._log_buffer.append((ts, level, message))
        if len(self._log_buffer) > 500:
            del self._log_buffer[: len(self._log_buffer) - 500]
        self._render_log_line(ts, level, message)
        self._write_log_file(ts, level, message)
        # Tray notification on fire so the user notices even when minimized.
        if level == "fire" and self.tray is not None:
            self.tray.showMessage(
                "Auto-Continue", message,
                QSystemTrayIcon.MessageIcon.Information, 4000,
            )

    def _write_log_file(self, ts: str, level: str, message: str) -> None:
        if not self._log_path:
            return
        try:
            try:
                if os.path.getsize(self._log_path) > 1_000_000:
                    old = self._log_path + ".old"
                    if os.path.exists(old):
                        os.remove(old)
                    os.replace(self._log_path, old)
            except OSError:
                pass
            with open(self._log_path, "a", encoding="utf-8") as fh:
                fh.write(f"{datetime.now():%Y-%m-%d} {ts}  "
                         f"[{level}]  {message}\n")
        except OSError:
            pass

    def _render_log_line(self, ts: str, level: str, message: str) -> None:
        log_fg = self._palette["log_fg"]
        color = log_fg.get(level, log_fg["info"]).name()
        # Escape: window titles routinely contain '<'/'&' (the watcher's
        # own fallback title is literally '<hwnd N>'), which appendHtml
        # would otherwise parse as markup and silently strip.
        line = html.escape(f"{ts}  [{level}]  {message}")
        self.log_view.appendHtml(
            f'<span style="color:{color}">{line}</span>'
        )

    def _redraw_log_view(self) -> None:
        self.log_view.clear()
        for ts, level, msg in self._log_buffer:
            self._render_log_line(ts, level, msg)

    @pyqtSlot(Qt.ColorScheme)
    def _on_color_scheme_changed(self, scheme: "Qt.ColorScheme") -> None:
        self._palette = _make_palette(scheme)
        self._refresh_running_indicator()
        self._render_table()
        self._redraw_log_view()
        self._apply_banner_theme()

    # ---- Table rendering -------------------------------------------------

    def _render_table(self) -> None:
        rows = self._latest_snapshot
        self.table.setRowCount(len(rows))
        now = datetime.now(pytz.UTC)

        for r, row in enumerate(rows):
            title_item = QTableWidgetItem(row["title"])
            title_item.setToolTip(f"hwnd={row['hwnd']}")
            self.table.setItem(r, 0, title_item)

            status_item = QTableWidgetItem(
                STATUS_LABEL.get(row["status"], row["status"])
            )
            bg = self._palette["status_bg"].get(row["status"])
            if bg is not None:
                status_item.setBackground(bg)
                # Pair fg with bg so text stays readable on whichever
                # theme we're tracking (Qt's default text color would
                # otherwise collide with our explicit cell bg).
                status_item.setForeground(self._palette["status_fg"])
            self.table.setItem(r, 1, status_item)

            self.table.setItem(r, 2, QTableWidgetItem(
                _fmt_local(row["reset_utc"])
            ))
            self.table.setItem(r, 3, QTableWidgetItem(
                _fmt_countdown(row["reset_utc"], now)
            ))
            self.table.setItem(r, 4, QTableWidgetItem(
                _fmt_local(row["last_sent_utc"])
            ))

            # Model dropdown — sent as `/model <name>` right before the
            # effort/continue sequence. "(none)" means skip /model and leave
            # the session on whatever model it's currently using.
            model_combo = QComboBox()
            for level in MODEL_LEVELS:
                model_combo.addItem(MODEL_LABEL[level], userData=level)
            current_m = row.get("model", "")
            try:
                idx_m = MODEL_LEVELS.index(current_m)
            except ValueError:
                idx_m = 0
            model_combo.setCurrentIndex(idx_m)
            model_combo.setToolTip(
                "Auto-prefix the next continue with `/model <name>`. "
                "(none) leaves the session's model unchanged. "
                "Setting persists per window across restarts."
            )
            model_combo.currentIndexChanged.connect(
                lambda _i, t=row["title"], cb=model_combo:
                self._on_model_changed(t, cb.currentData())
            )
            self.table.setCellWidget(r, 5, model_combo)

            # Effort dropdown — sent as `/effort <level>` right before
            # `continue`. "(none)" means skip /effort and just send continue.
            effort_combo = QComboBox()
            for level in EFFORT_LEVELS:
                effort_combo.addItem(EFFORT_LABEL[level], userData=level)
            current = row.get("effort", "")
            try:
                idx = EFFORT_LEVELS.index(current)
            except ValueError:
                idx = 0
            effort_combo.setCurrentIndex(idx)
            effort_combo.setToolTip(
                "Auto-prefix the next continue with `/effort <level>`. "
                "Setting persists per window across restarts."
            )
            effort_combo.currentIndexChanged.connect(
                lambda _i, t=row["title"], cb=effort_combo:
                self._on_effort_changed(t, cb.currentData())
            )
            self.table.setCellWidget(r, 6, effort_combo)

            # Action buttons
            btn_widget = QWidget()
            hl = QHBoxLayout(btn_widget)
            hl.setContentsMargins(2, 0, 2, 0)
            hl.setSpacing(4)

            if row["excluded"]:
                un_btn = QPushButton("Include")
                un_btn.setToolTip("Resume watching this window")
                un_btn.clicked.connect(
                    lambda _, t=row["title"]: self._do_unexclude(t)
                )
                hl.addWidget(un_btn)
            else:
                now_btn = QPushButton("Now")
                now_btn.setToolTip("Send 'continue' immediately")
                now_btn.clicked.connect(
                    lambda _, h=row["hwnd"]: self.sig_fire_now.emit(h)
                )
                hl.addWidget(now_btn)

                skip_btn = QPushButton("Skip")
                skip_btn.setToolTip("Cancel pending continue for this row")
                skip_btn.setEnabled(row["status"] == ST_PENDING)
                skip_btn.clicked.connect(
                    lambda _, h=row["hwnd"]: self.sig_skip.emit(h)
                )
                hl.addWidget(skip_btn)

                ex_btn = QPushButton("Exclude")
                ex_btn.setToolTip("Stop watching this window (remembered)")
                ex_btn.clicked.connect(
                    lambda _, h=row["hwnd"], t=row["title"]:
                    self._do_exclude(h, t)
                )
                hl.addWidget(ex_btn)

                # Only relevant during cooldown — clears the suppression so
                # the next tick re-detects (useful for testing or when you
                # want to immediately catch a new limit hit after a fire).
                if row["status"] == ST_COOLDOWN:
                    cd_btn = QPushButton("Clear cooldown")
                    cd_btn.setToolTip(
                        "Forget the recent send so detection resumes now."
                    )
                    cd_btn.clicked.connect(
                        lambda _, h=row["hwnd"]:
                        self.sig_clear_cooldown.emit(h)
                    )
                    hl.addWidget(cd_btn)

            hl.addStretch()
            self.table.setCellWidget(r, 7, btn_widget)

    def _refresh_countdowns(self) -> None:
        # Lightweight repaint of column 3 only — avoids rebuilding action
        # widgets every second.
        rows = self._latest_snapshot
        if not rows:
            return
        now = datetime.now(pytz.UTC)
        for r, row in enumerate(rows):
            if r >= self.table.rowCount():
                break
            self.table.setItem(r, 3, QTableWidgetItem(
                _fmt_countdown(row["reset_utc"], now)
            ))

    # ---- Actions ---------------------------------------------------------

    def _do_exclude(self, hwnd: int, title: str) -> None:
        key = title_key(title)
        if key and key not in self._excluded_titles:
            self._excluded_titles.append(key)
            self.sig_exclude.emit(hwnd, title)
            self.sig_set_excluded.emit(list(self._excluded_titles))
            self._save_settings()
            self._append_log("info", f"excluded {key!r}")

    def _on_effort_changed(self, title: str, level: str) -> None:
        key = title_key(title)
        # Empty/none → remove the override entirely so the dict stays small.
        if level:
            self._effort_overrides[key] = level
        else:
            self._effort_overrides.pop(key, None)
        self.sig_set_effort_overrides.emit(dict(self._effort_overrides))
        self._save_settings()
        self._append_log(
            "info",
            f"effort for {title!r} set to {level or '(none)'!r}"
        )

    def _on_model_changed(self, title: str, name: str) -> None:
        key = title_key(title)
        if name:
            self._model_overrides[key] = name
        else:
            self._model_overrides.pop(key, None)
        self.sig_set_model_overrides.emit(dict(self._model_overrides))
        self._save_settings()
        self._append_log(
            "info",
            f"model for {title!r} set to {name or '(none)'!r}"
        )

    def _do_unexclude(self, title: str) -> None:
        key = title_key(title)
        if key in self._excluded_titles:
            self._excluded_titles.remove(key)
            self.sig_unexclude.emit(title)
            self.sig_set_excluded.emit(list(self._excluded_titles))
            self._save_settings()
            self._append_log("info", f"un-excluded {key!r}")

    # ---- Keep-awake (SetThreadExecutionState) ---------------------------

    # When ON we register CONTINUOUS | SYSTEM_REQUIRED | AWAYMODE_REQUIRED.
    # AWAYMODE_REQUIRED is the key piece that asks Windows to keep the
    # system in S0 working state even when the user invokes sleep — without
    # it, Modern Standby still kicks in and kills our background python.
    # When OFF we re-register only CONTINUOUS (which clears the previous
    # request, returning the system to normal sleep behavior).
    def _apply_keep_awake(self, on: bool) -> None:
        import ctypes
        ES_CONTINUOUS = 0x80000000
        ES_SYSTEM_REQUIRED = 0x00000001
        ES_AWAYMODE_REQUIRED = 0x00000040
        try:
            if on:
                flags = (ES_CONTINUOUS | ES_SYSTEM_REQUIRED
                         | ES_AWAYMODE_REQUIRED)
                rc = ctypes.windll.kernel32.SetThreadExecutionState(flags)
                if rc:
                    self._append_log(
                        "info",
                        "keep-awake ON — system will not enter sleep / "
                        "Modern Standby while this GUI runs"
                    )
                else:
                    self._append_log(
                        "warn",
                        "keep-awake request was rejected by the OS"
                    )
            else:
                ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)
                self._append_log("info",
                                 "keep-awake OFF — normal sleep allowed")
        except Exception as e:
            self._append_log("err", f"keep-awake call failed: {e}")

    # ---- Lifecycle -------------------------------------------------------

    def _shutdown_thread(self, thread: QThread) -> None:
        """quit() + generous wait; terminate as a last resort. Destroying a
        QThread object that is still running is qFatal (crash on quit) — a
        tick mid-UIA-scan or mid-send can easily outlive a short wait."""
        try:
            thread.quit()
            if not thread.wait(8000):
                thread.terminate()
                thread.wait(2000)
        except Exception:
            pass

    def closeEvent(self, event) -> None:
        # X button = full quit (per user preference). Tray-only background
        # mode is reached via the minimize button instead.
        self._save_settings()
        # Release the wake-lock so the system can sleep normally after we
        # quit. SetThreadExecutionState requests are process-scoped — the
        # OS clears them on process exit too, but doing it explicitly here
        # is cheap insurance.
        if self.keep_awake_check.isChecked():
            self._apply_keep_awake(False)
        try:
            self.sig_stop.emit()
        except Exception:
            pass
        self._shutdown_thread(self.worker_thread)
        self._shutdown_thread(self.updater_thread)
        super().closeEvent(event)
        # setQuitOnLastWindowClosed is False (so the minimize-to-tray case
        # doesn't kill the app), so the X button path needs to ask the app
        # to quit explicitly.
        QApplication.instance().quit()


# ===========================================================================

def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("Auto-Continue")
    app.setOrganizationName("auto_continue")

    # Single-instance guard: two copies would each type 'continue' into the
    # same windows (double submissions) and race each other's update staging.
    import ctypes
    kernel32 = ctypes.windll.kernel32
    global _INSTANCE_MUTEX  # keep the handle alive for the process lifetime
    _INSTANCE_MUTEX = kernel32.CreateMutexW(
        None, False, "Local\\AutoContinueGuiSingleton")
    if kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        QMessageBox.warning(
            None, "Auto-Continue",
            "Auto-Continue is already running — check the system tray.")
        return 0

    # Hiding the main window to the tray must not exit the app — the
    # watcher thread needs to keep running.
    app.setQuitOnLastWindowClosed(False)
    win = MainWindow()
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
