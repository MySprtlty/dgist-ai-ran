#!/bin/bash
# ra.sh - the ICNL Resource Allocator (RA) of the IITP 6G demo, in this package (ra/README.md). Per VA frame it sets the UL PRB floor of the
# phone's upload and picks the YOLO model of its RUN, inside the VA quota of the slicing budget, so the result is back within the deadline.
#   ra/xapp/xapp_ccma_ra  E2 side (FlexRIC xApp built against this package's FlexRIC): MAC + Slice SM reports every 10 ms in; Slice SM
#                         quotas / UE association and RC Style 2 UL PRB floors out (the floors need the gNB patch in oai/, .env RA_RC_FLOORS=1)
#   ra/ra_main.py         the decisions (ra/ra_policy.py). Budget: .env RA_STATIC_BUDGET (VA:NONVA PRBs per UL slot) with RA_BUDGET=static,
#                         or the Slicing lab's PPO xApp in RA mode with RA_BUDGET=ppo (PPO_OUTPUT=ra; that xApp mode is not in this repository
#                         yet). YOLO server times from the dApp agent (TCP $DAPP_AGENT_PORT, role "ra"), else a static table.
#   VA service            asks the RA per frame when .env VA_RA_HOOK=<RA_HOOK> (./va.sh restart after changing it)
# Do not run it next to the PPO slicing xApp in its normal mode (./xapp.sh): both would control the same Slice SM quotas.
# commands
#   build                                    cmake + ninja of ra/xapp against $FLEXRIC_BUILD (./ric.sh build first)
#   start [VA_RNTIS NONVA_RNTIS [UE_IPS]]    E2 side, then the RA. Without arguments the RNTIs and UE IPs come from ue_table.py (gNB log +
#                                            Open5GS journal; .env VA_IMSIS / NONVA_IMSIS). UE_IPS: ip=rnti,... (the RA learns a frame's UE
#                                            from the phone's IP). Example: ./ra.sh start 83e5,c8b4 2b4a,f8e8 10.45.0.23=83e5,10.45.0.21=c8b4
#   stop                                     RA first (floors back to 0, quotas removed: Slice SM delete), then the E2 side
#   restart [args] | status | logs [e2]
# settings: .env RA_* | logs: runs/ra/ra.log, runs/ra/e2.log, runs/ra/<yymmdd_HHMMSS>/ (ticks, frames, budgets, cmds CSV)
set -u
[ "$(id -u)" = 0 ] && { echo "!! run this as the nvidia user (not root / sudo): processes and files would become root-owned and the other scripts could not stop them"; exit 1; }
D=$(cd "$(dirname "$0")" && pwd); cd $D
set -a; . $D/.env; set +a
FR=$D/openairinterface5g/openair2/E2AP/flexric; B=$FR/${FLEXRIC_BUILD:-build_dgist}
SM=${FLEXRIC_SM_DIR:-$D/flexric_sm}; CONF=$D/config/flexric_xapp.conf
E2X=$D/ra/xapp/build/xapp_ccma_ra; PY=${RA_PYTHON:-python3}
R=$D/runs/ra; PIDF=$R/ra.pid; EPIDF=$R/e2.pid; mkdir -p $R
export RA_SHELL_EVT=${RA_SHELL_EVT:-127.0.0.1:9823} RA_SHELL_CMD=${RA_SHELL_CMD:-127.0.0.1:9824} RA_HOOK=${RA_HOOK:-127.0.0.1:9822}
export RA_SLICING_LISTEN=${RA_SLICING_LISTEN:-127.0.0.1:9820} RA_PPO_ADDR=${RA_PPO_ADDR:-127.0.0.1:9821} RA_RC_FLOORS=${RA_RC_FLOORS:-1}
export DAPP_CLIENT_DIR=$D/aerial-cuda-accelerated-ran/cuPHY-CP/dapp_hook/python
pid() { cat $PIDF 2>/dev/null; }; alive() { [ -n "$(pid)" ] && kill -0 $(pid) 2>/dev/null; }
epid() { cat $EPIDF 2>/dev/null; }; ealive() { [ -n "$(epid)" ] && kill -0 $(epid) 2>/dev/null; }
port_open() { (echo > /dev/tcp/127.0.0.1/$1) 2>/dev/null; }
sctp_listen() { ss -lnS 2>/dev/null | grep -q ":$1 "; }
udp_busy() { ss -lnu 2>/dev/null | grep -q ":$1 "; }
c_up() { [ "$(docker inspect -f '{{.State.Running}}' $1 2>/dev/null)" = true ]; }
current_rntis() { if [ -n "${VA_IMSIS:-}" ] && [ -n "${NONVA_IMSIS:-}" ]; then python3 $D/ue_table.py --rntis --va-imsis $VA_IMSIS --nonva-imsis $NONVA_IMSIS; else python3 $D/ue_table.py --rntis; fi; }
stop_pid() {  # <pid> <name> <seconds>
  local P=$1; kill -TERM $P 2>/dev/null
  for i in $(seq 1 $(( $3 * 2 ))); do kill -0 $P 2>/dev/null || return 0; sleep 0.5; done
  echo "   $2 still alive after $3 s: SIGKILL"; kill -KILL $P 2>/dev/null
}
case "${1:-status}" in
build)
  LOG=$D/runs/build_ra_xapp_$(date +%m%d_%H%M).log
  [ -f $B/src/xApp/libe42_xapp.a ] || { echo "!! no FlexRIC xApp library in $B (./ric.sh build first)"; exit 1; }
  echo "== RA E2 side build -> $E2X (FlexRIC $B; cores ${BUILD_CPUS:-0-3,65-71}) log $LOG"
  nice -n 10 taskset -c ${BUILD_CPUS:-0-3,65-71} cmake -S $D/ra/xapp -B $D/ra/xapp/build -G Ninja -DFLEXRIC_DIR=$FR -DFLEXRIC_BUILD=$B > $LOG 2>&1 \
    && nice -n 10 taskset -c ${BUILD_CPUS:-0-3,65-71} ninja -C $D/ra/xapp/build >> $LOG 2>&1 && { tail -1 $LOG; ls -la $E2X | cut -c1-120; } || { echo "!! build failed:"; tail -15 $LOG; exit 1; } ;;
