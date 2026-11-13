#!/bin/bash
# logs.sh - readable live logs of every part (colour: red = errors/failures, green = success/attach, yellow = warnings/holds)
#   logs.sh <target> [-n N] [--raw]      follow (Ctrl-C to stop); -n N = how many old lines first (default 40); --raw = no filter, no colour
#   targets
#     gnb    UE attach/release, RRC, NGAP, E2 agent + slice quota events, asserts            (docker logs dgist-gnb)
#     mac    per-UE MAC statistics lines (RNTI, sync, RSRP, MCS, BLER, SNR, bytes)             (docker logs dgist-gnb)
#     l1     cuphycontroller console: ready/START, per-second DL/UL line of the phones' cell, Err.ind, FATAL/ERR/WRN   (runs/<run>/l1_console.log)
#            the phones' cell is L1 cell 0, or cell 5 when the 5-cell traffic generator runs (./tgen.sh; emulator cells = 0-4)
#     tgen   traffic generator: per-second DL/UL lines of the 5 emulator cells (L1 side) + load switches   (runs/<run>/l1_console.log, tgen_load.log)
#     ru     RU emulator console: per-cell on-time percentages, late-slot table at exit, errors   (runs/<run>/ru_console.log)
#     testmac  testMAC console: per-cell DL/UL Mbps, slots, ERR / INV counters                   (runs/<run>/testmac_console.log)
#     dapp   dApp tenant inside the L1: agent ok/fail/queue, SM profile line                   (/tmp/dapp_tenant.log in dgist-l1)
#     va     VA service: frames (with VA_VERBOSE=1), phone diagnostics, agent connection, errors   (runs/va_service/uvicorn.log)
#     nonva  non-VA service                                                                   (runs/nonva_service/uvicorn.log)
#     ric    nearRT-RIC                                                                       (runs/ric/nearRT-RIC.log)
#     xapp   PPO xApp: READY/STORED/HOLD/DEADLINE_MISS, quotas, [DAPP] feed, Python bridge      (runs/xapp/xapp.log)
#     ppo    xApp decisions only (STORED / unchanged / HOLD) + dApp feed lines
#     moni   the lab's monitor xApp text log (per-second UE/service UL PRB, wait, throughput)   (log/moni_log/ul_input_*.log)
#     core   Open5GS AMF/SMF/UPF: registrations, PDU sessions, authentication, releases, warnings   (journalctl; needs journal read access)
#     ue     UE table, refreshed every 2 s
#     all    gnb events + xApp decisions + L1 line of the phones' cell, multiplexed
set -u
D=$(cd "$(dirname "$0")" && pwd); cd $D
set -a; . $D/.env; set +a
T=${1:-help}; shift || true
N=40; RAW=0
while [ $# -gt 0 ]; do case $1 in -n) N=$2; shift 2 ;; --raw) RAW=1; shift ;; *) shift ;; esac; done
R='\x1b[31m'; G='\x1b[32m'; Y='\x1b[33m'; C='\x1b[36m'; B='\x1b[1m'; Z='\x1b[0m'
colour() {   # generic colouring of a stream
  if [ $RAW = 1 ]; then cat; else
  sed -u -E "s/(FATAL|Assert|Exiting|ERR\b|ERROR|error|Err\.ind|fail|Fail|REJECT|reject|Authentication failure|MAC verification failed|NOT_CONFIRMED|DEADLINE_MISS|out-of-sync|unreachable|Traceback)/${R}\1${Z}/g; s/(RRCSetupComplete|RRCReconfigurationComplete|Added PDU Session|Registration complete|E2 SETUP RESPONSE|E2_SLICE_APPLIED|E2_SLICE_ACCEPT|STORED|READY|success|connected|L1 is ready|dApp tenant ready|in-sync|Healthy)/${G}\1${Z}/g; s/(WRN|WARNING|HOLD|warning|note:|unchanged|stale|retry)/${Y}\1${Z}/g; s/(RNTI [0-9a-f]{4}|rnti=[0-9a-f]{4}|imsi-[0-9]+|10\.45\.[0-9.]+)/${C}\1${Z}/g"; fi
}
filt() { if [ $RAW = 1 ]; then cat; else grep --line-buffered -E "$1"; fi; }
RUN=$(cat $D/runs/current_run 2>/dev/null || echo manual)
pc_of() {   # L1 cell index of the phones' cell for the console log $1: 5 with the 5-cell traffic generator (emulator cells 0-4), else 0
  if [ "$1" = $D/runs/$RUN/l1_console.log ] && [ "$(docker inspect -f '{{.State.Running}}' dgist-l1 2>/dev/null)" = true ]; then
    docker inspect -f '{{join .Config.Cmd " "}}' dgist-l1 2>/dev/null | grep -q l1_entrypoint_tg && echo 5 || echo 0
  else local c; c=$(tail -n 3000 "$1" 2>/dev/null | grep -oE '\] Cell +[0-9]+ \| DL' | grep -oE '[0-9]+' | sort -n | tail -1); echo ${c:-0}; fi
}
case $T in
gnb)   docker logs -f --tail $N dgist-gnb 2>&1 | filt 'initiating RA|RRCSetup|RRCReconfiguration|PDU Session|Release|release|out-of-sync|in-sync|UE .* removed|NGSetup|NGAP\]|Registration|E2 |E2AP|e2_agent|E2_SLICE|UL_TOTAL_PRB_CAP\] enabled|Assert|Exiting|\[E\]|ERR|FATAL|WARNING|Connect|connect' | colour ;;
mac)   docker logs -f --tail $N dgist-gnb 2>&1 | filt 'UE RNTI|dlsch_rounds|ulsch_rounds|UE [0-9a-f]+: MAC:' | colour ;;
l1)    f=$D/runs/$RUN/l1_console.log; [ -f $f ] || f=$(ls -t $D/runs/*/l1_console.log 2>/dev/null | head -1); PC=$(pc_of $f); echo "## $f (phones' cell = L1 cell $PC)"; tail -n $N -F $f | filt "Cell +$PC \| DL|Err\.ind|FATAL|ERR|WRN|L1 is ready|START\.req|CONFIG\.req|dApp|agent|tenant|green|Preamble" | colour ;;
tgen)  f=$D/runs/$RUN/l1_console.log; [ -f $D/runs/$RUN/ru_console.log ] || { echo "run $RUN has no traffic generator (./tgen.sh on, then ./all.sh up <name>)"; exit 1; }
       echo "## $f (emulator cells 0-4) + $D/runs/$RUN/tgen_load.log | now: $(bash $D/tgen.sh status-line 2>/dev/null | cut -c1-80)"
       ( tail -n 0 -F $D/runs/$RUN/tgen_load.log 2>/dev/null | sed -u 's/^/[load switch] /' & tail -n $((N * 5)) -F $f | filt 'Cell +[0-4] \| DL|Err\.ind|FATAL' & wait ) | colour ;;
ru)    f=$D/runs/$RUN/ru_console.log; [ -f $f ] || f=$(ls -t $D/runs/*/ru_console.log 2>/dev/null | head -1); [ -n "$f" ] || { echo "no RU emulator log (traffic generator never ran)"; exit 1; }
       echo "## $f"; tail -n $N -F $f | filt '\[RU\] Cell|LATE|ON TIME|ERR|WRN|FATAL|Finaliz|Bye|error|Error' | colour ;;
