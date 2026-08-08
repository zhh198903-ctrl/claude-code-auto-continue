# -*- coding: utf-8 -*-
"""No file in this repo may trip its own detectors.

This tool reads terminal scrollback, so its own source, tests, README and
in-app help become input the moment someone views them in a watched window —
a pager, an editor, `git show`, or Claude Code echoing a Read. A verbatim
banner in any of them makes the watchdog act on something that is not there.
The consequences are not uniform: the limit-picker and modal detectors press
a BARE ENTER, which submits whatever the user has half-typed.

The convention is to de-fang every example: split literals with adjacent-string
concatenation in code, or insert a `·` in prose. This test is what keeps that
convention honest — it slides a full SCAN_TAIL_CHARS window over every tracked
file and runs every detector, which is how a limit-picker hit sitting in
auto_continue.py's own docstring went unnoticed until a pre-release audit.

Plain harness like the others: exits non-zero on any failure.
"""
import io
import os
import sys

sys.stdout.reconfigure(encoding="utf-8")

import auto_continue as ac

ROOT = os.path.dirname(os.path.abspath(__file__))
STEP = 250                       # window stride; small enough not to skip a hit
SKIP_DIRS = {".git", "dist", "build", "__pycache__", ".pytest_cache"}
EXTS = (".py", ".md", ".yml", ".yaml", ".spec", ".txt", ".bat", ".ps1")

# (name, predicate, what the watchdog would DO if this fired for real)
DETECTORS = [
    ("limit banner", lambda t: ac.parse_limit_message(t) is not None,
     "schedules a continue"),
    ("limit picker", ac.parse_limit_prompt, "presses a BARE ENTER"),
    ("retry exhausted", ac.parse_retry_exhausted, "types continue"),
    ("connection error", ac.parse_econnreset_stuck, "types continue"),
    ("truncated response", ac.parse_server_error_stuck, "types continue"),
    ("oauth expired", ac.parse_oauth_expired, "warns (harmless)"),
    ("safeguard notice", ac.parse_fable_refusal, "starts a recovery"),
    ("safeguard picker", ac.parse_fable_picker, "presses a BARE ENTER"),
    ("switch dialog", ac.parse_switch_model_prompt, "presses a BARE ENTER"),
]


def tracked_files():
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in filenames:
            if fn.endswith(EXTS):
                yield os.path.join(dirpath, fn)


failures = 0
scanned = 0
for path in sorted(tracked_files()):
    rel = os.path.relpath(path, ROOT)
    try:
        text = io.open(path, encoding="utf-8").read()
    except (OSError, UnicodeDecodeError):
        continue
    scanned += 1
    for name, predicate, effect in DETECTORS:
        hit_at = None
        for i in range(0, max(1, len(text)), STEP):
            window = text[max(0, i - ac.SCAN_TAIL_CHARS + STEP):i + STEP]
            try:
                if predicate(window):
                    hit_at = i
                    break
            except Exception:
                pass
        if hit_at is not None:
            line = text[:hit_at].count("\n") + 1
            print(f"[FAIL] {rel} trips the {name} detector near line {line} "
                  f"— in a watched terminal this {effect}")
            failures += 1

print(f"[{'OK ' if not failures else 'FAIL'}] "
      f"{scanned} files scanned, {failures} self-trigger(s)")
sys.exit(1 if failures else 0)
