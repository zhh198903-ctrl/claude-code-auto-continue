# -*- coding: utf-8 -*-
"""Unit tests for updater.py — version compare, release-JSON normalize,
sha256, and swap-bat generation. No network, no Qt. Plain harness like
test_parse.py: exits non-zero on any failure.
"""
import os
import sys
import tempfile
import hashlib

sys.stdout.reconfigure(encoding="utf-8")

from updater import (
    parse_version, is_newer, _normalize_release, verify_sha256,
    build_swap_bat, update_failure_marker_path, ASSET_NAME,
)

failures = 0


def check(label, got, expected):
    global failures
    ok = got == expected
    print(f"[{'OK ' if ok else 'FAIL'}] {label}: got={got!r} expected={expected!r}")
    if not ok:
        failures += 1


def check_true(label, cond):
    global failures
    print(f"[{'OK ' if cond else 'FAIL'}] {label}: {cond}")
    if not cond:
        failures += 1


# ---- parse_version ----
print("---- parse_version ----")
check("v1.0.8", parse_version("v1.0.8"), (1, 0, 8))
check("1.0.8", parse_version("1.0.8"), (1, 0, 8))
check("V1.2", parse_version("V1.2"), (1, 2))
check("1.0.8-rc1", parse_version("1.0.8-rc1"), (1, 0, 8))
check("1.0.10", parse_version("1.0.10"), (1, 0, 10))
check("garbage", parse_version("garbage"), (0,))
check("empty", parse_version(""), (0,))
check("trailing junk seg", parse_version("2.0.x"), (2, 0, 0))

# ---- is_newer ----
print("\n---- is_newer ----")
check("1.0.8 > 1.0.7", is_newer("1.0.8", "1.0.7"), True)
check("1.0.7 == 1.0.7", is_newer("1.0.7", "1.0.7"), False)
check("1.0.7 < 1.0.8", is_newer("1.0.7", "1.0.8"), False)
check("1.1 > 1.0.9", is_newer("1.1", "1.0.9"), True)
check("v-prefix both", is_newer("v2.0.0", "v1.9.9"), True)
check("1.0.10 > 1.0.9", is_newer("1.0.10", "1.0.9"), True)
check("same padded", is_newer("1.0", "1.0.0"), False)

# ---- _normalize_release ----
print("\n---- _normalize_release ----")
full = {
    "tag_name": "v1.0.8",
    "html_url": "https://github.com/o/r/releases/tag/v1.0.8",
    "assets": [
        {"name": "other.txt", "browser_download_url": "http://x/other",
         "size": 1},
        {"name": ASSET_NAME,
         "browser_download_url": "https://github.com/o/r/releases/download/v1.0.8/Auto-Continue.exe",
         "size": 57229443,
         "digest": "sha256:43d25cf7c4fd7073d34e35e95bb3208b2c47d93d2b3ee8d66ac91960fe79eccd"},
    ],
}
n = _normalize_release(full)
check_true("full: not None", n is not None)
check("full.tag", n["tag"], "v1.0.8")
check("full.version", n["version"], "1.0.8")
check("full.asset_size", n["asset_size"], 57229443)
check("full.sha256", n["sha256"],
      "43d25cf7c4fd7073d34e35e95bb3208b2c47d93d2b3ee8d66ac91960fe79eccd")
check("full.html_url", n["html_url"],
      "https://github.com/o/r/releases/tag/v1.0.8")
check_true("full.asset_url is the exe",
           n["asset_url"].endswith("/Auto-Continue.exe"))

no_digest = {
    "tag_name": "v1.0.9",
    "assets": [{"name": ASSET_NAME, "browser_download_url": "http://x/exe",
                "size": 10}],
}
nd = _normalize_release(no_digest)
check_true("no_digest: not None", nd is not None)
check("no_digest.sha256 is None", nd["sha256"], None)

no_asset = {"tag_name": "v1.0.9", "assets": [{"name": "readme.md",
            "browser_download_url": "http://x/r"}]}
check("no matching asset -> None", _normalize_release(no_asset), None)
check("non-dict -> None", _normalize_release("nope"), None)
check("empty assets -> None", _normalize_release({"tag_name": "v1", "assets": []}), None)

