#!/bin/bash
# all.sh - one switch for the whole demo on this host (run as nvidia from /home/nvidia/_dgist)
#   up [name] [--xapp]   idempotent: starts what is down, in order: core (sudo password if down) -> nearRT-RIC -> L1 + gNB (dgist.sh up) -> VA service
#                        -> non-VA service [-> PPO slicing xApp, once 2 VA + 2 non-VA phones stream].  RIC before gNB: the OAI E2 agent
#                        aborts the gNB if its RIC disappears, so never restart the RIC while the gNB runs.
#   down [--core]        PPO xApp (+writer, guard) -> monitor -> non-VA -> VA -> gNB + L1 (dgist.sh down) -> nearRT-RIC  [-> core]; ends with what is still running
#   status               everything on one screen
#   restart [name]       down + up
#   mode [ours|baseline] show or switch the demo mode (.env DEMO_MODE; stack must be down). baseline = vanilla cuBB 25-3 on MIG 4g + naive YOLO on MIG 3g,
#                        no xApp; the MIG partition itself needs root: sudo -n scripts/mig.sh on|off (done here when sudo -n works, else printed)
# Individual switches: ./core.sh ./dgist.sh ./va.sh ./nonva.sh ./ric.sh ./xapp.sh | logs: ./logs.sh <target>
set -u
[ "$(id -u)" = 0 ] && { echo "!! run this as the nvidia user (not root / sudo): processes and files would become root-owned and the other scripts could not stop them"; exit 1; }
D=$(cd "$(dirname "$0")" && pwd); cd $D
set -a; . $D/.env; set +a
MODE=${DEMO_MODE:-ours}
port_open() { (echo > /dev/tcp/127.0.0.1/$1) 2>/dev/null; }
sctp_listen() { ss -lnS 2>/dev/null | grep -q ":$1 "; }
core_up() { pgrep -x open5gs-amfd > /dev/null && ip -br addr show ogstun 2>/dev/null | grep -q '10.45.0.1'; }
c_up() { [ "$(docker inspect -f '{{.State.Running}}' $1 2>/dev/null)" = true ]; }
step() { echo; echo "######## $*  ($(date +%T))"; }
case "${1:-status}" in
up)
  shift; NAME=""; XAPP=0; for a in "$@"; do case $a in --xapp) XAPP=1 ;; *) NAME=$a ;; esac; done
  echo "######## mode: $MODE$( [ "$MODE" = baseline ] && echo ' (vanilla cuBB on MIG 4g + naive YOLO on MIG 3g, no xApp)')"
  [ "$MODE" = baseline ] && [ $XAPP = 1 ] && { echo "   note: --xapp ignored in baseline mode"; XAPP=0; }
  FAIL=""
  if core_up; then echo "######## core: already up"; else step "core up"; bash $D/core.sh up || FAIL="$FAIL core"; fi
  if sctp_listen 36422; then echo "######## nearRT-RIC: already up"; else step "nearRT-RIC (before the gNB)"; bash $D/ric.sh start || FAIL="$FAIL ric"; RIC_LATE=$(c_up dgist-gnb && echo 1 || echo 0); fi
  if c_up dgist-l1 && c_up dgist-gnb; then echo "######## L1 + gNB: already up (run $(cat $D/runs/current_run 2>/dev/null))"
  else
    if c_up dgist-l1 || c_up dgist-gnb || [ -f $D/runs/current_run ]; then echo "######## L1 + gNB: partially up / stale run - cleaning with dgist.sh down"; bash $D/dgist.sh down > /dev/null 2>&1; fi
    step "L1 + gNB"; bash $D/dgist.sh up ${NAME:-all_$(date +%m%d_%H%M)} || FAIL="$FAIL l1/gnb"
  fi
  if port_open ${VA_PORT:-8000}; then echo "######## VA service: already up (:${VA_PORT:-8000})"; else step "VA service"; bash $D/va.sh start || FAIL="$FAIL va"; fi
  if port_open ${NONVA_PORT:-8001}; then echo "######## non-VA service: already up (:${NONVA_PORT:-8001})$(pgrep -f 'nonva_service.*uvicorn|_dgist/va_service/yolo_env/bin/uvicorn app:app --host 0.0.0.0 --port '${NONVA_PORT:-8001} > /dev/null || echo ' - NOTE: not ours (another process holds the port)')"; else step "non-VA service"; bash $D/nonva.sh start || FAIL="$FAIL nonva"; fi
  if c_up dgist-gnb; then
    echo "   waiting for the gNB's E2 SETUP ..."; for i in $(seq 1 30); do docker logs --tail 3000 dgist-gnb 2>&1 | grep -q 'E2 SETUP RESPONSE' && break; sleep 1; done
    docker logs --tail 3000 dgist-gnb 2>&1 | grep -q 'E2 SETUP RESPONSE' && echo "   gNB E2 agent connected to the RIC" || echo "   note: no E2 SETUP RESPONSE in the gNB log yet (./logs.sh gnb)"
  fi
  if [ $XAPP = 1 ]; then
    if [ -f $D/runs/xapp/xapp.pid ] && kill -0 $(cat $D/runs/xapp/xapp.pid) 2>/dev/null; then echo "######## PPO xApp: already running"
    else step "PPO xApp (needs the 4 phones attached)"; bash $D/xapp.sh start || echo "   -> start it later with ./xapp.sh start (and ./xapp.sh moni start) once all four phones are attached"; fi
  fi
  step "status"; bash $0 status
  [ "${RIC_LATE:-0}" = 1 ] && echo "   note: the RIC was started while the gNB was already up. If ./ric.sh status shows it died (stale E2 subscriptions of an earlier RIC), run: ./dgist.sh down && ./all.sh up"
  [ -z "$FAIL" ] && echo "== all up" || { echo "== FAILED components:$FAIL  (see the messages above)"; exit 1; } ;;
