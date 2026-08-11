# -*- coding: utf-8 -*-
"""Limit / retry / picker / oauth tests for gui.Watcher._tick — the loop the
shipped exe actually runs.

These scenarios used to live in test_tick.py against the CLI watcher, which
was a second copy of this logic. Deleting that copy (v2.0.5) would have left
the core limit flow with no coverage at all: test_fable.py exercises only the
model-recovery section of the tick. So the scenarios moved here, onto the loop
that ships.

Same harness as test_fable.py: stub windows, recorded sends, a controllable
clock. Exits non-zero on any failure.
"""
import os
import sys

sys.stdout.reconfigure(encoding="utf-8")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from datetime import datetime, timedelta

import pytz
from PyQt6.QtWidgets import QApplication

import auto_continue as ac
import gui

_app = QApplication.instance() or QApplication([])

failures = 0


def check(label, cond):
    global failures
    print(f"[{'OK ' if cond else 'FAIL'}] {label}")
    if not cond:
        failures += 1


class FakeDT(datetime):
    _now = None

    @classmethod
    def now(cls, tz=None):
        if tz is None:
            return cls._now.astimezone().replace(tzinfo=None)
        return cls._now.astimezone(tz)


gui.datetime = FakeDT
# The limit path computes the next reset via auto_continue.next_reset_datetime,
# which consults ITS module's clock — patching gui's alone leaves the pending
# armed against the real date.
ac.datetime = FakeDT


def set_now(dt):
    FakeDT._now = dt


def advance(secs):
    set_now(FakeDT._now + timedelta(seconds=secs))


class FakeTimer:
    @staticmethod
    def singleShot(ms, fn):
        pass


gui.QTimer = FakeTimer


class FakeWin:
    def __init__(self, hwnd, title):
        self.NativeWindowHandle = hwnd
        self.Name = title


WINDOWS = []
TEXTS = {}
SENT = []


def _send_lines(w, lines, dry_run=False):
    SENT.append((int(w.NativeWindowHandle), list(lines)))
    return True


gui.find_terminal_windows = lambda: WINDOWS
gui.read_terminal_text = lambda w: TEXTS.get(int(w.NativeWindowHandle), "")
gui.send_text_lines = _send_lines
gui.send_keys = lambda w, k, dry_run=False: True
gui.send_continue = lambda w, dry_run=False: _send_lines(w, ["continue"])
gui.list_tab_titles = lambda w: []


def new_watcher():
    w = gui.Watcher()
    w._running = True
    return w


def reset(hwnd_titles, text_by_hwnd):
    global WINDOWS, TEXTS
    WINDOWS = [FakeWin(h, t) for h, t in hwnd_titles]
    TEXTS = dict(text_by_hwnd)
    SENT.clear()


# Fixtures — de-fanged (adjacent-string splits) so displaying THIS file in a
# watched terminal can't trigger the real watchdog, same convention as every
# other file in the repo. test_selftrigger.py enforces it.
BANNER = ("You've hit your li" "mit · resets 11pm (Asia/Shanghai)\n"
          "/upgra" "de to increase your usage limit.")
RETRY_EXHAUSTED = "Retry" "ing in 0s · attempt 10/10"
ECONN = "API Error: Unable to conn" "ect to API (ECONN" "RESET)"
TRUNC = "API Error: Server error mid-resp" "onse."
OAUTH = "OAuth to" "ken has expired. Please run /log" "in."
SPIN = "  ✽ Swirling… (9s · ↓ 1k tokens)"

# 11pm Asia/Shanghai == 15:00 UTC. Start the clock well before it.
T0 = datetime(2026, 7, 26, 10, 0, 0, tzinfo=pytz.UTC)
FIRE_UTC = datetime(2026, 7, 26, 15, 0, 0, tzinfo=pytz.UTC)


# =============================================================================
print("---- A: the full limit lifecycle ----")
set_now(T0)
reset([(1, "win")], {1: BANNER})
w = new_watcher()
w._tick()
st = w._states[1]
check("A1 pending armed at 15:00 UTC", st.reset_utc == FIRE_UTC)
advance(3600)
w._tick()
check("A2 nothing sent while waiting", not SENT)
set_now(FIRE_UTC + timedelta(seconds=w._buffer + 5))
w._tick()
check("A3 fired once", SENT == [(1, ["continue"])])
check("A4 pending cleared, fired key kept",
      st.reset_utc is None and st.fired_key is not None)