testmac) f=$D/runs/$RUN/testmac_console.log; [ -f $f ] || f=$(ls -t $D/runs/*/testmac_console.log 2>/dev/null | head -1); [ -n "$f" ] || { echo "no testMAC log (traffic generator never ran)"; exit 1; }
       echo "## $f"; tail -n $N -F $f | filt 'Cell +[0-9]+ \| DL|ERR|WRN|FATAL|error|Error|CONFIG|START|launch pattern|Launch' | colour ;;
dapp)  docker exec dgist-l1 tail -n $N -F /tmp/dapp_tenant.log 2>/dev/null | filt 'agent|tenant ready|warm|engine|FAIL|fail|class' | colour ;;
va)    tail -n $N -F $D/runs/va_service/uvicorn.log | filt '\[VA\]|CLIENT|saved|Traceback|Error|error|WARNING|detect_frame.* (4|5)[0-9][0-9]|Started|startup|Uvicorn' | colour ;;
nonva) tail -n $N -F $D/runs/nonva_service/uvicorn.log | filt 'Non-VA|session|saved|Traceback|Error|error|WARNING| (4|5)[0-9][0-9] |Started|Uvicorn' | colour ;;
ric)   tail -n $N -F $D/runs/ric/nearRT-RIC.log | colour ;;
xapp)  tail -n $N -F $D/runs/xapp/xapp.log | colour ;;
ppo)   tail -n $N -F $D/runs/xapp/xapp.log | filt 'STORED|unchanged|HOLD|DEADLINE|READY|\[DAPP\]|COUNT_QUOTAS|ASSOC|NOT_CONFIRMED|Quotas removed|Python|exited|HOLD seq' | colour ;;
moni)  f=$(ls -t $D/log/moni_log/ul_input_*.log 2>/dev/null | head -1); [ -n "$f" ] || { echo "no monitor log (./xapp.sh moni start)"; exit 1; }; echo "## $f"; tail -n $N -F $f | colour ;;
core|amf|smf|upf)
  units="-u open5gs-amfd -u open5gs-smfd -u open5gs-upfd"; [ $T = amf ] && units="-u open5gs-amfd"; [ $T = smf ] && units="-u open5gs-smfd"; [ $T = upf ] && units="-u open5gs-upfd"
  J="journalctl -f -n $N $units --no-pager -o short"; if journalctl -n 1 -u open5gs-amfd --no-pager > /dev/null 2>&1; then :; else J="sudo $J"; echo "## (journal needs sudo for this account)"; fi
  $J | sed -u -E 's/^[A-Z][a-z]{2} [0-9]{2} ([0-9:]{8}) [^ ]+ open5gs-([a-z]+)d\[[0-9]+\]: [0-9/]+ [0-9:.]+: /\1 \2 /' | filt 'Registration|Deregistration|PDU|Authentication|MAC verification|Release|release|UE IPv4|InitialUEMessage|WARNING|ERROR|Added|Removed|Setup|setup' | colour ;;
ue)    while :; do clear; date +%T; python3 $D/ue_table.py; sleep 2; done ;;
all)
  ( docker logs -f --tail 5 dgist-gnb 2>&1 | filt 'initiating RA|RRCSetupComplete|Added PDU Session|Release|out-of-sync|E2_SLICE_(APPLIED|REJECT)|E2 SETUP|Assert|Exiting' | sed -u 's/^/[gnb ] /' &
    tail -n 2 -F $D/runs/xapp/xapp.log 2>/dev/null | filt 'STORED|HOLD|READY|DEADLINE|\[DAPP\]' | sed -u 's/^/[xapp] /' &
    f=$D/runs/$RUN/l1_console.log; PC=$(pc_of $f); [ -f $f ] && tail -n 1 -F $f | filt "Cell +$PC \| DL|Err\.ind|FATAL" | sed -u -E "s/.*(Cell +$PC \| DL.*)/[l1  ] \1/; s/^(\[[0-9:.]+\].*)/[l1  ] \1/" &
    wait ) | colour ;;
*) sed -n '2,23p' "$0" | sed 's/^# \{0,1\}//'; exit 2 ;;
esac
