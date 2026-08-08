"""Unit-test the limit-message regex against realistic scrollback samples."""
import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

from auto_continue import (
    NETWORK_POST_MATCH_TAIL, SWITCH_POST_MATCH_TAIL,
    compile_trigger_patterns, parse_limit_message, parse_retry_exhausted,
    parse_econnreset_stuck, parse_server_error_stuck, parse_fable_refusal,
    parse_fable_picker, parse_switch_model_prompt, parse_limit_prompt,
    parse_oauth_expired,
    next_reset_datetime,
)
from datetime import datetime, timedelta
import pytz

# The Anthropic wordings of the follow-up line seen in the wild. UP3 is the
# newest (2026-07): /extra-usage was renamed /usage-credits.
UP = "/upgrade or /extra-usage to finish what you're working on."
UP2 = "/upgrade to increase your usage limit."
UP3 = "/upgrade or /usage-credits to finish what you're working on."
samples = [
    # NEWEST wording from user's 2026-07-11 screenshot: "session limit",
    # reset with minutes, /usage-credits follow-up. This is the exact case
    # that previously went undetected (regex only accepted /extra-usage).
    ("You've hit your session limit · resets 5:20am (Asia/Shanghai)\n" + UP3,
     (5, 20, "am", "Asia/Shanghai")),
    # /usage-credits with a whole-hour reset.
    ("You've hit your limit · resets 9pm (Asia/Shanghai)\n" + UP3,
     (9, 0, "pm", "Asia/Shanghai")),
    # /usage-credits + bullet-operator separator (∙ U+2219, not the middle
    # dot) — Claude Code emits this glyph in some builds.
    ("You've hit your session limit ∙ resets 5:20am (Asia/Shanghai)\n" + UP3,
     (5, 20, "am", "Asia/Shanghai")),
    # /usage-credits + dot-operator separator (⋅ U+22C5).
    ("You've hit your session limit ⋅ resets 3am (UTC)\n" + UP3,
     (3, 0, "am", "UTC")),
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
print("---- next_reset_datetime ----")


def check_reset(label, cond):
    global failures
    status = "OK " if cond else "FAIL"
    print(f"[{status}] {label}")
    if not cond:
        failures += 1


# Basic invariants: result is aware UTC, in the future, within 24h, and has
# the requested wall-clock time in the requested zone.
def assert_reset(hour_12, minute, ampm, tz_name, expect_tz=None):
    res = next_reset_datetime(hour_12, minute, ampm, tz_name)
    now = datetime.now(pytz.UTC)
    ok = (res.tzinfo is not None and now < res <= now + timedelta(days=1))
    if expect_tz:
        local = res.astimezone(pytz.timezone(expect_tz))
        hour_24 = hour_12 % 12 + (12 if ampm == "pm" else 0)
        ok = ok and (local.hour, local.minute) == (hour_24, minute)
    check_reset(f"{hour_12}:{minute:02d}{ampm} ({tz_name})", ok)


assert_reset(2, 50, "pm", "Asia/Shanghai", "Asia/Shanghai")
assert_reset(12, 0, "am", "Asia/Shanghai", "Asia/Shanghai")   # midnight
assert_reset(12, 0, "pm", "Asia/Shanghai", "Asia/Shanghai")   # noon
assert_reset(11, 0, "pm", "America/New_York", "America/New_York")  # DST zone
assert_reset(9, 0, "am", "EDT", "America/New_York")   # abbrev → IANA map
assert_reset(6, 30, "pm", "PST", "America/Los_Angeles")
assert_reset(7, 0, "am", "NoSuch/Zone")               # local-tz fallback

# DST correctness: the target must carry the OFFSET OF THE TARGET MOMENT,
# not today's. Simulate by checking round-trip consistency: converting the
# result back to the zone must show exactly the requested wall-clock time
# (pytz normalize would reveal a wrong fixed offset as a shifted hour).
res = next_reset_datetime(11, 0, "pm", "America/New_York")
back = res.astimezone(pytz.timezone("America/New_York"))
check_reset("DST round-trip America/New_York 11pm",
            (back.hour, back.minute) == (23, 0))


# ---------- parse_retry_exhausted ----------
retry_samples = [
    # Plain attempt 10/10 near the tail — exhausted.
    ("· Working… (esc to interrupt)\n  Retry" "ing in 0s · attempt 10/10",
     True),
    # Mid-retry (7/10) — NOT exhausted.
    ("Retry" "ing in 0s · attempt 7/10", False),
    # 1/10 — definitely not exhausted.
    ("Retry" "ing in 12s · attempt 1/10", False),
    # Exhausted with bullet variant •
    ("Retry" "ing in 3s • attempt 10/10\n", True),
    # Older Claude variants without space before s.
    ("Retry" "ing in 0s·attempt 10/10", True),
    # Two retry sequences in the buffer — must look at the latest.
    ("Retry" "ing in 0s · attempt 10/10\n...later...\n"
     "Retry" "ing in 5s · attempt 3/10",
     False),
    # Latest is the exhausted one.
    ("Retry" "ing in 5s · attempt 3/10\n...later...\n"
     "Retry" "ing in 0s · attempt 10/10",
     True),
    # Trailing footer+recap of ~5000 chars (wide terminal) — within the
    # network tail allowance (6000), so still treated as current.
    ("Retry" "ing in 0s · attempt 10/10\n" + "x" * 5000, True),
    # Exhausted banner buried far above — stale, ignore.
    ("Retry" "ing in 0s · attempt 10/10\n" + "x" * 7000, False),
    # Unrelated text mentioning attempt 10/10 (no Retrying anchor).
    ("test failed on attempt 10/10 of the run", False),
    # No retry banner at all.
    ("nothing to see here", False),
    # 5/5 retry config (hypothetical lower max) — still exhausted (N >= total).
    ("Retry" "ing in 0s · attempt 5/5", True),
    # Long-form wording some builds print.
    ("Retry" "ing in 8 seconds… (attempt 10/10)", True),
    ("Retry" "ing in 8 seconds… (attempt 3/10)", False),
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


# ---------- parse_econnreset_stuck ----------
econn_samples = [
    # Exact form seen in the screenshot.
    ("⎿  · 电域模块 (S参数/RX Filter/CTLE/RX FFE/DFE) 直连光信号源...\n"
     "  API Error: Unable to conn" "ect to API (ECONNRESET)\n"
     "✻ Sautéed for 15m 46s · 1 shell still running",
     True),
    # Compact form.
    ("API Error: Unable to conn" "ect to API (ECONNRESET)", True),
    # Trailing footer + multi-line recap (~5000 chars, wide terminal) — the
    # exact regression from Image #14: error not at the very bottom but still
    # current. Within the 6000 network tail allowance → must detect.
    ("API Error: Unable to conn" "ect to API (ECONNRESET)\n"
     "* Worked for 15m 6s\n* recap: ...\n" + "x" * 5000, True),
    # Stale — buried far up in scrollback (beyond the network allowance).
    ("API Error: Unable to conn" "ect to API (ECONNRESET)\n" + "x" * 7000,
     False),
    # No match.
    ("everything fine here", False),
    # Bare token "ECONNRESET" without the API Error prefix — should NOT match
    # (avoids false-matching log files / source code mentioning the constant).
    ("socket error: ECONNRESET on fd 7", False),
    # The Retrying banner alone shouldn't trip this function.
    ("Retry" "ing in 0s · attempt 5/10", False),
    # Mixed case for the literal token.
    ("API Error: Unable to conn" "ect to API (econnreset)", True),
    # Two ECONNRESET lines — newer one near tail, should match.
    ("API Error: Unable to conn" "ect to API (ECONNRESET)\n...later...\n"
     "API Error: Unable to conn" "ect to API (ECONNRESET)",
     True),
    # Sibling errnos — same stuck state, must all be detected.
    ("API Error: Unable to conn" "ect to API (ETIMEDOUT)", True),
    ("API Error: Unable to conn" "ect to API (ECONNREFUSED)", True),
    ("API Error: Unable to conn" "ect to API (ENOTFOUND)", True),
    ("API Error: Unable to conn" "ect to API (EAI_AGAIN)", True),
    ("API Error: Unable to conn" "ect to API (EHOSTUNREACH)", True),
    # undici error codes.
    ("API Error: Unable to conn" "ect to API (UND_ERR_CONNECT_TIMEOUT)", True),
    # Node's generic wording.
    ("API Error: fetch fai" "led", True),
    # HTTP status errors are NOT network-stuck (Claude retries those itself).
    ("API Error: 500 {\"type\":\"error\"}", False),
    ("API Error: 529 overloaded", False),
]

print()
print("---- parse_econnreset_stuck ----")
for i, (text, expected) in enumerate(econn_samples):
    got = parse_econnreset_stuck(text)
    ok = got == expected
    status = "OK " if ok else "FAIL"
    print(f"[{status}] econn sample {i}: got={got!r} expected={expected!r}")
    if not ok:
        failures += 1


# ---------- parse_server_error_stuck ----------
server_error_samples = [
    # Exact form from the user's 2026-07-11 screenshot (Image #7).
    ("● API Error: Server error mid-resp" "onse. The response above may be "
     "incomplete.\n✻ Sautéed for 1m 6s", True),
    # Compact form.
    ("API Error: Server error mid-resp" "onse.", True),
    # Trailing footer + recap (~5000 chars, wide terminal) — within the 6000
    # network tail allowance → still current.
    ("API Error: Server error mid-resp" "onse. The response above may be "
     "incomplete.\n" + "x" * 5000, True),
    # Stale — buried far up in scrollback (beyond the network allowance).
    ("API Error: Server error mid-resp" "onse.\n" + "x" * 7000, False),
    # No match.
    ("everything fine here", False),
    # Must NOT confuse with the connectivity error (that's econnreset's job,
    # but neither should false-match the other).
    ("API Error: Unable to conn" "ect to API (ECONNRESET)", False),
    # Prose merely mentioning a server error without the "API Error:" prefix
    # — must NOT match (avoids logs / this discussion in scrollback).
    ("the server returned an error mid-response, retrying", False),
    # Two markers — newer one near tail, should match.
    ("API Error: Server error mid-resp" "onse.\n...later...\n"
     "API Error: Server error mid-resp" "onse.", True),
    # 2026-07-23 re-wording (user Image #1): same truncation class, new
    # leading phrase "Response stalled mid-str·eam" + the shared footer.
    ("● API Error: Response stalled mid-str" "eam. The response above may be "
     "incomplete.\n※ Baked for 26m 35s", True),
    # Compact stalled form (no footer).
    ("API Error: Response stalled mid-str" "eam.", True),
    # Forward-compat: an unseen leading phrase, but the shared footer within
    # the same `API Error` line still classifies it as a truncation.
    ("API Error: Streaming interrupted. The response above may be "
     "incomplete.", True),
    # Stale stalled marker buried far up scrollback (beyond the allowance).
    ("API Error: Response stalled mid-str" "eam.\n" + "x" * 7000, False),
]

print()
print("---- parse_server_error_stuck ----")
for i, (text, expected) in enumerate(server_error_samples):
    got = parse_server_error_stuck(text)
    ok = got == expected
    status = "OK " if ok else "FAIL"
    print(f"[{status}] server-error sample {i}: got={got!r} "
          f"expected={expected!r}")
    if not ok:
        failures += 1


# ---------- parse_fable_refusal ----------
# NOTE: like the limit-picker samples further down, every phrase the detectors
# anchor on is split with string concatenation so that DISPLAYING this test
# file inside a watched terminal cannot trigger a real Fable recovery — which
# would type /model, ESC and Enter into that live session. Keep the splits.
_SG = "safegu" "ards flagged"
_YES = "Yes, swi" "tch to"
_NO = "No, go b" "ack"

fable_refusal_samples = [
    # Exact terminal text (user Image #2): Fable safeguard block.
    ("● API Error: Fable 5's " + _SG + " this message "
     "(https://www.anthropic.com/legal/aup). They may flag safe, normal "
     "content as well.\n\nDouble press esc to edit your last message, or "
     "try a different model with /model.", True),
    # The 'can't respond ... with Fable' clause alone.
    ("Claude Code can" "'t respond to this request wi" "th Fable 5.", True),
    # Curly-apostrophe variant.
    ("Fable 5’s " + _SG + " this message", True),
    # No version number after Fable.
    ("Fable's " + _SG + " this message", True),
    # Unrelated prose mentioning Fable — must NOT match.
    ("everything is fine, still running on Fable 5", False),
    # Stale — buried far up scrollback. Padded off the CODE's own allowance so
    # the test tracks the constant instead of hard-coding a stale number.
    ("Fable 5's " + _SG + " this message\n"
     + "x" * (NETWORK_POST_MATCH_TAIL + 1000), False),
]

print()
print("---- parse_fable_refusal ----")
for i, (text, expected) in enumerate(fable_refusal_samples):
    got = parse_fable_refusal(text)
    ok = got == expected
    status = "OK " if ok else "FAIL"
    print(f"[{status}] fable sample {i}: got={got!r} expected={expected!r}")
    if not ok:
        failures += 1


# ---------- parse_switch_model_prompt ----------
switch_model_samples = [
    # The dialog from the user's Image #6 (switching to Fable 5).
    ("Switch model?\nYour next response will be slower and use more tokens\n\n"
     "This conversation is cached for the current model. Switching to Fable 5 "
     "means the full history gets re-read on your next message.\n\n"
     "> 1. " + _YES + " Fable 5\n  2. " + _NO, True),
    # Switching-to-Opus variant.
    ("Switch model?\n\n> 1. " + _YES + " Opus 5\n  2. " + _NO, True),
    # No dialog.
    ("just some normal output here", False),
    # 'Switch model?' words but no Yes/No options — not the dialog.
    ("should we Switch model? maybe later", False),
    # Affirmative option WITHOUT the negative one — not the dialog, so a bare
    # Enter must not be pressed.
    ("> 1. " + _YES + " Fable 5", False),
    # A full-width terminal stacks its footer (input box, rules, status bar)
    # below the modal; the dialog must still be detected through it.
    ("Switch model?\n> 1. " + _YES + " Fable 5\n  2. " + _NO + "\n"
     + "x" * (SWITCH_POST_MATCH_TAIL - 500), True),
    # Stale — buried far up scrollback. Padded off the CODE's own allowance so
    # the test tracks the constant instead of hard-coding a stale number.
    ("Switch model?\n> 1. " + _YES + " Fable 5\n  2. " + _NO + "\n"
     + "x" * (SWITCH_POST_MATCH_TAIL + 500), False),
]

# ---------- parse_fable_picker ----------
# The chooser Claude Code shows WITH the safeguard notice. Option 1 is
# pre-selected, so a bare Enter performs the model switch — this, not /model,
# is the real recovery path. Literals split like everything else here.
_SW = "Switch t" "o"
_ED = "Edit promp" "t and retry"
fable_picker_samples = [
    # Verbatim from a real block (2026-08-04).
    ("Session paused\n\nFable 5's " + _SG + " this message. The safeguards "
     "are intentionally broad right now and may flag safe and routine "
     "coding, cybersecurity, or biology work.\n\n"
     "> 1. " + _SW + " Opus 5\n  2. " + _ED + " with Fable 5", True),
    # The fallback model name changes every release — must not be hardcoded.
    ("> 1. " + _SW + " Sonnet 5\n  2. " + _ED + " with Fable 5", True),
    # Only the first option, no pair — not the picker, so no blind Enter.
    ("> 1. " + _SW + " Opus 5", False),
    # Ordinary prose.
    ("I had to switch to a different model yesterday", False),
    # Stale — scrolled too far up to still be open.
    ("> 1. " + _SW + " Opus 5\n  2. " + _ED + " with Fable 5\n"
     + "x" * (SWITCH_POST_MATCH_TAIL + 500), False),
]

# The notice must be recognised for ANY model, including ones that do not
# exist yet. Naming one model here is how detection silently dies at the next
# rename — Opus 4.8 retired and Opus 5 shipped during this project's life.
print()
print("---- safeguard notice is model-agnostic ----")
for _m in ("Fable 5", "Opus 5", "Opus 6", "Sonnet 5", "Haiku 4.5",
           "Sonata 7", "Nova 12.3"):
    _s = f"API Error: {_m}'s " + _SG + " this message."
    _ok = parse_fable_refusal(_s) is True
    print(f"[{'OK ' if _ok else 'FAIL'}] future model {_m!r} still detected")
    if not _ok:
        failures += 1

for _s, _why in (
    ("everything is fine, still running on Fable 5", "prose naming a model"),
    ("the safeguards are intentionally broad right now", "safeguards, no flag"),
    ("we flagged this for review later", "flagged, no safeguards"),
):
    _ok = parse_fable_refusal(_s) is False
    print(f"[{'OK ' if _ok else 'FAIL'}] not fooled by {_why}")
    if not _ok:
        failures += 1


print()
print("---- parse_fable_picker ----")
for i, (text, expected) in enumerate(fable_picker_samples):
    got = parse_fable_picker(text)
    ok = got == expected
    print(f"[{'OK ' if ok else 'FAIL'}] picker sample {i}: "
          f"got={got!r} expected={expected!r}")
    if not ok:
        failures += 1


print()
print("---- parse_switch_model_prompt ----")
for i, (text, expected) in enumerate(switch_model_samples):
    got = parse_switch_model_prompt(text)
    ok = got == expected
    status = "OK " if ok else "FAIL"
    print(f"[{status}] switch-model sample {i}: got={got!r} expected={expected!r}")
    if not ok:
        failures += 1


# ---------- parse_limit_prompt ----------
# The interactive limit picker. NOTE: key phrases below are built via string
# concatenation so that DISPLAYING this test file inside a watched terminal
# cannot false-trigger the detector.
PICKER = ("What do you want to do?\n"
          "> 1. Stop and wait for li" "mit to reset\n"
          "  2. Upgrade your plan\n"
          "Enter to conf" "irm · Esc to cancel")
BANNER = ("You've hit your li" "mit · resets 11pm (Asia/Shanghai)\n"
          "/upgra" "de to increase your usage limit.")
prompt_samples = [
    # Picker open at the tail — must press Enter.
    (PICKER, True),
    # Exact form from the screenshot, with preceding turn output.
    ("● 架构侦察一下,再上多智能体全面审查。\n"
     "✳ Waiting for 1 dynamic workflow to finish\n" + PICKER, True),
    # Some padding after the footer (narrow modal, wide terminal) — still ok.
    (PICKER + "\n" + " " * 800, True),
    # Buried under real output — stale, do NOT press Enter.
    (PICKER + "\n" + "x" * 2000, False),
    # Already confirmed: the limit banner appears AFTER the picker.
    (PICKER + "\n" + BANNER, False),
    # Banner BEFORE the picker (previous limit) — picker is current, Enter.
    (BANNER + "\n...much later...\n" + PICKER, True),
    # No picker at all.
    ("nothing here", False),
    # Similar words without the full three-part shape — no match.
    ("What do you want to do? open the pod bay doors\n"
     "Enter to conf" "irm", False),
]

print()
print("---- parse_limit_prompt ----")
for i, (text, expected) in enumerate(prompt_samples):
    got = parse_limit_prompt(text)
    ok = got == expected
    status = "OK " if ok else "FAIL"
    print(f"[{status}] prompt sample {i}: got={got!r} expected={expected!r}")
    if not ok:
        failures += 1


# ---------- parse_oauth_expired ----------
oauth_samples = [
    ("OAuth token has exp" "ired · Please run /log" "in", True),
    ("API Error: OAuth token has exp" "ired", True),
    ("OAuth token has exp" "ired\n" + "x" * 7000, False),  # stale
    ("everything fine", False),
]

print()
print("---- parse_oauth_expired ----")
for i, (text, expected) in enumerate(oauth_samples):
    got = parse_oauth_expired(text)
    ok = got == expected
    status = "OK " if ok else "FAIL"
    print(f"[{status}] oauth sample {i}: got={got!r} expected={expected!r}")
    if not ok:
        failures += 1


# ---------- compile_trigger_patterns (user-editable triggers) ----------
# The Advanced dialog lets users rewrite these regexes when Anthropic changes
# a banner's wording. A bad override must never silently disable a trigger or
# make one fire on everything, so validation is enforced here, not in the UI.
print()
print("---- compile_trigger_patterns ----")


def tcheck(label, cond):
    global failures
    print(f"[{'OK ' if cond else 'FAIL'}] {label}")
    if not cond:
        failures += 1


pats, errs = compile_trigger_patterns({})
tcheck("no overrides -> nothing compiled, no errors", pats == {} and errs == [])

pats, errs = compile_trigger_patterns({"econnreset": "BO" "OM"})
tcheck("valid override is accepted", list(pats) == ["econnreset"] and not errs)

pats, errs = compile_trigger_patterns({"econnreset": "(unclosed"})
tcheck("invalid regex rejected, default kept", pats == {} and len(errs) == 1)

# The limit banner's 4 groups feed the reset-time scheduler; fewer would raise
# mid-tick, so the override must be refused up front.
pats, errs = compile_trigger_patterns({"limit": "no groups here"})
tcheck("too-few-groups rejected", pats == {} and len(errs) == 1)

pats, errs = compile_trigger_patterns({"retry": r"tried (\d+) of (\d+)"})
tcheck("group-bearing override with enough groups accepted",
       list(pats) == ["retry"] and not errs)

# A pattern that can match the empty string matches at EVERY position — the
# tail anchor always passes, so every window would look stuck forever.
for empty_matcher in (".*", "x*", "fo" "o|"):
    pats, errs = compile_trigger_patterns({"econnreset": empty_matcher})
    tcheck(f"empty-matching {empty_matcher!r} rejected",
           pats == {} and len(errs) == 1)

# An override equal to the built-in default is not stored, so a future build's
# improved default still wins for triggers the user never really changed.
from auto_continue import TRIGGER_DEFAULTS as _TD
pats, errs = compile_trigger_patterns({"fable": _TD["fable"]})
tcheck("override identical to default is a no-op", pats == {} and not errs)

pats, errs = compile_trigger_patterns({"not_a_real_trigger": "x"})
tcheck("unknown trigger key ignored", pats == {} and not errs)

# End to end: a custom pattern actually drives the parser it belongs to.
pats, _ = compile_trigger_patterns({"fable": "custom" + " refusal wording"})
tcheck("custom pattern drives its parser",
       parse_fable_refusal("custom" " refusal wording", pats["fable"]) is True
       and parse_fable_refusal("Fable's " + _SG, pats["fable"]) is False)


# ---------- a streaming session is not stuck ----------
# Observed 2026-08-08: an 80-minute outage put EIGHT 'continue' prompts into
# one session. Each resend fired because the banner was still within the tail
# allowance — but the session had already recovered and was streaming, so every
# 'continue' just queued behind the running turn and all eight landed at once
# when it finished. A banner only means "stuck" while nothing has run since it.
print()
print("---- running-session gate ----")

_ERR = "API Error: Unable to conn" "ect to API (ECONNRESET)"
_RETRY = "Retry" "ing in 8s · attempt 10/10"
_TRUNC = "API Error: Server error mid-resp" "onse."
_BAR = "  [Opus 5] ███ 46% | 用量 █ 12% (2h 23m / 5h)\n  ⏱️  3h 59m"

running_samples = [
    # Live spinner after the banner: recovered, do not poke.
    (f"{_ERR}\n> \n  ✽ Swirling… (2m 0s · ↓ 5.0k tokens)", False),
    (f"{_ERR}\n  ✻ Misting… (2m 3s · ↓ 2.8k tokens · thinking)", False),
    # Older wording with no token counter.
    (f"{_ERR}\n  ✽ Working… (12s · " "esc to interrupt)", False),
    # Idle at the prompt: still stuck. The status bar's own parenthesised
    # times use a slash, not a middot, so they must not read as a spinner.
    (f"{_ERR}\n> \n{_BAR}", True),
    # Turn FINISHED after the banner — "Brewed for 3m 2s · …" carries no
    # parentheses, so it is not a spinner and the session is idle and stuck.
    (f"{_ERR}\n  ✻ Brewed for 3m 2s · 1 shell still running\n> ", True),
    # Spinner ABOVE the banner: that turn is the one that died. Still stuck.
    (f"  ✽ Swirling… (2m 0s · ↓ 5.0k tokens)\n{_ERR}\n> ", True),
]
for i, (text, expected) in enumerate(running_samples):
    tcheck(f"econnreset running-gate sample {i}",
           parse_econnreset_stuck(text) is expected)

# The gate applies to all three network detectors, so both tick loops inherit
# it — the GUI's and the CLI's — rather than one of them being fixed alone.
tcheck("retry-exhausted honours the gate",
       parse_retry_exhausted(f"{_RETRY}\n  ✽ Swirling… (9s · ↓ 1k tokens)")
       is False
       and parse_retry_exhausted(f"{_RETRY}\n> \n{_BAR}") is True)
tcheck("mid-stream truncation honours the gate",
       parse_server_error_stuck(f"{_TRUNC}\n  ✽ Swirling… (9s · ↓ 1k tokens)")
       is False
       and parse_server_error_stuck(f"{_TRUNC}\n> \n{_BAR}") is True)

sys.exit(1 if failures else 0)