SENT.clear()
# The banner lingers in scrollback; after the cooldown it must not re-arm.
advance(16 * 60)
w._tick()
check("A5 stale banner does not re-arm after cooldown",
      st.reset_utc is None and not SENT)
# Banner scrolls away -> fired key released, ready for the next real hit.
TEXTS[1] = "all clear"
w._tick()
check("A6 fired key cleared once the banner is gone", st.fired_key is None)
TEXTS[1] = BANNER
w._tick()
check("A7 a new banner re-arms", st.reset_utc is not None)

# =============================================================================
print("---- B: retry-exhausted banner pokes until recovery ----")
set_now(T0)
reset([(2, "win")], {2: RETRY_EXHAUSTED})
w = new_watcher()
w._tick()
check("B1 poke on attempt N/N", SENT == [(2, ["continue"])])
SENT.clear()
advance(w._retry_interval - 10)
w._tick()
check("B2 resend rate-limited inside the retry interval", not SENT)
advance(20)
w._tick()
check("B3 re-poke after the retry interval", SENT == [(2, ["continue"])])
SENT.clear()
TEXTS[2] = "recovered, back to work"
w._tick()
check("B4 recovery clears the retry state",
      w._states[2].retry_last_sent_utc is None and not SENT)

# =============================================================================
print("---- C: bare connection errors and truncation, gated on running ----")
for name, text in (("ECONN", ECONN), ("truncation", TRUNC)):
    set_now(T0)
    reset([(3, "win")], {3: text})
    w = new_watcher()
    w._tick()
    check(f"C1 {name}: idle stuck session is poked", SENT == [(3, ["continue"])])
    # The same banner with a live spinner after it is a session that already
    # recovered — poking it queues junk (measured: eight continues in one
    # 80-minute outage).
    set_now(T0)
    reset([(3, "win")], {3: text + "\n" + SPIN})
    w = new_watcher()
    w._tick()
    check(f"C2 {name}: a streaming session is left alone", not SENT)

# =============================================================================
print("---- D: oauth-expired never gets keystrokes ----")
set_now(T0)
reset([(4, "win")], {4: OAUTH})
w = new_watcher()
w._tick()
w._tick()
check("D1 no keystrokes for a dead session", not SENT)

# =============================================================================
print("---- E: one bad window can't blind the tick ----")
set_now(T0)


class BoomWin(FakeWin):
    @property
    def Name(self):
        raise ValueError("flaky UIA child")


reset([(6, "good")], {5: "x", 6: RETRY_EXHAUSTED})
WINDOWS.insert(0, BoomWin.__new__(BoomWin))
WINDOWS[0].NativeWindowHandle = 5
w = new_watcher()
w._tick()
check("E1 later window still processed", (6, ["continue"]) in SENT)

# ...and a window that throws MIDWAY through processing, after the tick has
# already committed to it, must not take the rest of the loop down either.
# Only the Fable block used to be guarded; everything after it ran bare, so
# one window's failure meant every window enumerated later went unwatched
# for that pass — with a single generic 'tick error' line as the symptom.
set_now(T0)


class _BoomText(str):
    """Reads fine once (so the window is adopted), then raises."""


_reads = {"n": 0}


def _flaky_read(w):
    h = int(w.NativeWindowHandle)
    if h == 8:
        _reads["n"] += 1
        raise RuntimeError("UIA read blew up mid-window")
    return TEXTS.get(h, "")


reset([(8, "bad"), (9, "good")], {9: RETRY_EXHAUSTED})
_saved_read = gui.read_terminal_text
gui.read_terminal_text = _flaky_read
LOGS_E = []
w = new_watcher()
w.log.connect(lambda k, m: LOGS_E.append((k, m)))
try:
    w._tick()
finally:
    gui.read_terminal_text = _saved_read
check("E2 a window that throws does not stop the ones after it",
      (9, ["continue"]) in SENT)
check("E3 and the failure is reported against the window that caused it",
      any(k == "err" and "tick error on" in m for k, m in LOGS_E))

