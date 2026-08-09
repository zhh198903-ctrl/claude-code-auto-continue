# -*- coding: utf-8 -*-
"""State-machine tests for the GUI watcher's Fable refusal-recovery.

This covers `gui.Watcher._tick()` — the loop the shipped exe actually runs —
NOT `auto_continue.tick()` (that one is test_tick.py's job). The recovery is
the only feature that autonomously types into a terminal beyond 'continue':
it sends /model, ESC and Enter, so a state-machine bug means keystrokes land
in the wrong place, a dialog gets cancelled, or a window is silently pinned
and stops being watched at all.

Stub windows, recorded sends, a controllable clock and a fake QTimer: no real
UIA, no real keystrokes, no event loop. Plain harness like test_tick.py —
exits non-zero on any failure.
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


# --- controllable clock -----------------------------------------------------

class FakeDT(datetime):
    _now = None

    @classmethod
    def now(cls, tz=None):
        if tz is None:
            return cls._now.astimezone().replace(tzinfo=None)
        return cls._now.astimezone(tz)


gui.datetime = FakeDT
T0 = datetime(2026, 7, 26, 10, 0, 0, tzinfo=pytz.UTC)


def set_now(dt):
    FakeDT._now = dt


def advance(secs):
    set_now(FakeDT._now + timedelta(seconds=secs))


# --- fake QTimer: records scheduled reticks, never fires ---------------------

class FakeTimer:
    scheduled = []

    @staticmethod
    def singleShot(ms, fn):
        FakeTimer.scheduled.append(int(ms))


gui.QTimer = FakeTimer


# --- stub windows + recorded sends ------------------------------------------

class FakeWin:
    def __init__(self, hwnd, title):
        self.NativeWindowHandle = hwnd
        self.Name = title


WINDOWS = []
TEXTS = {}
SENT = []          # (hwnd, kind, payload)
SEND_OK = [True]   # flip to False to simulate a foreground-steal failure


def _send_lines(w, lines, dry_run=False):
    if not SEND_OK[0]:
        return False
    SENT.append((int(w.NativeWindowHandle), "text", list(lines)))
    return True


def _send_keys(w, keyspec, dry_run=False):
    if not SEND_OK[0]:
        return False
    SENT.append((int(w.NativeWindowHandle), "keys", keyspec))
    return True


gui.find_terminal_windows = lambda: WINDOWS
gui.read_terminal_text = lambda w: TEXTS.get(int(w.NativeWindowHandle), "")
gui.send_text_lines = _send_lines
gui.send_keys = _send_keys
gui.send_continue = lambda w, dry_run=False: _send_lines(w, ["continue"])
gui.list_tab_titles = lambda w: []


# Message bodies, split so displaying THIS file in a watched terminal can't
# trigger a real recovery (same convention as test_parse.py).
_SG = "safegu" "ards flagged"
_YES = "Yes, swi" "tch to"
_NO = "No, go b" "ack"
_SW = "Switch t" "o"
# Status-bar context meter, as escapes so this file can't self-match.
_BARGLYPH = chr(0x2588) * 3 + chr(0x2591) * 7
_ED = "Edit promp" "t and retry"
NOTICE = f"API Error: Fable 5's {_SG} this message. They may flag safe content."
DIALOG = f"Switch model?\n> 1. {_YES} Opus 5\n  2. {_NO}"
# What Claude Code actually shows: the notice comes WITH a two-option picker
# whose first entry is pre-selected, so one Enter performs the switch.
PICKER = (f"Session paused\n\nFable 5's {_SG} this message. The safeguards "
          f"are intentionally broad right now.\n\n"
          f"> 1. {_SW} Opus 5\n  2. {_ED} with Fable 5")
CLEAN = "all good here, nothing to see"
ECONN = "API Error: Unable to connect to API (ECONN" "RESET)"
BANNER = ("You've hit your li" "mit · resets 11pm (Asia/Shanghai)\n"
          "/upgra" "de to increase your usage limit.")


def new_watcher(steps=None, enabled=True, delay=180, scope=None):
    """A Watcher wired for tests: running, feature on, default step script.

    `scope` gives an explicit per-window list. Without it the watcher runs in
    all-windows mode, where drift enforcement is deliberately inactive — it
    would otherwise steer every watched session onto the target, including
    ones the user deliberately put elsewhere that never saw a block.
    """
    w = gui.Watcher()
    w._running = True
    w.set_fable_config({
        "enabled": enabled,
        "all_windows": scope is None,
        "delay": delay,
        "steps": steps if steps is not None else gui.DEFAULT_FABLE_STEPS,
        "windows": list(scope or []),
    })
    return w


def reset(hwnd_titles, text_by_hwnd):
    global WINDOWS, TEXTS
    WINDOWS = [FakeWin(h, t) for h, t in hwnd_titles]
    TEXTS = dict(text_by_hwnd)
    SENT.clear()
    FakeTimer.scheduled.clear()
    SEND_OK[0] = True
    set_now(T0)


def texts_sent(hwnd=1):
    return [p for h, k, p in SENT if h == hwnd and k == "text"]


def keys_sent(hwnd=1):
    return [p for h, k, p in SENT if h == hwnd and k == "keys"]


# =============================================================================
print("---- A: config slot doesn't explode (regression: self.states) ----")
# This ran on every launch via the settings-apply path; an AttributeError here
# is raised inside a queued PyQt slot, which aborts the whole process.
try:
    w = new_watcher()
    w._states[1] = gui._WState(hwnd=1, title="t")
    w._states[1].fable_step = 3
    w.set_fable_config({"enabled": True, "all_windows": True, "delay": 10,
                        "steps": "continue", "windows": []})
    check("A1 set_fable_config survives a live state map", True)
    check("A2 out-of-range step reset when script shrinks",
          w._states[1].fable_step == -1)
except Exception as e:
    check(f"A1 set_fable_config raised {type(e).__name__}: {e}", False)

# Hostile / truncated persisted config must not raise either.
for bad in ({"delay": "abc"}, {"steps": ["a", "b"]}, {"windows": "notalist"},
            {"steps": ""}, {}):
    try:
        new_watcher().set_fable_config(dict(bad, enabled=True))
        ok = True
    except Exception as e:
        ok = False
        print(f"       raised on {bad}: {type(e).__name__}: {e}")
    check(f"A3 malformed config tolerated: {bad}", ok)


# =============================================================================
print("---- B: disabled by default, never acts ----")
reset([(1, "claude")], {1: NOTICE})
w = gui.Watcher()
w._running = True
w._tick()
check("B1 untouched watcher sends nothing", SENT == [])
check("B2 feature defaults to off", w._fable_enabled is False)


# =============================================================================
print("---- C: happy path, steps run in order ----")
# Script: /model opus, <esc>, <confirm>, continue
reset([(1, "claude")], {1: NOTICE})
w = new_watcher(steps="/model opus\n<esc>\n<confirm>\ncontinue")

w._tick()                                    # detect -> arm at step 0
check("C1 detection arms the recovery", w._states[1].fable_step == 0)
check("C2 nothing typed on the detecting tick", SENT == [])

w._tick()                                    # step 0: /model opus
check("C3 sends /model opus", texts_sent() == [["/model opus"]])
check("C4 advances to <esc>", w._states[1].fable_step == 1)

# <esc> with the dialog already up must be SKIPPED (ESC would cancel it).
TEXTS[1] = NOTICE + "\n" + DIALOG
w._tick()
check("C5 <esc> skipped while dialog is showing", keys_sent() == [])
check("C6 advances to <confirm>", w._states[1].fable_step == 2)

w._tick()                                    # step 2: confirm
check("C7 confirms the dialog with Enter", texts_sent()[-1] == [""])
check("C8 stays on <confirm> until the dialog clears",
      w._states[1].fable_step == 2)

TEXTS[1] = NOTICE + "\nswitched."            # dialog gone
advance(gui.FABLE_KEY_GAP_S + 1)             # brief settle after the Enter
w._tick()
check("C9 advances once the dialog is answered", w._states[1].fable_step == 3)

w._tick()                                    # step 3: continue
check("C10 sends continue", texts_sent()[-1] == ["continue"])
check("C11 run finishes and idles", w._states[1].fable_step == -1)
check("C12 latched as handled", w._states[1].fable_handled is True)


# =============================================================================
print("---- D: <esc> fires only when no dialog appeared ----")
reset([(1, "claude")], {1: NOTICE})
w = new_watcher(steps="/model opus\n<esc>\ncontinue")
w._tick()                                    # arm
w._tick()                                    # /model opus
check("D1 at <esc>", w._states[1].fable_step == 1)
w._tick()                                    # no dialog, settle not elapsed
check("D2 waits out the settle before ESC", keys_sent() == [])
advance(5)                                   # > 4s settle
w._tick()
check("D3 sends ESC to surface a queued dialog", keys_sent() == ["{Esc}"])
check("D4 advances after ESC", w._states[1].fable_step == 2)


# =============================================================================
print("---- E: <confirm> is bounded (no endless Enter) ----")
# The confirmed dialog's text lingers in scrollback, so "still matches" cannot
# be the exit condition — otherwise every retick presses Enter again, each one
# submitting whatever the user has typed.
# Regression from a live end-to-end run: the modal's text stays in the
# scrollback after it is answered, so treating "pattern still matches" as
# "still open" re-pressed Enter 8 times for ONE dialog. Every extra Enter
# submits whatever the user has typed. Exactly one Enter per dialog.
reset([(1, "claude")], {1: NOTICE + "\n" + DIALOG})
w = new_watcher(steps="<confirm>\ncontinue")
w._tick()                                    # arm
w._tick()                                    # confirm -> one Enter
check("E1 presses Enter once", texts_sent() == [[""]])
w._tick()                                    # immediate retick, dialog lingers
check("E2 no second Enter on the next tick", texts_sent() == [[""]])
advance(gui.FABLE_KEY_GAP_S + 1)
w._tick()                                    # dialog STILL matches
check("E3 still no second Enter after the settle", texts_sent() == [[""]])
check("E4 advances past the answered dialog instead of re-pressing",
      w._states[1].fable_step == 1)
for _ in range(6):                           # dialog text never scrolls away
    advance(5)
    w._tick()
check("E5 exactly one Enter ever reaches the window",
      len([t for t in texts_sent() if t == [""]]) == 1)

# And if a dialog never appears at all, the step still gives up.
reset([(1, "claude")], {1: NOTICE})
w = new_watcher(steps="<confirm>\ncontinue")
w._tick()
w._tick()
check("E6 no Enter sent when no dialog is showing", texts_sent() == [])
advance(gui.FABLE_CONFIRM_MAX_S + 1)
w._tick()
check("E7 hard deadline advances when no dialog appears",
      w._states[1].fable_step == 1)


# =============================================================================
print("---- F: <wait> does not restart the recovery ----")
# The notice is still on screen during the wait (the fallback model has barely
# printed anything). Re-detecting there would restart from step 0 and ESC the
# turn it just started, every few seconds, forever.
reset([(1, "claude")], {1: NOTICE})
w = new_watcher(steps="continue\n<wait>\n/model fable", delay=100)
w._tick()                                    # arm
w._tick()                                    # step 0: continue
check("F1 at <wait>", w._states[1].fable_step == 1)
before = len(SENT)
for _ in range(5):                           # notice still visible
    advance(5)
    w._tick()
check("F2 no restart while waiting", w._states[1].fable_step == 1)
check("F3 nothing retyped during the wait", len(SENT) == before)
advance(200)
w._tick()
check("F4 wait expires and advances", w._states[1].fable_step == 2)


# =============================================================================
print("---- G: <wait> banks only productive time ----")
# <wait> means "N seconds of real work on the fallback model". A network stall
# must cost the recovery nothing — real outages here ran 36 and 90 minutes, and
# expiring the wait during one sends /model into a still-stuck session.
reset([(1, "claude")], {1: NOTICE + "\n" + ECONN})
w = new_watcher(steps="continue\n<wait>\n/model fable", delay=60)
w._tick()                                    # arm
w._tick()                                    # continue -> at <wait>
check("G1 at <wait>", w._states[1].fable_step == 1)

for _ in range(6):                           # 180s of pure outage
    advance(30)
    w._tick()
check("G2 outage banks no run time", w._states[1].fable_wait_acc == 0.0)
check("G3 wait is still open after 3x the delay in wall time",
      w._states[1].fable_step == 1)
check("G4 outer handler still pokes the stalled fallback model",
      ["continue"] in texts_sent())

# Network recovers: now the clock actually starts.
TEXTS[1] = NOTICE + "\nworking normally now"
advance(30)
w._tick()
check("G5 productive ticks bank time", w._states[1].fable_wait_acc >= 30)
check("G6 not finished yet at 30s of 60s", w._states[1].fable_step == 1)
advance(35)
w._tick()
check("G7 advances once the full run time is banked",
      w._states[1].fable_step == 2)

# Anti-wedge backstop: a stale error that never clears must not pin the run
# forever — it gives up on wall time and says so.
reset([(1, "claude")], {1: NOTICE + "\n" + ECONN})
w = new_watcher(steps="continue\n<wait>\n/model fable", delay=60)
LOGS = []
w.log.connect(lambda k, m: LOGS.append((k, m)))
w._tick()
w._tick()
for _ in range(45):                          # well past delay * MAX_MULT
    advance(30)
    w._tick()
check("G8 permanent stall eventually gives up (no strand)",
      ["/model fable"] in texts_sent())
check("G9 and it says why", any("banked" in m for k, m in LOGS if k == "warn"))


# =============================================================================
print("---- H: a failed send does not advance the script ----")
# send_* return False when the window can't be brought forward. Advancing then
# would ESC a session that never got /model, and 'continue' on the old model.
reset([(1, "claude")], {1: NOTICE})
w = new_watcher(steps="/model opus\n<esc>\ncontinue")
w._tick()                                    # arm
SEND_OK[0] = False
w._tick()
check("H1 stays on the same step after a failed send",
      w._states[1].fable_step == 0)
check("H2 nothing recorded as sent", SENT == [])
check("H3 counts the failure", w._states[1].fable_tries == 1)
SEND_OK[0] = True
w._tick()
check("H4 retries and succeeds", texts_sent() == [["/model opus"]])
check("H5 advances after the retry", w._states[1].fable_step == 1)

# Give up rather than spin forever on a window we can never focus.
reset([(1, "claude")], {1: NOTICE})
w = new_watcher(steps="/model opus\ncontinue")
w._tick()
SEND_OK[0] = False
for _ in range(gui.FABLE_SEND_RETRIES + 1):
    w._tick()
check("H6 abandons after repeated send failures",
      w._states[1].fable_step == -1 and w._states[1].fable_handled is True)


# =============================================================================
print("---- I: a lingering notice must not blind the watchdog ----")
# After a run completes the notice can stay on screen. Owning the window there
# used to skip the limit / retry handlers below — the exact overnight stall
# this tool exists to prevent.
reset([(1, "claude")], {1: NOTICE})
w = new_watcher(steps="continue")
w._tick()                                    # arm
w._tick()                                    # run + finish
check("I1 handled after the run", w._states[1].fable_handled is True)
SENT.clear()
# Notice still present AND the session has now hit its rate limit.
TEXTS[1] = NOTICE + "\n" + BANNER
w._tick()
check("I2 limit banner still detected through a lingering notice",
      w._states[1].reset_utc is not None)

# A network stall behind a lingering notice must still be poked.
reset([(1, "claude")], {1: NOTICE})
w = new_watcher(steps="continue")
w._tick()
w._tick()
SENT.clear()
TEXTS[1] = NOTICE + "\n" + ECONN
advance(120)
w._tick()
check("I3 network stall still gets a continue",
      ["continue"] in texts_sent())


# =============================================================================
print("---- J: repeated refusals are bounded ----")
# The script ends by returning to Fable and retrying the SAME message Fable
# refused, so a second refusal is expected; retrying without bound would loop
# model switches all night.
reset([(1, "claude")], {1: NOTICE})
w = new_watcher(steps="continue")
for _ in range(12):
    advance(30)
    w._tick()
    st = w._states[1]
    if st.fable_step < 0 and st.fable_handled:
        st.fable_handled = False             # simulate the notice re-appearing
check("J1 recovery attempts are capped", w._states[1].fable_runs
      <= gui.FABLE_MAX_RUNS)


# =============================================================================
print("---- K: turning the feature off / stopping disarms in-flight runs ----")
reset([(1, "claude")], {1: NOTICE})
w = new_watcher(steps="continue\n<wait>\n/model fable", delay=300)
w._tick()
w._tick()
check("K1 mid-run before disable", w._states[1].fable_step == 1)
w.set_fable_config({"enabled": False, "all_windows": True, "delay": 300,
                    "steps": "continue\n<wait>\n/model fable", "windows": []})
check("K2 disabling resets the in-flight run", w._states[1].fable_step == -1)

reset([(1, "claude")], {1: NOTICE})
w = new_watcher(steps="continue\n<wait>\n/model fable", delay=300)
w._tick()
w._tick()
check("K3 mid-run before stop", w._states[1].fable_step == 1)
w.stop()
check("K4 stop() disarms the run", w._states[1].fable_step == -1)


# =============================================================================
print("---- L: targeting — only the window showing the notice is touched ----")
reset([(1, "claude-A"), (2, "claude-B")], {1: NOTICE, 2: CLEAN})
w = new_watcher(steps="/model opus\ncontinue")
w._tick()                                    # arm (window 1 only)
w._tick()                                    # step 0
check("L1 the affected window is driven", texts_sent(1) == [["/model opus"]])
check("L2 the clean window is untouched", texts_sent(2) == [])
check("L3 clean window never armed", w._states[2].fable_step == -1)


# =============================================================================
print("---- M: reticks are coalesced across windows ----")
# One timer per recovering window per tick used to compound geometrically:
# 2 windows -> 2 timers -> 4 -> 8, each a full UIA scan that also advanced the
# step machine.
reset([(1, "claude-A"), (2, "claude-B")], {1: NOTICE, 2: NOTICE})
w = new_watcher(steps="/model opus\n<wait>\ncontinue")
FakeTimer.scheduled.clear()
w._tick()
check("M1 both windows armed",
      w._states[1].fable_step == 0 and w._states[2].fable_step == 0)
check(f"M2 one tick schedules at most one retick "
      f"(got {len(FakeTimer.scheduled)})", len(FakeTimer.scheduled) <= 1)


# =============================================================================
print("---- N: opt-in scoping ----")
reset([(1, "claude-A"), (2, "claude-B")], {1: NOTICE, 2: NOTICE})
w = gui.Watcher()
w._running = True
w.set_fable_config({"enabled": True, "all_windows": False, "delay": 60,
                    "steps": "/model opus", "windows": ["claude-A"]})
w._tick()
w._tick()
check("N1 opted-in window runs", texts_sent(1) == [["/model opus"]])
check("N2 non-opted window ignored",
      texts_sent(2) == [] and w._states[2].fable_step == -1)


# =============================================================================
print("---- O: the safeguard picker IS the recovery ----")
# Regression from a real block (2026-08-04): the notice arrives WITH a picker
# whose option 1 ("Switch to <fallback>") is pre-selected. One Enter completes
# the switch — the old default script instead typed /model into an open modal
# and then ESC'd, which would have cancelled the picker.
reset([(1, "claude")], {1: PICKER})
w = new_watcher(steps="<confirm>")
w._tick()                                    # detect
check("O1 the picker's notice still triggers detection",
      w._states[1].fable_step == 0)
w._tick()                                    # <confirm> accepts the picker
check("O2 <confirm> accepts the picker with one Enter",
      texts_sent() == [[""]])
advance(gui.FABLE_KEY_GAP_S + 1)
w._tick()
check("O3 run completes after the single Enter",
      w._states[1].fable_step == -1 and w._states[1].fable_handled is True)

# <esc> must treat the picker as "a modal is already showing" — ESC would
# dismiss it, losing the offered switch.
reset([(1, "claude")], {1: PICKER})
w = new_watcher(steps="<esc>\n<confirm>")
w._tick()
w._tick()
check("O4 <esc> skipped while the picker is showing", keys_sent() == [])

# The shipped default script must run cleanly against the real UI.
reset([(1, "claude")], {1: PICKER})
w = new_watcher()                            # DEFAULT_FABLE_STEPS
w._tick()                                    # detect
w._tick()                                    # <confirm> -> Enter on picker
check("O5 default script confirms the picker first", texts_sent() == [[""]])
check("O6 default script does NOT type /model into the picker",
      not any("/model" in str(t) for t in texts_sent()))


# =============================================================================
print("---- P: the broken v1.0.16 step script is migrated on load ----")
# Saved settings win over the built-in default, so upgrading alone would leave
# every existing user on the script that ESC'd the picker away. Only the
# untouched legacy string is rewritten; a customised one must survive.
check("P1 legacy default differs from the current one",
      gui.LEGACY_FABLE_STEPS_V1016 != gui.DEFAULT_FABLE_STEPS)
check("P2 legacy script is the one that typed /model first",
      gui._parse_recovery_steps(gui.LEGACY_FABLE_STEPS_V1016)[0]
      == ("send", "/model opus"))
check("P3 current script confirms the picker first",
      gui._parse_recovery_steps(gui.DEFAULT_FABLE_STEPS)[0]
      == ("confirm", None))

# Exercise the load-path migration itself against a stored config.
import json as _json


class _FakeSettings:
    def __init__(self, store):
        self._s = store

    def value(self, key, default=None, type=None):
        return self._s.get(key, default)


def _migrate(stored_steps):
    """Mirror _load_settings' merge + migration on a stored fable_cfg."""
    cfg = {"enabled": False, "all_windows": True, "delay": 180,
           "steps": gui.DEFAULT_FABLE_STEPS, "windows": []}
    cfg.update(_json.loads(_json.dumps({"steps": stored_steps})))
    if cfg.get("steps") == gui.LEGACY_FABLE_STEPS_V1016:
        cfg["steps"] = gui.DEFAULT_FABLE_STEPS
    return cfg["steps"]


