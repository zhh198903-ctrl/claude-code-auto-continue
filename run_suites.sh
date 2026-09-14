#!/usr/bin/env bash
# Run every suite and report honestly. Counting "[OK ]" lines alone is what
# let a module that CRASHED partway report "197 ok, 0 bad" while 62 checks
# quietly stopped existing -- the absence of a failure line is not a pass.
cd "$(dirname "$0")" || exit 1
rc=0
for t in test_parse.py test_fable.py test_gui_tick.py test_local_api.py \
         test_selftrigger.py test_updater.py test_singleinstance.py; do
  out=$(PYTHONIOENCODING=utf-8 python "$t" 2>&1); code=$?
  ok=$(printf '%s' "$out" | grep -cE '^\[OK ?\]')
  bad=$(printf '%s' "$out" | grep -cE '^\[FAIL|^\[ERR')
  note=""
  [ "$code" -ne 0 ] && { note=" <-- EXIT $code"; rc=1; }
  printf '%s' "$out" | grep -q "Traceback" && { note="$note <-- TRACEBACK"; rc=1; }
  [ "$bad" -ne 0 ] && rc=1
  printf '%-22s %4s ok  %s bad%s\n' "$t" "$ok" "$bad" "$note"
  [ -n "$note" ] && printf '%s' "$out" | grep -A4 "Traceback" | head -8
done
echo "--- overall: $([ $rc -eq 0 ] && echo ALL GREEN || echo PROBLEMS) ---"
exit $rc