# =============================================================================
print("---- F: closed windows drop their state ----")
set_now(T0)
reset([(7, "win")], {7: BANNER})
w = new_watcher()
w._tick()
check("F1 state exists while the window does", 7 in w._states)
reset([], {})
w._tick()
check("F2 state dropped once the window closes", 7 not in w._states)


# =============================================================================
print("---- G: after-finish types the follow-up when a run completes ----")
# A watched session that finishes its run sits idle until a human notices.
# The per-window after-finish prompt keeps it producing — but only a session
# that actually RAN and then went quiet counts as finished; a fresh shell at
# a prompt, a blocked session, or a fresh fire's cooldown must not qualify.
NOTICE_G = ("API Error: Fable 5's safegu" "ards flagged this message. "
            "Claude Code can't respond with Fable 5.")
IDLE = "some finished output\n> "


def af_watcher(cmd="next task: refactor the parser", loops=None):
    set_now(T0)
    reset([(9, "win")], {9: IDLE + "\n" + SPIN})
    w = new_watcher()
    w.set_after_finish({"win": cmd})
    if loops is not None:
        w.set_after_finish_loops({"win": loops})
    return w


def af_cycle(w, cmd_output=IDLE):
    """Drive one completed run: running seen -> idle -> settle -> tick."""
    TEXTS[9] = cmd_output + "\n" + SPIN
    w._tick()
    TEXTS[9] = cmd_output
    advance(60)
    w._tick()
    advance(gui.AFTER_FINISH_SETTLE_S)
    w._tick()


w = af_watcher()
w._tick()                                       # running observed
TEXTS[9] = IDLE                                 # turn ends
advance(60)
w._tick()                                       # idle #1 — starts the clock
check("G1 no fire on the first idle sighting", not SENT)
advance(gui.AFTER_FINISH_SETTLE_S)
w._tick()
check("G2 fires the configured prompt once settled",
      SENT == [(9, ["next task: refactor the parser"])])
SENT.clear()
advance(120)
w._tick()
advance(120)
w._tick()
check("G3 does not repeat while still idle", not SENT)
# The prompt started a run which finished again — but Loops counts the runs
# REMAINING and the default is 1, now spent. Typing the same follow-up after
# every run is an infinite loop unless the user asked for it.
af_cycle(w)
check("G4 the default single run is spent — no second fire", not SENT)
check("G4a and the remaining count was decremented to zero",
      w._after_finish_loops.get("win") == 0)

# Spending a run is reported so the GUI can persist it — the whole reason
# Loops counts DOWN instead of up.
w = af_watcher()
SPENT = []
w.loops_spent.connect(lambda k, n: SPENT.append((k, n)))
w._tick()
TEXTS[9] = IDLE
advance(60)
w._tick()
advance(gui.AFTER_FINISH_SETTLE_S)
w._tick()
check("G4b the decrement is announced for persistence",
      SPENT == [("win", 0)])
SENT.clear()

# ...and a restart therefore cannot re-arm it: a fresh worker loading the
# persisted 0 must leave the window alone, even though the prompt text is
# still configured. This is the failure the countdown exists to prevent —
# the window's NEXT task inheriting the previous task's follow-up.
w = af_watcher(loops=0)
AF_LOGS = []
w.log.connect(lambda k, m: AF_LOGS.append((k, m)))
for _ in range(2):
    af_cycle(w)
check("G4c a restart with 0 runs left types nothing", not SENT)
check("G4d and it says so exactly once",
      len([m for k, m in AF_LOGS if "no runs left" in m]) == 1)

# Loops = unlimited (-1) is the old behaviour: re-arm after every run, and
# never decrement.
w = af_watcher(loops=-1)
w._tick()
TEXTS[9] = IDLE
advance(60)
w._tick()
advance(gui.AFTER_FINISH_SETTLE_S)
w._tick()                                       # fire 1
SENT.clear()
af_cycle(w)                                     # fire 2
check("G4e unlimited re-arms after every completed run",
      SENT == [(9, ["next task: refactor the parser"])])
check("G4f and unlimited never counts down",
      w._after_finish_loops.get("win") == -1)
SENT.clear()

# Loops = 2: two fires, counting down, then it stops.
w = af_watcher(loops=2)
w._tick()
TEXTS[9] = IDLE
advance(60)
w._tick()
advance(gui.AFTER_FINISH_SETTLE_S)
w._tick()                                       # fire 1
check("G4g one run spent leaves one",
      w._after_finish_loops.get("win") == 1)
