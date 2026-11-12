#!/bin/bash
# xapp.sh - the hsw PPO slicing xApp, ported to this package (two bundles, chosen with .env PPO_BUNDLE_NAME)
#   C xApp  : xapp_slice_ppo_ctrl (our FlexRIC build) - subscribes MAC(142)+Slice(145) SM, every 100 ms sends the 4 UEs' UL buffer/SNR/
#             throughput/PRB + current quotas to the Python actor and applies its answer as UL PRB count quotas (total 273, no lending)
#             through the Slice SM control (NR_UL_PRB_COUNT_V1). On stop it removes the quotas again.
#   bundles : xapp_gpu_aware_no_lending_6G  (default) 20 inputs = radio 18 + YOLO model + SMs allocated to YOLO, read from the compute file
#                 that our dapp_compute_writer.py keeps fresh from the dApp agent (sm_report.last / estimate) - the FAPI-Aware Scheduler feed
#             xapp_gpu_aware_no_lending      19 inputs = radio 18 + NVML GPU utilization; our dapp_reader.py only records the dApp values
#   rntis                        who is who: RNTI, IMSI, IP, service (from the service logs) and the VA_RNTIS/NONVA_RNTIS line
#   start [VA_RNTIS NONVA_RNTIS] without arguments the RNTIs come from the phones' IMSIs (.env VA_IMSIS / NONVA_IMSIS, stable across re-attach;
#                                fallback: which service each UE IP posts to). With XAPP_GUARD=1 a guard restarts the xApp when a phone re-attaches
#                                with a new RNTI (the lab xApp needs all four RNTIs it was started with; otherwise it HOLDs). The monitor
#                                xApp, if running, is restarted too (its service labels are also start-time RNTIs).
#   stop | restart | status | logs | trace [n]  (last n decisions with the dApp fields) | dapp (feed check) | compute (the compute file)
#   moni power [SEC]                       per-UE UL power table (ue_power.py: RSRP, PCMAX, PH, UE Ptx, PRBs it can afford vs the grants it gets, MCS/BLER/SNR)
#   moni [start|stop|status|logs] [VA NONVA]   the lab's read-only monitor xApp (xapp_ul_input_moni): per-second UE/service UL figures,
#                                text log ./log/moni_log/; no sub-command = foreground (Ctrl-C). Runs beside the PPO xApp (both are RIC clients)
# settings: .env PPO_BUNDLE_NAME, PPO_GPU_SOURCE (19-input bundle), PPO_DAPP, PPO_MIN_PRBS, FLEXRIC_BUILD, FLEXRIC_SM_DIR | logs: runs/xapp/
set -u
[ "$(id -u)" = 0 ] && { echo "!! run this as the nvidia user (not root / sudo): processes and files would become root-owned and the other scripts could not stop them"; exit 1; }
D=$(cd "$(dirname "$0")" && pwd); cd $D
set -a; . $D/.env; set +a
B=$D/openairinterface5g/openair2/E2AP/flexric/${FLEXRIC_BUILD:-build_dgist}
X=$B/examples/xApp/c/slice/xapp_slice_ppo_ctrl; MONI=$B/examples/xApp/c/kpm_rc/xapp_ul_input_moni
SM=${FLEXRIC_SM_DIR:-$D/flexric_sm}; CONF=$D/config/flexric_xapp.conf
R=$D/runs/xapp; PIDF=$R/xapp.pid; WPIDF=$R/compute_writer.pid; mkdir -p $R/ppo_input_log
BUNDLE=${PPO_BUNDLE_NAME:-xapp_gpu_aware_no_lending_6G}
export PPO_RADIO_INPUT=1 PPO_BUNDLE=$D/xapp_ppo/$BUNDLE PPO_PYTHON=$D/xapp_ppo/xapp_basic_no_lending/.venv/bin/python
export PPO_GPU_DEVICE=${PPO_GPU_DEVICE:-0} PPO_GPU_SOURCE=${PPO_GPU_SOURCE:-nvml} PPO_DAPP=${PPO_DAPP:-1} PPO_DAPP_AGENT=127.0.0.1:${DAPP_AGENT_PORT:-9810}
export PPO_MIN_PRBS=${PPO_MIN_PRBS:-5} PPO_LOG_DIR=$R/ppo_input_log DAPP_CLIENT_DIR=$D/aerial-cuda-accelerated-ran/cuPHY-CP/dapp_hook/python
export PPO_SM_INPUT_FILE=$R/compute_state.json PPO_COMPUTE_MODEL=${VA_MODEL:-26x}
unset PPO_GPU_INPUT_FIXED
IS6G=0; [ -f $PPO_BUNDLE/sm_runtime.py ] && IS6G=1
pid() { cat $PIDF 2>/dev/null; }
alive() { [ -n "$(pid)" ] && kill -0 $(pid) 2>/dev/null; }
wpid() { cat $WPIDF 2>/dev/null; }
walive() { [ -n "$(wpid)" ] && kill -0 $(wpid) 2>/dev/null; }
port_open() { (echo > /dev/tcp/127.0.0.1/$1) 2>/dev/null; }
sctp_listen() { ss -lnS 2>/dev/null | grep -q ":$1 "; }
c_up() { [ "$(docker inspect -f '{{.State.Running}}' $1 2>/dev/null)" = true ]; }
GPIDF=$R/guard.pid
gpid() { cat $GPIDF 2>/dev/null; }
galive() { [ -n "$(gpid)" ] && kill -0 $(gpid) 2>/dev/null; }
current_rntis() {   # IMSI-based (stable) when .env has VA_IMSIS/NONVA_IMSIS, else the service-log heuristic
  if [ -n "${VA_IMSIS:-}" ] && [ -n "${NONVA_IMSIS:-}" ]; then python3 $D/ue_table.py --rntis --va-imsis $VA_IMSIS --nonva-imsis $NONVA_IMSIS; else python3 $D/ue_table.py --rntis; fi; }
