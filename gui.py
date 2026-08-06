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
import re as _re
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
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog,
    QDialogButtonBox, QFormLayout, QHBoxLayout, QHeaderView, QLabel,
    QListWidget, QListWidgetItem, QMainWindow, QMenu, QMessageBox,
    QPlainTextEdit, QPushButton, QSpinBox, QStyle, QSystemTrayIcon,
    QTabWidget, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from auto_continue import (
    APP_VERSION, MAX_POST_MATCH_TAIL, NETWORK_POST_MATCH_TAIL,
    PROMPT_POST_MATCH_TAIL, SCAN_TAIL_CHARS, SWITCH_POST_MATCH_TAIL,
    TRIGGER_DEFAULTS,
    TRIGGER_SPECS, compile_trigger_patterns, find_terminal_windows,
    init_uia_thread, list_tab_titles, next_reset_datetime,
    current_model, fable_refusal_distance, parse_econnreset_stuck,
    parse_fable_picker,
    parse_fable_refusal,
    parse_limit_message,
    parse_limit_prompt, parse_oauth_expired, parse_retry_exhausted,
    parse_server_error_stuck, parse_switch_model_prompt, read_terminal_text,
    send_continue, send_keys, send_text_lines,
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
MODEL_LEVELS = [MODEL_NONE, "default", "opus", "sonnet", "haiku", "fable"]
MODEL_LABEL = {
    MODEL_NONE: "(none)",
    "default": "default",
    "opus": "opus",
    "sonnet": "sonnet",
    "haiku": "haiku",
    "fable": "fable",
}


def title_key(title: str) -> str:
    """Strip the leading WT spinner glyph + whitespace so the per-window
    settings (effort, exclusion) survive title churn while a session is
    actively running."""
    return _re.sub(r"^[^\w]+", "", title or "").strip()


# Fable recovery is an editable "step script" — one step per line:
#   plain text (e.g. /model opus, continue) → type it + Enter
#   <confirm> → wait for the "Switch model?" dialog, press Enter (= Yes)
#   <esc>     → press ESC to surface a QUEUED switch dialog — but ONLY if one
#               isn't already showing (ESC on an open dialog cancels it). Needed
#               when the session is still busy on Opus; a no-op when it's idle
#               (the dialog already popped from /model).
#   <enter>   → press a bare Enter
#   <wait> / <wait:N> → wait N seconds (bare <wait> uses the configured Delay)
# Default flow: switch to Opus (ESC if the turn is busy) + confirm + continue
# → run on Opus for <wait> seconds → switch back to Fable (ESC if busy) +
# confirm + continue. EITHER /model can queue behind a running turn and not pop
# the dialog; <esc> surfaces it, and it's skipped when a dialog is already up.
# Default flow, matched to the UI Claude Code actually shows. The safeguard
# notice comes WITH a picker whose option 1 ("Switch to <fallback>") is already
# selected, so <confirm> — one Enter — performs the whole switch. Typing
# /model is only needed to come BACK to the blocked model afterwards, and that
# path does raise the "Switch model?" dialog, hence the second <esc>/<confirm>.
DEFAULT_FABLE_STEPS = (
    "<confirm>\n"
    "<wait>\n"
    "/model fable\n"
    "<esc>\n"
    "<confirm>\n"
    "continue"
)

# The v1.0.16 default, which typed /model into the already-open picker and
# then ESC'd it away. Saved settings take precedence over the built-in
# default, so upgrading alone would leave every existing user on the broken
# script forever. If the stored steps are byte-identical to this — i.e. the
# user never edited them — they get migrated to the new default; a customised
# script is always left alone.
LEGACY_FABLE_STEPS_V1016 = (
    "/model opus\n"
    "<esc>\n"
    "<confirm>\n"
    "continue\n"
    "<wait>\n"
    "/model fable\n"
    "<esc>\n"
    "<confirm>\n"
    "continue"
)
SWITCH_SETTLE_S = 10       # if no dialog within this after a step, move on
FABLE_RETICK_MS = 6000     # fast follow-up tick while a recovery is running
# Every step is bounded. Without these a recovery can wedge a window: the
# confirm step would press Enter on every tick for as long as the dialog text
# stays in range, a <wait> would never expire while a stale network error sits
# in the tail, and a failed send would advance as if it had succeeded.
FABLE_CONFIRM_MAX_S = 30   # hard deadline for <confirm>, even after a dialog
FABLE_KEY_GAP_S = 3        # min gap between repeated Enters on one dialog
FABLE_SEND_RETRIES = 3     # re-attempts when a send can't reach foreground
FABLE_WAIT_MAX_MULT = 4    # <wait> can stretch to N× on stalls, then gives up
FABLE_STALE_RUN_S = 900    # a run older than this is abandoned, not resumed
FABLE_MAX_RUNS = 2         # consecutive recoveries before we stop retrying
# A handled notice drifts further from the tail as the session prints, so a
# match much CLOSER than the furthest we've seen is a genuinely new block.
#
# The margin is NOT cosmetic, and "distance only grows" is only true in the
# large. What we read is a live terminal viewport, not an append-only log:
# modals opening and closing rewrite the trailing rows, so the distance
# oscillates. Measured across two real recoveries on this session:
#     run 3: 732 -> 1083 -> 939 -> 981 -> 1083   (shrank 144)
#     run 4: 878 -> 1017 -> 837 -> 1017 -> 1119  (shrank 180)
# Worst real shrink was 180 chars, so 400 leaves ~2.2x headroom. Tightening
# this below ~250 would make ordinary redraws read as a fresh block and
# re-trigger the recovery on every one — do not "optimise" it without
# re-measuring against captured snapshots.
FABLE_FRESH_MARGIN = 400


def _last_model_step(steps):
    """Model name from the LAST `/model X` line in a step script, if any.

    That is what the run is meant to end on, so it is what the completion
    check compares the status bar against.
    """
    want = None
    for kind, arg in steps or ():
        if kind == "send" and str(arg).strip().lower().startswith("/model"):
            parts = str(arg).split()
            if len(parts) > 1:
                want = parts[1]
    return want


def _fable_reset(st) -> None:
    """Return a window to 'no recovery in flight'. Deliberately leaves
    `fable_handled` / `fable_runs` alone — those track whether we already
    tried for the CURRENT notice and are cleared only when it clears."""
    st.fable_step = -1
    st.fable_step_at = None
    st.fable_dlg_seen = False
    st.fable_tries = 0
    st.fable_wait_from = None
    st.fable_last_key_at = None
    st.fable_picker_used = False


def _parse_recovery_steps(text: str) -> list:
    """Parse the recovery step-script into a list of (kind, arg) tuples."""
    steps = []
    for raw in (text or "").splitlines():
        s = raw.strip()
        if not s:
            continue
        low = s.lower()
        if low == "<confirm>":
            steps.append(("confirm", None))
        elif low == "<esc>":
            steps.append(("esc", None))
        elif low == "<enter>":
            steps.append(("enter", None))
        elif low == "<wait>":
            steps.append(("wait", None))
        elif low.startswith("<wait:") and low.endswith(">"):
            try:
                steps.append(("wait", max(0, int(low[6:-1]))))
            except ValueError:
                steps.append(("wait", None))
        else:
            steps.append(("send", s))
    return steps


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
ST_FABLE = "fable"        # Recovering from a Fable safeguard block.


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
    # Multi-tab bookkeeping: background tabs can't be watched (WT exposes
    # only the active tab's content in the UIA tree). Count shown in the
    # table; warn-once flag for the log.
    tab_count: int = 1
    tabs_warned: bool = False
    # Fable refusal-recovery bookkeeping (opt-in windows). Step-script driven.
    fable_step: int = -1                          # -1 idle, else step index
    fable_step_at: Optional[datetime] = None      # when the current step began
    fable_dlg_seen: bool = False                  # confirmed a Switch dialog
    fable_handled: bool = False                   # latch (until notice clears)
    fable_tries: int = 0                          # failed sends on this step
    fable_wait_from: Optional[datetime] = None    # absolute <wait> start
    fable_last_key_at: Optional[datetime] = None  # rate-limit repeated Enter
    fable_runs: int = 0                           # recoveries since idle
    fable_notice_dist: Optional[int] = None       # tail-distance when latched
    fable_picker_used: bool = False               # picker accepted this run


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

        # Fable refusal-recovery config (opt-in per window; see AdvancedDialog).
        self._fable_enabled = False
        self._fable_delay = 180
        self._fable_steps = _parse_recovery_steps(DEFAULT_FABLE_STEPS)
        self._fable_windows: set[str] = set()
        self._fable_all_windows = True   # eligible on every watched window

        # User overrides for the detection regexes (Advanced → Triggers).
        # Only keys the user actually changed (and that validated) live here;
        # a missing key means "pass None" → the parser uses its built-in
        # default. Written on the worker thread via set_trigger_patterns.
        self._patterns: dict = {}

        # Earliest pending fast-retick, so N recovering windows still
        # produce ONE timer instead of N (which compounded geometrically).
        self._retick_at: Optional[datetime] = None

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

    @pyqtSlot(dict)
    def set_fable_config(self, cfg: dict) -> None:
        # Every field is defensively coerced: this dict round-trips through
        # QSettings as JSON, so a hand-edited or truncated store can deliver
        # any type. A bad value must fall back to the default, never raise —
        # this runs on the worker thread and an exception here would take the
        # whole watch loop down.
        was_on = self._fable_enabled
        self._fable_enabled = bool(cfg.get("enabled", False))
        try:
            self._fable_delay = max(1, int(cfg.get("delay", 180)))
        except (TypeError, ValueError):
            self._fable_delay = 180
        steps_src = cfg.get("steps")
        if not isinstance(steps_src, str) or not steps_src.strip():
            steps_src = DEFAULT_FABLE_STEPS
        self._fable_steps = _parse_recovery_steps(steps_src)
        self._fable_all_windows = bool(cfg.get("all_windows", True))
        wins = cfg.get("windows")
        if not isinstance(wins, (list, tuple, set)):
            wins = []
        self._fable_windows = {
            title_key(str(t)) for t in wins if title_key(str(t))
        }
        # A live edit can shorten the step script while a recovery is mid-run
        # (index would point past the end), and switching the feature OFF must
        # not leave a half-finished run armed to resume — hours later, into a
        # session that has long since moved on.
        n = len(self._fable_steps)
        turned_off = was_on and not self._fable_enabled
        for st in self._states.values():
            if turned_off or st.fable_step >= n:
                _fable_reset(st)

    @pyqtSlot(dict)
    def set_trigger_patterns(self, overrides: dict) -> None:
        """Install user regex overrides for the detection patterns. Invalid
        entries are rejected (the built-in default stays live) and reported —
        a trigger that silently stops matching is exactly the failure this
        feature exists to fix, so failing loud beats failing open."""
        try:
            patterns, errors = compile_trigger_patterns(overrides or {})
        except Exception as e:                    # never kill the worker
            self.log.emit("err", f"trigger patterns not applied: {e}")
            return
        self._patterns = patterns
        for msg in errors:
            self.log.emit("err", msg)
        if patterns:
            self.log.emit(
                "info",
                "custom trigger pattern(s) active: "
                + ", ".join(sorted(patterns)))

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
        # Disarm any in-flight Fable recovery. Without this a run parked on
        # <wait> resumes mid-script whenever the user presses Start again —
        # possibly hours later — and types /model + Enter into a session that
        # has moved on. Stopping must mean stopping.
        self._retick_at = None
        for st in self._states.values():
            _fable_reset(st)
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

    def _retick_soon(self, ms: int = FABLE_RETICK_MS) -> None:
        """Schedule a quick follow-up tick so a multi-step Fable recovery
        (confirm dialog → continue, timed waits) progresses in seconds rather
        than waiting a full poll interval. Runs on the worker thread's own
        event loop.

        COALESCED on purpose. This is called once per in-recovery window per
        tick, and each scheduled tick schedules more: with two windows
        recovering, one tick queues two timers, each of which queues two —
        2^k pending ticks, every one of them a full UIA scan that also
        advances the step machine. Keeping a single earliest-deadline timer
        makes the retick rate independent of how many windows are recovering.
        """
        try:
            now = datetime.now(pytz.UTC)
            target = now + timedelta(milliseconds=int(ms))
            if (self._retick_at is not None
                    and self._retick_at <= target
                    and self._retick_at >= now):
                return                       # a sooner tick is already pending
            self._retick_at = target
            QTimer.singleShot(int(ms), self._retick_fire)
        except Exception:
            pass

    def _modal_up(self, tail: str, st) -> bool:
        """True if a modal a bare Enter can accept is showing.

        The safeguard picker is only counted until this run has ACCEPTED it
        once. Its text stays in the scrollback afterwards, and at the picker's
        tail allowance that stale copy kept reading as "a modal is open" for
        the rest of the run — which made <esc> skip (so a switch queued behind
        a busy turn was never surfaced, the case <esc> exists for) and made the
        next <confirm> fire a bare Enter into a running turn. Four live
        recoveries hid this: the fallback model always finished in time, so a
        real dialog was there to accept.
        """
        if parse_switch_model_prompt(
                tail, self._patterns.get("switch_model")):
            return True
        if st.fable_picker_used:
            return False
        return parse_fable_picker(tail, self._patterns.get("fable_picker"))

    def _retick_fire(self) -> None:
        """Target of the coalescing timer. Only a tick that the retick timer
        itself triggered may clear the pending marker — clearing it at the top
        of every _tick (including the periodic poll) would let each poll queue
        a fresh self-sustaining chain, which is the accumulation the
        coalescing exists to prevent."""
        self._retick_at = None
        self._tick_safely()

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

            # 0.45. Multi-tab warning: WT exposes only the ACTIVE tab's
            # content, so Claude sessions in background tabs are invisible.
            tabs = list_tab_titles(w)
            st.tab_count = max(1, len(tabs))
            if st.tab_count > 1:
                if not st.tabs_warned:
                    others = [t for t in tabs if t != title]
                    self.log.emit(
                        "warn",
                        f"{title!r} has {st.tab_count} tabs — only the "
                        f"ACTIVE tab is watched; invisible: {others!r}. "
                        f"Open each Claude session in its own window "
                        f"(drag the tab out of the tab bar)."
                    )
                    st.tabs_warned = True
            else:
                st.tabs_warned = False

            # 0.5. Read scrollback once per tick so every detector below
            # shares the same view of the terminal.
            text = read_terminal_text(w)
            tail = text[-SCAN_TAIL_CHARS:] if text else ""
            dr = "[dry-run] " if self._dry_run else ""

            # Isolated: this is the only block driven by user-authored
            # free text (the recovery step script). An exception escaping
            # here would abort the WHOLE tick, so every window enumerated
            # after this one would silently stop being watched — with the
            # only symptom a single 'tick error' log line.
            try:
                # 0.53. Fable refusal-recovery (opt-in windows). When Fable's
                # safeguards block a turn the session stalls ON Fable. Recovery
                # runs the editable step-script (self._fable_steps): type /model,
                # confirm the "Switch model?" dialog, ESC to surface it, timed
                # waits, continue. Targeting is per-window — every send goes to
                # THIS window `w`, so a window with no notice is never switched
                # even in all-windows mode. While a recovery runs (or the notice
                # lingers, handled) the block `continue`s, so the outer continue /
                # limit / retry logic never touches a Fable-stalled window.
                _fable_ok = (self._fable_all_windows
                             or title_key(title) in self._fable_windows)
                if self._fable_enabled and _fable_ok:
                    steps = self._fable_steps
                    if 0 <= st.fable_step < len(steps):
                        # Abandon a run parked too long. A window can drop out
                        # of this block entirely (title drift, with per-window
                        # opt-in), freezing fable_step mid-script; when it
                        # drifts back hours later the run would otherwise
                        # resume and type /model + Enter into a session that
                        # moved on long ago.
                        if (st.fable_step_at is not None
                                and now - st.fable_step_at
                                >= timedelta(seconds=FABLE_STALE_RUN_S)):
                            self.log.emit(
                                "warn",
                                f"Fable-recover on {title!r}: run went stale; "
                                f"abandoning rather than resuming mid-script")
                            _fable_reset(st)
                            st.fable_handled = True
                            st.status = ST_FABLE
                            continue
                        kind, arg = steps[st.fable_step]

                        # <wait> does NOT own the window: it lets the OUTER
                        # continue / limit logic keep the fallback model unstuck
                        # (we don't duplicate that), and a network stall RESETS the
                        # countdown so the wait is N seconds of real run time.
                        if kind == "wait":
                            secs = arg if arg is not None else self._fable_delay
                            if st.fable_wait_from is None:
                                st.fable_wait_from = st.fable_step_at or now
                            stuck = bool(tail and (
                                parse_retry_exhausted(
                                    tail, self._patterns.get("retry"))
                                or parse_econnreset_stuck(
                                    tail, self._patterns.get("econnreset"))
                                or parse_server_error_stuck(
                                    tail, self._patterns.get("server_error"))))
                            # A stall resets the countdown so <wait> measures real
                            # run time — but CAPPED: a stale error line that never
                            # scrolls away would otherwise strand the session on
                            # the fallback model forever, silently.
                            capped = (now - st.fable_wait_from
                                      >= timedelta(seconds=secs
                                                   * FABLE_WAIT_MAX_MULT))
                            if stuck and not capped:
                                st.fable_step_at = now   # reset; outer resends continue
                            elif (capped
                                  or (st.fable_step_at is not None
                                      and now - st.fable_step_at
                                      >= timedelta(seconds=secs))):
                                if capped and stuck:
                                    self.log.emit(
                                        "warn",
                                        f"Fable-recover on {title!r}: wait capped "
                                        f"after repeated stalls; moving on")
                                st.fable_step += 1        # wait done → next step
                                st.fable_step_at = now
                                st.fable_dlg_seen = False
                                st.fable_wait_from = None
                                st.fable_tries = 0
                                st.status = ST_FABLE
                                self._retick_soon()
                                continue
                            st.status = ST_FABLE
                            self._retick_soon(15000)
                            # Fall through → the outer network/limit handlers keep
                            # the FALLBACK model unstuck during the wait (we don't
                            # duplicate that logic). They may overwrite `status`;
                            # that's cosmetic and `fable_step` still owns the run.

                        else:
                            # send / enter / esc / confirm own the window (skip outer).
                            # Every send is CHECKED: send_text_lines / send_keys
                            # return False when they can't bring the window to the
                            # foreground (the guard against typing into whatever
                            # the user is using). Advancing on a failed send is how
                            # you end up pressing ESC with no dialog open and then
                            # 'continue'-ing on the model you meant to leave.
                            advance = False
                            failed = False
                            if kind == "send":
                                self.log.emit(
                                    "fire", f"{dr}Fable-recover → {title!r}: {arg!r}")
                                if send_text_lines(w, [arg], dry_run=self._dry_run):
                                    advance = True
                                else:
                                    failed = True
                            elif kind == "enter":
                                if send_text_lines(w, [""], dry_run=self._dry_run):
                                    advance = True
                                else:
                                    failed = True
                            elif kind == "esc":
                                if tail and self._modal_up(tail, st):
                                    # Dialog already up — ESC would CANCEL it. Skip;
                                    # the next <confirm> accepts the showing dialog.
                                    advance = True
                                elif (st.fable_step_at is not None
                                      and now - st.fable_step_at
                                      >= timedelta(seconds=4)):
                                    # No dialog after a brief settle → the switch is
                                    # queued behind a busy turn; ESC surfaces it.
                                    self.log.emit(
                                        "fire",
                                        f"{dr}Fable-recover → {title!r}: ESC "
                                        f"(surface switch dialog)")
                                    if send_keys(w, "{Esc}", dry_run=self._dry_run):
                                        advance = True
                                    else:
                                        failed = True
                                # else: wait a moment for an idle dialog to appear
                            elif kind == "confirm":
                                # Either confirmable modal counts: the
                                # safeguard picker (Enter = "Switch to
                                # <fallback>", which IS the whole recovery) or
                                # the /model "Switch model?" Yes/No dialog.
                                dlg_up = bool(tail and self._modal_up(tail, st))
                                # A SHOWING modal is confirmed no matter how
                                # long this step has waited — the timeouts
                                # below only decide when to give up on a modal
                                # that never appeared. Checking overdue first
                                # was a live failure: at a 60s poll with a
                                # fixed 30s deadline, the first tick to reach
                                # this step was already overdue, so it advanced
                                # without ever looking at the open picker. The
                                # modal just sat there and the recovery
                                # silently did nothing.
                                # The deadline is also poll-relative now, so a
                                # slow poll can't expire it before a tick lands.
                                confirm_max = max(FABLE_CONFIRM_MAX_S,
                                                  2 * self._interval)
                                overdue = (
                                    st.fable_step_at is not None
                                    and now - st.fable_step_at
                                    >= timedelta(seconds=confirm_max))
                                if st.fable_dlg_seen:
                                    # EXACTLY ONE Enter per dialog, ever. The
                                    # confirmed modal's text STAYS in the
                                    # scrollback, so "the pattern still
                                    # matches" is not evidence the modal is
                                    # still open — measured live, re-pressing
                                    # on that signal fired 8 Enters for one
                                    # dialog, and every extra Enter submits
                                    # whatever sits in the user's input box as
                                    # a prompt. Settle briefly, then move on.
                                    if (st.fable_last_key_at is not None
                                            and now - st.fable_last_key_at
                                            >= timedelta(
                                                seconds=FABLE_KEY_GAP_S)):
                                        advance = True
                                elif dlg_up:
                                    self.log.emit(
                                        "fire",
                                        f"{dr}Fable-recover: confirming Switch-model "
                                        f"(Yes) → {title!r}")
                                    if send_text_lines(w, [""],
                                                       dry_run=self._dry_run):
                                        st.fable_dlg_seen = True
                                        st.fable_last_key_at = now
                                        # If this Enter went to the picker (no
                                        # switch dialog present), it is spent:
                                        # its text lingers and must not read
                                        # as an open modal again this run.
                                        if not parse_switch_model_prompt(
                                                tail,
                                                self._patterns.get(
                                                    "switch_model")):
                                            st.fable_picker_used = True
                                    else:
                                        failed = True
                                elif (overdue
                                      or (st.fable_step_at is not None
                                          and now - st.fable_step_at
                                          >= timedelta(
                                              seconds=SWITCH_SETTLE_S))):
                                    advance = True      # no dialog appeared
                            else:
                                advance = True          # unknown step → skip

                            if failed:
                                # Retry the same step next tick, bounded so a
                                # window we can never focus doesn't spin forever.
                                st.fable_tries += 1
                                if st.fable_tries >= FABLE_SEND_RETRIES:
                                    self.log.emit(
                                        "warn",
                                        f"Fable-recover on {title!r}: could not "
                                        f"send (window wouldn't come forward); "
                                        f"abandoning this recovery")
                                    _fable_reset(st)
                                    st.fable_handled = True
                            elif advance:
                                st.fable_step += 1
                                st.fable_step_at = now
                                st.fable_dlg_seen = False
                                st.fable_tries = 0
                                st.fable_wait_from = None
                                st.fable_last_key_at = None
                                if st.fable_step >= len(steps):
                                    _fable_reset(st)
                                    st.fable_handled = True
                                    # Say what it actually ended ON, and warn
                                    # when that isn't the model the last
                                    # /model step asked for. A run once
                                    # reported "done" while the session was
                                    # left on the fallback model — the failure
                                    # went unnoticed for an hour because the
                                    # log only ever said "done".
                                    ended_on = current_model(tail)
                                    wanted = _last_model_step(steps)
                                    if (wanted and ended_on
                                            and wanted.lower()
                                            not in ended_on.lower()):
                                        self.log.emit(
                                            "warn",
                                            f"Fable-recover finished on "
                                            f"{ended_on!r} but the script asked "
                                            f"for {wanted!r} → {title!r}; the "
                                            f"session was NOT switched back")
                                    else:
                                        self.log.emit(
                                            "info",
                                            f"Fable-recover done → {title!r}"
                                            + (f" (on {ended_on})"
                                               if ended_on else ""))
                            st.status = ST_FABLE
                            self._retick_soon()
                            continue

                    # Detection runs ONLY when no recovery is in flight. During a
                    # <wait> the block falls through to here, and the safeguard
                    # notice is still on screen (the fallback model has barely
                    # printed anything yet) — re-entering would restart the script
                    # from step 0, ESC-interrupting the very turn it just started,
                    # every few seconds, forever.
                    if st.fable_step < 0:
                        dist = (fable_refusal_distance(
                            tail, self._patterns.get("fable"))
                            if tail else None)
                        fable_hit = dist is not None
                        if fable_hit and st.fable_handled:
                            # The handled notice stays in scrollback for a long
                            # time (measured: tens of minutes), so "a notice is
                            # visible" can't distinguish a NEW block. Track the
                            # FURTHEST the handled notice has drifted: it only
                            # moves away as the session prints, so a match that
                            # is suddenly much closer to the tail is a fresh
                            # notice. Without this the recovery's own final
                            # 'continue' re-running the flagged message — the
                            # likeliest next event — would re-block unnoticed.
                            # NB: not named `seen` — that is the tick's set of
                            # live hwnds, and shadowing it corrupts the
                            # closed-window pruning at the end of the tick.
                            furthest = st.fable_notice_dist
                            if (furthest is not None
                                    and dist + FABLE_FRESH_MARGIN < furthest):
                                self.log.emit(
                                    "warn",
                                    f"new Fable safeguard on {title!r} while "
                                    f"the previous notice was still on screen")
                                st.fable_handled = False
                            else:
                                st.fable_notice_dist = (
                                    dist if furthest is None
                                    else max(furthest, dist))
                        if not fable_hit:
                            # Notice gone → the recovery worked. Re-arm for next time.
                            st.fable_handled = False
                            st.fable_runs = 0
                            st.fable_notice_dist = None
                        elif not st.fable_handled and steps:
                            if st.fable_runs >= FABLE_MAX_RUNS:
                                # The script ends by returning to Fable and
                                # retrying the SAME message Fable already refused,
                                # so a second refusal is expected. Retrying without
                                # bound would loop model-switches all night.
                                if not st.fable_handled:
                                    self.log.emit(
                                        "warn",
                                        f"Fable safeguard on {title!r} again after "
                                        f"{st.fable_runs} recoveries — giving up "
                                        f"on this message")
                                st.fable_handled = True
                            else:
                                st.fable_runs += 1
                                self.log.emit(
                                    "fire",
                                    f"{dr}Fable safeguard on {title!r}; running "
                                    f"recovery ({len(steps)} steps)")
                                _fable_reset(st)
                                st.fable_step = 0
                                st.fable_step_at = now
                                st.fable_notice_dist = dist
                                st.status = ST_FABLE
                                self._retick_soon()
                                continue
                        # Notice lingers but already handled: show it in the table,
                        # then FALL THROUGH. Owning the window here used to skip the
                        # limit / retry / oauth handlers below, so a session that
                        # ended on a lingering notice went permanently unwatched —
                        # the exact overnight stall this tool exists to prevent.
                        if st.fable_handled:
                            st.status = ST_FABLE

            except Exception as e:
                self.log.emit(
                    'err',
                    f'Fable-recover error on {title!r}: '
                    f'{type(e).__name__}: {e}; disabling it for this window')
                _fable_reset(st)
                st.fable_handled = True

            # 0.52. Dead-session states 'continue' can't fix: warn once.
            if tail and parse_oauth_expired(
                    tail, self._patterns.get("oauth")):
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
            if tail and parse_limit_prompt(
                    tail, self._patterns.get("limit_prompt"),
                    self._patterns.get("limit")):
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
            #   c) a server-side truncation (the "Server-error"/"Response-
            #      stalled" mid-stream wordings) — 'continue' resumes the
            #      cut-off turn
            stuck_reason = None
            if tail:
                if parse_retry_exhausted(
                        tail, self._patterns.get("retry")):
                    stuck_reason = "network retries exhausted"
                elif parse_econnreset_stuck(
                        tail, self._patterns.get("econnreset")):
                    stuck_reason = "network API error"
                elif parse_server_error_stuck(
                        tail, self._patterns.get("server_error")):
                    stuck_reason = "response truncated mid-stream"
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
                parsed = parse_limit_message(
                    tail, self._patterns.get("limit"))
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
                            and parse_limit_message(
                                tail, self._patterns.get("limit")) is None):
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
                "tabs": st.tab_count,
                "excluded": title_key(st.title) in self._excluded_titles,
                "model": self._model_overrides.get(title_key(st.title), ""),
                "effort": self._effort_overrides.get(title_key(st.title), ""),
            })
        # Stable ordering: retry (network down) / limit picker are the most
        # urgent, then rate-limit pending, then idle, then excluded.
        order = {ST_RETRY: 0, ST_PROMPT: 1, ST_FABLE: 2, ST_FIRING: 3,
                 ST_PENDING: 4, ST_SENT: 5, ST_IDLE: 6, ST_COOLDOWN: 7,
                 ST_EXCLUDED: 8}
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
    ST_FABLE:    "⇄ Fable-recover",
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
                ST_FABLE:    QColor("#0b4a4a"),
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
            ST_FABLE:    QColor("#c3fae8"),
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


