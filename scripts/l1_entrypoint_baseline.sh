#!/bin/bash
# _dgist BASELINE L1 entrypoint (2026-11-12) - runs as root inside the Aerial 25-3 container (compose service "l1" of docker-compose.baseline.yaml,
# container dgist-l1) on the MIG 4g instance. Starts the researchers' VANILLA cuBB 25-3 cuphycontroller_scf exactly as their own entrypoint
# does (MPS inside the container, config_benetal, /var/log/aerial), from their tree mounted read-only at /opt/nvidia/cuBB. No dApp hook, no
# green contexts, no FAPI-aware scheduler, no YOLO in this process (the Baseline YOLO server is the separate container dgist-yolo on the 3g instance).
# Markers for docker's healthcheck: /tmp/dgist_l1_ready. Logs: /opt/dgist/runs/$RUN_NAME/ (= ./runs/<name>/ on the host), the same files as the Ours entrypoint.
set -u; umask 022
SDK=/opt/nvidia/cuBB; DG=/opt/dgist; PHYLOG=/tmp/phy.log
L1_CONFIG=${BASELINE_L1_CONFIG:-config_benetal}; RUN=$DG/runs/${RUN_NAME:-manual}
OWN=$(stat -c %u:%g $DG/runs)
log() { local m="[dgist-l1 baseline $(date +%T)] $*"; echo "$m"; echo "$m" >> $RUN/entrypoint.log 2>/dev/null; }
rm -f /tmp/dgist_l1_ready /tmp/dgist_tenant_ready; mkdir -p $RUN/aerial_log; chown -R $OWN $RUN; : > $RUN/entrypoint.log; chown $OWN $RUN/entrypoint.log
BIN=$SDK/build.$(uname -m)/cuPHY-CP/cuphycontroller/examples/cuphycontroller_scf
[ -x $BIN ] || { log "missing $BIN (BASELINE_TREE on the host must be the researchers' aerial tree with build.aarch64)"; exit 1; }
[ -f $SDK/cuPHY-CP/cuphycontroller/config/cuphycontroller_$L1_CONFIG.yaml ] || { log "missing cuphycontroller_$L1_CONFIG.yaml in the vanilla tree"; exit 1; }
grep -q "^ENABLE_DAPP_HOOK:BOOL=ON" $SDK/build.$(uname -m)/CMakeCache.txt 2>/dev/null && log "warning: this build has ENABLE_DAPP_HOOK=ON - not a vanilla build"
log "CUDA_VISIBLE_DEVICES=${CUDA_VISIBLE_DEVICES:-unset} (privileged: nvidia-smi lists every instance) | $(nvidia-smi -L 2>/dev/null | grep -c MIG) MIG instance(s) on the host"

# the researchers' recipe: MPS inside the container (pipe /var), CUDA_DEVICE_MAX_CONNECTIONS=8, AERIAL_LOG_PATH, cuphycontroller_scf <config>
export CUDA_DEVICE_MAX_CONNECTIONS=8 CUDA_MPS_PIPE_DIRECTORY=/var CUDA_MPS_LOG_DIRECTORY=/var AERIAL_LOG_PATH=$RUN/aerial_log
echo quit | nvidia-cuda-mps-control 2>/dev/null; sleep 1
nvidia-cuda-mps-control -d && echo start_server -uid 0 | nvidia-cuda-mps-control || log "warning: MPS daemon did not start (the vanilla L1 normally runs with MPS)"
# the vanilla L1 writes its nvlog file to $AERIAL_LOG_PATH/phy.log: expose it as /tmp/phy.log so dgist.sh status/down read it like in the Ours mode
rm -f $PHYLOG $RUN/aerial_log/phy.log; ln -sfn $RUN/aerial_log/phy.log $PHYLOG
env | grep -E '^(CUDA_|AERIAL_|RUN_NAME|BASELINE_|NVIDIA_VISIBLE)' | sort > $RUN/l1_env.txt; echo "L1=vanilla cuBB 25-3 $BIN config $L1_CONFIG" >> $RUN/l1_env.txt
log "vanilla cuphycontroller_scf $L1_CONFIG | $BIN | MPS on | log $RUN"
( cd $SDK && exec $BIN $L1_CONFIG ) > $RUN/l1_console.log 2>&1 & L1=$!
chown $OWN $RUN/l1_console.log $RUN/l1_env.txt 2>/dev/null
stopping=0
on_term() { [ $stopping = 1 ] && return; stopping=1; log "stop requested - SIGTERM to the L1 (SIGKILL after 15 s)"; kill -TERM $L1 2>/dev/null; ( sleep 15; kill -KILL $L1 2>/dev/null ) & }
trap on_term TERM INT
collect() {
  grep -E ' (WRN|ERR|FATAL) |Send Err.ind|recovery|not responsive|Order kernel timeout|exit handler|CONFIG.req received|START.req|L1 is ready|RACH|Preambles' $PHYLOG > $RUN/phy_warn.log 2>/dev/null
  grep -E '\] Cell +0 \| DL' $PHYLOG > $RUN/l1_persec.log 2>/dev/null
  tail -n 2000000 $PHYLOG > $RUN/phy_tail.log 2>/dev/null
  echo quit | nvidia-cuda-mps-control 2>/dev/null
  chown -R $OWN $RUN; chmod -R a+rX $RUN/aerial_log 2>/dev/null
  log "logs: $RUN | L1 Err.ind $(grep -c 'Send Err.ind' $RUN/phy_warn.log 2>/dev/null), FATAL $(grep -c FATAL $RUN/phy_warn.log 2>/dev/null)"
}
l1_ready() { grep -q 'L1 is ready' $RUN/l1_console.log $PHYLOG 2>/dev/null; }
for i in $(seq 1 150); do l1_ready && break; kill -0 $L1 2>/dev/null || break; sleep 2; done
if l1_ready; then touch /tmp/dgist_l1_ready; log "L1 is ready (vanilla, MIG 4g)"
else log "L1 did not come up: $(grep -E 'FATAL|ERR' $RUN/l1_console.log 2>/dev/null | tail -2 | cut -c1-160 | tr '\n' ';')"; fi
rc=0; while :; do wait $L1; r=$?; kill -0 $L1 2>/dev/null || { rc=$r; break; }; done
rm -f /tmp/dgist_l1_ready
log "L1 exited ($rc)"; collect; exit $rc
