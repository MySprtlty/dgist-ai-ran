#!/bin/bash
# _dgist L1 entrypoint - runs as root inside the Aerial 25-3 container (compose service "l1", container dgist-l1).
# Starts OUR cuphycontroller_scf (CUDA green contexts, per-slot SM sizer, in-process YOLO dApp tenant, xApp agent on TCP $DAPP_AGENT_PORT)
# with the 1-cell Benetel configuration config/cuphycontroller_<L1_CONFIG>.yaml. Markers for docker's healthcheck: /tmp/dgist_l1_ready,
# /tmp/dgist_tenant_ready. Logs: /opt/dgist/runs/$RUN_NAME/ (= ./runs/<name>/ on the host). SIGTERM (docker stop) -> L1 stopped, logs collected.
set -u; umask 022
SDK=/opt/nvidia/cuBB; DG=/opt/dgist; CFG=$SDK/cuPHY-CP/cuphycontroller/config; PHYLOG=/tmp/phy.log
BUILD_DIR=${BUILD_DIR:-build.aarch64_f1002}; L1_CONFIG=${L1_CONFIG:-dgist}; RUN=$DG/runs/${RUN_NAME:-manual}
OWN=$(stat -c %u:%g $SDK)
log() { local m="[dgist-l1 $(date +%T)] $*"; echo "$m"; echo "$m" >> $RUN/entrypoint.log 2>/dev/null; }
rm -f /tmp/dgist_l1_ready /tmp/dgist_tenant_ready; mkdir -p $RUN; chown $OWN $RUN; : > $RUN/entrypoint.log; chown $OWN $RUN/entrypoint.log
BIN=$SDK/$BUILD_DIR/cuPHY-CP/cuphycontroller/examples/cuphycontroller_scf
[ -x $BIN ] || { log "missing $BIN - on the host: ./dgist.sh build-l1"; exit 1; }
for kv in SCF_FAPI_10_04:BOOL=OFF ENABLE_CONFORMANCE_TM_PDSCH_PDCCH:BOOL=OFF SCF_FAPI_10_04_SRS:BOOL=ON ENABLE_DAPP_HOOK:BOOL=ON; do
  grep -q "^$kv\$" $SDK/$BUILD_DIR/CMakeCache.txt || log "warning: $BUILD_DIR/CMakeCache.txt lacks $kv (the validated FAPI wire layout + dApp hook)"
done
for m in n s m l x; do [ -e $SDK/cuPHY-CP/dapp_hook/models/full26$m/seg00_sm80.engine ] || log "warning: YOLO engine models/full26$m/seg00_sm80.engine missing"; done

# 1. no MPS: the green contexts partition the GPU (the hsw entrypoint starts MPS; our L1 needs it off)
export CUDA_MPS_PIPE_DIRECTORY=/var CUDA_MPS_LOG_DIRECTORY=/var
echo quit | nvidia-cuda-mps-control 2>/dev/null; sleep 1
pgrep -f "nvidia-cuda-mps-serve[r]" > /dev/null && { log "an MPS server is still running - stop it first"; exit 1; }

# 2. our yamls under their own names (no file of the SDK's own configuration is touched)
cp $DG/config/cuphycontroller_$L1_CONFIG.yaml $DG/config/l2_adapter_$L1_CONFIG.yaml $CFG/ || { log "config/*_$L1_CONFIG.yaml missing"; exit 1; }
chown $OWN $CFG/cuphycontroller_$L1_CONFIG.yaml $CFG/l2_adapter_$L1_CONFIG.yaml

# 3. dApp environment (defaults = the validated 1-cell configuration; ../.env overrides)
export DAPP_TENANT=1 DAPP_AGENT=1 DAPP_AGENT_CELLS=1
export DAPP_PUSCH_PROFILES=${DAPP_PUSCH_PROFILES:-16,24,32,40,56} DAPP_PDSCH_PROFILES=${DAPP_PDSCH_PROFILES:-24,40}
export DAPP_SIZER_MODEL=$SDK/cuPHY-CP/dapp_hook/estimator/sm_model.txt
export DAPP_TENANT_CLASSES=${DAPP_TENANT_CLASSES:-24,40,48,56,64,72,80} DAPP_TENANT_FB_GUARD=0 DAPP_TENANT_LAYOUT=union
export DAPP_TENANT_CPU=${DAPP_TENANT_CPU:-45} DAPP_AGENT_CPU=${DAPP_AGENT_CPU:-46} DAPP_AGENT_PORT=${DAPP_AGENT_PORT:-9810}
export DAPP_AGENT_GRAPH_CHUNK=${DAPP_AGENT_GRAPH_CHUNK:-3} DAPP_AGENT_CHUNK_AHEAD=${DAPP_AGENT_CHUNK_AHEAD:-2} DAPP_AGENT_QOS=${DAPP_AGENT_QOS:-1}
export DAPP_TENANT_LOG=/tmp/dapp_tenant.log DAPP_TENANT_CSV=/tmp/dapp_tenant_slots.csv
[ -n "${PRACH_FORCE_THR0:-}" ] && export CUPHY_PRACH_FORCE_THR0=$PRACH_FORCE_THR0
rm -f $PHYLOG /tmp/dapp_tenant.log /tmp/dapp_tenant_slots.csv
env | grep -E '^(DAPP_|CUPHY_|BUILD_DIR|L1_CONFIG|RUN_NAME)' | sort > $RUN/l1_env.txt

