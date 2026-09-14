# -*- coding: utf-8 -*-
"""Tests for the local HTTP API.

These run against a REAL socket rather than by calling the handler directly:
the things most likely to be wrong here — a token check that passes when the
header is absent, a route that answers before authenticating, a listener that
stays up after the switch goes off — are all properties of the server as
assembled, and a hand-called handler would not show them.

Nothing here can type into a window: the send path is stubbed at the ctx
boundary, which is also where the GUI plugs the watcher in.
"""
import json
import sys
import urllib.error
import urllib.request

sys.path.insert(0, r"D:\claude\auto_continue")
sys.stdout.reconfigure(encoding="utf-8")

import local_api

PORT = 47901
_fails = 0


def check(label, cond):
    global _fails
    print(("[OK ] " if cond else "[FAIL] ") + label)
    if not cond:
        _fails += 1


def call(path, tok=None, body=None, port=PORT):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=(json.dumps(body).encode() if body is not None else None),
        method="POST" if body is not None else "GET")
    if tok is not None:
        req.add_header("X-AC-Token", tok)
    if body is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


print("---- A: the token is the whole fence ----")
sent = []
enabled = {"on": True}
ctx = {
    "version": "test",
    "token": local_api.new_token(),
    "enabled": lambda: enabled["on"],
    "snapshot": lambda: {"ts": 1, "windows": [{"hwnd": 291}]},
    "send": lambda h, lines: (sent.append((h, list(lines))), {"ok": True})[1],
}
api = local_api.LocalApi(ctx)
ok, err = api.start(PORT)
check(f"A0 listener came up ({err})", ok)
T = ctx["token"]

check("A1 no token is refused", call("/v1/version")[0] == 401)
check("A2 a wrong token is refused", call("/v1/version", "0" * 64)[0] == 401)
check("A3 an empty token is refused", call("/v1/version", "")[0] == 401)
# A token of a different length must not raise out of compare_digest; a 500
# here would be an unauthenticated caller crashing the handler thread.
check("A4 a short token is refused, not an error",
      call("/v1/version", "abc")[0] == 401)
check("A5 the right token is let through", call("/v1/version", T)[0] == 200)

print("---- B: the routes ----")
code, body = call("/v1/version", T)
check("B1 version names the app and the api level",
      body.get("app") == "Auto-Continue" and body.get("api") == local_api.API_VERSION)
# A companion offering remote approval races the tool's own auto-answer, so
# the setting has to be visible to it — otherwise its user presses "approve"
# on a watch and cannot tell why the prompt was sometimes already gone.
ctx["auto_permission"] = lambda: True
ctx["auto_choose"] = lambda: False
_, body = call("/v1/version", T)
check("B1a version reports the auto-answer switches",
      body.get("permission_autoanswer") is True
      and body.get("chooser_autoanswer") is False)
ctx["auto_permission"] = lambda: False
check("B1b and reports them live, not as a snapshot from startup",
      call("/v1/version", T)[1]["permission_autoanswer"] is False)
ctx["auto_permission"] = lambda: True
code, body = call("/v1/windows", T)
check("B2 windows returns the snapshot", body.get("windows") == [{"hwnd": 291}])
check("B3 a trailing slash is the same route", call("/v1/windows/", T)[0] == 200)
check("B4 an unknown route is 404", call("/v1/nope", T)[0] == 404)

print("---- C: send ----")
code, body = call("/v1/windows/0x123/send", T, {"lines": ["hi"]})
check("C1 a send reaches the handler with the parsed hwnd",
      body == {"ok": True} and sent and sent[-1][0] == 0x123)
check("C2 hex and decimal hwnds both parse",
      call("/v1/windows/291/send", T, {"lines": ["x"]})[1] == {"ok": True})
check("C3 an empty line list is rejected before anything is typed",
      call("/v1/windows/0x123/send", T, {"lines": []})[0] == 400
      and len(sent) == 2)
check("C4 a non-numeric hwnd is rejected",
      call("/v1/windows/zz/send", T, {"lines": ["x"]})[0] == 400)
