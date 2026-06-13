# -*- coding: utf-8 -*-
"""GitHub-based self-update logic for the Auto-Continue onefile build.

Pure stdlib (urllib / ssl / hashlib / json / subprocess / tempfile) — no Qt,
no third-party deps — so this module is unit-testable and adds nothing to the
PyInstaller spec. The Qt wrapper (Updater QObject + its thread) lives in
gui.py; this file holds the network + version + self-replace mechanics.

Self-update only works in a frozen build (the running onefile .exe). When run
from source, is_frozen() is False and stage_and_swap() refuses — you can't
replace a .py.
"""
from __future__ import annotations

import hashlib
import json
import os
import ssl
import subprocess
import sys
import tempfile
import urllib.request
from typing import Callable, Optional

GITHUB_OWNER = "zhh198903-ctrl"
GITHUB_REPO = "claude-code-auto-continue"
ASSET_NAME = "Auto-Continue.exe"
_USER_AGENT = "Auto-Continue-Updater"  # GitHub REST requires a User-Agent


# ---------------------------------------------------------------------------
# Version comparison (no `packaging` dependency)
# ---------------------------------------------------------------------------


def parse_version(v: str) -> tuple[int, ...]:
    """'v1.0.8' / '1.0.8-rc1' -> (1, 0, 8).

    Strips a leading 'v', splits on '.', and keeps the leading numeric run of
    each segment (so a pre-release/build suffix like '-rc1' is dropped). A
    non-numeric segment becomes 0. Never raises.
    """
    if not v:
        return (0,)
    v = v.strip()
    if v[:1] in ("v", "V"):
        v = v[1:]
    parts: list[int] = []
    for seg in v.split("."):
        digits = ""
        for ch in seg.strip():
            if ch.isdigit():
                digits += ch
            else:
                break
        parts.append(int(digits) if digits else 0)
    return tuple(parts) if parts else (0,)


def is_newer(remote: str, local: str) -> bool:
    """True iff `remote` is a strictly higher version than `local`.

    Zero-pads the shorter tuple so (1,0,8) > (1,0,7) and (1,1) > (1,0,9).
    """
    r = parse_version(remote)
    l = parse_version(local)
    n = max(len(r), len(l))
    r = r + (0,) * (n - len(r))
    l = l + (0,) * (n - len(l))
    return r > l


# ---------------------------------------------------------------------------
# GitHub release fetch
# ---------------------------------------------------------------------------


def _normalize_release(data: dict) -> Optional[dict]:
    """Turn a GitHub `releases/latest` JSON object into our normalized dict.

    Returns None if `data` isn't a dict or has no asset named ASSET_NAME with a
    download URL. Pure (no network) so unit tests can feed a sample payload.

    Returned dict:
      {tag, version, asset_url, asset_size, sha256|None, html_url}
    """
    if not isinstance(data, dict):
        return None
    tag = data.get("tag_name") or ""
    asset = None
    for a in data.get("assets") or []:
        if isinstance(a, dict) and a.get("name") == ASSET_NAME:
            asset = a
            break
    if asset is None:
        return None
    url = asset.get("browser_download_url")
    if not url:
        return None
    # The asset `digest` is best-effort ("sha256:<hex>"); may be absent.
    sha = None
    digest = asset.get("digest")
    if isinstance(digest, str) and digest.lower().startswith("sha256:"):
        sha = digest.split(":", 1)[1].strip().lower() or None
    version = tag[1:] if tag[:1] in ("v", "V") else tag
    return {
        "tag": tag,
        "version": version,
        "asset_url": url,
        "asset_size": int(asset.get("size") or 0),
        "sha256": sha,
        "html_url": data.get("html_url") or "",
    }


def fetch_latest_release(owner: str = GITHUB_OWNER,
                         repo: str = GITHUB_REPO,
                         timeout: int = 10) -> Optional[dict]:
    """GET /repos/{owner}/{repo}/releases/latest and normalize it.

    Returns the normalized dict, or None on ANY failure (network / HTTP / JSON
    / missing asset). The Qt wrapper turns None into a friendly log/toast.
    """
    url = f"https://api.github.com/repos/{owner}/{repo}/releases/latest"
    req = urllib.request.Request(url, headers={
        "User-Agent": _USER_AGENT,
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    })
    try:
        ctx = ssl.create_default_context()
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            raw = resp.read()
        data = json.loads(raw.decode("utf-8"))
    except Exception:
        return None
    return _normalize_release(data)


# ---------------------------------------------------------------------------
# Download + integrity
# ---------------------------------------------------------------------------


