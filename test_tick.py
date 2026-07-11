# -*- coding: utf-8 -*-
"""State-machine tests for auto_continue.tick() — the per-window decision
loop. Uses stub windows, recorded sends and a controllable clock: no real
UIA, no real keystrokes. Catches the regressions unit-testing the regexes
alone cannot (re-arm after cooldown, skip-when-handled, network/pending
interplay, the limit-picker flow).

Plain harness like test_parse.py: exits non-zero on any failure.
"""
import sys
sys.stdout.reconfigure(encoding="utf-8")

from datetime import datetime, timedelta

import pytz

import auto_continue as ac

failures = 0


def check(label, cond):
    global failures
    print(f"[{'OK ' if cond else 'FAIL'}] {label}")
    if not cond:
        failures += 1


# --- controllable clock -----------------------------------------------------

class FakeDT(datetime):
    _now = None  # aware UTC datetime

    @classmethod
    def now(cls, tz=None):
        if tz is None:
            # naive local (only the unknown-tz fallback path uses this)
            return cls._now.astimezone().replace(tzinfo=None)
        return cls._now.astimezone(tz)


ac.datetime = FakeDT


def set_now(dt_utc):
    FakeDT._now = dt_utc


# --- stub windows + recorded sends -------------------------------------------

class FakeWin:
    def __init__(self, hwnd, title):
        self.NativeWindowHandle = hwnd
        self.Name = title


class BadTitleWin:
    """Simulates a window whose UIA Name read raises (COMError)."""
    def __init__(self, hwnd):
        self.NativeWindowHandle = hwnd

    @property
    def Name(self):
        raise RuntimeError("simulated COMError")


class Args:
    interval = 30
    buffer = 20
    retry_interval = 30
    match = ""
    dry_run = False


WINDOWS = []
TEXTS = {}
SENT = []


def _fake_send_lines(w, lines, dry_run=False):
    SENT.append((int(w.NativeWindowHandle), list(lines)))
    return True


ac.find_terminal_windows = lambda: WINDOWS
ac.read_terminal_text = lambda w: TEXTS.get(int(w.NativeWindowHandle), "")
ac.send_text_lines = _fake_send_lines
ac.send_continue = lambda w, dry_run=False: _fake_send_lines(
    w, ["continue"], dry_run)

COOLDOWN = timedelta(minutes=15)
BUFFER = timedelta(seconds=20)
RETRY_IVL = timedelta(seconds=30)


def tick(states):
    ac.tick(states, Args, COOLDOWN, BUFFER, RETRY_IVL)


# Realistic message bodies (concatenation-split so viewing THIS file in a
# watched terminal can't false-trigger the detectors).
BANNER = ("You've hit your li" "mit · resets 11pm (Asia/Shanghai)\n"
          "/upgra" "de to increase your usage limit.")
BANNER2 = ("You've hit your li" "mit · resets 1:30am (Asia/Shanghai)\n"
           "/upgra" "de to increase your usage limit.")
PICKER = ("What do you want to do?\n"
          "> 1. Stop and wait for li" "mit to reset\n"
          "  2. Upgrade your plan\n"
          "Enter to conf" "irm · Esc to cancel")
ECONN = "API Error: Unable to connect to API (ECONN" "RESET)"
SERVER_ERR = ("API Error: Server error mid-resp" "onse. "
              "The response above may be incomplete.")
OAUTH = "OAuth token has exp" "ired · Please run /log" "in"

# Base moment: 2026-07-03 10:00 UTC == 18:00 Asia/Shanghai.
T0 = datetime(2026, 7, 3, 10, 0, 0, tzinfo=pytz.UTC)
# 11pm Shanghai that evening == 15:00 UTC.
FIRE_UTC = datetime(2026, 7, 3, 15, 0, 0, tzinfo=pytz.UTC)


# =============================================================================
print("---- A: full lifecycle, no bogus re-arm after cooldown ----")
WINDOWS[:] = [FakeWin(101, "sess-a")]
TEXTS.clear()
SENT.clear()
states = {}

set_now(T0)
TEXTS[101] = BANNER
tick(states)
st = states[101]
check("A1 pending armed at 15:00 UTC",
      st.pending_reset_utc == FIRE_UTC and st.pending_reset_key is not None)
check("A2 nothing sent while waiting", not SENT)

# Reset time + buffer passes; message still on screen (session blocked).
set_now(FIRE_UTC + timedelta(seconds=25))
tick(states)
check("A3 fired once", SENT == [(101, ["continue"])])
check("A4 pending cleared, fired_key kept",
      st.pending_reset_utc is None and st.fired_key is not None)

# 16 minutes later (cooldown over) the message is STILL in the scrollback
# (short resumed turn). The old bug re-armed a pending for TOMORROW 11pm.
SENT.clear()
set_now(FIRE_UTC + timedelta(minutes=16))
tick(states)
check("A5 stale message does not re-arm after cooldown",
      st.pending_reset_utc is None)

# Message scrolls away -> fired_key forgotten.
TEXTS[101] = "regular output, nothing special"
tick(states)
check("A6 fired_key cleared once message is gone", st.fired_key is None)

# A genuinely new limit arms again.
TEXTS[101] = BANNER2
tick(states)
check("A7 new limit re-arms", st.pending_reset_utc is not None
      and st.pending_reset_key[:2] == (1, 30))

# =============================================================================
print("---- B: fire skipped when user already handled it ----")
WINDOWS[:] = [FakeWin(102, "sess-b")]
TEXTS.clear()
SENT.clear()
states = {}

set_now(T0)
TEXTS[102] = BANNER
tick(states)
st = states[102]
check("B1 pending armed", st.pending_reset_utc == FIRE_UTC)