# Shared named objects for single-instance coordination (session-scoped).
_MUTEX_NAME = "Local\\AutoContinueGuiSingleton"
_SHOW_EVENT_NAME = "Local\\AutoContinueShowEvent"
_ACK_EVENT_NAME = "Local\\AutoContinueAckEvent"


class _ShowListener(QThread):
    """Waits on a shared named Win32 auto-reset event and emits
    `show_requested` when a *second* launch signals it — so double-clicking
    the exe (or re-running it) surfaces the already-running window instead
    of a dead-end 'already running' popup. It also sets the ACK event so the
    second launch knows a LIVE instance handled the request (a stale/leaked
    mutex with no live owner never acks, letting the new launch take over).
    Pure ctypes; no QtNetwork (which the frozen build excludes)."""

    show_requested = pyqtSignal()

    def __init__(self, show_handle: int, ack_handle: int, parent=None):
        super().__init__(parent)
        self._h = show_handle
        self._ack = ack_handle
        self._stop = False

    def run(self) -> None:
        import ctypes
        k = ctypes.windll.kernel32
        while not self._stop:
            # 400 ms timeout so stop() is honored promptly; auto-reset event
            # clears itself when the wait succeeds (rc == WAIT_OBJECT_0 == 0).
            rc = k.WaitForSingleObject(self._h, 400)
            if rc == 0 and not self._stop:
                self.show_requested.emit()
                # Tell the waiting second instance a live primary exists.
                if self._ack:
                    k.SetEvent(self._ack)

    def stop(self) -> None:
        self._stop = True


