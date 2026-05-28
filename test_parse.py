"""Unit-test the limit-message regex against realistic scrollback samples."""
import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

from auto_continue import (
    parse_limit_message, parse_retry_exhausted, next_reset_datetime,
)
from datetime import datetime
import pytz

# The two Anthropic wordings of the follow-up line seen in the wild.
UP = "/upgrade or /extra-usage to finish what you're working on."
UP2 = "/upgrade to increase your usage limit."
samples = [
    # Exact form from the original screenshot (whole-hour reset).
    ("You've hit your limit · resets 11pm (Asia/Shanghai)\n" + UP,
     (11, 0, "pm", "Asia/Shanghai")),
    # Curly apostrophe.
    ("You’ve hit your limit · resets 11pm (Asia/Shanghai)\n" + UP,
     (11, 0, "pm", "Asia/Shanghai")),
    # AM, single-digit hour, different timezone.
    ("You've hit your limit · resets 3am (America/Los_Angeles)\n" + UP,
     (3, 0, "am", "America/Los_Angeles")),
    # New wording: reset time WITH minutes + the newer /upgrade line.
    ("You've hit your limit · resets 2:50pm (Asia/Shanghai)\n" + UP2,
     (2, 50, "pm", "Asia/Shanghai")),
    # New /upgrade wording but whole-hour reset.
    ("You've hit your limit · resets 9am (UTC)\n" + UP2,
     (9, 0, "am", "UTC")),
    # Old /upgrade wording but with minutes.
    ("You've hit your limit · resets 7:05pm (Asia/Shanghai)\n" + UP,
     (7, 5, "pm", "Asia/Shanghai")),
    # Two limit lines in same buffer — must pick the latest one.
    ("You've hit your limit · resets 7pm (Asia/Shanghai)\n" + UP + "\n"
     + "...later...\n"
     "You've hit your limit · resets 11:30pm (Asia/Shanghai)\n" + UP2,
     (11, 30, "pm", "Asia/Shanghai")),
    # No match.
    ("nothing relevant here", None),
    # Different bullet glyph (•).
    ("You've hit your limit • resets 4pm (UTC)\n" + UP, (4, 0, "pm", "UTC")),
    # Stale match buried far below — should be rejected.
    ("You've hit your limit · resets 11pm (Asia/Shanghai)\n" + UP
     + "\n" + "x" * 6000,
     None),
    # Limit phrase WITHOUT the /upgrade follow-up line (e.g. this script's
    # own source code, or test data) — must NOT match.
    ("# example: You've hit your limit · resets 11pm (Asia/Shanghai)\n"
     "# (used in unit tests; no upgrade line)",
     None),
    # NEW wording variant: "session limit" (seen 2026-05-28).
    ("You've hit your session limit · resets 12am (Asia/Shanghai)\n" + UP2,
     (12, 0, "am", "Asia/Shanghai")),
    # Curly apostrophe + session limit.
    ("You’ve hit your session limit · resets 12am (Asia/Shanghai)\n" + UP2,
     (12, 0, "am", "Asia/Shanghai")),
    # Hypothetical "weekly limit" — same shape, should also match.
    ("You've hit your weekly limit · resets 9:30am (UTC)\n" + UP2,
     (9, 30, "am", "UTC")),
    # Hypothetical "usage limit" wording — also matches.
    ("You've hit your usage limit · resets 4pm (Asia/Shanghai)\n" + UP,
     (4, 0, "pm", "Asia/Shanghai")),
    # --- Different-timezone users (Anthropic renders the user's local tz). ---
    # West coast US.
    ("You've hit your session limit · resets 7pm (America/Los_Angeles)\n"
     + UP2, (7, 0, "pm", "America/Los_Angeles")),
    # London.
    ("You've hit your session limit · resets 11pm (Europe/London)\n" + UP2,
     (11, 0, "pm", "Europe/London")),
    # Tokyo with minutes.
    ("You've hit your session limit · resets 8:45am (Asia/Tokyo)\n" + UP2,
     (8, 45, "am", "Asia/Tokyo")),
    # Sao Paulo with hyphen in tz name.
    ("You've hit your session limit · resets 6pm (America/Sao_Paulo)\n"
     + UP2, (6, 0, "pm", "America/Sao_Paulo")),
    # UTC.
    ("You've hit your session limit · resets 12am (UTC)\n" + UP2,
     (12, 0, "am", "UTC")),
    # New York with EDT abbrev (Anthropic sometimes uses abbrev, sometimes
    # IANA). pytz won't recognize "EDT" but parse should still succeed —
    # next_reset_datetime falls back to Asia/Shanghai gracefully.
    ("You've hit your session limit · resets 9am (EDT)\n" + UP2,
     (9, 0, "am", "EDT")),
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
# Smoke test next_reset_datetime with minutes.
res = next_reset_datetime(2, 50, "pm", "Asia/Shanghai")
print(f"next reset for 2:50pm Asia/Shanghai → {res} (UTC) "
      f"= {res.astimezone(pytz.timezone('Asia/Shanghai'))}")


# ---------- parse_retry_exhausted ----------
retry_samples = [
    # Plain attempt 10/10 near the tail — exhausted.
    ("· Working… (esc to interrupt)\n  Retrying in 0s · attempt 10/10",
     True),
    # Mid-retry (7/10) — NOT exhausted.
    ("Retrying in 0s · attempt 7/10", False),
    # 1/10 — definitely not exhausted.
    ("Retrying in 12s · attempt 1/10", False),
    # Exhausted with bullet variant •
    ("Retrying in 3s • attempt 10/10\n", True),
    # Older Claude variants without space before s.
    ("Retrying in 0s·attempt 10/10", True),
    # Two retry sequences in the buffer — must look at the latest.
    ("Retrying in 0s · attempt 10/10\n...later...\n"
     "Retrying in 5s · attempt 3/10",
     False),
    # Latest is the exhausted one.
    ("Retrying in 5s · attempt 3/10\n...later...\n"
     "Retrying in 0s · attempt 10/10",
     True),
    # Exhausted banner buried far above — stale, ignore.
    ("Retrying in 0s · attempt 10/10\n" + "x" * 6000, False),
    # Unrelated text mentioning attempt 10/10 (no Retrying anchor).
    ("test failed on attempt 10/10 of the run", False),
    # No retry banner at all.
    ("nothing to see here", False),
    # 5/5 retry config (hypothetical lower max) — still exhausted (N >= total).
    ("Retrying in 0s · attempt 5/5", True),
]

print()
print("---- parse_retry_exhausted ----")
for i, (text, expected) in enumerate(retry_samples):
    got = parse_retry_exhausted(text)
    ok = got == expected
    status = "OK " if ok else "FAIL"
    print(f"[{status}] retry sample {i}: got={got!r} expected={expected!r}")
    if not ok:
        failures += 1

sys.exit(1 if failures else 0)
