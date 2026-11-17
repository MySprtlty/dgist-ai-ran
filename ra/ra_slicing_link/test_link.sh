#!/bin/bash
# test_link.sh (ICNL, 2026-11-11): the Slicing <-> RA link (INTERFACE.md) without FlexRIC or the MAC: ra_stub.py against
# test_slicing_side (the PPO xApp's rules acted out with ra_slicing_link.h), four scenarios. Ports 19820/19821 so a real
# PPO xApp / RA on 9820/9821 is not disturbed.   ./test_link.sh [python3]
set -u
D=$(cd "$(dirname "$0")" && pwd); PY=${1:-python3}; OUT=${OUT:-$D/build}; mkdir -p $OUT
CC=${CC:-cc}
$CC -std=c11 -O2 -Wall -Wextra -Werror -o $OUT/test_slicing_side $D/test_slicing_side.c || { echo "!! compile failed"; exit 1; }
$CC -std=c11 -D_POSIX_C_SOURCE=200809L -fsyntax-only -Wall -Wextra -Werror -Wno-unused-function -x c $D/ra_slicing_link.h || { echo "!! header alone does not compile"; exit 1; }
RA=127.0.0.1:19820; SL=127.0.0.1:19821; VA=4601,7a3c; NV=b210,c0de
fail=0
scenario() {  # name stub-mode expect count
  $PY -u $D/ra_stub.py --listen $RA --slicing $SL --va-rntis $VA --nonva-rntis $NV --mode $2 --quiet --csv $OUT/stub_$1.csv --duration $(( $4 / 10 + 8 )) > $OUT/stub_$1.log 2>&1 &
  local sp=$!; sleep 0.5
  $OUT/test_slicing_side --ra $RA --listen $SL --va $VA --nonva $NV --count $4 --expect $3 > $OUT/test_$1.log 2>&1
  local rc=$?; kill $sp 2>/dev/null; wait $sp 2>/dev/null
  printf "%-12s stub %-8s expect %-13s -> %s | %s\n" $1 $2 $3 $([ $rc = 0 ] && echo PASS || echo FAIL) "$(grep -E 'SUMMARY|START_NOT_READY|BOOTSTRAP' $OUT/test_$1.log | tail -1)"
  [ $rc = 0 ] || { fail=1; tail -5 $OUT/test_$1.log; }
}
scenario normal  normal  stored        50
scenario reject  reject  notconfirmed  10
scenario pending pending notconfirmed  10
scenario silent  silent  hold          10
exit $fail