# ---- verify_sha256 ----
print("\n---- verify_sha256 ----")
tmp = os.path.join(tempfile.gettempdir(), "ac_sha_test.bin")
with open(tmp, "wb") as f:
    f.write(b"hello auto-continue updater")
real = hashlib.sha256(b"hello auto-continue updater").hexdigest()
check("correct hash", verify_sha256(tmp, real), True)
check("upper-case hash", verify_sha256(tmp, real.upper()), True)
check("wrong hash", verify_sha256(tmp, "deadbeef"), False)
check("None expected -> True (best effort)", verify_sha256(tmp, None), True)
check("empty expected -> True", verify_sha256(tmp, ""), True)
os.remove(tmp)

# ---- build_swap_bat ----
print("\n---- build_swap_bat ----")
bat = build_swap_bat(r"C:\dir\Auto-Continue.exe.new",
                     r"C:\dir\Auto-Continue.exe", relaunch=True)
check_true("bat has chcp utf8", "chcp 65001" in bat)
check_true("bat moves new->target",
           'move /y "C:\\dir\\Auto-Continue.exe.new" "C:\\dir\\Auto-Continue.exe"' in bat)
check_true("bat uses ping (not timeout)", "ping -n 2 127.0.0.1" in bat and "timeout" not in bat)
check_true("bat retries on lock (errorlevel 1)", "if errorlevel 1" in bat)
check_true("bat relaunches", 'start "" "C:\\dir\\Auto-Continue.exe"' in bat)
check_true("bat self-deletes", '(goto) 2>nul & del "%~f0"' in bat)
check_true("bat caps the wait loop", "if %tries% gtr 150 goto giveup" in bat
           and "set /a tries+=1" in bat and ":giveup" in bat)

# Regression (the actual bug): give-up used to jump straight to :cleanup,
# placed AFTER the relaunch line, so a permanent failure skipped relaunch
# entirely and left the user with no Auto-Continue running at all (the
# caller already quit the app to drop the file lock before this bat runs).
# Both the success path and the give-up path must now reach the SAME
# relaunch line before self-delete.
relaunch_idx = bat.index('start ""')
giveup_idx = bat.index(":giveup")
cleanup_idx = bat.index(":cleanup")
success_skip_idx = bat.index("goto relaunch")
check_true("success path jumps past the give-up handling",
           success_skip_idx < giveup_idx)
check_true("give-up label precedes the relaunch line (give-up reaches it)",
           giveup_idx < relaunch_idx)
check_true("relaunch line precedes cleanup (runs before self-delete)",
           relaunch_idx < cleanup_idx)

# Regression: give-up must leave a marker behind. By the time this bat runs
# the app has already quit, so a silent give-up (old behavior) left no
# running app AND no trace of what happened — the exact silent overnight
# stall this tool exists to prevent.
marker = update_failure_marker_path(r"C:\dir\Auto-Continue.exe")
check("marker path sits next to the target exe", marker,
      r"C:\dir\Auto-Continue.exe.update-failed.txt")
check_true("bat writes the marker file", f'"{marker}"' in bat)
marker_idx = bat.index(f'"{marker}"')
check_true("marker is written on the give-up path, before relaunch",
           giveup_idx < marker_idx < relaunch_idx)

# relaunch=False must omit the relaunch line on BOTH paths (respected by
# tests / callers that don't want the bat to start anything) — but the
# give-up marker is unconditional: visibility into a permanent failure
# shouldn't depend on whether the caller wanted a relaunch.
bat_no = build_swap_bat(r"C:\d\new.exe", r"C:\d\t.exe", relaunch=False)
check_true("no-relaunch omits start", 'start ""' not in bat_no)
marker_no = update_failure_marker_path(r"C:\d\t.exe")
check_true("no-relaunch still writes the give-up marker",
           f'"{marker_no}"' in bat_no)
check_true("no-relaunch: give-up label still precedes cleanup",
           bat_no.index(":giveup") < bat_no.index(":cleanup"))

