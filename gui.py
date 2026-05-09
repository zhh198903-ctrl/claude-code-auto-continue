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
    QEvent, QObject, QSettings, QThread, QTimer, Qt, pyqtSignal, pyqtSlot,
)
from PyQt6.QtGui import QAction, QColor, QFont
from PyQt6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QHBoxLayout,
    QHeaderView, QLabel, QMainWindow, QMenu, QPlainTextEdit, QPushButton,
    QSpinBox, QStyle, QSystemTrayIcon, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget,
)

from auto_continue import (
    LIMIT_RE, SCAN_TAIL_CHARS, find_terminal_windows, find_termcontrol,
    next_reset_datetime, parse_limit_message, read_terminal_text,
    send_continue, send_text_lines,
)


# Effort levels offered in the per-window dropdown. The first sentinel
# means "don't send /effort, just continue as-is".
EFFORT_NONE = ""
EFFORT_LEVELS = [EFFORT_NONE, "max", "xhigh", "high", "medium", "low", "auto"]
EFFORT_LABEL = {
    EFFORT_NONE: "(none)",
    "max": "max",
    "xhigh": "xhigh",
    "high": "high",
    "medium": "medium",
    "low": "low",
    "auto": "auto",
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


@dataclass
class _WState:
    """Internal per-window state, kept inside the watcher thread only."""
    hwnd: int
    title: str
    status: str = ST_IDLE
    reset_utc: Optional[datetime] = None
    last_sent_utc: Optional[datetime] = None
    sent_flash_until: Optional[datetime] = None  # show "sent" for ~5s


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
        # {max,xhigh,high,medium,low}. Missing/empty value = no override.
        self._effort_overrides: dict[str, str] = {}

        self._interval = 30
        self._buffer = 20
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

    @pyqtSlot(bool)
    def set_dry_run(self, on: bool) -> None:
        self._dry_run = bool(on)

    @pyqtSlot(list)
    def set_excluded(self, titles: list) -> None:
        self._excluded_titles = {str(t) for t in titles}

    @pyqtSlot(dict)
    def set_effort_overrides(self, overrides: dict) -> None:
        # Filter out empty / "(none)" entries so the worker only stores
        # actionable overrides.
        self._effort_overrides = {
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
        self._excluded_titles.add(title)
        self._tick_safely()

    @pyqtSlot(str)
    def cmd_unexclude(self, title: str) -> None:
        self._excluded_titles.discard(title)
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
        try:
            self._tick()
        except Exception as e:
            self.log.emit("err", f"tick error: {type(e).__name__}: {e}")

    def _tick(self) -> None:
        now = datetime.now(pytz.UTC)
        cooldown = timedelta(minutes=15)
        buffer = timedelta(seconds=self._buffer)

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

            title = w.Name or f"<hwnd {hwnd}>"
            st = self._states.setdefault(hwnd, _WState(hwnd=hwnd, title=title))
            st.title = title

            # Excluded windows never get processed.
            if title in self._excluded_titles:
                st.status = ST_EXCLUDED
                continue

            # --- per-row commands accumulated since last tick ---
            if hwnd in self._cmd_skip:
                self._cmd_skip.discard(hwnd)
                if st.reset_utc is not None:
                    self.log.emit("info",
                                  f"skipped pending continue for {title!r}")
                st.reset_utc = None
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

            # 1. Pending? Decide whether to fire.
            if st.reset_utc is not None or force_fire:
                fire_at = (st.reset_utc or now) + buffer
                if force_fire or now >= fire_at:
                    st.status = ST_FIRING
                    effort = self._effort_overrides.get(title_key(title), "")
                    lines = []
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
                        f"sending {lines} to {title!r}"
                        + (" (forced)" if force_fire else "")
                    )
                    ok = send_text_lines(w, lines, dry_run=self._dry_run)
                    if ok:
                        st.last_sent_utc = now
                        st.reset_utc = None
                        st.status = ST_SENT
                        st.sent_flash_until = now + timedelta(seconds=5)
                        self.log.emit(
                            "info",
                            ("simulated (dry-run)" if self._dry_run else "sent")
                            + (f" /effort {effort} + " if effort else " ")
                            + f"continue → {title!r}"
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

            # 2. Cooldown after recent send? Skip detection.
            if (st.last_sent_utc
                    and now - st.last_sent_utc < cooldown
                    and st.status != ST_SENT):
                st.status = ST_COOLDOWN
                continue

            # 3. Detect.
            text = read_terminal_text(w)
            if not text:
                continue
            tail = text[-SCAN_TAIL_CHARS:]
            parsed = parse_limit_message(tail)
            if not parsed:
                # Drop back to idle if we were here for any other reason.
                if st.status not in (ST_SENT, ST_FIRING):
                    st.status = ST_IDLE
                continue

            hour_12, ampm, tz_name = parsed
            try:
                reset_utc = next_reset_datetime(hour_12, ampm, tz_name)
            except Exception as e:
                self.log.emit("err", f"reset calc failed for {title!r}: {e}")
                continue
            # Avoid log spam when the same limit message stays put.
            previously_pending = st.reset_utc == reset_utc
            st.reset_utc = reset_utc
            st.status = ST_PENDING
            if not previously_pending:
                local = (reset_utc + buffer).astimezone()
                self.log.emit(
                    "info",
                    f"limit on {title!r} → resets {hour_12}{ampm} "
                    f"({tz_name}); will fire at "
                    f"{local:%Y-%m-%d %H:%M:%S %Z}"
                )

        # Drop closed windows.
        for hwnd in list(self._states):
            if hwnd not in seen:
                del self._states[hwnd]

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
                "excluded": st.title in self._excluded_titles,
                "effort": self._effort_overrides.get(title_key(st.title), ""),
            })
        # Stable ordering: pending first, then idle, then excluded.
        order = {ST_FIRING: 0, ST_PENDING: 1, ST_SENT: 2,
                 ST_IDLE: 3, ST_COOLDOWN: 4, ST_EXCLUDED: 5}
        out.sort(key=lambda r: (order.get(r["status"], 9), r["title"].lower()))
        return out