# ===========================================================================
# "Start with Windows" — per-user boot autostart (HKCU Run key)
# ===========================================================================
# One checkbox toggles a per-user HKCU\...\Run entry that relaunches us at
# login with --minimized (straight to the tray). No admin rights needed. The
# registry entry — not QSettings — is the source of truth, so the checkbox
# stays honest even if the user removes it from Task Manager's Startup tab.

_RUN_KEY_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"
_RUN_VALUE_NAME = "Auto-Continue"
_MINIMIZED_FLAG = "--minimized"


def _autostart_command() -> str:
    """Command line Windows runs at login to bring us up minimized in tray."""
    if getattr(sys, "frozen", False):
        # PyInstaller onefile: sys.executable IS our exe.
        return f'"{sys.executable}" {_MINIMIZED_FLAG}'
    # Source checkout: relaunch the .pyw entry via pythonw (no console window).
    entry = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "Auto-Continue.pyw")
    pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    if not os.path.exists(pyw):
        pyw = sys.executable
    return f'"{pyw}" "{entry}" {_MINIMIZED_FLAG}'


def autostart_enabled() -> bool:
    """True when our HKCU Run entry currently exists."""
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY_PATH) as key:
            winreg.QueryValueEx(key, _RUN_VALUE_NAME)
        return True
    except FileNotFoundError:
        return False
    except OSError:
        return False