start)
  alive && { echo "RA already running (pid $(pid)) - $0 status"; exit 0; }
  [ -x $E2X ] || { echo "!! $E2X missing: $0 build"; exit 1; }
  [ -f $D/ra/ra_main.py ] || { echo "!! ra/ra_main.py missing"; exit 1; }
  sctp_listen 36422 || { echo "!! nearRT-RIC not listening on 36422: ./ric.sh start (then wait for the gNB's E2 SETUP)"; exit 1; }
  c_up dgist-gnb || echo "   note: gNB container dgist-gnb not running (./dgist.sh up) - the E2 side stops when the RIC has no E2 node"
  pgrep -f 'xapp_slice_(ppo|quota|prb|two_service)_ctrl' > /dev/null && { echo "!! a slice controller is running (only one may control the slices; start the RA first, a PPO xApp in RA mode after it): $(pgrep -af 'xapp_slice_' | cut -c1-100)"; exit 1; }
  ealive && { echo "!! the E2 side is still running without the RA (pid $(epid)): $0 stop"; exit 1; }
  for hp in $RA_SHELL_EVT $RA_SHELL_CMD $RA_HOOK $RA_SLICING_LISTEN; do udp_busy ${hp##*:} && { echo "!! UDP ${hp##*:} busy ($hp): $(ss -lnup 2>/dev/null | grep ":${hp##*:} " | head -1)"; exit 1; }; done
  MODE=${RA_BUDGET:-static}
  if [ $# -ge 3 ]; then VA_RNTIS=$2; NONVA_RNTIS=$3
  else line=$(current_rntis) || { echo "!! RNTI mapping not possible now (phones attached? .env VA_IMSIS/NONVA_IMSIS) - or give them: $0 start VA1,VA2 NV1,NV2 [ip=rnti,...]"; exit 1; }
       VA_RNTIS=$(echo "$line" | sed -E 's/.*VA_RNTIS=([0-9a-f,]+) NONVA.*/\1/'); NONVA_RNTIS=$(echo "$line" | sed -E 's/.*NONVA_RNTIS=([0-9a-f,]+).*/\1/'); fi
  UE_IPS=${4:-${RA_UE_IPS:-}}
  [ -n "$UE_IPS" ] || UE_IPS=$(python3 $D/ue_table.py --ue-ips 2>/dev/null | sed -n 's/^RA_UE_IPS=//p')
  [ -n "$UE_IPS" ] || { echo "!! UE IP -> RNTI mapping not possible now (ue_table.py --ue-ips: is the Open5GS journal readable?) - give it: $0 start $VA_RNTIS $NONVA_RNTIS 10.45.0.x=rnti,..."; exit 1; }
  for r in ${VA_RNTIS//,/ }; do echo ",$UE_IPS," | grep -q "=$r," || echo "   note: VA UE $r has no IP in [$UE_IPS]: its frames cannot get floors"; done
  echo "VA_RNTIS=$VA_RNTIS NONVA_RNTIS=$NONVA_RNTIS UE_IPS=$UE_IPS MODE=$MODE" > $R/rntis.txt
  ARGS="--deadline-ms ${RA_DEADLINE_MS:-${VA_DEADLINE_MS:-200}} --agent 127.0.0.1:${DAPP_AGENT_PORT:-9810} --ue-ip $UE_IPS --hook $RA_HOOK --cleanup"
  ARGS="$ARGS --shell-evt $RA_SHELL_EVT --shell-cmd $RA_SHELL_CMD --listen $RA_SLICING_LISTEN --slicing $RA_PPO_ADDR ${RA_EXTRA_ARGS:-}"
  case $MODE in
    static) ARGS="$ARGS --static-budget ${RA_STATIC_BUDGET:-164:109} --va-rntis $VA_RNTIS --nonva-rntis $NONVA_RNTIS" ;;
    ppo) ;;
    *) echo "!! .env RA_BUDGET=$MODE: static or ppo"; exit 1 ;;
  esac
  STAMP=$(date +%y%m%d_%H%M%S); LOG=$R/ra_$STAMP.log; ELOG=$R/e2_$STAMP.log; ln -sfn $LOG $R/ra.log; ln -sfn $ELOG $R/e2.log
  echo "== RA start $(date +%T): $MODE budget $( [ $MODE = static ] && echo "${RA_STATIC_BUDGET:-164:109} (VA:NONVA PRBs per UL slot)" || echo "from the PPO xApp in RA mode (listen $RA_SLICING_LISTEN)") | VA_RNTIS=$VA_RNTIS NONVA_RNTIS=$NONVA_RNTIS | RC floors $( [ $RA_RC_FLOORS = 1 ] && echo on || echo off)"
  (cd $R && { nohup stdbuf -oL -eL $E2X -c $CONF -p $SM/ > $ELOG 2>&1 < /dev/null & echo $! > $EPIDF; })
  for i in $(seq 1 30); do grep -q '\[RA-E2\] E2 nodes' $ELOG 2>/dev/null && break; ealive || break; sleep 0.5; done
  if ! { ealive && grep -q '\[RA-E2\] E2 nodes' $ELOG; }; then echo "!! E2 side not up - log tail:"; tail -8 $ELOG | cut -c1-160; ealive && stop_pid $(epid) "E2 side" 5; rm -f $EPIDF; exit 1; fi
  echo "   E2 side (pid $(epid)): $(grep -m1 '\[RA-E2\] E2 nodes' $ELOG | cut -c1-170)"
  (cd $R && { nohup stdbuf -oL -eL $PY -u $D/ra/ra_main.py $ARGS --log-dir $R/$STAMP > $LOG 2>&1 < /dev/null & echo $! > $PIDF; })
  ok=0; for i in $(seq 1 20); do
    if [ $MODE = static ]; then grep -qE '\[RA\] [0-9:]+ quota [1-9][0-9]*:[1-9][0-9]* budget seq 1 (stored|applied)' $LOG 2>/dev/null && { ok=1; break; }
    else grep -q '\[RA\] up' $LOG 2>/dev/null && { ok=1; break; }; fi
    alive || break; sleep 0.5; done
  if [ $ok = 1 ]; then
    echo "   RA (pid $(pid)): $(grep -m1 '\[RA\] up' $LOG | cut -c1-150)"
    grep -m1 '\[RA\] dApp agent' $LOG | cut -c1-150 | sed 's/^/   /'
    [ $MODE = static ] && echo "   $(grep -E '\[RA\] [0-9:]+ quota' $LOG | tail -1 | cut -c1-150)"
    [ $MODE = ppo ] && echo "   waiting for budgets on $RA_SLICING_LISTEN: start the PPO xApp in RA mode now (PPO_OUTPUT=ra PPO_RA_ADDR=$RA_SLICING_LISTEN PPO_RA_LISTEN=$RA_PPO_ADDR)"
    [ "${VA_RA_HOOK:-}" = "$RA_HOOK" ] || echo "   !! the VA service is not linked (.env VA_RA_HOOK='${VA_RA_HOOK:-}'): set VA_RA_HOOK=$RA_HOOK in .env, then ./va.sh restart"
    echo "   $0 status | $0 logs | $0 stop"
  else echo "!! RA did not come up - log tail:"; tail -10 $LOG | cut -c1-170; bash $0 stop; exit 1; fi ;;
