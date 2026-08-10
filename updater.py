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
import time
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
                   total_hint: int = 0,
                   attempts: int = 5,
                   retry_wait: float = 3.0,
                   part_path: Optional[str] = None,
                   keep_partial: bool = False) -> str:
    """Stream `url` to `dest_path`, reporting progress.

    Writes to `dest_path + '.part'` then os.replace()s into place on success
    (partial-download safety: a killed download never leaves a runnable but
    half-written exe). `progress_cb(downloaded, total)` is called after each
    chunk; `total` comes from Content-Length, falling back to `total_hint`
    (the asset size) when the header is absent. `timeout` is the per-read
    socket timeout, so a slow-but-progressing transfer never trips it.

    RESUMES. A dropped connection keeps its `.part` file and the next of
    `attempts` tries asks for `bytes=<what we have>-`; on a link where tens
    of MB move at tens of KB/s, starting over on every drop can mean never
    finishing at all. A server that ignores Range answers 200 instead of
    206, and then the partial is discarded and the transfer restarts — the
    old behaviour, rather than a file with a seam in it. The size is checked
    against Content-Length before the rename, because a body that stops
    early is not an error the socket reports.

    `keep_partial` leaves the partial behind when the attempts run out, so
    the next RUN continues instead of only the next retry — on a link that
    drops more often than a full transfer takes, starting from zero every
    launch never finishes. `part_path` puts that file somewhere the caller
    controls rather than beside `dest_path`.

    A kept partial records which asset it belongs to in a sidecar. Without
    that, a release published while an old partial sat there would have its
    bytes appended to the wrong ones — the SHA-256 check catches it, but only
    after paying for the whole download again.

    Returns `dest_path`. Raises the last error once the attempts are spent.
    """
    part = part_path or (dest_path + ".part")
    meta = part + ".meta"
    ctx = ssl.create_default_context()
    last_err = None

    # Whose bytes are these? A partial kept from a previous run is only worth
    # resuming if it belongs to the same asset.
    tag = json.dumps({"url": url, "size": total_hint}, sort_keys=True)
    if os.path.exists(part):
        try:
            same = open(meta, encoding="utf-8").read() == tag
        except OSError:
            same = False
        if not same:
            for path in (part, meta):
                try:
                    os.remove(path)
                except OSError:
                    pass

    for attempt in range(attempts):
        # Whatever survived the previous attempt is the offset to ask from.
        have = os.path.getsize(part) if os.path.exists(part) else 0
        headers = {"User-Agent": _USER_AGENT}
        if have:
            headers["Range"] = f"bytes={have}-"
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout,
                                        context=ctx) as resp:
                # 206 continues; a 200 means the server ignored the Range and
                # is sending the whole file, so the partial must be discarded
                # rather than appended to.
                resumed = resp.status == 206 and have > 0
                if not resumed:
                    have = 0
                length = int(resp.headers.get("Content-Length") or 0)
                total = (have + length) if length else total_hint
                downloaded = have
                with open(part, "ab" if resumed else "wb") as fh:
                    while True:
                        buf = resp.read(chunk)
                        if not buf:
                            break
                        fh.write(buf)
                        downloaded += len(buf)
                        if progress_cb:
                            progress_cb(downloaded, total)
            # A truncated body is not an error at the socket level — the read
            # just ends — so check the size before believing it is done.
            if total and os.path.getsize(part) < total:
                raise IOError(
                    f"connection ended early at {os.path.getsize(part)} of "
                    f"{total} bytes")
            os.replace(part, dest_path)
            try:
                os.remove(meta)
            except OSError:
                pass
            return dest_path
        except KeyboardInterrupt:
            raise
        except BaseException as e:
            last_err = e
            # The partial file STAYS. That is the whole point: the next
            # attempt resumes from it. It is only ever renamed to the real
            # name once complete, so nothing half-written is runnable.
            if attempt + 1 < attempts:
                time.sleep(retry_wait)

    if keep_partial and os.path.exists(part):
        try:
            open(meta, "w", encoding="utf-8").write(tag)
        except OSError:
            pass
    else:
        for path in (part, meta):
            try:
                os.remove(path)
            except OSError:
                pass
    raise last_err


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


def update_failure_marker_path(target_exe: str) -> str:
    """Where build_swap_bat records a permanent swap failure: next to
    `target_exe`, named `<target>.update-failed.txt`.

    Written only on the give-up path (wait-loop cap hit), since by the time
    the bat runs the app has already quit (the caller quits it so the file
    lock drops) — there is no live process left to raise a toast, so the
    marker is the only trace of the failure. A human can find it next to the
    exe; a future app-side startup check could surface/clear it too. This
    module doesn't read it back — it only defines where the bat writes it.
    """
    return target_exe + ".update-failed.txt"


def build_swap_bat(new_exe: str, target_exe: str, relaunch: bool = True) -> str:
    """Return the .bat text that waits for the old exe's lock to drop, moves
    the new exe over it, and relaunches `target_exe` — on BOTH the success
    path and the give-up path — then self-deletes. Split out for testing.

    Uses `ping` (not `timeout`) for the wait because a detached process has no
    console and `timeout` would error. `chcp 65001` + quoted paths handle
    spaces / unicode (the user runs CJK terminals).

    The wait loop is CAPPED (~150 tries ≈ 2.5 min): a permanent failure
    (staged .new quarantined by AV, unwritable target dir, app never exits)
    must not leave a hidden detached cmd.exe looping forever. On give-up the
    `move` never happened, so `target_exe` is still the old exe on disk —
    but the caller (gui.py's `_on_update_ready`) already quit the app to
    drop the file lock, so nothing is running any more. To avoid silently
    stranding the user with no Auto-Continue at all, give-up writes a small
    UTF-8 marker file next to `target_exe` (see `update_failure_marker_path`)
    recording that the swap failed, then falls through to the SAME relaunch
    line the success path uses, starting the old exe back up. `relaunch`
    (default True; tests pass False to inspect the bat without that line)
    gates that single relaunch line, which both paths converge on.
    """
    marker = update_failure_marker_path(target_exe)
    relaunch_line = f'start "" "{target_exe}"\r\n' if relaunch else ""
    return (
        "@echo off\r\n"
        "chcp 65001 >nul\r\n"
        "set tries=0\r\n"
        ":waitloop\r\n"
        "set /a tries+=1\r\n"
        "if %tries% gtr 150 goto giveup\r\n"
        f'move /y "{new_exe}" "{target_exe}" >nul 2>&1\r\n'
        "if errorlevel 1 (\r\n"
        "    ping -n 2 127.0.0.1 >nul\r\n"
        "    goto waitloop\r\n"
        ")\r\n"
        "goto relaunch\r\n"
        ":giveup\r\n"
        "echo Auto-Continue update failed after %tries% attempts: exe still "
        f'locked or unwritable; kept previous version running. > "{marker}"\r\n'
        ":relaunch\r\n"
        f"{relaunch_line}"
        ":cleanup\r\n"
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
