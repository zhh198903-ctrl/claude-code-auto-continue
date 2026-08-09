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


print()
print("RESULT:", "ALL OK" if not failures else f"{failures} FAILURE(S)")
sys.exit(1 if failures else 0)