def raw_post(path, tok, payload: bytes):
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}{path}",
                                 data=payload, method="POST")
    req.add_header("X-AC-Token", tok)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


code, body = raw_post("/v1/windows/0x123/send", T, b"{not json")
check("C5 a malformed body is rejected, not raised",
      code == 400 and body.get("reason") == "bad_body" and len(sent) == 2)
check("C6 send still needs the token",
      call("/v1/windows/0x123/send", None, {"lines": ["x"]})[0] == 401
      and len(sent) == 2)

print("---- D: the switch ----")
enabled["on"] = False
check("D1 every route reports disabled once the switch is off",
      call("/v1/version", T)[0] == 503 and call("/v1/windows", T)[0] == 503)
check("D2 and a send cannot get through either",
      call("/v1/windows/0x123/send", T, {"lines": ["x"]})[0] == 503
      and len(sent) == 2)
enabled["on"] = True

print("---- E: the discovery file ----")
# Point it somewhere disposable FIRST. These checks write a token and then
# delete it, and the real path is the one a running Auto-Continue is using —
# running this suite once destroyed the live instance's discovery file, so a
# client could no longer find a port that was still serving perfectly well.
# A test that reaches outside its own sandbox is a test that breaks things
# nobody thought were being tested.
import os
import tempfile
_tmpdir = tempfile.mkdtemp(prefix="ac-api-test-")
_real_discovery_path = local_api.discovery_path
local_api.discovery_path = lambda: os.path.join(_tmpdir, "local_api.json")
check("E0 the discovery checks are pointed away from the real file",
      local_api.discovery_path() != _real_discovery_path())

path = local_api.write_discovery(PORT, T, "9.9.9")
with open(path, encoding="utf-8") as fh:
    disc = json.load(fh)
check("E1 it carries the port, token and api level",
      disc["port"] == PORT and disc["token"] == T
      and disc["api"] == local_api.API_VERSION)
local_api.clear_discovery()
check("E2 switching off takes the token file with it",
      not os.path.exists(path))
check("E3 clearing twice is not an error",
      (local_api.clear_discovery() or True))
check("E4 and the real file was never touched",
      local_api.discovery_path() != _real_discovery_path())
local_api.discovery_path = _real_discovery_path

print("---- G: keys and focus ----")
keyed, focused = [], []
ctx["keys"] = lambda h, k: (keyed.append((h, k)), {"ok": True})[1]
ctx["focus"] = lambda h: (focused.append(h), {"ok": True})[1]
check("G1 a keyspec reaches the handler",
      call("/v1/windows/0x1a/keys", T, {"keyspec": "{Esc}"})[1] == {"ok": True}
      and keyed[-1] == (0x1a, "{Esc}"))
check("G2 an empty keyspec is rejected before anything is sent",
      call("/v1/windows/0x1a/keys", T, {"keyspec": ""})[0] == 400
      and len(keyed) == 1)
check("G3 focus needs no body",
      call("/v1/windows/0x1a/focus", T, {})[1] == {"ok": True}
      and focused[-1] == 0x1a)
check("G4 both still need the token",
      call("/v1/windows/0x1a/keys", None, {"keyspec": "{Esc}"})[0] == 401
      and len(keyed) == 1)

scanned = []
ctx["scan"] = lambda h: (scanned.append(h),
                         {"ok": True, "window": {"hwnd": h}})[1]
check("G5 scan takes no body and returns the refreshed row",
      call("/v1/windows/0x2b/scan", T, {})[1] == {"ok": True,
                                                 "window": {"hwnd": 0x2b}}
      and scanned[-1] == 0x2b)
check("G6 scan needs the token too",
      call("/v1/windows/0x2b/scan", None, {})[0] == 401
      and len(scanned) == 1)

print("---- H: text is behind its own switch ----")
text_on = {"on": False}
ctx["text_enabled"] = lambda: text_on["on"]
ctx["text"] = lambda h, tail: {"hwnd": h, "text": "x" * tail}
check("H1 off by default, and says which switch refused it",
      call("/v1/windows/0x1a/text", T)[0] == 403
      and call("/v1/windows/0x1a/text", T)[1]["reason"] == "text_disabled")
