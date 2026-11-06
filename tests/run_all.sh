#!/usr/bin/env bash
# Run every offline test suite in this package. No game required.
cd "$(dirname "$0")/.." || exit 1
fail=0
for t in tests/test_*.py tests/replay_conquest.py; do
  printf '%-42s ' "$(basename "$t")"
  if out=$(python3 "$t" 2>&1); then
    echo "PASS  $(echo "$out" | tail -1 | cut -c1-56)"
  else
    echo "FAIL"
    echo "$out" | tail -5 | sed 's/^/    /'
    fail=1
  fi
done
[ "$fail" -eq 0 ] && echo "ALL SUITES PASSED" || echo "SOME SUITES FAILED"
exit "$fail"