stop)
  if alive; then P=$(pid); echo "== RA stop (pid $P): floors to 0, quotas removed, then exit"; stop_pid $P RA 6; grep -E 'quotas removed|stopped:' $R/ra.log 2>/dev/null | tail -2 | cut -c1-200 | sed 's/^/   /'
  else echo "RA not running"; fi; rm -f $PIDF
  if ealive; then P=$(epid); echo "== RA E2 side stop (pid $P)"; stop_pid $P "E2 side" 5; else echo "RA E2 side not running"; fi; rm -f $EPIDF ;;
restart) bash $0 stop; shift; bash $0 start "$@" ;;
status)
  if alive; then echo "RA: running (pid $(pid)) since $(stat -c %y $PIDF | cut -c1-19) | log $(readlink -f $R/ra.log) | $(cat $R/rntis.txt 2>/dev/null)"; else echo "RA: stopped"; fi
  if ealive; then echo "E2 side: running (pid $(epid)) | $(grep -m1 '\[RA-E2\] E2 nodes' $R/e2.log 2>/dev/null | cut -c9-160)"; else echo "E2 side: stopped"; fi
  echo "RIC 36422: $(sctp_listen 36422 && echo listening || echo closed) | gNB: $(c_up dgist-gnb && echo up || echo down) | dApp agent ${DAPP_AGENT_PORT:-9810}: $(port_open ${DAPP_AGENT_PORT:-9810} && echo open || echo closed) | VA service hook: ${VA_RA_HOOK:-off}$( [ -n "${VA_RA_HOOK:-}" ] && [ "${VA_RA_HOOK}" != "$RA_HOOK" ] && echo " (!= RA_HOOK $RA_HOOK)")"
  if alive; then echo "-- last:"; grep -E '^\[RA\] [0-9:]+ quota' $R/ra.log | tail -1 | cut -c1-230 | sed 's/^/   /'
    grep -E '^\[RA\] (floor not applied|bye|dApp agent)' $R/ra.log | tail -2 | cut -c1-200 | sed 's/^/   /'; fi ;;
logs) [ "${2:-}" = e2 ] && tail -n 40 -f $R/e2.log || tail -n 40 -f $R/ra.log ;;
*) sed -n '2,19p' $0 | sed 's/^# \{0,1\}//'; exit 1 ;;
esac
