#!/bin/bash
# dgist.sh - the two labs' 1-cell COTS-UE stack on this GH200, run from /home/nvidia/_dgist (nvidia account; docker group, sudo only for 'core')
#   L1   = DGIST cuphycontroller: CUDA green contexts, per-channel SM sizer, in-process YOLO dApp, xApp agent (TCP)  -> container dgist-l1
#   L2+  = hsw OAI gNB: MAC scheduler UL count-quota + E2 agent changes, DGIST empty-UCI fix                           -> container dgist-gnb
#   core = Open5GS, native systemd services of this host                                                                -> scripts/core.sh
# commands
#   up [name]        prechecks -> docker compose up -d (L1 -> healthy -> gNB) -> CONFIG/START + NG Setup report; logs in runs/<name>/
#   down             gNB log -> runs/<name>/oai.log -> docker compose down (the L1 entrypoint collects phy_warn/persec/tail/dApp logs)
#   status           containers, RAN processes, MPS, core, agent port, GPU, last cell-0 traffic line, UE counters
#   logs [l1|gnb]    follow the live log (default gnb)
#   ue [name]        UE attach phases from the gNB log (live container, else runs/<name>/oai.log)
#   test [model] [req/s] [seconds]   xApp smoke test: ra_stub.py inside dgist-l1 sends 4K JPEGs to the agent (default 26x 2 15)
#   check            host prechecks only
#   core up|down|status              Open5GS + UE NAT (sudo)                                         = ./core.sh
#   va start|stop|status|test|...    VA service: phone frames -> RUN to the dApp -> reply (va_service/) = ./va.sh
#   nonva start|stop|status|test     non-VA live-streaming service of the other phone (nonva_service/, no inference) = ./nonva.sh
#   ric start|stop|status|build      FlexRIC nearRT-RIC of this package                                  = ./ric.sh
#   xapp start|stop|status|rntis    hsw GPU-aware PPO slicing xApp + dApp feed                          = ./xapp.sh
#   all up|down|status               everything in order                                                 = ./all.sh
#   logs <target>                    readable live logs                                                  = ./logs.sh
#   build-l1 [ninja targets]         incremental build of ./aerial-cuda-accelerated-ran/$BUILD_DIR in a throwaway Aerial container (FULL=1 reconfigures)
#   build-oai                        rebuild $OAI_IMAGE from ./openairinterface5g
# settings: .env | env: NO_CORE=1 (skip the Open5GS check), ALL_CPUS=1 (builds on every core)
set -u
[ "$(id -u)" = 0 ] && { echo "!! run this as the nvidia user (not root / sudo): processes and files would become root-owned and the other scripts could not stop them"; exit 1; }
D=$(cd "$(dirname "$0")" && pwd); cd $D
set -a; . $D/.env; set +a
OUT=$D/runs; CUR=$OUT/current_run; L1C=dgist-l1; GNBC=dgist-gnb; PHYLOG=/tmp/phy.log; T=$D/aerial-cuda-accelerated-ran; mkdir -p $OUT
cmd=${1:-status}
c_up() { [ "$(docker inspect -f '{{.State.Running}}' $1 2>/dev/null)" = true ]; }
in_l1() { docker exec $L1C bash -c "$1" 2>/dev/null; }
ran_procs() { ps -eo stat,comm --no-headers | awk '$1 !~ /^Z/ && $2 ~ /^(phy_main|cuphycontroller|ru_emulator|test_mac|mac_main|nr-softmodem)/ {print $2}' | sort | uniq -c | tr '\n' ' '; }
core_up() { pgrep -x open5gs-amfd > /dev/null && ip -br addr show ogstun 2>/dev/null | grep -q '10.45.0.1'; }
port_open() { (echo > /dev/tcp/127.0.0.1/$DAPP_AGENT_PORT) 2>/dev/null; }
mps_on() { pgrep -f "nvidia-cuda-mps-serve[r]" > /dev/null; }
gnb_started() { in_l1 "grep -qE 'START.req received|Received START.request' $PHYLOG"; }
ue_counts() { local L=$1; echo "RA $(grep -c 'initiating RA procedure' $L) RRC $(grep -c RRCSetupComplete $L) PDU $(grep -c 'Added PDU Session' $L) RA-fail $(grep -cE 'RA failed|Contention Resolution timer expired' $L)"; }