def download_asset(url: str,
                   dest_path: str,
                   progress_cb: Optional[Callable[[int, int], None]] = None,
                   chunk: int = 1 << 16,
                   timeout: int = 30,
                   total_hint: int = 0) -> str:
    """Stream `url` to `dest_path`, reporting progress.

    Writes to `dest_path + '.part'` then os.replace()s into place on success
    (partial-download safety: a killed download never leaves a runnable but
    half-written exe). `progress_cb(downloaded, total)` is called after each
    chunk; `total` comes from Content-Length, falling back to `total_hint`
    (the asset size) when the header is absent. `timeout` is the per-read
    socket timeout, so a slow-but-progressing transfer never trips it.
    Returns `dest_path`. Raises on network / IO error (after cleaning up the
    .part file).
    """
    part = dest_path + ".part"
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            total = int(resp.headers.get("Content-Length") or 0) or total_hint
            downloaded = 0
            with open(part, "wb") as fh:
                while True:
                    buf = resp.read(chunk)
                    if not buf:
                        break
                    fh.write(buf)
                    downloaded += len(buf)
                    if progress_cb:
                        progress_cb(downloaded, total)
        os.replace(part, dest_path)
    except BaseException:
        try:
            os.remove(part)
        except OSError:
            pass
        raise
    return dest_path


def verify_sha256(path: str, expected: Optional[str]) -> bool:
    """True if `expected` is falsy (best-effort: GitHub digest may be absent)
    or the file's sha256 hex matches (case-insensitive). Reads in 1 MiB blocks.
    """
    if not expected:
        return True
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest().lower() == expected.strip().lower()


# ---------------------------------------------------------------------------
# Frozen-build helpers + Windows self-replace
# ---------------------------------------------------------------------------


def is_frozen() -> bool:
    """True when running as the PyInstaller onefile exe (not `python gui.py`)."""
    return bool(getattr(sys, "frozen", False))


def current_exe_path() -> str:
    """Absolute path to the running exe (sys.executable). When not frozen this
    is the Python interpreter — callers must gate apply on is_frozen()."""
    return os.path.abspath(sys.executable)


# CreateProcess flags for a detached, window-less helper.
_DETACHED_PROCESS = 0x00000008
_CREATE_NO_WINDOW = 0x08000000


def build_swap_bat(new_exe: str, target_exe: str, relaunch: bool = True) -> str:
    """Return the .bat text that waits for the old exe's lock to drop, moves
    the new exe over it, relaunches, and self-deletes. Split out for testing.

    Uses `ping` (not `timeout`) for the wait because a detached process has no
    console and `timeout` would error. `chcp 65001` + quoted paths handle
    spaces / unicode (the user runs CJK terminals).
    """
    relaunch_line = f'start "" "{target_exe}"\r\n' if relaunch else ""
    return (
        "@echo off\r\n"
        "chcp 65001 >nul\r\n"
        ":waitloop\r\n"
        f'move /y "{new_exe}" "{target_exe}" >nul 2>&1\r\n'
        "if errorlevel 1 (\r\n"
        "    ping -n 2 127.0.0.1 >nul\r\n"
        "    goto waitloop\r\n"
        ")\r\n"
        f"{relaunch_line}"
        '(goto) 2>nul & del "%~f0"\r\n'
    )


def stage_and_swap(new_exe: str,
                   target_exe: Optional[str] = None,
                   relaunch: bool = True) -> "subprocess.Popen":
    """Launch a detached helper that replaces the running exe with `new_exe`.

    Frozen-only (raises RuntimeError otherwise). Writes the swap .bat to
    %TEMP%, launches it detached with no window, and returns the Popen. The
    CALLER must then quit the app promptly so the exe's file lock drops and the
    bat's `move` can succeed.
    """
    if not is_frozen():
        raise RuntimeError("stage_and_swap() is only valid in a frozen build")
    target = os.path.abspath(target_exe or current_exe_path())
    new_exe = os.path.abspath(new_exe)
    bat_path = os.path.join(tempfile.gettempdir(), f"ac_update_{os.getpid()}.bat")
    with open(bat_path, "w", encoding="utf-8") as fh:
        fh.write(build_swap_bat(new_exe, target, relaunch=relaunch))
    return subprocess.Popen(
        ["cmd", "/c", bat_path],
        creationflags=_DETACHED_PROCESS | _CREATE_NO_WINDOW,
        close_fds=True,
        cwd=os.path.dirname(target) or None,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def staged_exe_path() -> str:
    """Where a downloaded update is written: `<exe_dir>/Auto-Continue.exe.new`
    (same volume as the target so the swap `move` is an atomic rename)."""
    return os.path.join(os.path.dirname(current_exe_path()), ASSET_NAME + ".new")