guard_start() { [ "${XAPP_GUARD:-1}" = 1 ] || return 0; galive && return 0; { nohup bash $0 guard >> $R/guard.log 2>&1 < /dev/null & echo $! > $GPIDF; }; echo "   guard (pid $(gpid)): restarts the xApp with new RNTIs when a phone re-attaches (log $R/guard.log)"; }
guard_stop() { galive && { kill -TERM $(gpid) 2>/dev/null; }; rm -f $GPIDF; }
compute_show() { if [ -f $PPO_SM_INPUT_FILE ]; then python3 -c "
import json,time; d=json.load(open('$PPO_SM_INPUT_FILE')); print(f\"   compute file: model {d['model_id']} allocated_sm {d['allocated_sm']} age {time.time()*1000-d['timestamp_ms']:.0f} ms ({'fresh' if time.time()*1000-d['timestamp_ms']<2000 else 'STALE: policy holds'})\")"; else echo "   compute file: none ($PPO_SM_INPUT_FILE)"; fi; }
writer_start() {
  walive && return 0
  WLOG=$R/compute_writer_$(date +%m%d_%H%M%S).log; ln -sfn $WLOG $R/compute_writer.log
  { nohup $PPO_PYTHON $PPO_BUNDLE/dapp_compute_writer.py --output $PPO_SM_INPUT_FILE > $WLOG 2>&1 < /dev/null & echo $! > $WPIDF; }
  for i in $(seq 1 20); do [ -f $PPO_SM_INPUT_FILE ] && break; sleep 0.5; done
  [ -f $PPO_SM_INPUT_FILE ] && echo "   compute writer up (pid $(wpid)): $(tail -1 $WLOG | cut -c1-150)" || echo "   note: compute writer running but no file yet (dApp agent down? the policy holds until it appears): $(tail -1 $WLOG | cut -c1-120)"
}
writer_stop() { walive && { kill -TERM $(wpid) 2>/dev/null; sleep 1; kill -KILL $(wpid) 2>/dev/null; }; rm -f $WPIDF; }
case "${1:-status}" in
rntis) python3 $D/ue_table.py; echo; current_rntis || true; [ -f $R/rntis.txt ] && echo "xApp started with: $(cat $R/rntis.txt)" ;;
guard)   # loop: when the 4 phones' RNTIs differ from the ones the running xApp was started with, restart it (stop removes quotas, start re-associates)
  echo "[GUARD] $(date +%T) watching RNTIs of VA_IMSIS=${VA_IMSIS:-?} NONVA_IMSIS=${NONVA_IMSIS:-?} every ${XAPP_GUARD_S:-5} s"
  while :; do sleep ${XAPP_GUARD_S:-5}
    alive || { echo "[GUARD] $(date +%T) xApp not running - guard exits"; rm -f $GPIDF; exit 0; }
    cur=$(current_rntis 2>/dev/null) || continue      # a phone is (re)attaching: wait until all four are back
    [ "$cur" = "$(cat $R/rntis.txt 2>/dev/null)" ] && continue
    echo "[GUARD] $(date +%T) RNTI change: was [$(cat $R/rntis.txt 2>/dev/null)] now [$cur] -> restarting the xApp"
    bash $0 stop --keep-guard; bash $0 start --keep-guard || echo "[GUARD] restart failed; retrying on the next change"
    if [ -f $R/moni.pid ] && kill -0 $(cat $R/moni.pid) 2>/dev/null; then echo "[GUARD] $(date +%T) restarting the monitor xApp with the new RNTIs"; bash $0 moni stop; bash $0 moni start; fi
  done ;;