check() {
  local ok=1 kv m im other rp apps hp
  docker compose version > /dev/null 2>&1 || { echo "!! docker compose v2 missing"; ok=0; }
  nvidia-smi -L > /dev/null 2>&1 || { echo "!! no GPU visible"; ok=0; }
  [ "$(nvidia-smi --query-gpu=mig.mode.current --format=csv,noheader 2>/dev/null)" = Disabled ] || { echo "!! GPU MIG mode is not Disabled"; ok=0; }
  for im in $AERIAL_IMAGE $OAI_IMAGE; do docker image inspect $im > /dev/null 2>&1 || { echo "!! image $im missing ($0 build-oai builds the gNB image)"; ok=0; }; done
  [ -x $T/$BUILD_DIR/cuPHY-CP/cuphycontroller/examples/cuphycontroller_scf ] || { echo "!! L1 binary $BUILD_DIR/cuPHY-CP/cuphycontroller/examples/cuphycontroller_scf missing ($0 build-l1)"; ok=0; }
  for kv in SCF_FAPI_10_04:BOOL=OFF ENABLE_CONFORMANCE_TM_PDSCH_PDCCH:BOOL=OFF SCF_FAPI_10_04_SRS:BOOL=ON ENABLE_DAPP_HOOK:BOOL=ON; do grep -q "^$kv\$" $T/$BUILD_DIR/CMakeCache.txt 2>/dev/null || { echo "!! $BUILD_DIR/CMakeCache.txt: expected $kv (validated FAPI wire layout + dApp hook)"; ok=0; }; done
  for m in n s m l x; do [ -e $T/cuPHY-CP/dapp_hook/models/full26$m/seg00_sm80.engine ] || { echo "!! YOLO engine models/full26$m/seg00_sm80.engine missing"; ok=0; }; done
  [ -f $D/config/cuphycontroller_$L1_CONFIG.yaml ] && [ -f $D/config/l2_adapter_$L1_CONFIG.yaml ] && [ -f $D/config/gnb.conf ] || { echo "!! config/ files missing (cuphycontroller_$L1_CONFIG.yaml, l2_adapter_$L1_CONFIG.yaml, gnb.conf)"; ok=0; }
  other=$(docker ps --format '{{.Names}}' | grep -E '^(nv-cubb|oai-gnb.*|dgist-l1|dgist-gnb)$' | tr '\n' ' '); [ -z "$other" ] || { echo "!! L1/gNB container(s) running: $other - stop that stack first (never two L1s on this GPU/NIC)"; ok=0; }
  rp=$(ran_procs); [ -z "$rp" ] || { echo "!! RAN processes alive: $rp"; ok=0; }
  mps_on && echo "   note: an MPS server is running; the L1 entrypoint stops it (green contexts need MPS off)"
  apps=$(nvidia-smi --query-compute-apps=pid,process_name --format=csv,noheader 2>/dev/null | grep -v nvidia-cuda-mps-server | grep -c .); [ "$apps" = 0 ] || echo "!! $apps other process(es) on the GPU: $(nvidia-smi --query-compute-apps=pid,process_name --format=csv,noheader | tr '\n' ' ')"
  hp=$(awk '/HugePages_Free/{print $2}' /proc/meminfo); [ "${hp:-0}" -ge 20 ] || echo "!! only ${hp:-0} hugepages free"
  pgrep -x ptp4l > /dev/null && pgrep -x phc2sys > /dev/null || { echo "!! ptp4l/phc2sys not running (PTP)"; ok=0; }
  [ "$(cat /sys/class/net/$FH_IF/operstate 2>/dev/null)" = up ] || { echo "!! $FH_IF (Benetel fronthaul link) is not up"; ok=0; }
  ping -c1 -W1 $RU_IP > /dev/null 2>&1 && echo "   Benetel RU $RU_IP answers ping" || echo "   note: Benetel RU $RU_IP does not answer ping"
  if [ "${NO_CORE:-0}" != 1 ]; then core_up && echo "   Open5GS up (amfd running, ogstun 10.45.0.1)" || { echo "!! Open5GS not up: $0 core up   (NO_CORE=1 skips this check)"; ok=0; }; fi
  [ "$(df --output=avail -BG $D | tail -1 | tr -dc 0-9)" -ge 10 ] || echo "!! less than 10 GB free on $D"
  [ -f ${FLEXRIC_SM_DIR:-/usr/local/lib/flexric}/libmac_sm.so ] || echo "!! E2 service models missing in ${FLEXRIC_SM_DIR:-/usr/local/lib/flexric} (./ric.sh install-sm)"
  ss -lnS 2>/dev/null | grep -q ":36421 " || echo "   note: no nearRT-RIC listening (./ric.sh start BEFORE the gNB; the E2 agent retries, but a RIC that disappears later aborts the gNB)"
  [ $ok = 1 ]
}