def set_autostart(enabled: bool) -> None:
    """Add or remove the HKCU Run entry. Raises OSError on a registry failure
    (the caller reverts the checkbox to autostart_enabled())."""
    import winreg
    if enabled:
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, _RUN_KEY_PATH, 0,
                                winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, _RUN_VALUE_NAME, 0, winreg.REG_SZ,
                              _autostart_command())
    else:
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY_PATH, 0,
                                winreg.KEY_SET_VALUE) as key:
                winreg.DeleteValue(key, _RUN_VALUE_NAME)
        except FileNotFoundError:
            pass  # nothing to remove


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
    sig_set_fable_config = pyqtSignal(dict)
    sig_set_trigger_patterns = pyqtSignal(dict)
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
        # Fable refusal-recovery config (Advanced dialog). Empty pattern =
        # use the built-in default.
        self._fable_cfg: dict = {
            "enabled": False, "all_windows": True, "delay": 180,
            "steps": DEFAULT_FABLE_STEPS, "windows": [],
        }
        # User regex overrides, keyed by TRIGGER_SPECS key. Only patterns the
        # user actually changed are stored, so a future build's improved
        # default still wins for every trigger they never touched.
        self._trigger_patterns: dict = {}
        # Set when _load_settings rewrote a stale v1.0.16 step script.
        self._migrated_fable_steps = False
        # The release dict from the latest "update available" result, if any.
        self._pending_release: Optional[dict] = None
        # Single-instance "show me" listener thread (attached in main()).
        self._show_listener: Optional[_ShowListener] = None
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
        self.start_btn.setMinimumWidth(96)
        self.start_btn.setMinimumHeight(40)
        _sbf = self.start_btn.font()
        _sbf.setBold(True)
        _sbf.setPointSize(_sbf.pointSize() + 1)
        self.start_btn.setFont(_sbf)
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

        # Boot autostart (stacked under "Start on launch" in the header).
        # Backed by the HKCU Run entry rather than QSettings.
        self.boot_check = QCheckBox("Start with Windows")
        self.boot_check.setToolTip(
            "Launch Auto-Continue automatically when you sign in to "
            "Windows, minimized to the system tray. Pair with 'Start on "
            "launch' above for hands-off protection from boot. Adds a "
            "per-user registry Run entry (no admin needed); uncheck to "
            "remove it."
        )
        self.boot_check.toggled.connect(self._on_boot_toggled)

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
        # Dry-run / Keep-awake stacked vertically as a compact pair.
        drk_box = QVBoxLayout()
        drk_box.setSpacing(2)
        drk_box.setContentsMargins(0, 0, 0, 0)
        drk_box.addWidget(self.dry_run_check)
        drk_box.addWidget(self.keep_awake_check)
        header.addLayout(drk_box)
        # Stack the boot-autostart switch directly beneath "Start on launch"
        # — both are auto-start options, so keep them visually paired.
        autostart_box = QVBoxLayout()
        autostart_box.setSpacing(2)
        autostart_box.setContentsMargins(0, 0, 0, 0)
        autostart_box.addWidget(self.autostart_check)
        autostart_box.addWidget(self.boot_check)
        header.addLayout(autostart_box)
        header.addSpacing(12)
        header.addWidget(QLabel("poll"))
        header.addWidget(self.interval_spin)
        header.addWidget(QLabel("buffer"))
        header.addWidget(self.buffer_spin)
        header.addWidget(QLabel("retry"))
        header.addWidget(self.retry_spin)
        header.addStretch()
        self.advanced_btn = QPushButton("Advanced…")
        self.advanced_btn.setToolTip(
            "Advanced settings — edit the trigger patterns that decide when "
            "auto-continue fires, and the opt-in Fable refusal-recovery")
        self.advanced_btn.clicked.connect(self._open_advanced)
        header.addWidget(self.advanced_btn)
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

        # Initial prominent Start/Stop styling + status dot.
        self._refresh_running_indicator()

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
        # Clear the minimized bit so showNormal doesn't restore-as-minimized,
        # then show + force to the foreground. Used both by the tray click
        # and by a second launch signalling us to surface (single-instance).
        self.setWindowState(
            self.windowState() & ~Qt.WindowState.WindowMinimized)
        self.show()
        self.showNormal()
        self.raise_()
        self.activateWindow()
        # Belt-and-braces native show + foreground grab. Qt's show() is a
        # no-op if its cached state already says "visible" (which desyncs
        # from the real window if anything hid it out from under Qt), and
        # activateWindow() won't steal foreground from another process on
        # Windows. A direct ShowWindow + SetForegroundWindow guarantees the
        # window actually appears and comes forward. (The signalling second
        # instance is exiting, so the foreground grab is allowed.)
        try:
            import ctypes
            hwnd = int(self.winId())
            user32 = ctypes.windll.user32
            user32.ShowWindow(hwnd, 9)   # SW_RESTORE (un-minimize)
            user32.ShowWindow(hwnd, 5)   # SW_SHOW    (force visible)
            user32.SetForegroundWindow(hwnd)
            user32.BringWindowToTop(hwnd)
        except Exception:
            pass

    @pyqtSlot()
    def _on_show_requested(self) -> None:
        self._show_from_tray()
        if self.tray is not None:
            self.tray.showMessage(
                "Auto-Continue", "Already running — window restored.",
                QSystemTrayIcon.MessageIcon.Information, 2500)

    def notify_started_in_tray(self) -> None:
        """Boot-launched straight into the tray (--minimized): keep the window
        hidden and drop a one-shot balloon so the user knows we're alive. Also
        suppresses the later first-minimize tip (this already served it)."""
        self._notified_minimize_to_tray = True
        if self.tray is not None:
            self.tray.showMessage(
                "Auto-Continue",
                "Running in the system tray. Right-click the tray icon to "
                "open the window or quit.",
                QSystemTrayIcon.MessageIcon.Information, 3500)

    def attach_show_listener(self, show_handle: int, ack_handle: int) -> None:
        """Start the single-instance 'show me' listener on the given named
        event handles (created in main())."""
        if not show_handle:
            return
        self._show_listener = _ShowListener(show_handle, ack_handle, self)
        self._show_listener.show_requested.connect(self._on_show_requested)
        self._show_listener.start()

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
        self.sig_set_fable_config.connect(self.worker.set_fable_config)
        self.sig_set_trigger_patterns.connect(
            self.worker.set_trigger_patterns)

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

    def _open_advanced(self) -> None:
        # Current watched windows (title_key -> display) for the opt-in list.
        # Persisted opt-ins not currently open are re-added inside the dialog
        # so they can still be un-ticked.
        seen = {}
        for r in self._latest_snapshot:
            k = title_key(r["title"])
            if k:
                seen[k] = r["title"]
        dlg = AdvancedDialog(self, dict(self._fable_cfg), seen,
                             dict(self._trigger_patterns))
        if dlg.exec():
            self._fable_cfg = dlg.result_config()
            self._trigger_patterns = dlg.result_patterns()
            self.sig_set_fable_config.emit(dict(self._fable_cfg))
            self.sig_set_trigger_patterns.emit(
                dict(self._trigger_patterns))
            self._save_settings()
            on = bool(self._fable_cfg.get("enabled"))
            allw = bool(self._fable_cfg.get("all_windows", True))
            scope = ("all watched windows" if allw
                     else f"{len(self._fable_cfg.get('windows', []))} window(s)")
            self._append_log(
                "info",
                "Fable-recover " + ("ON" if on else "OFF") + f"; scope: {scope}")
            if self._trigger_patterns:
                self._append_log(
                    "warn",
                    "custom trigger pattern(s): "
                    + ", ".join(sorted(self._trigger_patterns))
                    + " — built-in defaults are overridden for these")
            if on:
                self._append_log(
                    "warn",
                    "Fable-recover needs Claude Code /config → “Switch models "
                    "when a message is flagged” = false, or no notice appears")

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
                   self.autostart_check, self.boot_check]
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
            # Boot-autostart is backed by the HKCU Run entry, not QSettings —
            # read the registry so the checkbox mirrors reality even if the
            # user toggled it from Task Manager's Startup tab.
            self.boot_check.setChecked(autostart_enabled())
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
            raw_f = self.settings.value("fable_cfg", "", type=str) or ""
            try:
                loaded_f = json.loads(raw_f) if raw_f else {}
                if isinstance(loaded_f, dict):
                    self._fable_cfg.update(loaded_f)
                    # Saved settings win over the built-in default, so a user
                    # who never customised the script would stay on the
                    # v1.0.16 one — which types /model into the open picker
                    # and then ESCs it away — even after upgrading. Migrate
                    # that exact string; anything edited is left untouched.
                    if (self._fable_cfg.get("steps")
                            == LEGACY_FABLE_STEPS_V1016):
                        self._fable_cfg["steps"] = DEFAULT_FABLE_STEPS
                        self._migrated_fable_steps = True
            except Exception:
                pass
            raw_t = self.settings.value("trigger_patterns", "", type=str) or ""
            try:
                loaded_t = json.loads(raw_t) if raw_t else {}
                if isinstance(loaded_t, dict):
                    self._trigger_patterns = {
                        str(k): str(v) for k, v in loaded_t.items()
                        if isinstance(v, str) and v.strip()
                    }
            except Exception:
                self._trigger_patterns = {}
            # Migrate the v1.0.16-dev layout, where the Fable detect pattern
            # lived inside fable_cfg before all triggers became editable.
            legacy = self._fable_cfg.pop("pattern", "")
            if (isinstance(legacy, str) and legacy.strip()
                    and "fable" not in self._trigger_patterns
                    and legacy.strip() != TRIGGER_DEFAULTS.get("fable")):
                self._trigger_patterns["fable"] = legacy.strip()
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
        self.sig_set_fable_config.emit(dict(self._fable_cfg))
        self.sig_set_trigger_patterns.emit(dict(self._trigger_patterns))
        if self._migrated_fable_steps:
            self._append_log(
                "warn",
                "Fable-recover steps migrated from the v1.0.16 script "
                "(it typed /model into the picker and then ESC'd it "
                "away); the new one confirms the picker instead")
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
        self.settings.setValue("fable_cfg", json.dumps(self._fable_cfg))
        self.settings.setValue(
            "trigger_patterns", json.dumps(self._trigger_patterns))

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
        # Prominent Start/Stop button — green to start, red to stop.
        bg = "#e03131" if running else "#2f9e44"
        hover = "#f03e3e" if running else "#37b24d"
        self.start_btn.setStyleSheet(
            "QPushButton {"
            f" background:{bg}; color:white; font-weight:bold;"
            " border:none; border-radius:5px; padding:6px 16px; }"
            f"QPushButton:hover {{ background:{hover}; }}")

    @pyqtSlot(list)
    def _on_snapshot(self, rows: list) -> None:
        self._latest_snapshot = rows
        # Skip the full table rebuild when nothing user-visible changed —
        # rebuilding destroys the cell widgets, which closes any model/
        # effort dropdown the user has open and resets row selection.
        # (The countdown column is repainted by its own 1s timer.)
        sig = [(r["hwnd"], r["title"], r["status"], r["reset_utc"],
                r["last_sent_utc"], r["excluded"], r["model"], r["effort"],
                r.get("tabs", 1))
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
            tabs = row.get("tabs", 1)
            title_text = row["title"]
            if tabs > 1:
                title_text += f"   ⚠ {tabs} tabs"
            title_item = QTableWidgetItem(title_text)
            tip = f"hwnd={row['hwnd']}"
            if tabs > 1:
                tip += (f"\n⚠ This window has {tabs} tabs — only the ACTIVE"
                        f" tab is watched.\nWindows Terminal does not expose"
                        f" background tabs' content.\nDrag each Claude tab"
                        f" out into its own window for full coverage.")
            title_item.setToolTip(tip)
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

    @pyqtSlot(bool)
    def _on_boot_toggled(self, on: bool) -> None:
        # Registry write can fail (locked-down policy, AV); if it does, log it
        # and snap the checkbox back to the registry's real state so the UI
        # never claims a startup entry exists when it doesn't.
        try:
            set_autostart(on)
        except OSError as e:
            self._append_log(
                "err", f"couldn't update Windows startup entry: {e}")
            self.boot_check.blockSignals(True)
            self.boot_check.setChecked(autostart_enabled())
            self.boot_check.blockSignals(False)
            return
        self._append_log(
            "info",
            "will start with Windows, minimized to the tray" if on
            else "will no longer start with Windows")

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
        if self._show_listener is not None:
            self._show_listener.stop()
            self._show_listener.wait(1000)
        self._shutdown_thread(self.worker_thread)
        self._shutdown_thread(self.updater_thread)
        super().closeEvent(event)
        # setQuitOnLastWindowClosed is False (so the minimize-to-tray case
        # doesn't kill the app), so the X button path needs to ask the app
        # to quit explicitly.
        QApplication.instance().quit()


# ===========================================================================
# Advanced settings sub-window
# ===========================================================================

class AdvancedDialog(QDialog):
    """Advanced sub-window with two tabs:

    * **Triggers** — the regexes that decide when auto-continue fires. Editable
      because Anthropic re-words these banners without notice and a rename
      silently stops the tool from firing until a new build ships.
    * **Fable recovery** — opt-in: when Fable's safeguards block a turn (the
      session stalls on Fable), recover on a fallback model and switch back
      after a delay.
    """

    def __init__(self, parent, cfg: dict, windows: dict, patterns: dict):
        super().__init__(parent)
        self.setWindowTitle("Advanced")
        self.resize(660, 640)
        self._orig_windows = list(cfg.get("windows", []))
        outer = QVBoxLayout(self)
        tabs = QTabWidget()
        outer.addWidget(tabs, 1)

        trig_tab = QWidget()
        tabs.addTab(trig_tab, "Triggers")
        self._build_triggers_tab(trig_tab, patterns or {})

        fable_tab = QWidget()
        tabs.addTab(fable_tab, "Fable recovery")
        root = QVBoxLayout(fable_tab)

        intro = QLabel(
            "When Fable's safeguards block a turn the session stalls on "
            "Fable. For each ticked window this sends the “on detect” "
            "keys (switch off Fable and continue), waits N seconds, then sends "
            "the “switch back” keys (return to Fable)."
        )
        intro.setWordWrap(True)
        root.addWidget(intro)

        prereq = QLabel(
            "⚠ Prerequisite: in Claude Code run /config and set “Switch "
            "models when a message is flagged” to false — otherwise Claude "
            "Code auto-switches the model itself and no detectable notice "
            "appears for this to act on."
        )
        prereq.setWordWrap(True)
        prereq.setStyleSheet("font-weight: bold;")
        root.addWidget(prereq)

        form = QFormLayout()
        self.enable_chk = QCheckBox("Enable Fable refusal-recovery")
        self.enable_chk.setChecked(bool(cfg.get("enabled", False)))
        form.addRow(self.enable_chk)

        self.delay_spin = QSpinBox()
        self.delay_spin.setRange(1, 3600)
        self.delay_spin.setSuffix(" s")
        try:
            self.delay_spin.setValue(int(cfg.get("delay", 180) or 180))
        except (TypeError, ValueError):
            self.delay_spin.setValue(180)
        form.addRow("Wait for <wait> steps", self.delay_spin)
        root.addLayout(form)

        detect_hint = QLabel(
            "Detection pattern for the safeguard notice lives on the "
            "<b>Triggers</b> tab (“Fable safeguard block”).")
        detect_hint.setWordWrap(True)
        root.addWidget(detect_hint)

        root.addWidget(QLabel(
            "Recovery steps (one per line):  plain line = type it + Enter · "
            "<confirm> = wait for “Switch model?” then Yes · <esc> = ESC to "
            "surface a queued dialog (skipped if one's already up) · <wait> = "
            "wait Delay seconds"))
        _steps_src = cfg.get("steps")
        if not isinstance(_steps_src, str) or not _steps_src.strip():
            _steps_src = DEFAULT_FABLE_STEPS
        self.steps_edit = QPlainTextEdit(_steps_src)
        self.steps_edit.setFixedHeight(150)
        root.addWidget(self.steps_edit)

        self.all_windows_chk = QCheckBox(
            "Apply to all watched windows (recommended)")
        self.all_windows_chk.setChecked(bool(cfg.get("all_windows", True)))
        self.all_windows_chk.setToolTip(
            "On: recovery is eligible on every watched window — but only the "
            "window that actually shows the Fable notice is ever switched. "
            "Off: restrict to the specific windows ticked below.")
        root.addWidget(self.all_windows_chk)

        root.addWidget(QLabel("…or only these windows:"))
        self.win_list = QListWidget()
        opted = {title_key(str(t)) for t in cfg.get("windows", [])
                 if title_key(str(t))}
        rows = dict(windows)                       # title_key -> display title
        for k in opted:
            rows.setdefault(k, k + "   (not currently open)")
        self._has_rows = bool(rows)
        if not rows:
            self.win_list.addItem(QListWidgetItem(
                "(no Claude windows detected yet)"))
        else:
            for k, disp in sorted(rows.items(), key=lambda kv: kv[1].lower()):
                it = QListWidgetItem(disp)
                it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                it.setCheckState(Qt.CheckState.Checked if k in opted
                                 else Qt.CheckState.Unchecked)
                it.setData(Qt.ItemDataRole.UserRole, k)
                self.win_list.addItem(it)
        root.addWidget(self.win_list, 1)

        def _sync_list(_=None):
            self.win_list.setEnabled(
                self._has_rows and not self.all_windows_chk.isChecked())
        self.all_windows_chk.toggled.connect(_sync_list)
        _sync_list()

        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        outer.addWidget(btns)

    # ---- Triggers tab ----------------------------------------------------

    def _build_triggers_tab(self, tab, patterns: dict) -> None:
        """One editable regex per detection trigger, with a live match tester.

        Editing state is held in `self._pat_edits` (key → current source) and
        only flushed to the widget on selection change, so switching rows
        doesn't lose a half-typed pattern.
        """
        lay = QVBoxLayout(tab)
        intro = QLabel(
            "These regexes decide when auto-continue fires. Edit one if "
            "Anthropic re-words a banner and detection stops working — you "
            "don't have to wait for a new build. Matching is always "
            "case-insensitive."
        )
        intro.setWordWrap(True)
        lay.addWidget(intro)

        # Working copy: start from saved overrides, fall back to the defaults.
        self._pat_edits = {}
        for spec in TRIGGER_SPECS:
            src = str((patterns or {}).get(spec["key"], "") or "").strip()
            self._pat_edits[spec["key"]] = src or spec["default"]

        self.trig_list = QListWidget()
        self.trig_list.setFixedHeight(150)
        for spec in TRIGGER_SPECS:
            it = QListWidgetItem()
            it.setData(Qt.ItemDataRole.UserRole, spec["key"])
            self.trig_list.addItem(it)
        lay.addWidget(self.trig_list)

        self.trig_help = QLabel("")
        self.trig_help.setWordWrap(True)
        self.trig_help.setStyleSheet("color: palette(mid);")
        lay.addWidget(self.trig_help)

        self.pat_edit = QPlainTextEdit()
        self.pat_edit.setFixedHeight(96)
        self.pat_edit.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        _f = QFont("Consolas")
        _f.setStyleHint(QFont.StyleHint.Monospace)
        self.pat_edit.setFont(_f)
        lay.addWidget(self.pat_edit)

        row = QHBoxLayout()
        self.pat_status = QLabel("")
        self.pat_status.setWordWrap(True)
        row.addWidget(self.pat_status, 1)
        self.reset_one_btn = QPushButton("Reset this")
        self.reset_all_btn = QPushButton("Reset all")
        row.addWidget(self.reset_one_btn)
        row.addWidget(self.reset_all_btn)
        lay.addLayout(row)

        lay.addWidget(QLabel(
            "Test — paste terminal text here to check what it matches:"))
        self.test_edit = QPlainTextEdit()
        self.test_edit.setPlaceholderText(
            "e.g. paste the banner Claude Code printed…")
        lay.addWidget(self.test_edit, 1)
        self.test_result = QLabel("")
        self.test_result.setWordWrap(True)
        lay.addWidget(self.test_result)

        self._cur_key = None
        self.trig_list.currentRowChanged.connect(self._on_trigger_row)
        self.pat_edit.textChanged.connect(self._on_pattern_edited)
        self.test_edit.textChanged.connect(self._refresh_test)
        self.reset_one_btn.clicked.connect(self._reset_current_pattern)
        self.reset_all_btn.clicked.connect(self._reset_all_patterns)
        self._refresh_trigger_labels()
        self.trig_list.setCurrentRow(0)

    def _spec(self, key):
        for s in TRIGGER_SPECS:
            if s["key"] == key:
                return s
        return None

    def _refresh_trigger_labels(self) -> None:
        """Mark rows whose pattern differs from the built-in default."""
        for i in range(self.trig_list.count()):
            it = self.trig_list.item(i)
            key = it.data(Qt.ItemDataRole.UserRole)
            spec = self._spec(key)
            custom = self._pat_edits.get(key, "") != spec["default"]
            it.setText(spec["label"] + ("   • customised" if custom else ""))

    def _on_trigger_row(self, row: int) -> None:
        if row < 0:
            return
        key = self.trig_list.item(row).data(Qt.ItemDataRole.UserRole)
        self._cur_key = None                      # suppress the edit hook
        spec = self._spec(key)
        need = spec["groups"]
        self.trig_help.setText(
            spec["help"]
            + (f"  (needs {need} capture group(s))" if need else ""))
        self.pat_edit.setPlainText(self._pat_edits.get(key, ""))
        self._cur_key = key
        self._validate_current()

    def _on_pattern_edited(self) -> None:
        if self._cur_key is None:
            return
        self._pat_edits[self._cur_key] = self.pat_edit.toPlainText().strip()
        self._refresh_trigger_labels()
        self._validate_current()

    def _compile_current(self):
        """Compile the shown pattern → (regex|None, error_message|"")."""
        key = self._cur_key
        if key is None:
            return None, ""
        spec = self._spec(key)
        src = self._pat_edits.get(key, "").strip()
        if not src:
            return None, "empty — the built-in default will be used"
        try:
            rx = _re.compile(src, _re.IGNORECASE)
        except _re.error as e:
            return None, f"✗ invalid regex: {e}"
        if rx.groups < spec["groups"]:
            return None, (f"✗ needs {spec['groups']} capture group(s), "
                          f"found {rx.groups}")
        return rx, ""

    def _validate_current(self) -> None:
        rx, err = self._compile_current()
        if err:
            self.pat_status.setText(err)
            self.pat_status.setStyleSheet(
                "color: #e03131;" if err.startswith("✗") else "")
        else:
            spec = self._spec(self._cur_key)
            same = self._pat_edits.get(self._cur_key) == spec["default"]
            self.pat_status.setText(
                "✓ valid — built-in default" if same else "✓ valid — customised")
            self.pat_status.setStyleSheet("color: #2f9e44;")
        self._refresh_test()

    def _refresh_test(self) -> None:
        sample = self.test_edit.toPlainText()
        if not sample:
            self.test_result.setText("")
            return
        rx, err = self._compile_current()
        if rx is None:
            self.test_result.setText("(fix the pattern above to test)")
            self.test_result.setStyleSheet("color: #e03131;")
            return
        m = None
        for m in rx.finditer(sample):
            pass                                   # keep the LAST match
        if m is None:
            self.test_result.setText("✗ no match in the sample text")
            self.test_result.setStyleSheet("color: #e03131;")
            return
        shown = m.group(0)
        if len(shown) > 160:
            shown = shown[:160] + "…"
        tail = len(sample) - m.end()
        extra = ""
        # Mirror the runtime tail anchor: a match too far from the end is
        # treated as stale scrollback and would NOT fire.
        limit = {"limit": MAX_POST_MATCH_TAIL,
                 "limit_prompt": PROMPT_POST_MATCH_TAIL,
                 "switch_model": SWITCH_POST_MATCH_TAIL}.get(
                     self._cur_key, NETWORK_POST_MATCH_TAIL)
        if tail > limit:
            extra = (f"  ⚠ but it sits {tail} chars from the end (limit "
                     f"{limit}) — at runtime this counts as stale scrollback "
                     f"and would NOT fire")
        groups = ""
        if m.re.groups:
            groups = "   groups: " + ", ".join(
                repr(g) for g in m.groups())
        self.test_result.setText(f"✓ matched: {shown!r}{groups}{extra}")
        self.test_result.setStyleSheet(
            "color: #e8590c;" if extra else "color: #2f9e44;")

    def _reset_current_pattern(self) -> None:
        if self._cur_key is None:
            return
        self._pat_edits[self._cur_key] = self._spec(self._cur_key)["default"]
        self.pat_edit.setPlainText(self._pat_edits[self._cur_key])
        self._refresh_trigger_labels()

    def _reset_all_patterns(self) -> None:
        for spec in TRIGGER_SPECS:
            self._pat_edits[spec["key"]] = spec["default"]
        if self._cur_key is not None:
            self.pat_edit.setPlainText(self._pat_edits[self._cur_key])
        self._refresh_trigger_labels()

    # ---- accept / results ------------------------------------------------

    def accept(self) -> None:
        """Refuse to close on an unusable pattern — silently falling back to
        the default would leave the user believing their edit took effect."""
        bad = []
        for spec in TRIGGER_SPECS:
            src = self._pat_edits.get(spec["key"], "").strip()
            if not src or src == spec["default"]:
                continue
            try:
                rx = _re.compile(src, _re.IGNORECASE)
            except _re.error as e:
                bad.append(f"• {spec['label']}: {e}")
                continue
            if rx.groups < spec["groups"]:
                bad.append(f"• {spec['label']}: needs {spec['groups']} "
                           f"capture group(s), found {rx.groups}")
        if bad:
            QMessageBox.warning(
                self, "Invalid trigger pattern",
                "These patterns can't be used:\n\n" + "\n".join(bad)
                + "\n\nFix them on the Triggers tab, or press “Reset this”.")
            return

        # Recovery steps are typed verbatim into a terminal. Braces are key
        # syntax to the sender, so they are escaped rather than rejected —
        # but an empty script with the feature enabled is a silent no-op.
        cfg = self.result_config()
        if cfg["enabled"]:
            if not _parse_recovery_steps(cfg["steps"]):
                QMessageBox.warning(
                    self, "No recovery steps",
                    "Fable refusal-recovery is enabled but the step list is "
                    "empty, so nothing would happen.\n\nAdd steps, or untick "
                    "“Enable Fable refusal-recovery”.")
                return
            if not cfg["all_windows"] and not cfg["windows"]:
                QMessageBox.warning(
                    self, "No windows selected",
                    "Fable refusal-recovery is enabled but no windows are "
                    "ticked, so it would never run.\n\nTick at least one "
                    "window, or choose “Apply to all watched windows”.")
                return
        super().accept()

    def result_patterns(self) -> dict:
        """Only genuinely-customised patterns are persisted, so a future
        build's improved default automatically wins for untouched triggers."""
        out = {}
        for spec in TRIGGER_SPECS:
            src = self._pat_edits.get(spec["key"], "").strip()
            if src and src != spec["default"]:
                out[spec["key"]] = src
        return out

    def result_config(self) -> dict:
        if not self.win_list.isEnabled():
            wins = list(self._orig_windows)        # nothing to edit; preserve
        else:
            wins = []
            for i in range(self.win_list.count()):
                it = self.win_list.item(i)
                if (it.flags() & Qt.ItemFlag.ItemIsUserCheckable
                        and it.checkState() == Qt.CheckState.Checked):
                    k = it.data(Qt.ItemDataRole.UserRole)
                    if k:
                        wins.append(k)
        return {
            "enabled": self.enable_chk.isChecked(),
            "all_windows": self.all_windows_chk.isChecked(),
            "delay": self.delay_spin.value(),
            "steps": self.steps_edit.toPlainText(),
            "windows": wins,
        }


# ===========================================================================

def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("Auto-Continue")
    app.setOrganizationName("auto_continue")

    # Single-instance guard: two copies would each type 'continue' into the
    # same windows (double submissions) and race each other's update staging.
    #
    # use_last_error + proper HANDLE restype: reading the "already exists"
    # status via a bare kernel32.GetLastError() is unreliable (an intervening
    # ctypes/Win32 call can clobber the thread's last-error); ctypes' own
    # last-error slot is captured atomically with the call.
    import ctypes
    from ctypes import wintypes
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.CreateMutexW.restype = wintypes.HANDLE
    k.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
    k.CreateEventW.restype = wintypes.HANDLE
    k.CreateEventW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.BOOL,
                               wintypes.LPCWSTR]

    global _INSTANCE_MUTEX  # keep the handle alive for the process lifetime
    _INSTANCE_MUTEX = k.CreateMutexW(None, False, _MUTEX_NAME)
    already_exists = ctypes.get_last_error() == 183  # ERROR_ALREADY_EXISTS

    # Two shared auto-reset events. A second launch signals SHOW so the
    # running instance surfaces its window (Win11 buries tray icons in the
    # overflow flyout, making a hidden window otherwise hard to recover —
    # a dead-end 'already running' popup didn't help). The running instance
    # answers on ACK; a second launch that gets no ACK within 1.5 s concludes
    # the mutex is STALE (crashed/leaked prior owner) and takes over as the
    # primary, so a stuck mutex can never permanently brick startup.
    show_event = k.CreateEventW(None, False, False, _SHOW_EVENT_NAME)
    ack_event = k.CreateEventW(None, False, False, _ACK_EVENT_NAME)
    if already_exists:
        if ack_event:
            k.ResetEvent(ack_event)
        if show_event:
            k.SetEvent(show_event)          # ask the running instance to show
        acked = (ack_event
                 and k.WaitForSingleObject(ack_event, 1500) == 0)
        if acked:
            return 0                        # a live instance handled it
        # No ACK → stale mutex; fall through and run as the primary.

    # Hiding the main window to the tray must not exit the app — the
    # watcher thread needs to keep running.
    app.setQuitOnLastWindowClosed(False)
    win = MainWindow()
    # --minimized (used by the "Start with Windows" boot entry) brings the app
    # up straight in the tray with no window on screen. Falls back to a normal
    # minimized window if no system tray is available.
    start_minimized = any(a in ("--minimized", "--min", "--tray")
                          for a in sys.argv[1:])
    if start_minimized and win.tray is not None:
        win.notify_started_in_tray()
    elif start_minimized:
        win.showMinimized()
    else:
        win.show()
    win.attach_show_listener(show_event, ack_event)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