# User typed continue themselves; session moved on, banner scrolled away.
TEXTS[102] = "user continued manually\n" + ("y" * 7000)
set_now(FIRE_UTC + timedelta(seconds=25))
tick(states)
check("B2 no redundant continue sent", not SENT)
check("B3 pending cleared", st.pending_reset_utc is None)

# =============================================================================
print("---- C: interactive limit picker ----")
WINDOWS[:] = [FakeWin(103, "sess-c")]
TEXTS.clear()
SENT.clear()
states = {}

set_now(T0)
TEXTS[103] = PICKER
tick(states)
st = states[103]
check("C1 bare Enter sent to confirm picker", SENT == [(103, [""])])
check("C2 no cooldown started (banner must be detected right after)",
      st.last_sent_utc is None)

# 10s later, picker still open (Enter didn't land yet): rate-limited.
SENT.clear()
set_now(T0 + timedelta(seconds=10))
tick(states)
check("C3 resend rate-limited", not SENT)

# 35s later, still open: poke again.
set_now(T0 + timedelta(seconds=35))
tick(states)
check("C4 re-poked after retry interval", SENT == [(103, [""])])

# Enter landed: banner appears below the picker text -> arm the pending,
# and do NOT press Enter again.
SENT.clear()
TEXTS[103] = PICKER + "\n" + BANNER
set_now(T0 + timedelta(seconds=70))
tick(states)
check("C5 no Enter once banner shown", not SENT)
check("C6 pending armed from post-picker banner",
      st.pending_reset_utc == FIRE_UTC)
check("C7 prompt state cleared", st.prompt_last_sent_utc is None)

# =============================================================================
print("---- D: network outage while a pending elapses ----")
WINDOWS[:] = [FakeWin(104, "sess-d")]
TEXTS.clear()
SENT.clear()
states = {}

set_now(T0)
TEXTS[104] = BANNER
tick(states)
st = states[104]
check("D1 pending armed", st.pending_reset_utc == FIRE_UTC)

# Network dies before the reset moment; the error sits below the banner.
TEXTS[104] = BANNER + "\n" + ECONN
set_now(FIRE_UTC + timedelta(seconds=25))
SENT.clear()
tick(states)
check("D2 network poke sent", SENT == [(104, ["continue"])])
check("D3 elapsed pending absorbed by the network poke "
      "(no second continue queued)",
      st.pending_reset_utc is None and st.last_sent_utc is not None)

# Network recovers, banner still visible: cooldown + fired_key must both
# prevent another send.
TEXTS[104] = BANNER
SENT.clear()
set_now(FIRE_UTC + timedelta(minutes=16))
tick(states)
check("D4 no re-fire after recovery", not SENT
      and st.pending_reset_utc is None)

# =============================================================================
print("---- E: retry-exhausted banner ----")
WINDOWS[:] = [FakeWin(105, "sess-e")]
TEXTS.clear()
SENT.clear()
states = {}

set_now(T0)
TEXTS[105] = "Retrying in 0s · attempt 10/10"
tick(states)
st = states[105]
check("E1 poke on attempt N/N", SENT == [(105, ["continue"])])

SENT.clear()
set_now(T0 + timedelta(seconds=35))
tick(states)
check("E2 re-poke after retry interval", SENT == [(105, ["continue"])])

TEXTS[105] = "· Working…"
SENT.clear()
tick(states)
check("E3 recovery clears retry state", st.retry_last_sent_utc is None
      and not st.retry_logged and not SENT)

# =============================================================================
print("---- E2: server-error mid-response gets poked like a network stall ----")
WINDOWS[:] = [FakeWin(115, "sess-e2")]
TEXTS.clear()
SENT.clear()
states = {}

set_now(T0)
TEXTS[115] = SERVER_ERR
tick(states)
st = states[115]
check("E2a poke on server-error mid-response", SENT == [(115, ["continue"])])

# Still cut off after retry interval (500 burst): poke again.
SENT.clear()
set_now(T0 + timedelta(seconds=35))
tick(states)
check("E2b re-poke after retry interval", SENT == [(115, ["continue"])])

# A full turn completes; marker scrolls out of tail range -> retry cleared.
TEXTS[115] = "● here is the finished answer\n" + ("z" * 7000)
SENT.clear()
tick(states)
check("E2c recovery clears retry state once marker is stale",
      st.retry_last_sent_utc is None and not st.retry_logged and not SENT)

# =============================================================================
print("---- F: oauth-expired never gets keystrokes ----")
WINDOWS[:] = [FakeWin(106, "sess-f")]
TEXTS.clear()
SENT.clear()
states = {}

set_now(T0)
TEXTS[106] = OAUTH
tick(states)
check("F1 no keystrokes for oauth-expired", not SENT)
check("F2 warned once", states[106].oauth_logged)

# =============================================================================
print("---- G: one bad window can't blind the tick ----")
WINDOWS[:] = [BadTitleWin(107), FakeWin(108, "sess-g")]
TEXTS.clear()
SENT.clear()
states = {}

set_now(T0)
TEXTS[108] = BANNER
tick(states)
check("G1 bad-title window survived the tick", 107 in states)
check("G2 later window still processed",
      states[108].pending_reset_utc == FIRE_UTC)

# =============================================================================
print("---- H: closed windows drop state ----")
WINDOWS[:] = [FakeWin(108, "sess-g")]
tick(states)
check("H1 closed window state dropped", 107 not in states and 108 in states)

print()
print("RESULT:", "ALL OK" if failures == 0 else f"{failures} FAILURE(S)")
sys.exit(1 if failures else 0)
