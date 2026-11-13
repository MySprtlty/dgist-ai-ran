#!/bin/bash
# tgen.sh - 5-cell traffic generator of the demo (run as nvidia from /home/nvidia/_dgist), both modes (Ours / Baseline)
#   The l1 container then also runs the RU emulator + testMAC: 5 emulator cells (L1 cells 0-4, NIC loop aerial02 <-> aerial03) next to the
#   phones' Benetel cell (L1 cell 5). The 5 cells carry test-vector traffic; only their UPLINK (PUSCH) load changes with the level.
#   on | off                 .env TRAFFIC_GEN=1|0 - takes effect at the next ./all.sh up (the L1 has to start with 6 cells or with 1)
#   load low|mid|high|auto   switch the load of the 5 cells WHILE they run (no restart): low = 1, mid = 2, high = 4 UEs per cell and uplink slot;
#                            auto = a new level every 3 s (1 -> 2 -> 4 -> 3 UEs). Both tools change at the same frame, about 0.3 s later.
#   status                   switch, processes, load level, the 5 cells' traffic, RU-emulator on-time figures, log size
#   .env: TRAFFIC_GEN, TRAFFIC_LOAD (level at start), TRAFFIC_GEN_TV_DIR, TRAFFIC_GEN_LOG_DIR (full logs: several GB per hour, kept off the root disk)
# Logs: ./logs.sh tgen | ru | testmac ; files runs/<run>/ru_console.log, testmac_console.log, tglog/ (full nvlog files)
set -u
[ "$(id -u)" = 0 ] && { echo "!! run this as the nvidia user (not root / sudo)"; exit 1; }
D=$(cd "$(dirname "$0")" && pwd); cd $D
set -a; . $D/.env; set +a
L1C=dgist-l1; RUN=$D/runs/$(cat $D/runs/current_run 2>/dev/null || echo manual)
TGLOG=${TRAFFIC_GEN_LOG_DIR:-/data/dgist_tgen/log}
c_up() { [ "$(docker inspect -f '{{.State.Running}}' $1 2>/dev/null)" = true ]; }
live() { c_up $L1C && docker inspect -f '{{join .Config.Cmd " "}}' $L1C 2>/dev/null | grep -q l1_entrypoint_tg; }   # this L1 container was started with the generator
procs() { local p; p=$(ps -eo args= | grep -oE '/(ru_emulator|test_mac) ' | tr -d '/ ' | sort -u | tr '\n' ' '); echo "${p:-none}"; }
load_now() { python3 $D/scripts/tgen_load.py show 2>&1 | head -1; }
# last per-second line of each emulator cell in the L1 console (cells 0-4): cell count, DL / UL Mbps sums, UL slots, CRC errors
emu() { tail -n 5000 $RUN/l1_console.log 2>/dev/null | grep -E '\] Cell +[0-4] \| DL' | tail -5 | sed -E 's/.*Cell +([0-9]+) \| DL +([0-9.]+) Mbps +([0-9]+) Slots \| UL +([0-9.]+) Mbps +([0-9]+) Slots CRC +([0-9]+).*/\1 \2 \3 \4 \5 \6/' \
  | awk '{ n++; dl += $2; ul += $4; us += $5; crc += $6 } END { if (n) printf "%d cells, DL %.0f Mbps, UL %.0f Mbps in %d slots, CRC errors %d (last second)", n, dl, ul, us, crc; else printf "no per-second line of cells 0-4 yet" }'; }
# RU emulator per-second lines: share of on-time slots it saw from the L1 (DL C-plane, DL U-plane, UL C-plane), minimum over the 5 cells
ru_on() { tail -n 400 $RUN/ru_console.log 2>/dev/null | grep -E '\[RU\] Cell +[0-9]+ UL' | tail -5 | sed -E 's/.*DL_C_ON +([0-9.]+)% DL_U_ON +([0-9.]+)% UL_C_ON +([0-9.]+)%.*/\1 \2 \3/' \
  | awk 'BEGIN { a = b = c = 101 } NF == 3 { n++; if ($1 < a) a = $1; if ($2 < b) b = $2; if ($3 < c) c = $3 } END { if (n) printf "DL-C %.2f %% DL-U %.2f %% UL-C %.2f %% (min of %d cells)", a, b, c, n; else printf "no RU line yet" }'; }
