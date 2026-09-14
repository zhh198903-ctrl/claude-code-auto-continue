# -*- coding: utf-8 -*-
"""A local HTTP API so a companion program can read window state and type.

Why this lives in Auto-Continue rather than in the program that wants it:
driving a Claude Code window means enumerating it through UI Automation,
checking that the foreground actually landed on the target, typing, and
putting the old foreground back. Two programs doing that at once fight over
the foreground and double-type into sessions. So this tool stays the only
driver, and anything else asks it.

The API is deliberately generic and knows nothing about who is calling: it
binds 127.0.0.1 only and every request carries a token. What it hands out is
the ability to put keystrokes into the user's working sessions, so the token
is compared in constant time and the discovery file is written where only
that user can read it.

One capability is held back behind a second switch: `/text` returns what a
session has on screen -- the conversation, the code, whatever is there. That
is a different kind of exposure from listing windows and typing into them, so
it is off unless the user says otherwise, and says so about that specifically.
"""
import json
import os
import queue
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

API_VERSION = 1
DEFAULT_PORT = 47830
SEND_TIMEOUT_S = 10.0
# Two calls for the same window inside this gap are the caller repeating
# itself, not two separate intentions.
MIN_SEND_GAP_S = 0.5
# A rescan is read-only, so it is allowed more often than a keystroke — but
# not unlimited: each one is a real UIA read, and the whole reason this exists
# is to avoid spending those on windows nobody is waiting on.
MIN_SCAN_GAP_S = 1.0
# How long an idle SSE connection waits before a ping goes out. Without it a
# silent stream is indistinguishable from a dead one at the far end.
SSE_PING_S = 10.0
# A subscriber that stops reading must not grow a queue without limit; when it
# overflows the connection is dropped rather than the process.
SSE_QUEUE_MAX = 200
TEXT_TAIL_DEFAULT = 2000
TEXT_TAIL_MAX = 20000


def discovery_path() -> str:
    """Where a client looks to find the port and token.

    Under %APPDATA% rather than beside the exe: the exe may sit in a folder
    the user cannot write to, and a discovery file nobody can create is a
    discovery file nobody can use.
    """
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    return os.path.join(base, "Auto-Continue", "local_api.json")


def write_discovery(port: int, token: str, version: str) -> str:
    path = discovery_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"port": port, "token": token,
                   "version": version, "api": API_VERSION}, fh)
    os.replace(tmp, path)      # never leave a half-written token on disk
    return path


def clear_discovery() -> None:
    """Remove the discovery file when the API is switched off.

    Leaving it behind advertises a port that is not listening and a token that
    grants nothing, which reads to a client as "the server is broken" rather
    than "the user turned it off".
    """
    try:
        os.remove(discovery_path())
    except OSError:
        pass


def new_token() -> str:
    return secrets.token_hex(32)


class Broker:
    """Fan-out for the event stream.

    Publishers are Qt threads, subscribers are HTTP threads, so the only thing
    crossing between them is a bounded queue per subscriber. A subscriber that
    stops reading fills its queue and gets dropped; it must not be able to
    grow memory in the app it is watching, and it must not be able to block
    the publisher either -- the whole point of this tool is that the watcher
    keeps running.
    """

    def __init__(self):
        self._subs = []
        self._lock = threading.Lock()

    def subscribe(self) -> queue.Queue:
        q = queue.Queue(SSE_QUEUE_MAX)
        with self._lock:
            self._subs.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            if q in self._subs:
                self._subs.remove(q)

    @property
    def count(self) -> int:
        with self._lock:
            return len(self._subs)

    def publish(self, event: str, data) -> None:
        with self._lock:
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait((event, data))
            except queue.Full:
                # Not reading? Then stop being told. Dropping the slowest
                # subscriber is better than stalling the publisher.
                self.unsubscribe(q)