af_cycle(w)                                     # fire 2
check("G4h Loops=2 fires twice", len(SENT) == 2)
SENT.clear()
af_cycle(w)                                     # spent
check("G4i the third completed run gets nothing", not SENT)

# Setting Loops again re-arms the same prompt — no need to retype it.
w.set_after_finish_loops({"win": 1})
af_cycle(w)
check("G4j setting Loops again re-arms the prompt",
      SENT == [(9, ["next task: refactor the parser"])])
SENT.clear()

# Dry-run must not spend the budget: the send types nothing and still
# reports success, so counting there charged a run that never happened — and
# because the count is persisted, one dry-run pass left every configured
# prompt at zero runs for good.
w = af_watcher()
w._dry_run = True
SPENT = []
w.loops_spent.connect(lambda k, n: SPENT.append((k, n)))
w._tick()
TEXTS[9] = IDLE
advance(60)
w._tick()
advance(gui.AFTER_FINISH_SETTLE_S)
w._tick()
check("G4k dry-run leaves the remaining count alone",
      w._after_finish_loops.get("win", 1) == 1 and not SPENT)
SENT.clear()


# Never ran while watched -> never fires.
set_now(T0)
reset([(9, "win")], {9: IDLE})
w = new_watcher()
w.set_after_finish({"win": "anything"})
for _ in range(3):
    w._tick()
    advance(gui.AFTER_FINISH_SETTLE_S + 5)
check("G5 a session never seen running is left alone", not SENT)

# A standing safeguard notice is a BLOCK, not a finish.
w = af_watcher()
w._tick()
TEXTS[9] = NOTICE_G + "\n" + IDLE
advance(60)
w._tick()
advance(gui.AFTER_FINISH_SETTLE_S + 5)
w._tick()
check("G6 a blocked session is not poked", not SENT)

# No configured command -> feature entirely off for the window.
set_now(T0)
reset([(9, "win")], {9: IDLE + "\n" + SPIN})
w = new_watcher()
w._tick()
TEXTS[9] = IDLE
advance(60)
w._tick()
advance(gui.AFTER_FINISH_SETTLE_S + 5)
w._tick()
check("G7 no command, no keystrokes", not SENT)

# Sanitisation: garbage collapses instead of reaching the tick.
w = new_watcher()
w.set_after_finish({"win": "   ", "other": 42, "ok": " do things "})
check("G8 blank entries are dropped, values coerced and trimmed",
      w._after_finish == {"other": "42", "ok": "do things"})
w.set_after_finish_loops({"a": "2", "b": -5, "c": "abc", "d": 0})
check("G9 loops values are coerced; garbage dropped, unlimited clamped",
      w._after_finish_loops == {"a": 2, "b": -1, "d": 0})
w.set_after_finish_loops("not-a-dict")
check("G10 a corrupt loops store collapses to defaults",
      w._after_finish_loops == {})

# v2.0.6 stored a CAP where 0 meant unlimited; the countdown store reads 0 as
# spent. Migrating that exact value is the difference between a prompt that
# repeats forever and one that silently never fires again. These drive the
# SHIPPED function — an earlier version of this check compared a dict
# comprehension written here against itself, so a broken migration would
# have sailed through it.
check("G11 the v2.0.6 'unlimited' cap migrates to the new sentinel",
      gui.migrate_after_finish_loops({"a": 0, "b": 3, "c": 1}, False)
      == {"a": -1, "b": 3, "c": 1})
check("G11b once migrated, 0 is a real value and stays put",
      gui.migrate_after_finish_loops({"a": 0, "b": 3}, True)
      == {"a": 0, "b": 3})
check("G11c garbage and out-of-range values are coerced, not raised",
      gui.migrate_after_finish_loops(
          {"a": "2", "b": "x", "c": -9, "d": None}, True)
      == {"a": 2, "c": -1})
check("G11d a non-dict store collapses instead of exploding",
      gui.migrate_after_finish_loops("not-a-dict", True) == {}
      and gui.migrate_after_finish_loops(None, False) == {})