dapp) N=3 $PPO_PYTHON $PPO_BUNDLE/dapp_reader.py ;;
compute) [ "${2:-}" = restart ] && { writer_stop; writer_start; }; compute_show; [ -f $R/compute_writer.log ] && tail -3 $R/compute_writer.log | cut -c1-200 ;;
start)
  alive && { echo "PPO xApp already running (pid $(pid)) - $0 status"; exit 0; }
  [ -x $X ] || { echo "!! $X missing (./ric.sh build)"; exit 1; }
  [ -x $PPO_PYTHON ] || { echo "!! $PPO_PYTHON missing"; exit 1; }
  [ -f $PPO_BUNDLE/ppo_socket_server.py ] || { echo "!! bundle $PPO_BUNDLE missing ppo_socket_server.py"; exit 1; }
  sctp_listen 36422 || { echo "!! nearRT-RIC not listening on 36422: ./ric.sh start (then wait for the gNB's E2 SETUP)"; exit 1; }
  c_up dgist-gnb || { echo "!! gNB container not running (./dgist.sh up)"; exit 1; }
  pgrep -f 'xapp_slice_(ppo|quota|prb|two_service)_ctrl' > /dev/null && { echo "!! another slice controller is running (ours or the lab's - only one may control the slices): $(pgrep -af 'xapp_slice_' | cut -c1-100)"; exit 1; }
  port_open ${VA_PORT:-8000} || echo "   note: VA service down (./va.sh start)"; port_open ${NONVA_PORT:-8001} || echo "   note: non-VA service down (./nonva.sh start)"
  port_open ${DAPP_AGENT_PORT:-9810} || echo "   note: dApp agent port closed - $( [ $IS6G = 1 ] && echo 'the 6G policy HOLDS until the compute file is fresh' || echo 'the dApp feed will retry')"
  if [ $# -ge 3 ]; then VA_RNTIS=$2; NONVA_RNTIS=$3
  else line=$(current_rntis) || { echo "!! RNTI mapping not possible now (phones attached? .env VA_IMSIS/NONVA_IMSIS) - or give them: $0 start VA1,VA2 NV1,NV2"; exit 1; }
       VA_RNTIS=$(echo "$line" | sed -E 's/.*VA_RNTIS=([0-9a-f,]+) NONVA.*/\1/'); NONVA_RNTIS=$(echo "$line" | sed -E 's/.*NONVA_RNTIS=([0-9a-f,]+).*/\1/'); fi
  export VA_RNTIS NONVA_RNTIS; echo "VA_RNTIS=$VA_RNTIS NONVA_RNTIS=$NONVA_RNTIS" > $R/rntis.txt
  LOG=$R/xapp_$(date +%m%d_%H%M%S).log; ln -sfn $LOG $R/xapp.log
  echo "== PPO xApp start $(date +%T): bundle $BUNDLE ($( [ $IS6G = 1 ] && echo '20 inputs: radio + YOLO model + SM from the dApp' || echo "19 inputs: radio + GPU ($PPO_GPU_SOURCE)")) | VA_RNTIS=$VA_RNTIS NONVA_RNTIS=$NONVA_RNTIS | min PRBs $PPO_MIN_PRBS"
  echo "   python $PPO_PYTHON | log $LOG | C-xApp own log: $D/log/ppo_output_log/"
  [ $IS6G = 1 ] && writer_start
  (cd $R && { nohup $X -c $CONF -p $SM/ > $LOG 2>&1 < /dev/null & echo $! > $PIDF; })
  for i in $(seq 1 60); do grep -q -E '\[PPO\] READY|START_NOT_READY|Stop the other controller|Exactly one E2 node|Cannot|exited before ready|requires two|parser.error|error:' $LOG 2>/dev/null && break; alive || break; sleep 1; done
  if alive && grep -q '\[PPO\] READY' $LOG; then echo "   READY: quotas 137:136 bootstrapped, 4 UEs associated; decisions every 100 ms -> $0 status / $0 trace / ./logs.sh ppo"
    [ "${2:-}" = --keep-guard ] || guard_start
  else echo "!! not ready - log tail:"; tail -12 $LOG | cut -c1-160; alive && { kill -TERM $(pid); sleep 3; }; rm -f $PIDF; writer_stop; exit 1; fi ;;
stop)
  [ "${2:-}" = --keep-guard ] || guard_stop
  if alive; then P=$(pid); echo "== PPO xApp stop (pid $P): removing the quotas, then exit"; kill -TERM $P 2>/dev/null
    for i in $(seq 1 25); do kill -0 $P 2>/dev/null || break; sleep 1; done
    kill -0 $P 2>/dev/null && { echo "   still alive: SIGKILL"; kill -KILL $P 2>/dev/null; pkill -f ppo_socket_server.py 2>/dev/null; }
    rm -f $PIDF; grep -E 'Quotas removed|Cleanup not confirmed|Stopping' $R/xapp.log 2>/dev/null | tail -2 | sed 's/^/   /'
  else echo "PPO xApp not running"; rm -f $PIDF; fi
  writer_stop; echo "   stopped" ;;