check("P4 untouched legacy script is migrated",
      _migrate(gui.LEGACY_FABLE_STEPS_V1016) == gui.DEFAULT_FABLE_STEPS)
_custom = "/model haiku\n<confirm>\ncontinue"
check("P5 a customised script is NOT touched", _migrate(_custom) == _custom)
check("P6 the current script is left as-is",
      _migrate(gui.DEFAULT_FABLE_STEPS) == gui.DEFAULT_FABLE_STEPS)


# =============================================================================
print("---- Q: a SECOND block is caught while the first notice lingers ----")
# Observed live: after a recovery, the handled notice stays matchable in the
# scrollback for tens of minutes. The script ends by retrying the very message
# that was flagged, so an immediate re-block is the likeliest next event — and
# with a plain "already handled" latch it would go unnoticed, stalling the
# session with nobody watching.
reset([(1, "claude")], {1: NOTICE})
w = new_watcher(steps="continue")
w._tick()                                    # detect
w._tick()                                    # run + finish
check("Q1 first run handled", w._states[1].fable_handled is True)
first_dist = w._states[1].fable_notice_dist
check("Q1b latched the notice's distance", first_dist is not None)

# The old notice drifts away as the session prints — no re-trigger.
TEXTS[1] = NOTICE + "\n" + ("output line\n" * 40)
SENT.clear()
w._tick()
check("Q2 drifting old notice does not re-trigger",
      w._states[1].fable_step == -1 and SENT == [])