case "$cmd" in
check) check && echo "== ready: $0 up [name]" || { echo "== fix the !! lines first"; exit 1; } ;;
up)
  NAME=${2:-dgist_$(date +%m%d_%H%M)}; RUN=$OUT/$NAME
  [ -f $CUR ] && { echo "!! a run is registered as current ($(cat $CUR)): $0 down first"; exit 1; }
  echo "== $NAME: L1 (DGIST, $BUILD_DIR, config $L1_CONFIG, dApp classes $DAPP_TENANT_CLASSES) + OAI gNB ($OAI_IMAGE, cpuset $OAI_CPUSET) $(date +%T)"
  check || { echo "precheck failed"; exit 1; }
  mkdir -p $RUN; echo "$NAME" > $CUR
  echo "   docker compose up -d: the gNB starts once the L1 is healthy (L1 ready + dApp engines loaded + agent port open; up to ~10 min)"
  RUN_NAME=$NAME docker compose up -d > $RUN/compose_up.log 2>&1; grep -E 'Healthy|Started|rror|unhealthy' $RUN/compose_up.log | sed 's/^/   /'
  c_up $L1C || { echo "!! L1 container is not running:"; tail -5 $RUN/l1_console.log 2>/dev/null; docker logs $L1C 2>&1 | tail -3; rm -f $CUR; exit 1; }
  c_up $GNBC || { echo "!! gNB container is not running (L1 unhealthy?): docker logs $L1C"; docker logs $L1C 2>&1 | tail -5; echo "   tear down with: $0 down"; exit 1; }
  echo "   L1 + dApp ready $(date +%T) | $(docker logs $L1C 2>&1 | grep -E 'dApp tenant ready' | tail -1 | cut -c1-120)"
  for i in $(seq 1 90); do gnb_started && break; c_up $GNBC || break; sleep 2; done
  gnb_started && echo "   cell 0 CONFIG/START done $(date +%T)" || echo "!! no START.request for cell 0 within 180 s ($0 logs gnb)"
  for i in $(seq 1 15); do docker logs $GNBC 2>&1 | grep -q NGSetupResponse && break; sleep 2; done
  docker logs $GNBC 2>&1 | grep -q NGSetupResponse && echo "   NG Setup with the AMF ok" || echo "   note: no NGSetupResponse yet (core network? $0 logs gnb)"
  echo "   running - a COTS UE can attach now | status: $0 status | attach: $0 ue | dApp: $0 test | stop: $0 down"
  echo "   VA service for the phone (https://10.45.0.1:${VA_PORT:-8000}/): ./va.sh start$( (echo > /dev/tcp/127.0.0.1/${VA_PORT:-8000}) 2>/dev/null && echo '  (already running; it reconnects to the new L1 on the first frame)')"
  ;;
down)
  NAME=$(cat $CUR 2>/dev/null || echo stop_$(date +%m%d_%H%M)); RUN=$OUT/$NAME; mkdir -p $RUN
  echo "== down ($NAME) $(date +%T)"
  docker ps -a --format '{{.Names}}' | grep -qx $GNBC && docker logs $GNBC > $RUN/oai.log 2>&1
  docker compose down -t 40 2>&1 | grep -E 'Stopped|Removed|rror' | sed 's/^/   /'
  rm -f $CUR
  echo "   L1 Err.ind $(grep -c 'Send Err.ind' $RUN/phy_warn.log 2>/dev/null), FATAL $(grep -c FATAL $RUN/phy_warn.log 2>/dev/null) | UE: $( [ -f $RUN/oai.log ] && ue_counts $RUN/oai.log ) | logs: $RUN"
  echo "   RAN processes now: $(rp=$(ran_procs); echo ${rp:-none}) | GPU: $(nvidia-smi --query-gpu=utilization.gpu,memory.used --format=csv,noheader)"
  ;;
