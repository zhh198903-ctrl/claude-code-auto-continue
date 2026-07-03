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
    build_swap_bat, ASSET_NAME,
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
check_true("bat caps the wait loop", "if %tries% gtr 150 goto cleanup" in bat
           and "set /a tries+=1" in bat and ":cleanup" in bat)
check_true("bat relaunch line precedes cleanup label (skip on give-up)",
           bat.index('start ""') < bat.index(":cleanup"))
bat_no = build_swap_bat(r"C:\d\new.exe", r"C:\d\t.exe", relaunch=False)
check_true("no-relaunch omits start", 'start ""' not in bat_no)

print()
print("RESULT:", "ALL OK" if failures == 0 else f"{failures} FAILURE(S)")
sys.exit(1 if failures else 0)