# ===========================================================================
# Main window
# ===========================================================================

# Cell colors per status.
STATUS_COLORS = {
    ST_PENDING:  QColor("#fff3bf"),  # yellow
    ST_FIRING:   QColor("#ffd8a8"),  # orange
    ST_SENT:     QColor("#b2f2bb"),  # green
    ST_COOLDOWN: QColor("#dee2e6"),  # gray
    ST_EXCLUDED: QColor("#f8f9fa"),  # very light gray
}
STATUS_LABEL = {
    ST_IDLE:     "Idle",
    ST_PENDING:  "⏳ Waiting",
    ST_FIRING:   "▶ Sending…",
    ST_SENT:     "✓ Sent",
    ST_COOLDOWN: "Cooldown",
    ST_EXCLUDED: "Excluded",
}
LOG_COLORS = {
    "info": QColor("#212529"),
    "warn": QColor("#b07a00"),
    "err":  QColor("#c92a2a"),
    "fire": QColor("#1864ab"),
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


class MainWindow(QMainWindow):
    # Signals into the watcher (auto-connected via Qt::QueuedConnection
    # because the watcher lives on a different thread).
    sig_start = pyqtSignal()
    sig_stop = pyqtSignal()
    sig_set_interval = pyqtSignal(int)
    sig_set_buffer = pyqtSignal(int)
    sig_set_dry_run = pyqtSignal(bool)
    sig_set_excluded = pyqtSignal(list)
    sig_fire_now = pyqtSignal(int)
    sig_skip = pyqtSignal(int)
    sig_exclude = pyqtSignal(int, str)
    sig_unexclude = pyqtSignal(str)
    sig_clear_cooldown = pyqtSignal(int)
    sig_set_effort_overrides = pyqtSignal(dict)

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Auto-Continue · Claude Code")
        self.resize(960, 620)

        self.settings = QSettings("auto_continue", "gui")
        self._latest_snapshot: list = []
        # Must exist before _load_settings runs, because _load_settings
        # toggles widgets that fire valueChanged → _save_settings, which
        # reads this attribute. Empty list is the right default.
        self._excluded_titles: list = []
        # Per-window effort overrides keyed by stable title (see title_key).
        self._effort_overrides: dict = {}

        self._build_ui()
        self._build_worker()
        self._load_settings()

        # Periodic 1s repaint just for the live countdown column.
        self._tick_timer = QTimer(self)
        self._tick_timer.setInterval(1000)
        self._tick_timer.timeout.connect(self._refresh_countdowns)
        self._tick_timer.start()

    # ---- UI construction -------------------------------------------------

    def _build_ui(self) -> None:
        central = QWidget()
        root = QVBoxLayout(central)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(8)

        # Header bar
        header = QHBoxLayout()

        self.status_dot = QLabel("●")
        self.status_dot.setStyleSheet("color: #adb5bd; font-size: 16px;")
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

        header.addWidget(self.status_dot)
        header.addWidget(self.status_text)
        header.addSpacing(6)
        header.addWidget(self.start_btn)
        header.addSpacing(12)
        header.addWidget(self.dry_run_check)
        header.addWidget(self.keep_awake_check)
        header.addSpacing(12)
        header.addWidget(QLabel("poll"))
        header.addWidget(self.interval_spin)
        header.addWidget(QLabel("buffer"))
        header.addWidget(self.buffer_spin)
        header.addStretch()
        root.addLayout(header)

        # Window table
        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels(
            ["Window", "Status", "Reset", "Countdown", "Last sent",
             "Effort", "Action"]
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
        for i in range(1, 7):
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
        quit_act = QAction("Quit", self)
        quit_act.triggered.connect(self._quit_from_tray)
        menu.addAction(show_act)
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
        self.sig_set_dry_run.connect(self.worker.set_dry_run)
        self.sig_set_excluded.connect(self.worker.set_excluded)
        self.sig_fire_now.connect(self.worker.cmd_fire_now)
        self.sig_skip.connect(self.worker.cmd_skip)
        self.sig_exclude.connect(self.worker.cmd_exclude)
        self.sig_unexclude.connect(self.worker.cmd_unexclude)
        self.sig_clear_cooldown.connect(self.worker.cmd_clear_cooldown)
        self.sig_set_effort_overrides.connect(self.worker.set_effort_overrides)

        # Receive snapshots and logs.
        self.worker.snapshot.connect(self._on_snapshot)
        self.worker.log.connect(self._append_log)
        self.worker.running_changed.connect(self._on_running_changed)

        self.worker_thread.start()

    # ---- Settings persistence -------------------------------------------

    def _load_settings(self) -> None:
        # Block widget signals so that programmatically populating the
        # controls doesn't trigger a chain of valueChanged → _save_settings
        # → sig_set_* before the worker is even ready.
        widgets = [self.interval_spin, self.buffer_spin, self.dry_run_check,
                   self.keep_awake_check]
        for w in widgets:
            w.blockSignals(True)
        try:
            self.interval_spin.setValue(int(self.settings.value("interval", 30)))
            self.buffer_spin.setValue(int(self.settings.value("buffer", 20)))
            self.dry_run_check.setChecked(
                self.settings.value("dry_run", False, type=bool)
            )
            self.keep_awake_check.setChecked(
                self.settings.value("keep_awake", False, type=bool)
            )
            excl = self.settings.value("excluded", [], type=list) or []
            self._excluded_titles = list(excl)
            # QSettings can't roundtrip arbitrary dicts, so we serialize
            # effort overrides as JSON.
            import json
            raw = self.settings.value("effort_overrides", "", type=str) or ""
            try:
                self._effort_overrides = json.loads(raw) if raw else {}
            except Exception:
                self._effort_overrides = {}
        finally:
            for w in widgets:
                w.blockSignals(False)

        # Now push the final config to the worker exactly once.
        self.sig_set_interval.emit(self.interval_spin.value())
        self.sig_set_buffer.emit(self.buffer_spin.value())
        self.sig_set_dry_run.emit(self.dry_run_check.isChecked())
        self.sig_set_excluded.emit(self._excluded_titles)
        self.sig_set_effort_overrides.emit(self._effort_overrides)
        # Apply keep-awake state immediately if the user had it ON before.
        if self.keep_awake_check.isChecked():
            self._apply_keep_awake(True)

    def _save_settings(self) -> None:
        import json
        self.settings.setValue("interval", self.interval_spin.value())
        self.settings.setValue("buffer", self.buffer_spin.value())
        self.settings.setValue("dry_run", self.dry_run_check.isChecked())
        self.settings.setValue("keep_awake", self.keep_awake_check.isChecked())
        self.settings.setValue("excluded", list(self._excluded_titles))
        self.settings.setValue(
            "effort_overrides", json.dumps(self._effort_overrides)
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
            self.status_dot.setStyleSheet(
                "color: #2f9e44; font-size: 16px;"
            )
            self.status_text.setText("Running")
            self.start_btn.setText("Stop")
        else:
            self.status_dot.setStyleSheet(
                "color: #adb5bd; font-size: 16px;"
            )
            self.status_text.setText("Stopped")
            self.start_btn.setText("Start")

    @pyqtSlot(list)
    def _on_snapshot(self, rows: list) -> None:
        self._latest_snapshot = rows
        self._render_table()

    @pyqtSlot(str, str)
    def _append_log(self, level: str, message: str) -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        line = f"{ts}  [{level}]  {message}"
        # Color via inline HTML so the highlight is per-line.
        color = LOG_COLORS.get(level, QColor("black")).name()
        self.log_view.appendHtml(
            f'<span style="color:{color}">{line}</span>'
        )
        # Tray notification on fire so the user notices even when minimized.
        if level == "fire" and self.tray is not None:
            self.tray.showMessage(
                "Auto-Continue", message,
                QSystemTrayIcon.MessageIcon.Information, 4000,
            )

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
            color = STATUS_COLORS.get(row["status"])
            if color is not None:
                status_item.setBackground(color)
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
            self.table.setCellWidget(r, 5, effort_combo)

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
            self.table.setCellWidget(r, 6, btn_widget)

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
        if title not in self._excluded_titles:
            self._excluded_titles.append(title)
            self.sig_exclude.emit(hwnd, title)
            self.sig_set_excluded.emit(self._excluded_titles)
            self._save_settings()
            self._append_log("info", f"excluded {title!r}")

    def _on_effort_changed(self, title: str, level: str) -> None:
        key = title_key(title)
        # Empty/none → remove the override entirely so the dict stays small.
        if level:
            self._effort_overrides[key] = level
        else:
            self._effort_overrides.pop(key, None)
        self.sig_set_effort_overrides.emit(self._effort_overrides)
        self._save_settings()
        self._append_log(
            "info",
            f"effort for {title!r} set to {level or '(none)'!r}"
        )

    def _do_unexclude(self, title: str) -> None:
        if title in self._excluded_titles:
            self._excluded_titles.remove(title)
            self.sig_unexclude.emit(title)
            self.sig_set_excluded.emit(self._excluded_titles)
            self._save_settings()
            self._append_log("info", f"un-excluded {title!r}")

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
        self.worker_thread.quit()
        self.worker_thread.wait(2000)
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
    # Hiding the main window to the tray must not exit the app — the
    # watcher thread needs to keep running.
    app.setQuitOnLastWindowClosed(False)
    win = MainWindow()
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