logsz() { local d=$TGLOG/$(basename $RUN); [ -d $d ] && echo "$(du -sh $d 2>/dev/null | cut -f1) in $d" || echo "-"; }
set_env() {   # $1 = key, $2 = value: replace the line in .env or append it
  if grep -q "^$1=" $D/.env; then sed -i "s|^$1=.*|$1=$2|" $D/.env; else echo "$1=$2" >> $D/.env; fi
}
case "${1:-status}" in
on|off)
  v=0; [ "$1" = on ] && v=1
  set_env TRAFFIC_GEN $v; echo "== .env TRAFFIC_GEN=$v (5-cell traffic generator $1, mode ${DEMO_MODE:-ours}, level at start: ${TRAFFIC_LOAD:-low})"
  if c_up $L1C; then echo "   the running L1 keeps its layout ($(live && echo '6 cells, generator on' || echo '1 cell, generator off')); apply with: ./all.sh restart <name>   (L1 + gNB restart, the phones re-attach)"
  else echo "   next: ./all.sh up <name>"; fi ;;
load)
  lv=${2:-}; [ -n "$lv" ] || { echo "usage: $0 load low|mid|high|auto   | now: $(live && load_now || echo 'generator not running')"; exit 2; }
  live || { echo "!! the traffic generator is not running (./tgen.sh on, then ./all.sh up <name>)"; exit 1; }
  docker exec $L1C python3 /opt/dgist/scripts/tgen_load.py "$lv" || { echo "!! load switch failed"; exit 1; }
  echo "$(date +%T) load $lv" >> $RUN/tgen_load.log 2>/dev/null   # time line of the switches of this run
  ;;
status-line)   # one line for dgist.sh status
  if live; then echo "running ($(procs)) | $(load_now) | $(emu) | RU on-time $(ru_on) | full logs $(logsz)"
  else echo "$( [ "${TRAFFIC_GEN:-0}" = 1 ] && echo 'on in .env, not running' || echo off)"; fi ;;
status|show)
  echo "switch    : .env TRAFFIC_GEN=${TRAFFIC_GEN:-0} (level at start ${TRAFFIC_LOAD:-low}) | mode ${DEMO_MODE:-ours}"
  if live; then
    echo "running   : yes - L1 with 6 cells (emulator cells 0-4, phones' cell 5), processes: $(procs), run $(basename $RUN)"
    echo "load      : $(load_now)   (change: $0 load low|mid|high|auto)"
    echo "5 cells   : $(emu)"
    echo "RU on-time: $(ru_on)"
    echo "L1        : Err.ind $(grep -c 'Send Err.ind' $RUN/l1_console.log 2>/dev/null) FATAL $(grep -c FATAL $RUN/l1_console.log 2>/dev/null) | phones' cell 5: $(tail -n 5000 $RUN/l1_console.log 2>/dev/null | grep -E '\] Cell +5 \| DL' | tail -1 | sed -E 's/.*(DL +[0-9.]+ Mbps).*(UL +[0-9.]+ Mbps +[0-9]+ Slots CRC +[0-9]+).*/\1 \2/')"
    echo "full logs : $(logsz)"
    [ -s $RUN/tgen_load.log ] && echo "switches  : $(tail -n 5 $RUN/tgen_load.log | tr '\n' ';')"
  else
    echo "running   : no$(c_up $L1C && echo ' (the L1 runs with 1 cell)')"
  fi ;;
*) sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'; exit 2 ;;
esac
