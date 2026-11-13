#!/bin/bash
# _dgist BASELINE YOLO server entrypoint (2026-11-12) - runs as root inside the Aerial 25-3 container (compose service "yolo" of
# docker-compose.baseline.yaml, container dgist-yolo) on the MIG 3g instance (60 SMs). A NAIVE inference server: the stand-alone tenant
# binary of our tree with every scheduling feature switched off - one static partition = the whole 3g instance (DAPP_TENANT_FIXED), no FAPI
# (FREERUN), no deadline admission (DAPP_AGENT_QOS=0), no graph chunking / pausing (DAPP_AGENT_GRAPH_CHUNK=0), no model selection of its own (one
# model: the .env VA_MODEL, batch-3 FHD engine built inside the 3g instance = the same decode + batch-3 pipeline + NMS as Ours). Requests arrive on
# the same TCP agent port (DAPP_AGENT_PORT) and run back to back in arrival order, so the VA service, its logs and the viewer are unchanged.
# VA_MODEL_SELECT=1 (the VA service's rule, VA_MODEL_WEIGHTS = requests per block for 26n,26s,26m,26l,26x): the other models with a count above 0
# are loaded too if their engine for this instance exists; a model without one is left out and the VA service sends its share as VA_MODEL.
# Marker for docker's healthcheck: /tmp/dgist_yolo_ready. Logs: /opt/dgist/runs/$RUN_NAME/yolo_* (= ./runs/<name>/ on the host).
set -u; umask 022
SDK=/opt/nvidia/cuBB; DG=/opt/dgist; RUN=$DG/runs/${RUN_NAME:-manual}; M=$SDK/cuPHY-CP/dapp_hook/models
OWN=$(stat -c %u:%g $SDK)
log() { local m="[dgist-yolo baseline $(date +%T)] $*"; echo "$m"; echo "$m" >> $RUN/yolo_entrypoint.log 2>/dev/null; }
rm -f /tmp/dgist_yolo_ready; mkdir -p $RUN; chown $OWN $RUN; : > $RUN/yolo_entrypoint.log; chown $OWN $RUN/yolo_entrypoint.log
BIN=$SDK/${BUILD_DIR:-build.aarch64_f1002}/cuPHY-CP/dapp_hook/dapp_tenant_main
[ -x $BIN ] || { log "missing $BIN (./dgist.sh build-l1)"; exit 1; }
MODEL=${VA_MODEL:-26x}; SET=migfhd26${MODEL#26}
NSM=$(python3 -c 'import ctypes; cu=ctypes.CDLL("libcuda.so.1"); cu.cuInit(0); d=ctypes.c_int(); cu.cuDeviceGet(ctypes.byref(d),0); n=ctypes.c_int(); cu.cuDeviceGetAttribute(ctypes.byref(n),16,d); print(n.value)' 2>/dev/null || echo 0)
SM=${BASELINE_YOLO_SM:-auto}; [ "$SM" = auto ] && SM=$NSM
ENG=$M/$SET/seg00_sm$SM.engine
if [ ! -s $ENG ]; then alt=$(ls $M/$SET/seg00_sm*.engine 2>/dev/null | head -1); [ -n "$alt" ] && { log "no engine for $SM SMs, using $(basename $alt)"; ENG=$alt; SM=$(basename $alt | sed -E 's/seg00_sm([0-9]+).engine/\1/'); }; fi
[ -s $ENG ] || { log "missing $M/$SET/seg00_sm$NSM.engine (scripts/build_yolo_engines_mig.sh ${MODEL#26} builds it inside the 3g instance)"; exit 1; }
[ "$SM" = "$NSM" ] || log "warning: engine class $SM differs from the instance SM count $NSM"
SPEC=$MODEL=$M/$SET; LOADED=$MODEL
if [ "${VA_MODEL_SELECT:-0}" = 1 ]; then
  IFS=, read -r -a WT <<< "${VA_MODEL_WEIGHTS:-}"; i=0
  for m in n s m l x; do
    w=${WT[$i]:-0}; w=${w// /}; i=$((i + 1)); [ "26$m" = "$MODEL" ] && continue
    case "$w" in ''|*[!0-9]*) continue ;; esac; [ "$w" -gt 0 ] || continue
    if [ -s $M/migfhd26$m/seg00_sm$SM.engine ]; then SPEC=$SPEC,26$m=$M/migfhd26$m; LOADED="$LOADED 26$m"
    else log "model selection: no engine $M/migfhd26$m/seg00_sm$SM.engine - 26$m is not loaded, its requests run as $MODEL (scripts/build_yolo_engines_mig.sh $m builds it)"; fi
  done
fi
log "CUDA_VISIBLE_DEVICES=${CUDA_VISIBLE_DEVICES:-unset} (privileged: nvidia-smi lists every instance) | $(nvidia-smi -L 2>/dev/null | grep -c MIG) MIG instance(s) on the host"
# no MPS here: one process owns the whole 3g instance
export CUDA_MPS_PIPE_DIRECTORY=/var CUDA_MPS_LOG_DIRECTORY=/var; echo quit | nvidia-cuda-mps-control 2>/dev/null; sleep 1
export DAPP_TENANT=1 DAPP_AGENT=1 DAPP_TENANT_FREERUN=1 DAPP_TENANT_FIXED=$SM DAPP_TENANT_CLASSES=$SM
export DAPP_PUSCH_PROFILES=16 DAPP_PDSCH_PROFILES=0 DAPP_AGENT_QOS=0 DAPP_AGENT_GRAPH_CHUNK=0 DAPP_AGENT_CHUNK_AHEAD=0
export DAPP_INPUT=fhd DAPP_TENANT_MODELS=$SPEC DAPP_AGENT_WARMUP=$M/imgfhd/fhd_0_q85.jpg,$M/imgfhd/fhd_1_q85.jpg
export DAPP_AGENT_PORT=${DAPP_AGENT_PORT:-9810} DAPP_TENANT_CPU=${DAPP_TENANT_CPU:-45} DAPP_AGENT_CPU=${DAPP_AGENT_CPU:-46}
export DAPP_TENANT_LOG=/tmp/dapp_tenant.log DAPP_TENANT_CSV=
rm -f /tmp/dapp_tenant.log
env | grep -E '^(DAPP_|BUILD_DIR|RUN_NAME|BASELINE_|VA_MODEL|NVIDIA_VISIBLE)' | sort > $RUN/yolo_env.txt; chown $OWN $RUN/yolo_env.txt
log "naive YOLO server: $LOADED batch-3 FHD on $SM SMs (MIG 3g instance has $NSM), engine $(basename $ENG), agent TCP $DAPP_AGENT_PORT, no QoS / chunking; the model of a request is the one it names"
( cd $SDK && exec $BIN 86400000 ) > $RUN/yolo_console.log 2>&1 & T=$!
chown $OWN $RUN/yolo_console.log 2>/dev/null
stopping=0
on_term() { [ $stopping = 1 ] && return; stopping=1; log "stop requested"; kill -TERM $T 2>/dev/null; ( sleep 10; kill -KILL $T 2>/dev/null ) & }
trap on_term TERM INT
port_open() { (echo > /dev/tcp/127.0.0.1/$DAPP_AGENT_PORT) 2>/dev/null; }
for i in $(seq 1 240); do port_open && break; grep -qE 'tenant NOT started|FATAL' /tmp/dapp_tenant.log 2>/dev/null && break; kill -0 $T 2>/dev/null || break; sleep 2; done
if port_open; then touch /tmp/dgist_yolo_ready; log "ready: agent on TCP $DAPP_AGENT_PORT | $(grep -E 'CUDA graphs captured|engines' /tmp/dapp_tenant.log 2>/dev/null | tail -1 | cut -c1-100)"
else log "YOLO server did not start: $(grep -E 'tenant NOT started|FATAL|error' /tmp/dapp_tenant.log $RUN/yolo_console.log 2>/dev/null | tail -2 | cut -c1-160 | tr '\n' ';')"; fi
rc=0; while :; do wait $T; r=$?; kill -0 $T 2>/dev/null || { rc=$r; break; }; done
rm -f /tmp/dgist_yolo_ready
cp /tmp/dapp_tenant.log $RUN/yolo_tenant.log 2>/dev/null; chown -R $OWN $RUN
log "YOLO server exited ($rc)"; exit $rc