restart) bash $0 stop; shift; bash $0 start "$@" ;;
status)
  if alive; then echo "PPO xApp: running (pid $(pid)) since $(stat -c %y $PIDF | cut -c1-19) | bundle $BUNDLE | log $(readlink -f $R/xapp.log 2>/dev/null)"; else echo "PPO xApp: stopped (bundle $BUNDLE)"; fi
  echo "RIC 36422: $(sctp_listen 36422 && echo listening || echo closed) | gNB: $(c_up dgist-gnb && echo up || echo down) | dApp agent $PPO_DAPP_AGENT: $(port_open ${DAPP_AGENT_PORT:-9810} && echo open || echo closed) | compute writer: $(walive && echo running || echo stopped)"
  [ $IS6G = 1 ] && compute_show
  echo "RNTIs: started with [$(cat $R/rntis.txt 2>/dev/null || echo -)] | now [$(current_rntis 2>/dev/null || echo "not all 4 phones attached")] | guard: $(galive && echo "running (pid $(gpid))" || echo off) | SIMs: VA ${VA_IMSIS:-?} non-VA ${NONVA_IMSIS:-?}"
  if alive; then
    echo "-- last decisions:"; grep -E 'STORED|unchanged|HOLD|DEADLINE_MISS|NOT_CONFIRMED' $R/xapp.log | tail -3 | cut -c1-150 | sed 's/^/   /'
    echo "-- dApp feed:"; { grep '\[DAPP\]' $R/xapp.log | tail -1; [ -f $R/compute_writer.log ] && grep '\[COMPUTE\]' $R/compute_writer.log | tail -1; } | cut -c1-220 | sed 's/^/   /'
    echo "-- gNB side (last E2 slice events):"; docker logs --tail 3000 dgist-gnb 2>&1 | grep -E 'E2_SLICE_(ACCEPT|APPLIED|REJECT)' | tail -2 | cut -c1-170 | sed 's/^/   /'
  fi
  echo "-- UEs:"; python3 $D/ue_table.py | sed 's/^/   /' ;;