status)
  echo "containers: $(docker ps --format '{{.Names}}({{.Status}})' | grep -E 'dgist-|nv-cubb|oai-gnb' | tr '\n' ' ')"
  echo "RAN processes: $(rp=$(ran_procs); echo ${rp:-none}) | MPS: $(mps_on && echo ON || echo off) | Open5GS: $(core_up && echo up || echo down) | xApp agent port $DAPP_AGENT_PORT: $(port_open && echo open || echo closed) | current run: $(cat $CUR 2>/dev/null || echo none)"
  echo "GPU: $(nvidia-smi --query-gpu=utilization.gpu,memory.used --format=csv,noheader)"
  if c_up $L1C; then echo "L1: Err.ind $(in_l1 "grep -c 'Send Err.ind' $PHYLOG") FATAL $(in_l1 "grep -c FATAL $PHYLOG") | cell 0: $(in_l1 "grep -E '\] Cell +0 \| DL' $PHYLOG | tail -1" | sed -E 's/.*(DL +[0-9.]+ Mbps).*(UL +[0-9.]+ Mbps +[0-9]+ Slots CRC +[0-9]+).*/\1 \2/') | dApp: $(in_l1 'grep -E "agent ok" /tmp/dapp_tenant.log | tail -1' | sed -E 's/.*(agent ok [0-9]+ fail [0-9]+ queue [0-9]+).*/\1/')"; fi
  if c_up $GNBC; then docker logs $GNBC > $OUT/oai_live.log 2>&1; echo "gNB: NGSetup $(grep -c NGSetupResponse $OUT/oai_live.log) | UE: $(ue_counts $OUT/oai_live.log)"; fi
  echo "VA service (port ${VA_PORT:-8000}): $( (echo > /dev/tcp/127.0.0.1/${VA_PORT:-8000}) 2>/dev/null && echo "up - ./va.sh status" || echo "down - ./va.sh start")"
  echo "non-VA service (port ${NONVA_PORT:-8001}): $( (echo > /dev/tcp/127.0.0.1/${NONVA_PORT:-8001}) 2>/dev/null && echo "up - ./nonva.sh status" || echo "down - ./nonva.sh start")"
  echo "nearRT-RIC: $(ss -lnS 2>/dev/null | grep -q ":36422 " && echo up || echo down) | PPO xApp: $( [ -f $OUT/xapp/xapp.pid ] && kill -0 $(cat $OUT/xapp/xapp.pid) 2>/dev/null && echo running || echo stopped)   (./ric.sh, ./xapp.sh)"
  ;;
logs)
  case "${2:-gnb}" in
    l1)  R=$OUT/$(cat $CUR 2>/dev/null || echo manual); tail -n 50 -f $R/l1_console.log ;;
    *)   docker logs -f --tail 50 $GNBC ;;
  esac ;;