down)
  shift; CORE=0; for a in "$@"; do [ "$a" = --core ] && CORE=1; done
  step "PPO xApp + compute writer + guard"; bash $D/xapp.sh stop
  step "monitor xApp"; bash $D/xapp.sh moni stop
  step "non-VA service"; bash $D/nonva.sh stop
  step "VA service"; bash $D/va.sh stop
  step "gNB + L1"; bash $D/dgist.sh down
  if c_up dgist-gnb; then echo "!! gNB still running - leaving the RIC up (the E2 agent would abort the gNB)"; else step "nearRT-RIC (after the gNB)"; bash $D/ric.sh stop; fi
  [ $CORE = 1 ] && { step "core down"; bash $D/core.sh down; }
  step "status"; bash $0 status
  left=""; sctp_listen 36422 && left="$left ric"; c_up dgist-l1 && left="$left l1"; c_up dgist-gnb && left="$left gnb"; port_open ${VA_PORT:-8000} && left="$left va:8000"; port_open ${NONVA_PORT:-8001} && left="$left :8001(not-ours?)"; pgrep -f '_dgist/.*xapp_slice_ppo_ctrl' > /dev/null && left="$left xapp"; pgrep -f '_dgist/.*xapp_ul_input_moni' > /dev/null && left="$left moni"; pgrep -f '_dgist/.*dapp_compute_writer' > /dev/null && left="$left writer"
  [ -z "$left" ] && echo "== all down$( [ $CORE = 1 ] || echo ' (core left running)')" || echo "== still running:$left" ;;