class _Handler(BaseHTTPRequestHandler):
    server_version = "AutoContinue"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    # --- plumbing --------------------------------------------------------
    def log_message(self, fmt, *args):
        pass          # the app keeps its own log; stderr here goes nowhere

    def _reply(self, code: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except OSError:
            pass      # client hung up mid-reply; nothing to salvage

    def _authed(self) -> bool:
        want = getattr(self.server, "ctx", {}).get("token") or ""
        got = self.headers.get("X-AC-Token") or ""
        # Constant time: this token is the only thing between any local
        # process and the user's sessions.
        return bool(want) and secrets.compare_digest(want, got)

    def _guard(self) -> bool:
        ctx = getattr(self.server, "ctx", {})
        if not ctx.get("enabled", lambda: False)():
            self._reply(503, {"ok": False, "reason": "disabled"})
            return False
        if not self._authed():
            self._reply(401, {"ok": False, "reason": "unauthorized"})
            return False
        return True

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    @staticmethod
    def _parts(path: str) -> list:
        return [p for p in path.split("?", 1)[0].rstrip("/").split("/") if p]

    # --- routes ----------------------------------------------------------
    def do_GET(self):
        if not self._guard():
            return
        ctx = self.server.ctx
        raw = self.path.split("?", 1)
        path = raw[0].rstrip("/")
        parts = self._parts(self.path)
        if path == "/v1/version":
            # The auto-answer switches are reported because a companion
            # program offering remote approval is in a race with them: this
            # tool answers a permission prompt within a pass, so a human
            # deciding on a watch may find the prompt already gone. Reporting
            # the setting lets that program say so, instead of the user
            # wondering why approval sometimes does nothing.
            self._reply(200, {"app": "Auto-Continue",
                              "version": ctx.get("version", ""),
                              "api": API_VERSION,
                              "permission_autoanswer":
                                  bool(ctx.get("auto_permission",
                                               lambda: False)()),
                              "chooser_autoanswer":
                                  bool(ctx.get("auto_choose",
                                               lambda: False)())})
        elif path == "/v1/windows":
            self._reply(200, ctx["snapshot"]())
        elif path == "/v1/events":
            self._stream_events()
        elif (len(parts) == 4 and parts[0] == "v1" and parts[1] == "windows"
                and parts[3] == "text"):
            if not ctx.get("text_enabled", lambda: False)():
                # Deliberately its own switch: listing windows and typing into
                # them is one thing, handing over what a session has on screen
                # is another, and the user agreed to the first.
                self._reply(403, {"ok": False, "reason": "text_disabled"})
                return
            try:
                hwnd = int(parts[2], 0)
            except ValueError:
                self._reply(400, {"ok": False, "reason": "bad_hwnd"})
                return
            tail = TEXT_TAIL_DEFAULT
            if len(raw) > 1:
                for kv in raw[1].split("&"):
                    k, _, v = kv.partition("=")
                    if k == "tail":
                        try:
                            tail = max(1, min(TEXT_TAIL_MAX, int(v)))
                        except ValueError:
                            pass
            self._reply(200, ctx["text"](hwnd, tail))
        else:
            self._reply(404, {"ok": False, "reason": "no_route"})

    def do_POST(self):
        if not self._guard():
            return
        ctx = self.server.ctx
        parts = self._parts(self.path)
        if parts == ["v1", "settings"]:
            # The only route that changes what the user configured. Kept to a
            # whitelist of two booleans and refused outright for anything else:
            # a caller holding the token can already type into sessions, so
            # this is not new power — but a checkbox changing underneath the
            # person who ticked it is a surprise, and surprises are what the
            # log line and the live checkbox update are for.
            try:
                data = self._body()
            except Exception:
                self._reply(400, {"ok": False, "reason": "bad_body"})
                return
            if (not isinstance(data, dict) or not data
                    or set(data) - {"auto_permission", "auto_choose"}
                    or any(not isinstance(v, bool) for v in data.values())):
                self._reply(400, {"ok": False, "reason": "bad_body"})
                return
            self._reply(200, ctx["settings"](data))
            return
        if not (len(parts) == 4 and parts[0] == "v1" and parts[1] == "windows"):
            self._reply(404, {"ok": False, "reason": "no_route"})
            return
        verb = parts[3]
        if verb not in ("send", "keys", "focus", "scan"):
            self._reply(404, {"ok": False, "reason": "no_route"})
            return
        try:
            hwnd = int(parts[2], 0)
        except ValueError:
            self._reply(400, {"ok": False, "reason": "bad_hwnd"})
            return
        if verb == "focus":
            self._reply(200, ctx["focus"](hwnd))
            return
        if verb == "scan":
            # No body, and no typing: a caller may poll this every few
            # seconds, so it reads and returns and does nothing else.
            self._reply(200, ctx["scan"](hwnd))
            return
        try:
            data = self._body()
        except Exception:
            self._reply(400, {"ok": False, "reason": "bad_body"})
            return
        if verb == "send":
            lines = [str(x) for x in (data.get("lines") or [])]
            if not lines:
                self._reply(400, {"ok": False, "reason": "no_lines"})
                return
            self._reply(200, ctx["send"](hwnd, lines))
        else:
            keyspec = str(data.get("keyspec") or "")
            if not keyspec:
                self._reply(400, {"ok": False, "reason": "no_keyspec"})
                return
            self._reply(200, ctx["keys"](hwnd, keyspec))

    # --- SSE -------------------------------------------------------------
    def _stream_events(self) -> None:
        """Hold the connection open and write events as they happen.

        Runs on this request's own thread for as long as the client stays, so
        the server has to be a threading one -- which it is. Everything that
        can end the stream (client gone, queue overflow, shutdown) ends it by
        returning, never by raising into the server loop.
        """
        broker = self.server.ctx.get("broker")
        if broker is None:
            self._reply(404, {"ok": False, "reason": "no_route"})
            return
        q = broker.subscribe()
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            while True:
                try:
                    event, data = q.get(timeout=SSE_PING_S)
                except queue.Empty:
                    # A silent stream and a dead one look identical to the
                    # client until something arrives, so say something.
                    event, data = "ping", {"ts": int(time.time() * 1000)}
                payload = json.dumps(data, ensure_ascii=False)
                chunk = f"event: {event}\ndata: {payload}\n\n".encode("utf-8")
                self.wfile.write(chunk)
                self.wfile.flush()
        except (OSError, ValueError):
            pass          # client went away, or the socket closed under us
        finally:
            broker.unsubscribe(q)


class _Server(ThreadingHTTPServer):
    """ThreadingHTTPServer that does not shout when a client walks away.

    An SSE subscriber closing its connection is the normal end of a stream,
    but the stock handler prints a traceback for the resulting reset. Left
    alone that noise goes to stderr on every disconnect, where it looks like a
    fault in logs and test output — and a source of routine false alarms is
    how a real one ends up ignored.
    """

    def handle_error(self, request, client_address):
        import sys as _sys
        exc = _sys.exc_info()[1]
        if isinstance(exc, (ConnectionError, BrokenPipeError, OSError)):
            return
        super().handle_error(request, client_address)


class LocalApi:
    """Owns the listening socket and nothing else.

    Everything real is handed back through `ctx` callables, so this object has
    no opinion about windows, tokens or typing. That keeps the thread boundary
    obvious: all of this runs on HTTP threads, and anything touching UI
    Automation is somebody else's problem to marshal onto the watcher.
    """

    def __init__(self, ctx: dict):
        self.ctx = ctx
        ctx.setdefault("broker", Broker())
        self._srv = None
        self._thread = None
        self._port = 0

    @property
    def broker(self) -> Broker:
        return self.ctx["broker"]

    @property
    def running(self) -> bool:
        """What this object BELIEVES. Not evidence — see alive()."""
        return self._srv is not None

    @property
    def port(self) -> int:
        return self._port

    def alive(self, timeout: float = 1.0) -> bool:
        """Whether the listener actually answers a connection.

        `running` is a field this object sets when it starts a thread; it stays
        True whatever happens to the socket afterwards. Seen for real on
        2026-09-13: connections were refused while the process was healthy and
        the OS connection table still listed the port as LISTEN, and because
        nothing checked, the app went on believing it was serving, never
        restarted it, and never said a word — with the discovery file still on
        disk pointing clients at it. A field is a memory of an intention; this
        is the question actually being asked.
        """
        if self._srv is None or not self._port:
            return False
        import socket as _socket
        try:
            with _socket.create_connection(("127.0.0.1", self._port),
                                           timeout):
                return True
        except OSError:
            return False

    def start(self, port: int) -> tuple:
        """(ok, error). A port already in use is reported, not raised: the
        app has to keep watching windows whether or not the API came up."""
        if self._srv is not None:
            return True, ""
        try:
            srv = _Server(("127.0.0.1", int(port)), _Handler)
        except OSError as exc:
            return False, str(exc)
        srv.ctx = self.ctx
        srv.daemon_threads = True
        self._srv = srv
        # Remembered so alive() has somewhere to knock. Taken from the socket
        # rather than the argument so port 0 (pick one for me) also works.
        self._port = int(srv.server_address[1])
        self._thread = threading.Thread(
            target=srv.serve_forever, kwargs={"poll_interval": 0.5},
            name="auto-continue-local-api", daemon=True)
        self._thread.start()
        return True, ""

    def stop(self) -> None:
        srv, self._srv = self._srv, None
        if srv is None:
            return
        try:
            srv.shutdown()
            srv.server_close()
        except Exception:
            pass
        self._thread = None