trace) python3 $D/xapp_ppo/trace_tail.py ${2:-5} $R/ppo_input_log $PPO_BUNDLE/logs ;;
logs) tail -n 40 -f $R/xapp.log ;;
moni)   # the lab's read-only monitor xApp (MAC 142 + Slice 145): per-second UE/service UL figures; log -> ./log/moni_log/ul_input_*.log
  [ -x $MONI ] || { echo "!! $MONI missing (./ric.sh build)"; exit 1; }
  MPIDF=$R/moni.pid; mpid() { cat $MPIDF 2>/dev/null; }; malive() { [ -n "$(mpid)" ] && kill -0 $(mpid) 2>/dev/null; }
  sub=${2:-fg}; shift; [ $# -gt 0 ] && shift
  case "$sub" in
  start|fg)
    malive && { echo "monitor already running (pid $(mpid)) - $0 moni logs"; exit 0; }
    sctp_listen 36422 || { echo "!! nearRT-RIC not listening (./ric.sh start)"; exit 1; }
    if [ $# -ge 2 ]; then export VA_RNTIS=$1 NONVA_RNTIS=$2; else eval "export $(current_rntis)" || { echo "!! RNTI mapping not possible now ($0 rntis)"; exit 1; }; fi
    echo "== monitor xApp: VA=$VA_RNTIS NONVA=$NONVA_RNTIS | text log -> $D/log/moni_log/ | RIC $(grep -oE '[0-9]+(\.[0-9]+){3}' $CONF | head -1)"
    if [ "$sub" = fg ]; then cd $R && exec $MONI -c $CONF -p $SM/; fi
    MLOG=$R/moni_$(date +%m%d_%H%M%S).log; ln -sfn $MLOG $R/moni.log
    (cd $R && { nohup stdbuf -oL $MONI -c $CONF -p $SM/ > $MLOG 2>&1 < /dev/null & echo $! > $MPIDF; })
    for i in $(seq 1 20); do grep -q -E 'Text log|Successfully subscribed|rror' $MLOG 2>/dev/null && break; sleep 0.5; done
    echo "   pid $(mpid) | $(grep -m1 'Text log' $MLOG | cut -c1-140) | follow: $0 moni logs  or  ./logs.sh moni" ;;
  stop) malive && { kill -TERM $(mpid) 2>/dev/null; sleep 2; kill -KILL $(mpid) 2>/dev/null; echo "   monitor stopped: $(grep 'Log saved' $R/moni.log 2>/dev/null | tail -1)"; } || echo "monitor not running"; rm -f $MPIDF ;;
  status) malive && echo "monitor: running (pid $(mpid)) | display log $(readlink -f $R/moni.log) | text log $(ls -t $D/log/moni_log/ul_input_*.log 2>/dev/null | head -1)" || echo "monitor: stopped"; ls -t $D/log/moni_log/*.log 2>/dev/null | head -3 | sed 's/^/   /' ;;
  power) shift; [ -f $R/rntis.txt ] && . $R/rntis.txt; VA_RNTIS=${VA_RNTIS:-} NONVA_RNTIS=${NONVA_RNTIS:-} python3 $D/ue_power.py --watch ${1:-2} --log "${@:2}" ;;   # per-UE UL power picture (RSRP, PCMAX, PH, Ptx, affordable PRB vs grants, MCS/BLER/SNR), CSV -> log/moni_log/ue_power_*.csv
  logs) f=$(readlink -f $R/moni.log 2>/dev/null); [ -r "$f" ] || { echo "!! cannot read $f ($(stat -c %U "$f" 2>/dev/null)-owned - started with sudo? fix: sudo chown -R nvidia:nvidia $D/runs $D/log) or no monitor started yet ($0 moni start)"; exit 1; }; malive || echo "(monitor not running - showing the last log)"; tail -n 30 -f $f ;;
  *) echo "usage: $0 moni [start|stop|status|logs|power [SEC]] [VA1,VA2 NV1,NV2]   (no sub-command = foreground, Ctrl-C to stop; power = ue_power.py table every SEC s)"; exit 2 ;;
  esac ;;
*) sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'; exit 2 ;;
esac