restart) shift; bash $0 down; bash $0 up "$@" ;;
mode)
  want=${2:-}; cur=$MODE
  [ -z "$want" ] && { echo "mode: $cur | MIG: $(nvidia-smi --query-gpu=mig.mode.current --format=csv,noheader 2>/dev/null) $(nvidia-smi -L 2>/dev/null | grep -c 'MIG ') instance(s)"; exit 0; }
  case $want in ours|baseline) ;; *) echo "usage: $0 mode [ours|baseline]"; exit 2 ;; esac
  c_up dgist-l1 || c_up dgist-gnb || c_up dgist-yolo && { echo "!! stack is up - $0 down first"; exit 1; }
  grep -q '^DEMO_MODE=' $D/.env && sed -i "s/^DEMO_MODE=.*/DEMO_MODE=$want/" $D/.env || echo "DEMO_MODE=$want" >> $D/.env
  echo "== .env DEMO_MODE=$want"
  need=off; [ $want = baseline ] && need=on
  have=off; nvidia-smi -L 2>/dev/null | grep -q 'MIG 4g' && nvidia-smi -L 2>/dev/null | grep -q 'MIG 3g' && have=on
  if [ $have = $need ]; then echo "   MIG already $need"; else
    if sudo -n true 2>/dev/null; then sudo -n bash $D/scripts/mig.sh $need; else echo "   MIG must be switched $need by an account with sudo:  sudo -n $D/scripts/mig.sh $need"; fi
  fi
  [ $want = baseline ] && ! ls $D/aerial-cuda-accelerated-ran/cuPHY-CP/dapp_hook/models/migfhd26${VA_MODEL#26}/seg00_sm*.engine > /dev/null 2>&1 && echo "   Baseline engine missing: scripts/build_yolo_engines_mig.sh ${VA_MODEL#26}  (after MIG on)"
  echo "   next: $0 up <name>" ;;
status)
  echo "mode      : $MODE$( [ "$MODE" = baseline ] && echo " (vanilla cuBB 25-3 on MIG 4g + naive YOLO on MIG 3g, no xApp)")"
  echo "core      : $(core_up && echo up || echo DOWN) (Open5GS, ogstun 10.45.0.1)"
  echo "L1 / gNB  : $(c_up dgist-l1 && echo "l1 up" || echo "l1 DOWN") / $(c_up dgist-gnb && echo "gnb up" || echo "gnb DOWN") | dApp agent $(port_open ${DAPP_AGENT_PORT:-9810} && echo open || echo closed) | run: $(cat $D/runs/current_run 2>/dev/null || echo none)"
  echo "VA        : $(port_open ${VA_PORT:-8000} && echo "up :${VA_PORT:-8000}" || echo DOWN) | non-VA: $(port_open ${NONVA_PORT:-8001} && echo "up :${NONVA_PORT:-8001}" || echo DOWN)"
  echo "RIC       : $(sctp_listen 36422 && echo "up (E2 36421 / E42 36422)" || echo DOWN) | PPO xApp: $( [ -f $D/runs/xapp/xapp.pid ] && kill -0 $(cat $D/runs/xapp/xapp.pid) 2>/dev/null && echo "running ($(grep -E 'STORED|unchanged|HOLD' $D/runs/xapp/xapp.log 2>/dev/null | tail -1 | sed -E 's/.*\[PPO\] //' | cut -c1-60))" || echo stopped)"
  echo "xApp side : monitor $(pgrep -f '_dgist/.*xapp_ul_input_moni' > /dev/null && echo running || echo stopped) | compute writer $(pgrep -f '_dgist/.*dapp_compute_writer' > /dev/null && echo running || echo stopped) | guard $( [ -f $D/runs/xapp/guard.pid ] && kill -0 $(cat $D/runs/xapp/guard.pid) 2>/dev/null && echo running || echo off)"
  if c_up dgist-l1; then bash $D/dgist.sh status 2>/dev/null | grep -E '^(L1|GPU|gNB)' | sed 's/^/   /'; fi
  echo "UEs:"; python3 $D/ue_table.py 2>/dev/null | sed 's/^/   /' ;;
*) sed -n '2,8p' "$0" | sed 's/^# \{0,1\}//'; exit 2 ;;
esac
