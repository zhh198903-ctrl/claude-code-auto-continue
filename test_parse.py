"""Unit-test the limit-message regex against realistic scrollback samples."""
import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

from auto_continue import parse_limit_message, next_reset_datetime
from datetime import datetime
import pytz

UP = "/upgrade or /extra-usage to finish what you're working on."
samples = [
    # Exact form from the screenshot.
    ("You've hit your limit · resets 11pm (Asia/Shanghai)\n" + UP,
     (11, "pm", "Asia/Shanghai")),
    # Curly apostrophe.
    ("You’ve hit your limit · resets 11pm (Asia/Shanghai)\n" + UP,
     (11, "pm", "Asia/Shanghai")),
    # AM, single-digit hour, different timezone.
    ("You've hit your limit · resets 3am (America/Los_Angeles)\n" + UP,
     (3, "am", "America/Los_Angeles")),
    # Two limit lines in same buffer — must pick the latest one.
    ("You've hit your limit · resets 7pm (Asia/Shanghai)\n" + UP + "\n"
     + "...later...\n"
     "You've hit your limit · resets 11pm (Asia/Shanghai)\n" + UP,
     (11, "pm", "Asia/Shanghai")),
    # No match.
    ("nothing relevant here", None),
    # Different bullet glyph (•).
    ("You've hit your limit • resets 4pm (UTC)\n" + UP, (4, "pm", "UTC")),
    # Stale match buried far below — should be rejected.
    ("You've hit your limit · resets 11pm (Asia/Shanghai)\n" + UP
     + "\n" + "x" * 6000,
     None),
    # Limit phrase WITHOUT the /upgrade follow-up line (e.g. this script's
    # own source code, or test data) — must NOT match.
    ("# example: You've hit your limit · resets 11pm (Asia/Shanghai)\n"
     "# (used in unit tests; no upgrade line)",
     None),
]

failures = 0
for i, (text, expected) in enumerate(samples):
    got = parse_limit_message(text)
    ok = got == expected
    status = "OK " if ok else "FAIL"
    print(f"[{status}] sample {i}: got={got!r} expected={expected!r}")
    if not ok:
        failures += 1

print()
# Smoke test next_reset_datetime — current date is 2026-05-06, so 11pm Shanghai.
res = next_reset_datetime(11, "pm", "Asia/Shanghai")
print(f"next reset for 11pm Asia/Shanghai → {res} (UTC) "
      f"= {res.astimezone(pytz.timezone('Asia/Shanghai'))}")

sys.exit(1 if failures else 0)