# The After finish dialog snapshots the remaining count when it opens, and a
# modal exec() keeps pumping queued signals — so the worker can spend a run
# while the dialog is up. Writing the untouched pre-fire number back on OK
# would silently re-arm a prompt that had just been used, which is exactly
# the repeat-firing the budget exists to prevent. (The file already carries
# the same class of fix for the fable opt-out list.)
check("G12 a run spent while the dialog was open wins over the snapshot",
      gui.reconcile_loops(chosen=1, snapshot=1, fired=0) == 0)
check("G12b but moving the spinner is an explicit instruction and wins",
      gui.reconcile_loops(chosen=5, snapshot=1, fired=0) == 5)
check("G12c with no fire in the meantime the dialog's value stands",
      gui.reconcile_loops(chosen=1, snapshot=1, fired=None) == 1)
check("G12d setting the spinner back to the snapshot after a fire re-arms",
      gui.reconcile_loops(chosen=0, snapshot=0, fired=0) == 0)
check("G12e unlimited chosen during a fire is still honoured",
      gui.reconcile_loops(chosen=-1, snapshot=1, fired=0) == -1)


# =============================================================================
print("---- H: auto-answering a chooser the session is waiting on ----")
# Claude Code stops on a chooser and waits; until someone picks, the window
# is idle for no better reason than an unanswered question. Enter accepts the
# option it already pre-selected. Fixtures are de-fanged (the marker and the
# digit split apart) so this file cannot make a watched terminal press Enter.
_M = "> "
CHOOSER = "Ready to continue?\n" + _M + "1" ". Keep going\n  2" ". Stop here"
PERMISSION = ("Do you want to proceed?\n" + _M + "1" ". Yes\n"
              "  2" ". Yes, and don" + chr(8217) + "t ask again\n"
              "  3" ". No, and tell Claude what to do differently")
# An input box with nothing typed in it. NOT what sits under an open
# chooser — a chooser replaces the box entirely while it waits, measured on
# a live prompt — so this belongs only to scenarios about the box itself.
EMPTY_BOX = "\n>" + chr(0xa0) + "   "
# What an open chooser really looks like from below: the hint, no box.
NO_BOX = "\nEnter to select - Tab/Arrow keys to navigate - Esc to cancel"
DRAFT_BOX = "\n>" + chr(0xa0) + "half a thought"

set_now(T0)
reset([(20, "win")], {20: CHOOSER + NO_BOX})
w = new_watcher()
w.set_auto_answer(True, True)          # choosers ship off; this is
                                       # a chooser scenario
w._tick()
check("H1 an ordinary chooser is answered with a bare Enter",
      SENT == [(20, [""])])
SENT.clear()
w._tick()
advance(30)
w._tick()
check("H2 the same chooser is answered exactly once", not SENT)

# The answered text lingers, so a NEW chooser must still be recognised.
TEXTS[20] = ("Ready to continue?\n" + _M + "1" ". Something else\n"
             "  2" ". No" + NO_BOX)
advance(5)
w._tick()
check("H3 a different chooser is answered", SENT == [(20, [""])])
SENT.clear()

# A draft in the input box turns Enter into "submit my half-written message".
set_now(T0)
reset([(21, "win")], {21: CHOOSER + DRAFT_BOX})
w = new_watcher()
w.set_auto_answer(True, True)
w._tick()
check("H4 nothing is pressed while the user has text typed", not SENT)

# Mid-turn there is nothing to answer.
set_now(T0)
reset([(22, "win")], {22: CHOOSER + "\n" + SPIN + NO_BOX})
w = new_watcher()
w.set_auto_answer(True, True)
w._tick()
check("H5 a running turn is left alone", not SENT)

# Permission requests authorise work, so they ride on their own switch.
set_now(T0)
reset([(23, "win")], {23: PERMISSION + NO_BOX})
w = new_watcher()
w.set_auto_answer(True, False)                  # choosers on, permission off
w._tick()
check("H6 a permission request is NOT answered by the chooser switch",
      not SENT)
w.set_auto_answer(True, True)
w._tick()
check("H7 ...and IS answered once its own switch is on",
      SENT == [(23, [""])])
SENT.clear()

set_now(T0)
reset([(24, "win")], {24: CHOOSER + NO_BOX})
w = new_watcher()
w.set_auto_answer(False, True)                  # choosers off
w._tick()
check("H8 an ordinary chooser is left alone when its switch is off",
      not SENT)