text_on["on"] = True
code, body = call("/v1/windows/0x1a/text", T)
check("H2 on, it returns the tail with the default length",
      code == 200 and len(body["text"]) == local_api.TEXT_TAIL_DEFAULT)
check("H3 tail= is honoured",
      len(call("/v1/windows/0x1a/text?tail=10", T)[1]["text"]) == 10)
check("H4 an absurd tail is clamped, not obeyed",
      len(call("/v1/windows/0x1a/text?tail=999999", T)[1]["text"])
      == local_api.TEXT_TAIL_MAX)
check("H5 a junk tail falls back instead of erroring",
      len(call("/v1/windows/0x1a/text?tail=abc", T)[1]["text"])
      == local_api.TEXT_TAIL_DEFAULT)

print("---- I: the event stream ----")
import threading as _th
seen_events = []


def _read_sse():
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}/v1/events")
    req.add_header("X-AC-Token", T)
    try:
        with urllib.request.urlopen(req, timeout=6) as r:
            ev = None
            for raw in r:
                line = raw.decode("utf-8").strip()
                if line.startswith("event:"):
                    ev = line.split(":", 1)[1].strip()
                elif line.startswith("data:") and ev:
                    seen_events.append((ev, line.split(":", 1)[1].strip()))
                    if len(seen_events) >= 2:
                        return
    except Exception:
        pass


t = _th.Thread(target=_read_sse, daemon=True)
t.start()
import time as _time
for _ in range(40):                     # wait for the subscription to land
    if api.broker.count:
        break
    _time.sleep(0.05)
check("I1 a subscriber is registered while it is connected",
      api.broker.count == 1)
api.broker.publish("fire", {"level": "fire", "msg": "hello"})
api.broker.publish("log", {"level": "info", "msg": "second"})
t.join(timeout=6)
check("I2 published events arrive, in order, with their names",
      len(seen_events) == 2
      and seen_events[0][0] == "fire" and "hello" in seen_events[0][1]
      and seen_events[1][0] == "log")
# A dead socket only announces itself when something is written to it, so
# the drop happens on the next event (or the next ping) rather than the
# instant the client vanishes. Push one so the cleanup is observable now
# instead of up to SSE_PING_S later.
for _ in range(40):
    if api.broker.count == 0:
        break
    api.broker.publish("log", {"level": "info", "msg": "poke"})
    _time.sleep(0.05)
check("I3 the subscriber is dropped once the client goes away",
      api.broker.count == 0)

# A subscriber that stops reading must not grow the queue without bound.
_q = api.broker.subscribe()
for i in range(local_api.SSE_QUEUE_MAX + 5):
    api.broker.publish("log", {"i": i})
check("I4 a subscriber that stops reading is dropped, not queued forever",
      api.broker.count == 0)

check("I5 the stream needs the token too",
      call("/v1/events", None)[0] == 401)

print("---- J: believing it is up is not the same as being up ----")
# Seen for real on 2026-09-13: connections were refused while the process was
# healthy and the OS still listed the port as LISTEN. Nothing checked, so the
# app went on believing it served, never restarted it and never said a word —
# with the discovery file still pointing clients at it.
check("J1 a live listener answers when knocked on", api.alive())
check("J2 running and alive agree while nothing is wrong",
      api.running and api.alive())
# Kill the socket underneath without going through stop(), which is what the
# failure looked like from outside: the object still thinks it is serving.
api._srv.shutdown()          # stop the loop first, or it raises on a dead
api._srv.server_close()      # socket and the noise looks like a real fault
check("J3 running still says yes — it is a memory, not a measurement",
      api.running)
check("J4 alive() tells the truth anyway", not api.alive())
api._srv = None
check("J5 with no server at all, alive() is false rather than an error",
      not api.alive())

api.stop()
check("F1 stop() releases the port", not api.running)
try:
    call("/v1/version", T)
    gone = False
except Exception:
    gone = True
check("F2 nothing answers once it is stopped", gone)

print()
print("RESULT: " + ("ALL OK" if _fails == 0 else f"{_fails} FAILURE(S)"))
sys.exit(1 if _fails else 0)
