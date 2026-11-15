# slot_level_env.sh - sourced by l1_entrypoint.sh and l1_entrypoint_tg.sh inside the l1 container (branch demo-slot-level, 2026-11-15).
# Slot-level SM switching of the YOLO tenant (research of 2026-11-12/13, aerial-cuda-accelerated-ran/cuPHY-CP/dapp_hook/tenant/dapp_tenant.cu):
#   .env DAPP_TENANT_ENGINE_SM=<N> > 0 with DAPP_INPUT=fhd : every model has ONE TensorRT engine, built with all N SMs of the GPU visible
#       (models/fhd<N>_26<m>/seg00_sm<N>.engine, scripts/build_yolo_engines.sh fhd132). It runs on every SM class, so the class can change
#       inside one inference. 0 = the per-class engines and the rule of branch demo (one SM class per inference).
#   .env DAPP_AGENT_PIECE=1 (default when the one-engine sets are on): an inference is a chain of CUDA-graph pieces (DAPP_AGENT_GRAPH_CHUNK nodes
#       each) and the tenant picks the SM class of each piece from the SMs the L1 leaves free in the slots that piece touches. Classes:
#       .env DAPP_TENANT_CLASSES_SL; pieces are queued until DAPP_AGENT_AHEAD_US of work is ahead on the GPU (150 = the 6-cell research runs,
#       tenant/sl_fluct.sh arm "ours"). 0 = one class per inference on the same one-engine sets (research arm "req").
# Needs from the caller: log(), M (models dir), MODE (ours|baseline). Sets ESM (0 = slot-level off), RULE (text for the start banner).
sl_init() {
  ESM=${DAPP_TENANT_ENGINE_SM:-0}; case "$ESM" in ''|*[!0-9]*) ESM=0 ;; esac; RULE="one SM class per inference"
  [ $MODE = ours ] || { ESM=0; return 0; }
  if [ $ESM -gt 0 ] && [ "${DAPP_INPUT:-4k}" != fhd ]; then
    log "warning: DAPP_TENANT_ENGINE_SM=$ESM needs DAPP_INPUT=fhd (there are no 4K one-engine sets) - request-level rule with the per-class engines"; ESM=0
  fi
  [ $ESM -gt 0 ] || return 0
  local m; for m in n s m l x; do
    [ -e $M/fhd${ESM}_26$m/seg00_sm$ESM.engine ] || log "warning: YOLO engine models/fhd${ESM}_26$m/seg00_sm$ESM.engine missing (scripts/build_yolo_engines.sh fhd$ESM)"
  done
}
# after the caller has exported the dApp environment of branch demo (DAPP_TENANT_MODELS / DAPP_TENANT_CLASSES of the input profile)
sl_env() {
  if [ $ESM -gt 0 ]; then
    ESET=fhd${ESM}_26; export DAPP_TENANT_ENGINE_SM=$ESM
    export DAPP_TENANT_MODELS=$(c=""; for m in n s m l x; do printf "%s26%s=%s" "$c" $m $M/$ESET$m; c=,; done)
    local pc=1; [ "${DAPP_AGENT_PIECE:-1}" = 0 ] && pc=0
    if [ $pc = 1 ]; then case "${DAPP_AGENT_GRAPH_CHUNK:-3}" in ''|0|*[!0-9]*)   # the tenant cuts an inference into pieces only with a piece size
      log "warning: DAPP_AGENT_PIECE=1 needs DAPP_AGENT_GRAPH_CHUNK > 0 (now '${DAPP_AGENT_GRAPH_CHUNK:-}') - one SM class per inference on the $ESM-SM engines instead"; pc=0 ;; esac; fi
    if [ $pc = 1 ]; then
      export DAPP_AGENT_PIECE=1 DAPP_AGENT_AHEAD_US=${DAPP_AGENT_AHEAD_US:-150}
      export DAPP_TENANT_CLASSES=${DAPP_TENANT_CLASSES_SL:-8,16,24,32,40,48,56,64,72,80,88,96,104}
      RULE="slot-level: SM class per graph piece, one $ESM-SM engine per model, pieces queued $DAPP_AGENT_AHEAD_US us ahead"
    else
      export DAPP_AGENT_PIECE=0 DAPP_AGENT_AHEAD_US=0; RULE="one SM class per inference on the $ESM-SM engines"
    fi
  else   # exactly the rule of branch demo: the slot-level keys are pinned off whatever .env says
    export DAPP_TENANT_ENGINE_SM=0 DAPP_AGENT_PIECE=0 DAPP_AGENT_AHEAD_US=0
  fi
}
# after the agent port is open: how many models the tenant runs in piece mode (its log names each one)
sl_ready() {
  [ "${DAPP_AGENT_PIECE:-0}" = 1 ] || return 0
  local on off i; for i in 1 2 3 4 5; do grep -q 'piece mode' /tmp/dapp_tenant.log 2>/dev/null && break; sleep 1; done   # the tenant log is written by a side thread
  on=$(grep -c 'piece mode ON' /tmp/dapp_tenant.log 2>/dev/null); off=$(grep -c 'piece mode OFF' /tmp/dapp_tenant.log 2>/dev/null)
  log "slot-level: ${on:-0} model(s) in piece mode (SM class per graph piece), ${off:-0} on the request-level rule"
  [ "${on:-0}" -gt 0 ] && [ "${off:-0}" = 0 ] || log "warning: slot-level is on in .env but not every model runs in piece mode (lines 'piece mode' in /tmp/dapp_tenant.log of the l1 container; after the run in runs/${RUN_NAME:-manual}/dapp_tenant.log)"
}