# Now a genuinely NEW notice appears at the tail: closer than the latched one.
TEXTS[1] = NOTICE + "\n" + ("output line\n" * 40) + "\n" + NOTICE
w._tick()
check("Q3 fresh notice re-arms the recovery", w._states[1].fable_step == 0)
check("Q4 counted as a second run", w._states[1].fable_runs == 2)

# ...but still bounded: a third consecutive block gives up rather than looping.
w._tick()                                    # runs step 0 of run 2
for _ in range(4):
    advance(30)
    TEXTS[1] = TEXTS[1] + "\n" + NOTICE      # keep re-blocking
    w._tick()
check("Q5 repeated fresh blocks stay capped",
      w._states[1].fable_runs <= gui.FABLE_MAX_RUNS)

# A notice that merely jitters slightly must NOT count as new.
reset([(1, "claude")], {1: NOTICE})
w = new_watcher(steps="continue")
w._tick()
w._tick()
SENT.clear()
TEXTS[1] = NOTICE + "\n" + ("x" * (gui.FABLE_FRESH_MARGIN // 2))
w._tick()
check("Q6 sub-margin drift is not mistaken for a new block",
      w._states[1].fable_step == -1 and SENT == [])

# Replay of the real oscillation measured on a live session (see the comment
# on FABLE_FRESH_MARGIN). The terminal is a viewport, not an append-only log:
# modals opening and closing rewrite the trailing rows, so the distance dips.
# None of these dips may read as a new block.
for label, seq in (("run3", [732, 1083, 939, 981, 1083]),
                   ("run4", [878, 1017, 837, 1017, 1119])):
    furthest = None
    spurious = []
    for d in seq:
        if furthest is not None and d + gui.FABLE_FRESH_MARGIN < furthest:
            spurious.append(d)
        furthest = d if furthest is None else max(furthest, d)
    check(f"Q7 {label}: real redraw jitter never reads as a new block",
          not spurious)


# =============================================================================
print("---- R: the spent picker must stop reading as an open modal ----")
# Found by driving the branch four live recoveries never reached: the fallback
# model still mid-turn when /model fires. The picker's text stays in the
# scrollback after it is accepted, so at its tail allowance it kept looking
# like an open modal — <esc> skipped (never surfacing the queued switch, the
# one thing <esc> exists for) and the next <confirm> fired a bare Enter into a
# running turn.
BUSY_AFTER_PICKER = (
    PICKER + "\n* Synthesizing... (turn still running)\n"
    "GOT '/model fable'\n"
    "   (switch queued behind the running turn - no dialog yet)\n")

reset([(1, "claude")], {1: PICKER})
w = new_watcher(steps="<confirm>\n/model fable\n<esc>\n<confirm>\ncontinue")
w._tick()                                    # detect
w._tick()                                    # <confirm> accepts the picker
check("R1 picker accepted with one Enter", texts_sent() == [[""]])
check("R2 picker marked spent", w._states[1].fable_picker_used is True)

advance(gui.FABLE_KEY_GAP_S + 1)
TEXTS[1] = BUSY_AFTER_PICKER                 # picker text lingers, no real modal
w._tick()                                    # advance off <confirm>
w._tick()                                    # send /model fable
check("R3 sent /model fable", texts_sent()[-1] == ["/model fable"])

# <esc> must now SEE no modal and fire, instead of being fooled by the stale
# picker text sitting in the scrollback.
w._tick()
advance(5)                                   # past the 4s settle
w._tick()
check("R4 <esc> fires to surface the queued switch",
      keys_sent() == ["{Esc}"])

# And a plain buffer with only the stale picker must not read as a modal.
st = w._states[1]
check("R5 spent picker no longer counts as an open modal",
      w._modal_up(BUSY_AFTER_PICKER, st) is False)
st2 = gui._WState(hwnd=9, title="x")
check("R6 an unspent picker still counts",
      w._modal_up(BUSY_AFTER_PICKER, st2) is True)
check("R7 a real switch dialog always counts",
      w._modal_up(BUSY_AFTER_PICKER + "\n" + DIALOG, st) is True)

# A fresh run re-arms the picker (it is per-run, not permanent).
gui._fable_reset(st)
check("R8 reset re-arms the picker for the next run",
      st.fable_picker_used is False)


# =============================================================================
print("---- S: <confirm> must outlast the poll interval ----")
# Observed live: with a 60s poll and a fixed 30s confirm deadline, the first
# tick to evaluate <confirm> was already overdue, so the step advanced without
# ever pressing Enter. The modal stayed open and the recovery did nothing —
# silently, since the "confirming" line only logs when an attempt is made.
reset([(1, "claude")], {1: PICKER})
w = new_watcher(steps="<confirm>\ncontinue")
w._interval = 60                             # the real configured poll
w._tick()                                    # detect / arm
advance(59)                                  # one poll later, retick missed
w._tick()
check("S1 still attempts the confirm a whole poll later",
      texts_sent() == [[""]])

# And it must still give up eventually rather than pressing forever.
reset([(1, "claude")], {1: NOTICE})          # notice but NO modal to accept
w = new_watcher(steps="<confirm>\ncontinue")
w._interval = 60
w._tick()
advance(59)
w._tick()
check("S2 no Enter when there is no modal", texts_sent() == [])
check("S3 with nothing to accept it moves on instead of stalling",
      w._states[1].fable_step == 1)

# The decisive case: a modal STILL showing long past every timeout must still
# be confirmed. Testing the deadline before the modal is what broke live.
reset([(1, "claude")], {1: PICKER})
w = new_watcher(steps="<confirm>\ncontinue")
w._interval = 60
w._tick()                                    # arm
advance(10 * 60)                             # far past every deadline
w._tick()
check("S4 a showing modal is confirmed however late the tick lands",
      texts_sent() == [[""]])


# =============================================================================
print("---- T: a run must say what model it actually ended on ----")
# The 7th live recovery logged a plain "done" while the session had been left
# on the fallback model. Nothing in the log said so, and the failure went
# unnoticed for an hour. Completion now reports the model, and warns on a
# mismatch with what the script's last /model step asked for.
_BAR_F = "  " + chr(91) + "Fable 5" + chr(93) + " " + _BARGLYPH + " 44% | usage"
_BAR_O = "  " + chr(91) + "Opus 5" + chr(93) + " " + _BARGLYPH + " 63% | usage"

check("T1 last /model step is read from the script",
      gui._last_model_step(
          gui._parse_recovery_steps(gui.DEFAULT_FABLE_STEPS)) == "fable")
check("T2 no /model step -> nothing to compare",
      gui._last_model_step(gui._parse_recovery_steps("continue")) is None)
check("T3 status bar parses", ac.current_model(_BAR_F) == "Fable 5"
      and ac.current_model(_BAR_O) == "Opus 5")
check("T4 absent status bar is None", ac.current_model("no bar here") is None)

# Ended on the right model -> informational only.
reset([(1, "claude")], {1: NOTICE + "\n" + _BAR_F})
w = new_watcher(steps="/model fable")
LOGS = []
w.log.connect(lambda k, m: LOGS.append((k, m)))
w._tick()
w._tick()
warns = [m for k, m in LOGS if k == "warn" and "NOT switched back" in m]
check("T5 correct end model logs no warning", not warns)

# Ended on the WRONG model -> loud.
reset([(1, "claude")], {1: NOTICE + "\n" + _BAR_O})
w = new_watcher(steps="/model fable")
LOGS = []
w.log.connect(lambda k, m: LOGS.append((k, m)))
w._tick()
w._tick()
warns = [m for k, m in LOGS if k == "warn" and "NOT switched back" in m]
check("T6 wrong end model is reported loudly", len(warns) == 1)

# The status bar is genuinely absent whenever a full-screen modal covers the
# viewport — seen live when the session sat on Claude Code's interrupt menu.
# An unreadable bar must stay silent, never manufacture a "not switched back".
reset([(1, "claude")], {1: NOTICE + "\n4. Type something.\n5. Chat about this"})
w = new_watcher(steps="/model fable")
LOGS = []
w.log.connect(lambda k, m: LOGS.append((k, m)))
w._tick()
w._tick()
warns = [m for k, m in LOGS if k == "warn" and "NOT switched back" in m]
check("T7 unreadable status bar produces no false warning", not warns)
# Silence is the one unacceptable ending. With the notice still standing that
# report is the "not cleared" warning rather than a plain done — but it must
# still never be the misleading "NOT switched back" one checked above.
ends = [m for k, m in LOGS
        if "Fable-recover done" in m or "NOT cleared" in m]
check("T8 completion is still reported", len(ends) == 1)


# =============================================================================
print("---- U: the default script must not interrupt running turns ----")
# <esc> destroyed the user's work twice in production: once ending a queued
# instruction, once killing a task 5m50s in. It only ever bought a slightly
# earlier switch-back, which the completion check now reports on anyway.
_steps = gui._parse_recovery_steps(gui.DEFAULT_FABLE_STEPS)
check("U1 no <esc> in the shipped default",
      not any(k == "esc" for k, _ in _steps))
check("U2 <esc> is still available for people who opt in",
      gui._parse_recovery_steps("<esc>") == [("esc", None)])

# Claude Code sometimes switches by itself, leaving no modal to accept. The
# script must survive that: <confirm> finds nothing, times out, and the run
# becomes just the switch back.
_AUTO = (NOTICE + "\nSwitched to Opus 5. Send feedback with /feedback\n"
         + "  " + chr(91) + "Opus 5" + chr(93) + " ~~ 60%")
reset([(1, "claude")], {1: _AUTO})
w = new_watcher()                            # DEFAULT_FABLE_STEPS
w._interval = 60
w._tick()                                    # detect
w._tick()                                    # <confirm> -> nothing to accept
check("U3 no Enter pressed when Claude Code already switched",
      texts_sent() == [])
advance(2 * 60 + 5)                          # past the confirm deadline
w._tick()
check("U4 the run moves on instead of stalling",
      w._states[1].fable_step >= 1)
check("U5 and never sends ESC", keys_sent() == [])


# =============================================================================
print("---- V: parked on the fallback model is a failure, not a resting state ----")
# The point of enabling this is to keep WORKING on the chosen model. A run can
# end off-target (switch-back never landed, or Claude Code switched by itself
# and the notice scrolled away) and nothing used to bring it home.
_BAR_FABLE = "  " + chr(91) + "Fable 5" + chr(93) + " " + _BARGLYPH + " 44%"
_BAR_OPUS = "  " + chr(91) + "Opus 5" + chr(93) + " " + _BARGLYPH + " 63%"

reset([(1, "claude")], {1: "working away\n" + _BAR_OPUS})
w = new_watcher(scope=["claude"])            # target = fable, no notice
w._tick()
check("V1 grace period holds off the first correction", texts_sent() == [])
advance(gui.FABLE_DRIFT_GRACE_S + 5)
w._tick()                                    # arms the restore script
w._tick()                                    # sends it
check("V2 steers back with /model fable", texts_sent() == [["/model fable"]])
check("V3 restore script is a switch, not a work injection",
      not any("continue" in str(t) for t in texts_sent()))

# On target: nothing happens, ever.
reset([(1, "claude")], {1: "working away\n" + _BAR_FABLE})
w = new_watcher(scope=["claude"])
for _ in range(4):
    advance(gui.FABLE_DRIFT_GRACE_S + 5)
    w._tick()
check("V4 on-target window is left alone", texts_sent() == [])

# A safeguard notice on screen means the normal recovery owns it — drift
# correction must not race that.
reset([(1, "claude")], {1: NOTICE + "\n" + _BAR_OPUS})
w = new_watcher(steps="continue")
w._tick()
w._tick()                                    # recovery runs and finishes
SENT.clear()
advance(gui.FABLE_DRIFT_GRACE_S + 5)
w._tick()
check("V5 no correction while the notice is still showing",
      not any("/model" in str(t) for t in texts_sent()))

# Bounded: it must not fight the user forever.
reset([(1, "claude")], {1: "working away\n" + _BAR_OPUS})
w = new_watcher(scope=["claude"])
for _ in range(10):
    advance(gui.FABLE_DRIFT_RETRY_S + gui.FABLE_DRIFT_GRACE_S + 10)
    w._tick()
    w._tick()
sent = [t for t in texts_sent() if "/model" in str(t)]
check("V6 corrections are capped", len(sent) <= gui.FABLE_DRIFT_MAX)

# A script with no /model step names no target, so nothing is enforced.
reset([(1, "claude")], {1: "working away\n" + _BAR_OPUS})
w = new_watcher(steps="continue")
for _ in range(3):
    advance(gui.FABLE_DRIFT_GRACE_S + 5)
    w._tick()
check("V7 no /model step means no target to enforce",
      not any("/model" in str(t) for t in texts_sent()))


# =============================================================================
print("---- X: a switch the user makes by hand is an instruction ----")
# Steering back a model the user chose deliberately turns the feature into
# something that fights its owner. A change observed while this tool typed
# nothing is the user; untick the window and stop enforcing it.
reset([(1, "claude")], {1: "working away\n" + _BAR_FABLE})
w = new_watcher()
unticked = []
w.fable_untick.connect(lambda k: unticked.append(k))
w._tick()                                    # observes it on target
check("X1 on-target window is quietly tracked",
      w._states[1].fable_last_model == "Fable 5")

# The user switches it themselves; we sent nothing.
advance(gui.FABLE_USER_SWITCH_QUIET_S + 10)
TEXTS[1] = "working away\n" + _BAR_OPUS
SENT.clear()
w._tick()
check("X2 the manual switch is noticed", w._states[1].fable_user_optout is True)
check("X3 the window is unticked", unticked == [gui.title_key("claude")])
check("X4 nothing is typed to undo it", texts_sent() == [])

# And it stays hands-off from then on.
for _ in range(4):
    advance(gui.FABLE_DRIFT_GRACE_S + gui.FABLE_DRIFT_RETRY_S + 10)
    w._tick()
check("X5 it keeps its hands off afterwards", texts_sent() == [])

# A window that has been off-target since a failed run never CHANGED under us,
# so it is still repaired rather than unticked.
reset([(1, "claude")], {1: "working away\n" + _BAR_OPUS})
w = new_watcher(scope=["claude"])
unticked = []
w.fable_untick.connect(lambda k: unticked.append(k))
w._tick()
advance(gui.FABLE_DRIFT_GRACE_S + 5)
w._tick()
w._tick()
check("X6 a never-on-target window is still steered back",
      any("/model" in str(t) for t in texts_sent()))
check("X7 and is not unticked", unticked == [])

# A change right after our own keystrokes is ours, not the user's.
reset([(1, "claude")], {1: "working away\n" + _BAR_FABLE})
w = new_watcher()
unticked = []
w.fable_untick.connect(lambda k: unticked.append(k))
w._tick()
w._states[1].fable_acted_at = FakeDT._now      # pretend we just typed
TEXTS[1] = "working away\n" + _BAR_OPUS
advance(5)                                    # well inside the quiet window
w._tick()
check("X8 a change we caused is not mistaken for the user",
      w._states[1].fable_user_optout is False and unticked == [])

# Re-ticking the window in Advanced clears the opt-out.
reset([(1, "claude")], {1: "working away\n" + _BAR_FABLE})
w = new_watcher()
w._tick()
advance(gui.FABLE_USER_SWITCH_QUIET_S + 10)
TEXTS[1] = "working away\n" + _BAR_OPUS
w._tick()
check("X9 opted out after the manual switch",
      w._states[1].fable_user_optout is True)
w._states[1].title = "claude"
w.set_fable_config({"enabled": True, "all_windows": False, "delay": 180,
                    "steps": gui.DEFAULT_FABLE_STEPS, "windows": ["claude"]})
check("X10 re-ticking it in Advanced resumes enforcement",
      w._states[1].fable_user_optout is False)


# =============================================================================
print("---- Y: the opt-out must actually hold, in every mode ----")
# Everything here was found by a pre-release audit. Each check corresponds to
# a way the feature could still type into a window it had promised to leave
# alone, or steer one it was never asked to.

# An opted-out window is off limits to EVERYTHING, not just drift. Gating only
# the drift branch left a genuine safeguard block free to run a full recovery.
reset([(1, "claude")], {1: PICKER + "\n" + _BAR_OPUS})
w = new_watcher()                            # all_windows=True
w._states[1] = gui._WState(hwnd=1, title="claude")
w._states[1].fable_user_optout = True
for _ in range(4):
    advance(60)
    w._tick()
check("Y1 a safeguard block does not act on an opted-out window",
      SENT == [])

# The opt-out is carried in the config, so it survives a restart even in
# all-windows mode where there is no tick to remove.
w2 = new_watcher()
w2.set_fable_config({"enabled": True, "all_windows": True, "delay": 180,
                     "steps": gui.DEFAULT_FABLE_STEPS, "windows": [],
                     "optout": ["claude"]})
reset([(1, "claude")], {1: PICKER + "\n" + _BAR_OPUS})
for _ in range(3):
    advance(60)
    w2._tick()
check("Y2 a persisted opt-out survives a fresh worker", SENT == [])

# Drift enforcement needs an explicit tick. In all-windows mode it would
# switch every watched session onto the target, including ones deliberately
# put on another model that never saw a block.
reset([(1, "claude")], {1: "working away\n" + _BAR_OPUS})
w = new_watcher()                            # all_windows=True
for _ in range(4):
    advance(gui.FABLE_DRIFT_GRACE_S + gui.FABLE_DRIFT_RETRY_S + 10)
    w._tick()
check("Y3 all-windows mode never drift-steers an unticked window",
      texts_sent() == [])

# ...but an explicitly ticked window still is.
reset([(1, "claude")], {1: "working away\n" + _BAR_OPUS})
w = gui.Watcher()
w._running = True
w.set_fable_config({"enabled": True, "all_windows": False, "delay": 180,
                    "steps": gui.DEFAULT_FABLE_STEPS, "windows": ["claude"]})
advance(gui.FABLE_DRIFT_GRACE_S + 5)
w._tick()
w._tick()
check("Y4 an explicitly ticked window is still steered",
      any("/model" in str(t) for t in texts_sent()))

# An unreadable status bar is UNKNOWN, not on-target: treating it as on-target
# reset the counters and made the cap unreachable.
reset([(1, "claude")], {1: "working away\n" + _BAR_OPUS})
w = gui.Watcher()
w._running = True
w.set_fable_config({"enabled": True, "all_windows": False, "delay": 180,
                    "steps": gui.DEFAULT_FABLE_STEPS, "windows": ["claude"]})
for _ in range(3):
    advance(gui.FABLE_DRIFT_GRACE_S + gui.FABLE_DRIFT_RETRY_S + 10)
    w._tick()
    w._tick()
    TEXTS[1] = "modal covers the bar\n4. Type something."   # unreadable
    advance(30)
    w._tick()
    TEXTS[1] = "working away\n" + _BAR_OPUS
check("Y5 an unreadable bar does not reset the drift cap",
      w._states[1].fable_drift_runs <= gui.FABLE_DRIFT_MAX)

# A /model we typed can land minutes later, after the quiet window expires.
# The clock alone cannot tell that from a hand switch, so we remember what we
# asked for.
reset([(1, "claude")], {1: "working away\n" + _BAR_FABLE})
w = gui.Watcher()
w._running = True
w.set_fable_config({"enabled": True, "all_windows": False, "delay": 180,
                    "steps": "/model opus\ncontinue", "windows": ["claude"]})
w._states[1] = gui._WState(hwnd=1, title="claude")
w._states[1].fable_last_model = "Fable 5"
w._states[1].fable_our_models.add("opus")
unticked = []
w.fable_untick.connect(lambda k: unticked.append(k))
advance(gui.FABLE_USER_SWITCH_QUIET_S + 300)     # long past the quiet window
TEXTS[1] = "working away\n" + _BAR_OPUS
w._tick()
check("Y6 a late landing of a model WE asked for is not blamed on the user",
      w._states[1].fable_user_optout is False and unticked == [])


# =============================================================================
print("---- W: the in-app help must not describe itself into a trigger ----")
# The help text explains the very phrases the detectors look for, so it is the
# most natural place to accidentally paste a real banner. It is rendered into
# a dialog, but it also lives in the source, which people read in watched
# terminals.
_help = gui.HELP_HTML.format(version=gui.APP_VERSION)
check("W1 help formats with the version", gui.APP_VERSION in _help)
for _name, _fn in (
    ("safeguard notice", ac.parse_fable_refusal),
    ("safeguard picker", ac.parse_fable_picker),
    ("switch-model dialog", ac.parse_switch_model_prompt),
    ("limit picker", ac.parse_limit_prompt),
    ("connection error", ac.parse_econnreset_stuck),
    ("truncated response", ac.parse_server_error_stuck),
    ("oauth expired", ac.parse_oauth_expired),
):
    check(f"W2 help does not trip the {_name} detector", _fn(_help) is False)
check("W3 help does not trip the limit banner",
      ac.parse_limit_message(_help) is None)


# =============================================================================
print("---- AA: the script must be able to leave the blocked model itself ----")
# Measured live 2026-08-09. The v2.0.1 default leaned on Claude Code offering a
# picker whose first option is the fallback; <confirm> pressed Enter on it and
# Claude Code did the switching. The current build offers no picker — the
# safeguard message is plain text — so the run confirmed nothing, ran
# `/model fable` against a session already on Fable, and closed by resending
# the blocked message to the model that had just refused it. Blocked again 7
# seconds later, and the run still logged success.
_steps_now = gui._parse_recovery_steps(gui.DEFAULT_FABLE_STEPS)
_targets = [gui._model_step_target(a) for k, a in _steps_now if k == "send"]
_targets = [t for t in _targets if t]
check("AA1 the default switches away from the target, not just back to it",
      len(set(t.lower() for t in _targets)) >= 2)
check("AA2 and still ends on the target",
      gui._last_model_step(_steps_now) == "fable")

# A /model step against the model already showing is skipped: typed into a
# running turn it queues, and the switch dialog surfaces minutes later with
# nobody left to confirm it.
def _drive(text, steps, ticks=4):
    """Latch the notice and run the script out. The first tick only latches;
    sends start on the next one."""
    reset([(1, "claude")], {1: text})
    wa = new_watcher(steps=steps)
    for _ in range(ticks):
        wa._tick()
        advance(1)
    return wa


_drive(NOTICE + "\n" + _BAR_F, "/model fable\ncontinue")
check("AA3 /model skipped when the bar already reads that model",
      ["/model fable"] not in texts_sent())
check("AA4 the run still advances past the skipped step",
      texts_sent() == [["continue"]])

_drive(NOTICE + "\n" + _BAR_O, "/model fable")
check("AA5 /model IS sent when the bar reads a different model",
      texts_sent() == [["/model fable"]])

# An unreadable bar must not read as "already there" — acting is the safe
# answer when we cannot tell where the session is.
_drive(NOTICE + "\n4. Type something.\n5. Chat about this", "/model fable")
check("AA6 an unreadable bar does not suppress the switch",
      texts_sent() == [["/model fable"]])

check("AA7 prefix match tolerates the bar's version and variant",
      gui._model_matches("Opus 5 (1M context)", "opus") is True
      and gui._model_matches("Fable 5", "fable") is True
      and gui._model_matches("Opus 5", "fable") is False
      and gui._model_matches(None, "opus") is False)

# Ending on the right model is not the same as having cleared the block.
reset([(1, "claude")], {1: NOTICE + "\n" + _BAR_F})
w = new_watcher(steps="continue")
LOGS = []
w.log.connect(lambda k, m: LOGS.append((k, m)))
w._tick()
advance(1)
w._tick()
check("AA8 a run that left the notice standing is not reported as success",
      any(k == "warn" and "NOT cleared" in m for k, m in LOGS)
      and not any("Fable-recover done" in m for k, m in LOGS))


# =============================================================================
print("---- Z: the Advanced dialog must fit on a normal screen ----")
# A QLabel without setWordWrap reports its whole one-line text as its minimum
# width, and a layout can never go below a child's minimum — so one forgotten
# wrap silently overrides resize() and the dialog opens wider than the screen.
from PyQt6.QtWidgets import QLabel                       # noqa: E402

_dlg = gui.AdvancedDialog(
    None,
    {"enabled": True, "windows": ["some-window"], "steps": "continue"},
    {"some-window": "✳ some-window", "other": "⠂ other"},
    {},
)
_MAXW = 900                        # comfortably inside a 1366-wide laptop
check(f"Z1 dialog opens under {_MAXW}px wide", _dlg.width() <= _MAXW)
# The real invariant: resize() can never go below a child's minimum, so if the
# minimum exceeds the size we ask for, the resize() call is a no-op and the
# dialog opens as wide as its longest line of text.
check("Z2 the requested size is actually achievable "
      f"(min {_dlg.minimumSizeHint().width()} <= {_dlg.width()})",
      _dlg.minimumSizeHint().width() <= _dlg.width())

_unwrapped = [
    lbl.text()[:60] for lbl in _dlg.findChildren(QLabel)
    if len(lbl.text()) > 55 and not lbl.wordWrap()
]
check("Z3 every long label wraps: " + (_unwrapped[0] if _unwrapped else "-"),
      not _unwrapped)

# Both tabs, not just the one that happens to be showing: a hidden tab's
# labels still contribute to the dialog's minimum width.
_dlg.deleteLater()


print()
print("RESULT:", "ALL OK" if not failures else f"{failures} FAILURE(S)")
sys.exit(1 if failures else 0)