# Both default ON — a fresh worker answers without being configured.
# The two do not ship the same way, on purpose. A permission request only
# says yes to work already asked for; a chooser is where a DECISION sits —
# which option, and whether a written plan starts running — so that one is
# the owner's to switch on.
check("H9 choosers ship OFF, permission requests ship ON",
      new_watcher()._auto_choose is False
      and new_watcher()._auto_permission is True)

# The shape a real chooser actually has: no input box at all. Every fixture
# above prints one, which is what let a "no box means maybe a draft" rule
# pass the suite while refusing to act on every real chooser.
set_now(T0)
reset([(26, "win")], {26: CHOOSER + NO_BOX})
w = new_watcher()
w.set_auto_answer(True, True)
w._tick()
check("H14 a chooser that replaced the input box is still answered",
      SENT == [(26, [""])])
SENT.clear()

# Questions arrive in runs — a form asks several and then offers Submit as
# one more chooser — so after answering one, look again soon instead of
# waiting a whole poll. Measured live: a four-step form took three minutes
# at the default interval.
gui.QTimer.scheduled = []
set_now(T0)
reset([(28, "win")], {28: CHOOSER + NO_BOX})
w = new_watcher()
w.set_auto_answer(True, True)
w._tick()
check("H16 answering schedules a fast follow-up tick",
      SENT == [(28, [""])] and w._retick_at is not None)
SENT.clear()

# The safeguard picker and the "Switch model?" confirmation are choosers
# too, but confirming either CHANGES WHICH MODEL the session runs on. That
# belongs to the recovery feature, which is opt-in for exactly that reason —
# and answering out of band also puts a second Enter on a dialog the
# recovery's own <confirm> step is about to answer.
_SG2 = "safegu" "ards flagged"
_SW2 = "Switch t" "o"
_ED2 = "Edit promp" "t and retry"
PICKER2 = (f"Session paused\n\nFable 5's {_SG2} this message.\n\n"
           f"> " "1" f". {_SW2} Opus 5\n  2" f". {_ED2} with Fable 5")
DIALOG2 = ("Switch model?\n> " "1" ". Yes, swi" "tch to Opus 5\n"
           "  2" ". No, go b" "ack")

for _name, _text in (("safeguard picker", PICKER2),
                     ("switch-model dialog", DIALOG2)):
    set_now(T0)
    reset([(27, "win")], {27: _text + NO_BOX})
    w = new_watcher()
    w._tick()
    w._tick()
    check(f"H15 the {_name} is left to the recovery, not auto-answered",
          not SENT)

# A send is REFUSED when the window will not come to the foreground — which
# is correct, since typing blind is how keys land in the wrong app. What was
# not correct was announcing the answer first: live, a window that could not
# be focused produced a "fire" line claiming it had been answered, once per
# poll, while nothing had been typed at all.
set_now(T0)
reset([(25, "win")], {25: CHOOSER + NO_BOX})
_ok = {"v": False}
_saved_send = gui.send_text_lines
gui.send_text_lines = lambda w, lines, dry_run=False: (
    _send_lines(w, lines) if _ok["v"] else False)
LOGS_H = []
w = new_watcher()
w.set_auto_answer(True, True)
w.log.connect(lambda k, m: LOGS_H.append((k, m)))
try:
    for _ in range(3):
        advance(5)
        w._tick()
    check("H10 a refused send types nothing", not SENT)
    check("H11 and is not reported as an answer",
          not any("answered the" in m for _k, m in LOGS_H))
    check("H12 the failure is reported once, not once per poll",
          len([m for k, m in LOGS_H
               if k == "warn" and "wouldn't come forward" in m]) == 1)
    # Once the window can be focused, the answer goes through and says so.
    _ok["v"] = True
    advance(5)
    w._tick()
    check("H13 the retry succeeds and reports the answer",
          SENT == [(25, [""])]
          and any("answered the chooser" in m for _k, m in LOGS_H))
finally:
    gui.send_text_lines = _saved_send
SENT.clear()