# 4. the L1 (the process renames itself phy_main)
log "cuphycontroller_scf $L1_CONFIG | $BUILD_DIR | dApp classes $DAPP_TENANT_CLASSES | agent TCP $DAPP_AGENT_PORT${CUPHY_PRACH_FORCE_THR0:+ | CUPHY_PRACH_FORCE_THR0=$CUPHY_PRACH_FORCE_THR0}"
( cd $SDK && exec $BIN $L1_CONFIG ) > $RUN/l1_console.log 2>&1 & L1=$!
chown $OWN $RUN/l1_console.log $RUN/l1_env.txt 2>/dev/null
stopping=0
on_term() { [ $stopping = 1 ] && return; stopping=1; log "stop requested - SIGTERM to the L1 (SIGKILL after 15 s)"; kill -TERM $L1 2>/dev/null; ( sleep 15; kill -KILL $L1 2>/dev/null ) & }
trap on_term TERM INT
collect() {
  grep -E ' (WRN|ERR|FATAL) |Send Err.ind|recovery|not responsive|Order kernel timeout|exit handler|CONFIG.req received|START.req|L1 is ready|RACH|Preambles|dApp sizer' $PHYLOG > $RUN/phy_warn.log 2>/dev/null
  grep -E '\] Cell +0 \| DL' $PHYLOG > $RUN/l1_persec.log 2>/dev/null
  tail -n 2000000 $PHYLOG > $RUN/phy_tail.log 2>/dev/null
  cp /tmp/dapp_tenant.log /tmp/dapp_tenant_slots.csv $RUN/ 2>/dev/null
  chown -R $OWN $RUN
  log "logs: $RUN | L1 Err.ind $(grep -c 'Send Err.ind' $RUN/phy_warn.log 2>/dev/null), FATAL $(grep -c FATAL $RUN/phy_warn.log 2>/dev/null)"
}
l1_ready() { grep -q 'L1 is ready' $RUN/l1_console.log $PHYLOG 2>/dev/null; }
port_open() { (echo > /dev/tcp/127.0.0.1/$DAPP_AGENT_PORT) 2>/dev/null; }
for i in $(seq 1 150); do l1_ready && break; kill -0 $L1 2>/dev/null || break; sleep 2; done
if l1_ready; then touch /tmp/dgist_l1_ready; log "L1 is ready"
else log "L1 did not come up: $(grep -E 'FATAL|ERR' $RUN/l1_console.log 2>/dev/null | tail -2 | cut -c1-160 | tr '\n' ';')"; fi
for i in $(seq 1 240); do port_open && break; grep -qE 'tenant NOT started|FATAL' /tmp/dapp_tenant.log $PHYLOG 2>/dev/null && break; kill -0 $L1 2>/dev/null || break; sleep 2; done
if port_open; then touch /tmp/dgist_tenant_ready; log "dApp tenant ready: xApp agent on TCP $DAPP_AGENT_PORT | $(grep -E 'CUDA graphs captured' /tmp/dapp_tenant.log 2>/dev/null | tail -1 | cut -c1-90)"
else log "dApp tenant did not start: $(grep -E 'tenant NOT started|FATAL' /tmp/dapp_tenant.log $PHYLOG 2>/dev/null | tail -1 | cut -c1-160)"; fi
rc=0; while :; do wait $L1; r=$?; kill -0 $L1 2>/dev/null || { rc=$r; break; }; done
rm -f /tmp/dgist_l1_ready /tmp/dgist_tenant_ready
log "L1 exited ($rc)"; collect; exit $rc