ue)
  R2=$OUT/${2:-$(cat $CUR 2>/dev/null || ls -t $OUT | grep -vE 'current_run|^stop_|\.log$' | head -1)}; L=$R2/oai.log
  if c_up $GNBC; then docker logs $GNBC > $OUT/oai_live.log 2>&1; L=$OUT/oai_live.log; fi
  [ -f "$L" ] || { echo "no gNB log ($L)"; exit 1; }
  first() { grep -m1 -E "$1" $L | cut -c1-24; }; cnt() { grep -cE "$1" $L; }
  echo "== UE attach phases ($L)"; printf '   %-44s %-26s %s\n' phase first count
  printf '   %-44s %-26s %s\n' "NG Setup with the AMF" "$(first NGSetupResponse)" "$(cnt NGSetupResponse)"
  printf '   %-44s %-26s %s\n' "RA started (Msg1)" "$(first 'initiating RA procedure')" "$(cnt 'initiating RA procedure')"
  printf '   %-44s %-26s %s\n' "Msg3 / CBRA succeeded" "$(first 'CBRA procedure succeeded|Received Ack of')" "$(cnt 'CBRA procedure succeeded|Received Ack of')"
  printf '   %-44s %-26s %s\n' "RRCSetupComplete (RRC_CONNECTED)" "$(first RRCSetupComplete)" "$(cnt RRCSetupComplete)"
  printf '   %-44s %-26s %s\n' "RRCReconfigurationComplete" "$(first RRCReconfigurationComplete)" "$(cnt RRCReconfigurationComplete)"
  printf '   %-44s %-26s %s\n' "PDU session (Added PDU Session)" "$(first 'Added PDU Session')" "$(cnt 'Added PDU Session')"
  for k in 'RA failed' 'Contention Resolution timer expired' 'out-of-sync' 'Assertion|Exiting' 'Release'; do n=$(cnt "$k"); [ "$n" -gt 0 ] && printf '   !! %-40s %4d  %s\n' "$k" "$n" "$(grep -m1 -E "$k" $L | cut -c1-120)"; done
  echo "-- last UE stats lines:"; grep -E 'UE RNTI|dlsch_rounds|ulsch_rounds|MAC:' $L | tail -4 | cut -c1-180 | sed 's/^/   /'
  [ -f $R2/phy_warn.log ] && echo "-- L1: Err.ind $(grep -c 'Send Err.ind' $R2/phy_warn.log), FATAL $(grep -c FATAL $R2/phy_warn.log), preamble detections $(grep -c 'Preambles detected' $R2/phy_warn.log)"
  true ;;
test)
  m=${2:-26x}; rate=${3:-2}; sec=${4:-15}
  port_open || { echo "xApp agent port $DAPP_AGENT_PORT closed - is the L1 running ($0 status)?"; exit 1; }
  echo "== xApp smoke test: model $m, $rate req/s, $sec s, 4K JPEGs from models/img4k (closed loop, ra_stub.py inside $L1C)"
  docker exec $L1C bash -c "cd /opt/nvidia/cuBB/cuPHY-CP/dapp_hook/python && taskset -c 65-71 python3 ra_stub.py --rate $rate --duration $sec --policy fixed:$m --deadline-ms 1000 --max-inflight 1 --csv /tmp/ra_test.csv" 2>&1 | tail -12
  echo "   (FlexRIC xApp with the same RUN path: aerial-cuda-accelerated-ran/cuPHY-CP/dapp_hook/xapp_flexric/README.md)"
  ;;
core) bash $D/scripts/core.sh "${2:-status}" ;;
va) shift; bash $D/va.sh "$@" ;;
nonva) shift; bash $D/nonva.sh "$@" ;;
ric) shift; bash $D/ric.sh "$@" ;;
xapp) shift; bash $D/xapp.sh "$@" ;;
all) shift; bash $D/all.sh "$@" ;;
logs) shift; bash $D/logs.sh "$@" ;;
ue-table|rntis) python3 $D/ue_table.py ;;
build-l1)
  shift; CPUS=$BUILD_CPUS; [ "${ALL_CPUS:-0}" = 1 ] && CPUS=0-$(( $(nproc --all) - 1 ))
  echo "== L1 build in a throwaway $AERIAL_IMAGE container (cores $CPUS${FULL:+, FULL=1 reconfigure}) $(date +%T)"
  docker run --rm --cpuset-cpus $CPUS -e BUILD_DIR=$BUILD_DIR -e FULL=${FULL:-0} -e JOBS=${JOBS:-} -v $T:/opt/nvidia/cuBB -v $D/scripts:/opt/dgist/scripts:ro -w /opt/nvidia/cuBB -u root $AERIAL_IMAGE bash /opt/dgist/scripts/build_l1.sh "$@" 2>&1 | tee $OUT/build_l1_$(date +%m%d_%H%M).log | tail -15
  ;;
build-oai) [ "${ALL_CPUS:-0}" = 1 ] && export BUILD_CPUS=0-$(( $(nproc --all) - 1 )); bash $D/scripts/build_oai_image.sh ;;
*) sed -n '2,19p' "$0" | sed 's/^# \{0,1\}//'; exit 2 ;;
esac