# ---------------------------------------------------------------------------
# S: @pyqtSlot signatures must match the signals wired to them
# ---------------------------------------------------------------------------
# A decorator that declares FEWER arguments than its signal does not fail
# loudly. PyQt truncates the emitted arguments, the call raises TypeError
# inside the slot, and Qt answers an unhandled slot exception with qFatal --
# a hard process abort. That is what killed the packaged exe with 0xc0000409
# in Qt6Core a few seconds after every after-finish send: @pyqtSlot(str) on
# _on_loops_spent, fed by loops_spent = pyqtSignal(str, int). Nothing showed
# up in the activity log, because the process died inside the slot, before
# the slot's own log line. Three separate crashes before it was caught, and
# each one left every watched window unwatched.
import re as _re

_src = open("gui.py", encoding="utf-8").read()
_SIG = _re.compile(r"(\w+)\s*=\s*pyqtSignal\(([^)]*)\)")
_SLOT = _re.compile(r"@pyqtSlot\(([^)]*)\)\s*(?:\n\s*#[^\n]*)*\n\s*def\s+(\w+)\(")
_DEF = _re.compile(r"def\s+(\w+)\(self(?:,\s*([^)]*))?\)")
_CONN = _re.compile(r"\.(\w+)\.connect\(self\.(?:worker\.)?(\w+)\)")


def _count(s):
    return len([x for x in s.split(",") if x.strip()])


_sigs = {m.group(1): _count(m.group(2)) for m in _SIG.finditer(_src)}
_slots = {m.group(2): _count(m.group(1)) for m in _SLOT.finditer(_src)}
_defs = {m.group(1): _count(m.group(2) or "") for m in _DEF.finditer(_src)}

check("S0 the scan actually found the slots (guards against a dead regex)",
      len(_slots) > 20 and "_on_loops_spent" in _slots)

_bad = [f"{n}: slot declares {c}, function takes {_defs[n]}"
        for n, c in _slots.items() if n in _defs and _defs[n] != c]
check("S1 every @pyqtSlot matches its function arity: "
      + (_bad[0] if _bad else "-"), not _bad)

_wired = [f"{sig}({_sigs[sig]}) -> {slot}({_slots[slot]})"
          for sig, slot in
          [(m.group(1), m.group(2)) for m in _CONN.finditer(_src)]
          if sig in _sigs and slot in _slots
          and _sigs[sig] != _slots[slot]]
check("S2 every connected signal matches its slot: "
      + (_wired[0] if _wired else "-"), not _wired)

check("S3 loops_spent still carries BOTH key and remaining count",
      _sigs.get("loops_spent") == 2 and _slots.get("_on_loops_spent") == 2)


# ---------------------------------------------------------------------------
# T: a limit banner read AFTER its reset already passed
# ---------------------------------------------------------------------------
# The banner carries only a time of day, so "the next 9:30pm" is tomorrow once
# 9:30pm has gone. Reading one late — an unwatched window, or a pattern that
# only began matching after an update — then parks the session for a full day
# over a limit that lifted hours ago. Measured live on 2026-08-11: a 21:30
# reset parsed at 21:51 was scheduled for 21:31 the NEXT day.
_stale = gui.STALE_RESET_H
check("T1 the staleness horizon clears the 5-hour window", _stale >= 6)

# The rule itself, on explicit datetimes so no frozen clock is involved:
# anything computed further out than the horizon is a banner whose moment has
# gone, and anything inside it is a real wait.
_base = datetime(2026, 8, 11, 14, 0, tzinfo=pytz.UTC)


def _is_stale(hours_out):
    return (_base + timedelta(hours=hours_out)) - _base > timedelta(
        hours=gui.STALE_RESET_H)


check("T2 a reset 23h out is treated as a banner already passed",
      _is_stale(23))
check("T3 a reset 2h out is a real wait, not stale", not _is_stale(2))
check("T4 the boundary is exclusive, so 6h exactly still waits",
      not _is_stale(gui.STALE_RESET_H))

# And the guard has to be wired into the scheduling path, not just defined.
_gsrc = open("gui.py", encoding="utf-8").read()
check("T5 the scheduler consults the horizon before arming a countdown",
      "STALE_RESET_H" in _gsrc.split("new_reset = next_reset_datetime")[1][:2000])

print()
print("RESULT:", "ALL OK" if not failures else f"{failures} FAILURE(S)")
sys.exit(1 if failures else 0)
