# -*- coding: utf-8 -*-
"""Single-instance coordination tests (Windows named-event handshake).

Verifies the mechanism the frozen exe relies on:
  * _ShowListener fires show_requested when the SHOW event is signalled and
    then sets the ACK event (proving a live primary answered);
  * a stale/leaked mutex with no live listener never acks (so a second
    launch would take over instead of bricking).

Offscreen Qt, no real windows. Plain harness; exits non-zero on failure.
"""
import ctypes
import sys
from ctypes import wintypes

sys.stdout.reconfigure(encoding="utf-8")

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PyQt6.QtWidgets import QApplication
from PyQt6.QtCore import QTimer

app = QApplication([])
import gui

failures = 0


def check(label, cond):
    global failures
    print(f"[{'OK ' if cond else 'FAIL'}] {label}")
    if not cond:
        failures += 1


k = ctypes.WinDLL("kernel32", use_last_error=True)
k.CreateEventW.restype = wintypes.HANDLE
k.CreateEventW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.BOOL,
                           wintypes.LPCWSTR]
k.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]

# Unique names so a real running Auto-Continue can't perturb the test.
SHOW = "Local\\AC_TEST_SHOW"
ACK = "Local\\AC_TEST_ACK"
show_ev = k.CreateEventW(None, False, False, SHOW)
ack_ev = k.CreateEventW(None, False, False, ACK)
check("named events created", bool(show_ev) and bool(ack_ev))

# ---- 1. listener fires + acks ------------------------------------------------
got = {"n": 0}
lis = gui._ShowListener(show_ev, ack_ev)
lis.show_requested.connect(lambda: got.__setitem__("n", got["n"] + 1))
lis.start()


def signal_show():
    ev = k.CreateEventW(None, False, False, SHOW)  # opens existing
    k.SetEvent(ev)


QTimer.singleShot(300, signal_show)
QTimer.singleShot(1200, app.quit)
app.exec()

acked = k.WaitForSingleObject(ack_ev, 0) == 0  # ACK should be signalled
check("show_requested fired", got["n"] >= 1)
check("listener set ACK (live primary answered)", acked)

lis.stop()
lis.wait(1000)

# ---- 2. no listener => no ACK (stale-mutex takeover path) --------------------
# Drain any residual ACK, signal SHOW with nobody listening, ACK stays unset.
k.WaitForSingleObject(ack_ev, 0)
signal_show()
no_ack = k.WaitForSingleObject(ack_ev, 800) != 0  # WAIT_TIMEOUT (0x102)
check("no ACK when no live listener (=> new instance takes over)", no_ack)

print()
print("RESULT:", "ALL OK" if failures == 0 else f"{failures} FAILURE(S)")
sys.exit(1 if failures else 0)