# =============================================================================
print()
print("---- download resumes instead of starting over ----")
# On a link where tens of MB move at tens of KB/s, a drop that restarts the
# transfer can mean it never finishes. The partial file is the offset for the
# next attempt; it is only renamed to the real name once complete, so nothing
# half-written is ever runnable.
import io                                                   # noqa: E402
import os as _os                                            # noqa: E402
import tempfile as _tf                                      # noqa: E402
import updater                                              # noqa: E402

PAYLOAD = bytes(range(256)) * 40          # 10240 bytes
_saved_urlopen = updater.urllib.request.urlopen


class _Resp(io.BytesIO):
    """Enough of an HTTP response for the downloader."""

    def __init__(self, body, status, headers):
        super().__init__(body)
        self.status = status
        self.headers = headers

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _make_server(cut_after, log):
    """Serves PAYLOAD, honouring Range, truncating the body at `cut_after`
    bytes on the FIRST call only — i.e. one dropped connection."""
    state = {"calls": 0}

    def fake_urlopen(req, timeout=None, context=None):
        state["calls"] += 1
        rng = req.headers.get("Range") or req.headers.get("range")
        start = 0
        if rng:
            start = int(rng.split("=")[1].split("-")[0])
        log.append(start)
        body = PAYLOAD[start:]
        status = 206 if rng else 200
        # Content-Length always states what SHOULD arrive. A real
        # interruption keeps that promise and stops delivering; shortening
        # the header to match the truncated body would make a half file look
        # complete, which is the very thing the size check catches.
        declared = len(body)
        if state["calls"] == 1 and cut_after is not None:
            body = body[:cut_after]
        return _Resp(body, status, {"Content-Length": str(declared)})

    return fake_urlopen


_tmp = _tf.mkdtemp()
_dest = _os.path.join(_tmp, "asset.bin")
_log = []
updater.urllib.request.urlopen = _make_server(3000, _log)
try:
    updater.download_asset("https://example.invalid/asset.bin", _dest,
                           attempts=4, retry_wait=0)
finally:
    updater.urllib.request.urlopen = _saved_urlopen

check_true("resume: the file is complete and correct",
      _os.path.exists(_dest) and open(_dest, "rb").read() == PAYLOAD)
check_true(f"resume: the retry asked from where it stopped (offsets {_log})",
      len(_log) >= 2 and _log[0] == 0 and _log[1] == 3000)
check_true("resume: no .part left behind", not _os.path.exists(_dest + ".part"))

# A server that ignores Range answers 200 with the whole body; appending
# would splice two copies together, so the partial must be discarded.
_dest2 = _os.path.join(_tmp, "asset2.bin")
open(_dest2 + ".part", "wb").write(b"stale bytes that must not survive")
_log2 = []


def _ignores_range(req, timeout=None, context=None):
    _log2.append(req.headers.get("Range"))
    return _Resp(PAYLOAD, 200, {"Content-Length": str(len(PAYLOAD))})


updater.urllib.request.urlopen = _ignores_range
try:
    updater.download_asset("https://example.invalid/asset2.bin", _dest2,
                           attempts=2, retry_wait=0)
finally:
    updater.urllib.request.urlopen = _saved_urlopen

check_true("a server that ignores Range restarts cleanly, no seam",
      open(_dest2, "rb").read() == PAYLOAD)

# A body that stops early is not a socket error — the size has to be checked
# before the rename, or a truncated exe gets installed.
_dest3 = _os.path.join(_tmp, "asset3.bin")
updater.urllib.request.urlopen = _make_server(None, [])


def _always_short(req, timeout=None, context=None):
    return _Resp(PAYLOAD[:100], 200, {"Content-Length": str(len(PAYLOAD))})


updater.urllib.request.urlopen = _always_short
_raised = False
try:
    updater.download_asset("https://example.invalid/asset3.bin", _dest3,
                           attempts=2, retry_wait=0)
except Exception:
    _raised = True
finally:
    updater.urllib.request.urlopen = _saved_urlopen

check_true("a truncated body raises instead of installing a short file",
      _raised and not _os.path.exists(_dest3))


print()
print("RESULT:", "ALL OK" if failures == 0 else f"{failures} FAILURE(S)")
sys.exit(1 if failures else 0)
