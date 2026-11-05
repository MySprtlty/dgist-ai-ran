#!/bin/bash
# ric.sh - the FlexRIC nearRT-RIC of this package (built from ./openairinterface5g/openair2/E2AP/flexric into $FLEXRIC_BUILD)
#   The OAI gNB's E2 agent (config/gnb.conf: near_ric 127.0.0.1) connects to it on SCTP 36421; xApps connect on 36422 (E42).
#   start | stop [--force] | restart | status | logs    (stop refuses while the gNB runs: the OAI E2 agent aborts the gNB when its RIC disappears)
#   install-sm    copy the service-model libraries of our build into $FLEXRIC_SM_DIR (the gNB container mounts that directory
#                 as /usr/local/lib/flexric, the RIC and the xApps load it with -p: all three must use the same build)
#   build         (re)build FlexRIC: cmake + ninja into $FLEXRIC_BUILD on the housekeeping cores (about 10 min)
# settings: .env FLEXRIC_BUILD, FLEXRIC_SM_DIR, RIC_IP | config/flexric.conf | logs: runs/ric/
set -u
[ "$(id -u)" = 0 ] && { echo "!! run this as the nvidia user (not root / sudo): processes and files would become root-owned and the other scripts could not stop them"; exit 1; }
D=$(cd "$(dirname "$0")" && pwd); cd $D
set -a; . $D/.env; set +a
FR=$D/openairinterface5g/openair2/E2AP/flexric; B=$FR/${FLEXRIC_BUILD:-build_dgist}
RIC=$B/examples/ric/nearRT-RIC; SM=${FLEXRIC_SM_DIR:-$D/flexric_sm}; CONF=$D/config/flexric.conf
R=$D/runs/ric; PIDF=$R/ric.pid; mkdir -p $R
pid() { p=$(cat $PIDF 2>/dev/null); [ -n "$p" ] && kill -0 $p 2>/dev/null && { echo $p; return; }; pgrep -f "_dgist/.*examples/ric/nearRT-RIC" | head -1; }   # pid file, else a running nearRT-RIC of this package (the lab's own RIC on 127.0.0.1 is not ours)
alive() { [ -n "$(pid)" ]; }
port_open() { (echo > /dev/tcp/127.0.0.1/$1) 2>/dev/null; }
sctp_listen() { ss -lnS 2>/dev/null | grep -q ":$1 "; }
install_sm() {
  [ -f $B/src/sm/mac_sm/libmac_sm.so ] || { echo "!! no SM libraries in $B (run: $0 build)"; return 1; }
  mkdir -p $SM; find $B/src/sm -name 'lib*_sm.so' -exec cp -f {} $SM/ \;   # KPM lives one level deeper (kpm_sm/kpm_sm_v02.03/)
  chmod 755 $SM; chmod 644 $SM/*.so   # the gNB container (other uid) must read them; nvidia's umask 027 would make the dir 750
  [ -f $SM/libkpm_sm.so ] || echo "!! libkpm_sm.so missing - the gNB registers RAN function 2 (KPM); the RIC asserts without it"
  echo "   SM libraries -> $SM: $(ls $SM | tr '\n' ' ')"
}
case "${1:-status}" in
build)
  LOG=$D/runs/build_flexric_$(date +%m%d_%H%M).log
  echo "== FlexRIC build -> $B (E2AP_V2, KPM_V2_03, bundled sqlite3, static libs; cores ${BUILD_CPUS:-0-3,65-71}) log $LOG"
  nice -n 10 taskset -c ${BUILD_CPUS:-0-3,65-71} cmake -S $FR -B $B -G Ninja -DCMAKE_BUILD_TYPE=Debug -DE2AP_VERSION=E2AP_V2 -DKPM_VERSION=KPM_V2_03 -DXAPP_DB=SQLITE3_XAPP -DXAPP_MULTILANGUAGE=OFF -DBUILDING_LIBRARY=STATIC > $LOG 2>&1 \
    && nice -n 10 taskset -c ${BUILD_CPUS:-0-3,65-71} ninja -C $B >> $LOG 2>&1 && { tail -1 $LOG; install_sm; } || { echo "!! build failed:"; tail -15 $LOG; exit 1; } ;;
install-sm) install_sm ;;
start)
  alive && { echo "nearRT-RIC already running (pid $(pid), $(ps -o user= -p $(pid) 2>/dev/null) ) - E2 $(sctp_listen 36421 && echo listening || echo NOT listening)"; echo $(pid) > $PIDF 2>/dev/null; exit 0; }
  [ -x $RIC ] || { echo "!! $RIC missing: $0 build"; exit 1; }
  [ -f $SM/libmac_sm.so ] || install_sm || exit 1
  [ -f $CONF ] || { echo "!! $CONF missing"; exit 1; }
  sctp_listen 36421 && { echo "!! something else listens on SCTP 36421 (the lab's nearRT-RIC? pgrep -af nearRT-RIC); not starting a second one"; exit 1; }
  LOG=$R/nearRT-RIC_$(date +%m%d_%H%M%S).log; ln -sfn $LOG $R/nearRT-RIC.log
  echo "== nearRT-RIC start $(date +%T): $RIC -c $CONF -p $SM/  (RIC IP $(grep -oE "NEAR_RIC_IP *= *[0-9.]+" $CONF | grep -oE "[0-9.]+$"): E2 :36421, E42 :36422)"
  (cd $R && { nohup stdbuf -oL -eL $RIC -c $CONF -p $SM/ > $LOG 2>&1 < /dev/null & echo $! > $PIDF; })
  for i in $(seq 1 20); do sctp_listen 36422 && break; alive || break; sleep 0.5; done
  if alive && sctp_listen 36422; then echo "   up (pid $(pid)); the gNB's E2 agent connects by itself (gNB log: E2 SETUP) | log $LOG";
    [ "$(docker inspect -f '{{.State.Running}}' dgist-gnb 2>/dev/null)" = true ] && echo "   note: the gNB was already running. If it still holds E2 subscriptions of a previous RIC (xApps that were connected to it), their indications hit this new RIC and FlexRIC asserts (map_ric_id.c 'Not found xApp RIC ID') within ~1 min. Then: ./dgist.sh down && ./all.sh up  (RIC first, gNB fresh)."
  else echo "!! did not come up:"; tail -15 $LOG; exit 1; fi ;;
stop)
  alive || { echo "nearRT-RIC not running"; rm -f $PIDF; exit 0; }
  if [ "$(docker inspect -f '{{.State.Running}}' dgist-gnb 2>/dev/null)" = true ] && [ "${2:-}" != --force ]; then
    echo "!! the gNB is running: its E2 agent ABORTS the gNB (nr-softmodem assertion in e2_agent.c) when the RIC disappears."; echo "   stop the gNB first (./dgist.sh down) or use: $0 stop --force"; exit 1; fi
  P=$(pid); echo "== nearRT-RIC stop (pid $P, owner $(ps -o user= -p $P 2>/dev/null))"
  kill -TERM $P 2>/dev/null || { echo "   not ours - trying sudo kill (password may be asked)"; sudo kill -TERM $P; }
  for i in $(seq 1 10); do kill -0 $P 2>/dev/null || break; sleep 1; done
  kill -0 $P 2>/dev/null && { kill -KILL $P 2>/dev/null || sudo kill -KILL $P; }; rm -f $PIDF; echo "   stopped (the gNB E2 agent keeps retrying; harmless)" ;;
restart) bash $0 stop; bash $0 start ;;
status)
  if alive; then echo "nearRT-RIC: running (pid $(pid)) | SCTP 36421 (E2) $(sctp_listen 36421 && echo listening || echo CLOSED) | 36422 (E42) $(sctp_listen 36422 && echo listening || echo CLOSED) | log $(readlink -f $R/nearRT-RIC.log 2>/dev/null)"
  else echo "nearRT-RIC: stopped$(f=$(readlink -f $R/nearRT-RIC.log 2>/dev/null); [ -f "$f" ] && grep -q -E "Assertion|Segmentation" "$f" && echo " - last instance died: $(grep -m1 -E "Assertion|Segmentation" "$f" | sed -E "s/.*(Assertion|Segmentation)/\1/" | cut -c1-90)")"; fi
  echo "SM libraries: $SM ($(ls $SM 2>/dev/null | grep -c '_sm.so') .so) | build: $B $( [ -x $RIC ] && echo ok || echo MISSING)"
  if alive; then echo "E2 nodes (from the log): $(grep -c -E 'E2 SETUP-REQUEST|E2 Setup Request' $R/nearRT-RIC.log 2>/dev/null) setup request(s), $(grep -c -E 'RIC_SUBSCRIPTION_REQUEST|Subscription Request' $R/nearRT-RIC.log 2>/dev/null) subscription(s)"; tail -3 $R/nearRT-RIC.log | sed 's/^/   /' | cut -c1-160; fi ;;
logs) tail -n 50 -f $R/nearRT-RIC.log ;;
*) sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//'; exit 2 ;;
esac
