#!/bin/bash
# _dgist L1 entrypoint WITH the 5-cell traffic generator (2026-11-13; .env TRAFFIC_GEN=1, both modes) - runs as root inside the Aerial 25-3
# container (compose service "l1" of docker-compose[.baseline].yaml + docker-compose.tgen[.baseline].yaml, container dgist-l1).
# One cuphycontroller serves 6 cells:
#   L1 cells 0-4 = RU-emulator cells with testMAC as their L2 (nvIPC prefix nvipc1, loop aerial02 <-> aerial03) = the traffic generator
#   L1 cell  5   = the Benetel cell of the OAI gNB (nvIPC prefix nvipc, aerial00) = the phones, unchanged
#   DEMO_MODE=ours     : our green-context L1 + in-process YOLO tenant + agent, as l1_entrypoint.sh but with the 6-cell profile set
#   DEMO_MODE=baseline : the SAME binary with every dApp feature off (MPS, static mps_sm_*, hook disabled) on the MIG 4g instance;
#                        the naive YOLO server stays the separate container dgist-yolo on the 3g instance
# Order (validated in cuPHY-CP/dapp_hook/sweep/hybrid/hybrid_run.sh): RU emulator -> L1 -> [compose starts the gNB once the L1 is healthy]
# -> testMAC right after the gNB's CONFIG.request for cell 5 (wait_all_cell_config: no slot indication before every cell is configured).
# Load level at run time: scripts/tgen_load.py (./tgen.sh load low|mid|high|auto) writes the control word both tools map.
# Markers for docker's healthcheck: /tmp/dgist_l1_ready, /tmp/dgist_tenant_ready (Ours). Small logs: /opt/dgist/runs/$RUN_NAME/ (= ./runs/<name>/);
# the full nvlog files (several GB per hour) go to /opt/dgist/tglog/$RUN_NAME/ (= .env TRAFFIC_GEN_LOG_DIR/<name>/ on /data), not to the root disk.
# The two validated 1-cell entrypoints (l1_entrypoint.sh, l1_entrypoint_baseline.sh) are not used and not changed by this path.
set -u; umask 022
SDK=/opt/nvidia/cuBB; DG=/opt/dgist; CFG=$SDK/cuPHY-CP/cuphycontroller/config; PHYLOG=/tmp/phy.log
BUILD_DIR=${BUILD_DIR:-build.aarch64_f1002}; MODE=${DEMO_MODE:-ours}; NAME=${RUN_NAME:-manual}; RUN=$DG/runs/$NAME
NC=5; PC=5                                   # emulator cells 0..NC-1, phone cell PC: fixed by the generated config/tgen files
PAT=${TRAFFIC_GEN_PATTERN:-tmp_fluct}; BF=${TRAFFIC_GEN_BLOCK_FRAMES:-1}; BR=${TRAFFIC_GEN_BLOCK_REPEAT:-300}
LOADF=/dev/shm/dgist_tgen_load               # ipc: host -> the same file on the host (dashboard, tgen.sh)
[ "$MODE" = baseline ] && TGC=${TG_BASELINE_L1_CONFIG:-dgist_tgbase} || TGC=${TG_L1_CONFIG:-dgist_tg}
LOGD=$DG/tglog/$NAME; HOSTLOGD=${TRAFFIC_GEN_LOG_DIR:-/data/dgist_tgen/log}/$NAME
OWN=$(stat -c %u:%g $DG/runs); B=$SDK/$BUILD_DIR/cuPHY-CP
BIN=$B/cuphycontroller/examples/cuphycontroller_scf; RUBIN=$B/ru-emulator/ru_emulator/ru_emulator; TMBIN=$B/testMAC/testMAC/test_mac
log() { local m="[dgist-l1 tgen $(date +%T)] $*"; echo "$m"; echo "$m" >> $RUN/entrypoint.log 2>/dev/null; }
rm -f /tmp/dgist_l1_ready /tmp/dgist_tenant_ready; mkdir -p $RUN $LOGD; chown $OWN $RUN $LOGD; : > $RUN/entrypoint.log; chown $OWN $RUN/entrypoint.log
case $MODE in ours|baseline) ;; *) log "unknown DEMO_MODE '$MODE'"; exit 1 ;; esac

# 0. checks
for b in $BIN $RUBIN $TMBIN; do [ -x $b ] || { log "missing $b - on the host: ./dgist.sh build-l1 (ru_emulator and test_mac are part of the build)"; exit 1; }; done
for kv in SCF_FAPI_10_04:BOOL=OFF ENABLE_CONFORMANCE_TM_PDSCH_PDCCH:BOOL=OFF SCF_FAPI_10_04_SRS:BOOL=ON ENABLE_DAPP_HOOK:BOOL=ON; do
  grep -q "^$kv\$" $SDK/$BUILD_DIR/CMakeCache.txt || log "warning: $BUILD_DIR/CMakeCache.txt lacks $kv (the validated FAPI wire layout + dApp hook)"
done
LP=$SDK/testVectors/multi-cell/launch_pattern_F08_${NC}C_$PAT.yaml
[ -f $LP ] || { log "missing $LP (.env TRAFFIC_GEN_TV_DIR must hold the test vectors of pattern $PAT and multi-cell/)"; exit 1; }
[ -f $SDK/testVectors/cuPhyChEstCoeffs.h5 ] || { log "missing testVectors/cuPhyChEstCoeffs.h5 in TRAFFIC_GEN_TV_DIR"; exit 1; }
T=$DG/config/tgen
for f in cuphycontroller_$TGC.yaml l2_adapter_$TGC.yaml nvipc_multi_dgist_tg.yaml ru_dgist_tg.yaml testmac_dgist_tg.yaml; do [ -f $T/$f ] || { log "missing config/tgen/$f"; exit 1; }; done
M=$SDK/cuPHY-CP/dapp_hook/models; ESET=full26; [ "${DAPP_INPUT:-4k}" = fhd ] && ESET=fhd26
if [ $MODE = ours ]; then for m in n s m l x; do [ -e $M/$ESET$m/seg00_sm80.engine ] || log "warning: YOLO engine models/$ESET$m/seg00_sm80.engine missing (scripts/build_yolo_engines.sh ${DAPP_INPUT:-4k})"; done; fi

# 1. MPS: off for the green contexts (Ours), on for the static split inside the MIG 4g instance (Baseline, as the researchers' recipe)
export CUDA_MPS_PIPE_DIRECTORY=/var CUDA_MPS_LOG_DIRECTORY=/var
echo quit | nvidia-cuda-mps-control 2>/dev/null; sleep 1
if [ $MODE = ours ]; then
  pgrep -f "nvidia-cuda-mps-serve[r]" > /dev/null && { log "an MPS server is still running - stop it first"; exit 1; }
else
  log "CUDA_VISIBLE_DEVICES=${CUDA_VISIBLE_DEVICES:-unset} (privileged: nvidia-smi lists every instance) | $(nvidia-smi -L 2>/dev/null | grep -c MIG) MIG instance(s) on the host"
  nvidia-cuda-mps-control -d && echo start_server -uid 0 | nvidia-cuda-mps-control || log "warning: MPS daemon did not start (the Baseline L1 runs with MPS)"
fi

# 2. the generated yamls under their own names (no file of the SDK's own configuration is touched)
cp $T/cuphycontroller_$TGC.yaml $T/l2_adapter_$TGC.yaml $T/nvipc_multi_dgist_tg.yaml $CFG/ || { log "copy into $CFG failed"; exit 1; }
cp $T/ru_dgist_tg.yaml $SDK/cuPHY-CP/ru-emulator/config/ && cp $T/testmac_dgist_tg.yaml $SDK/cuPHY-CP/testMAC/testMAC/ || { log "copy of the RU-emulator / testMAC yaml failed"; exit 1; }
chown $OWN $CFG/cuphycontroller_$TGC.yaml $CFG/l2_adapter_$TGC.yaml $CFG/nvipc_multi_dgist_tg.yaml $SDK/cuPHY-CP/ru-emulator/config/ru_dgist_tg.yaml $SDK/cuPHY-CP/testMAC/testMAC/testmac_dgist_tg.yaml

# 3. logs: nvlog files of the three programs on /data (AERIAL_LOG_PATH), reachable under the usual /tmp names; a link to the host path in the run dir
export AERIAL_LOG_PATH=$LOGD
for f in phy.log ru.log testmac.log dapp_tenant_slots.csv; do : > $LOGD/$f; chown $OWN $LOGD/$f; ln -sfn $LOGD/$f /tmp/$f; done
ln -sfn $HOSTLOGD $RUN/tglog
stopping=0; trap 'stopping=1' TERM INT
nap() { local i; for i in $(seq 1 $1); do [ $stopping = 1 ] && return 1; sleep 1; done; return 0; }
gone() { local p=$1 n=$2 i; for i in $(seq 1 $n); do kill -0 $p 2>/dev/null || return 0; sleep 1; done; return 1; }

# 4. load switch (control word mapped by both tools) + the RU emulator
python3 $DG/scripts/tgen_load.py --file $LOADF init ${TRAFFIC_LOAD:-low} > /tmp/tgen_load.init 2>&1 || { log "load switch init failed: $(cat /tmp/tgen_load.init)"; exit 1; }
chmod 644 $LOADF
TGENV="RU_LP_BLOCK_FRAMES=$BF RU_LP_BLOCK_REPEAT=$BR TESTMAC_LP_BLOCK_FRAMES=$BF TESTMAC_LP_BLOCK_REPEAT=$BR RU_LP_BLOCK_CTL=$LOADF TESTMAC_LP_BLOCK_CTL=$LOADF"
log "traffic generator: $NC emulator cells, pattern $PAT (one load level per frame, auto = a new level every $((BF * BR * 10)) ms), $(cat /tmp/tgen_load.init)"
( cd $SDK && exec env $TGENV $RUBIN F08 ${NC}C $PAT --config ru_dgist_tg.yaml ) > $RUN/ru_console.log 2>&1 & RU=$!
chown $OWN $RUN/ru_console.log 2>/dev/null
nap 25 || { log "stop requested while the RU emulator was starting"; kill -TERM $RU 2>/dev/null; gone $RU 20 || kill -KILL $RU 2>/dev/null; chown -R -h $OWN $RUN $LOGD 2>/dev/null; exit 0; }
kill -0 $RU 2>/dev/null || { log "RU emulator exited during start: $(grep -E ' (ERR|FATAL) |rror' $RUN/ru_console.log | tail -2 | cut -c1-200 | tr '\n' ';')"; exit 1; }

# 5. the L1 environment
[ -n "${PRACH_FORCE_THR0:-}" ] && export CUPHY_PRACH_FORCE_THR0=$PRACH_FORCE_THR0
if [ $MODE = ours ]; then
  # dApp environment as l1_entrypoint.sh, with the 6-cell values of the research runs (tenant/sl_fluct.sh): 10 PUSCH + 2 PDSCH profiles,
  # no idle-slot rule (testMAC sends a TTI request for every slot)
  export DAPP_TENANT=1 DAPP_AGENT=1 DAPP_AGENT_CELLS=$((NC + 1))
  export DAPP_PUSCH_PROFILES=${TG_PUSCH_PROFILES:-16,24,32,40,48,56,64,72,88,104} DAPP_PDSCH_PROFILES=${TG_PDSCH_PROFILES:-24,40}
  export DAPP_SIZER_MODEL=$SDK/cuPHY-CP/dapp_hook/estimator/sm_model.txt
  export DAPP_TENANT_CLASSES=${DAPP_TENANT_CLASSES:-24,40,48,56,64,72,80} DAPP_TENANT_FB_GUARD=0 DAPP_TENANT_LAYOUT=union
  export DAPP_TENANT_EMPTY_US=${TG_TENANT_EMPTY_US:--1}
  export DAPP_TENANT_CPU=${DAPP_TENANT_CPU:-45} DAPP_AGENT_CPU=${DAPP_AGENT_CPU:-46} DAPP_AGENT_PORT=${DAPP_AGENT_PORT:-9810}
  export DAPP_AGENT_GRAPH_CHUNK=${DAPP_AGENT_GRAPH_CHUNK:-3} DAPP_AGENT_CHUNK_AHEAD=${DAPP_AGENT_CHUNK_AHEAD:-2} DAPP_AGENT_QOS=${DAPP_AGENT_QOS:-1}
  export DAPP_TENANT_LOG=/tmp/dapp_tenant.log
  export DAPP_TENANT_CSV=$([ "${DAPP_SLOT_CSV:-1}" = 1 ] && echo /tmp/dapp_tenant_slots.csv)
  export DAPP_INPUT=${DAPP_INPUT:-4k}
  if [ "$DAPP_INPUT" = fhd ]; then
    export DAPP_TENANT_MODELS=$(c=""; for m in n s m l x; do printf "%s26%s=%s" "$c" $m $M/fhd26$m; c=,; done)
    export DAPP_AGENT_WARMUP=$M/imgfhd/fhd_0_q85.jpg,$M/imgfhd/fhd_1_q85.jpg
    export DAPP_TENANT_CLASSES=${DAPP_TENANT_CLASSES_FHD:-24,40,48,56,64,72,80,88}
  fi
  [ -n "${TG_TENANT_CLASSES:-}" ] && export DAPP_TENANT_CLASSES=$TG_TENANT_CLASSES
  [ "${DAPP_SLOT_CSV:-1}" = 1 ] && ln -sfn $HOSTLOGD/dapp_tenant_slots.csv $RUN/dapp_tenant_slots.csv
  WHAT="green contexts | PUSCH profiles $DAPP_PUSCH_PROFILES | PDSCH $DAPP_PDSCH_PROFILES | dApp classes $DAPP_TENANT_CLASSES | input $DAPP_INPUT ($ESET) | agent TCP $DAPP_AGENT_PORT"
else
  # Baseline: no dApp feature may reach the L1 (.env passes DAPP_* into the container). CUDA_DEVICE_MAX_CONNECTIONS = 8 = the image default
  # = the researchers' recipe (l1_entrypoint_baseline.sh) = the research MIG arm; it MUST be set: cuphydriver context.cpp:412-413 formats the
  # getenv() result without a null check and the L1 dies with SIGSEGV when the variable is missing (measured 2026-11-13, run tg_base_dbg1).
  # The mps_sm_* values of config/tgen/cuphycontroller_dgist_tgbase.yaml are those of the research MIG arm (fullstack_run.sh MIG_SM=1).
  for v in $(compgen -e | grep '^DAPP_'); do unset $v; done
  export CUDA_DEVICE_MAX_CONNECTIONS=8
  WHAT="MPS static split on the MIG 4g instance | dApp hook off | no YOLO in this process"
fi
env | grep -E '^(DAPP_|CUPHY_|CUDA_|AERIAL_|NVIDIA_VISIBLE|BUILD_DIR|RUN_NAME|DEMO_MODE|TRAFFIC_|TG_)' | sort > $RUN/l1_env.txt
echo "TGEN: $TGENV | L1 config $TGC | full logs $HOSTLOGD" >> $RUN/l1_env.txt

# 6. the L1 (the process renames itself phy_main)
log "cuphycontroller_scf $TGC ($MODE, 6 cells) | $BUILD_DIR | $WHAT${CUPHY_PRACH_FORCE_THR0:+ | CUPHY_PRACH_FORCE_THR0=$CUPHY_PRACH_FORCE_THR0}"
# debug only: TG_L1_GDB=1 ./dgist.sh up <name> runs the L1 under gdb -batch, so a crash leaves its backtrace at the end of l1_console.log
WRAP=(); [ "${TG_L1_GDB:-0}" = 1 ] && { WRAP=(gdb -q -batch -ex "handle SIG32 SIG33 SIG34 SIGUSR1 SIGUSR2 SIGPIPE nostop noprint pass" -ex run -ex "bt 40" --args); log "debug: the L1 runs under gdb (TG_L1_GDB=1) - not for measurements"; }
( cd $SDK && exec "${WRAP[@]}" $BIN $TGC ) > $RUN/l1_console.log 2>&1 & L1=$!
chown $OWN $RUN/l1_console.log $RUN/l1_env.txt 2>/dev/null
TM=""
l1_ready() { grep -q 'L1 is ready' $RUN/l1_console.log $PHYLOG 2>/dev/null; }
port_open() { (echo > /dev/tcp/127.0.0.1/${DAPP_AGENT_PORT:-9810}) 2>/dev/null; }
for i in $(seq 1 300); do l1_ready && break; kill -0 $L1 2>/dev/null || break; nap 1 || break; done
if l1_ready; then touch /tmp/dgist_l1_ready; log "L1 is ready (6 cells)"
else log "L1 did not come up: $(grep -E 'FATAL|ERR' $RUN/l1_console.log | tail -2 | cut -c1-200 | tr '\n' ';')"; fi
if [ $MODE = ours ] && l1_ready; then
  for i in $(seq 1 480); do port_open && break; grep -qE 'tenant NOT started|FATAL' /tmp/dapp_tenant.log 2>/dev/null && break; kill -0 $L1 2>/dev/null || break; nap 1 || break; done
  if port_open; then touch /tmp/dgist_tenant_ready; log "dApp tenant ready: agent on TCP $DAPP_AGENT_PORT | $(grep -E 'CUDA graphs captured|green contexts' /tmp/dapp_tenant.log 2>/dev/null | tail -1 | cut -c1-120)"
  else log "tenant did not start: $(grep -E 'tenant NOT started|FATAL|error' /tmp/dapp_tenant.log 2>/dev/null | tail -2 | cut -c1-200 | tr '\n' ';')"; fi
fi

# 7. main loop: start testMAC once the gNB has configured cell PC; leave when the L1 exits or a stop is requested
ru_dead=0; tm_dead=0
while [ $stopping = 0 ] && kill -0 $L1 2>/dev/null; do
  if [ -z "$TM" ] && grep -q "CONFIG.req received for cell_id=$PC " $RUN/l1_console.log $PHYLOG 2>/dev/null; then
    sleep 1
    ( cd $SDK && exec env $TGENV stdbuf --output=L $TMBIN F08 ${NC}C $PAT --config testmac_dgist_tg.yaml ) > $RUN/testmac_console.log 2>&1 & TM=$!
    chown $OWN $RUN/testmac_console.log 2>/dev/null
    log "gNB configured cell $PC -> testMAC started for emulator cells 0-$((NC - 1)) (load: $(python3 $DG/scripts/tgen_load.py --file $LOADF show 2>&1))"
  fi
  [ -n "$TM" ] && [ $tm_dead = 0 ] && ! kill -0 $TM 2>/dev/null && { tm_dead=1; log "!! testMAC exited - the emulator cells carry no traffic any more: $(grep -E ' (ERR|FATAL) ' $RUN/testmac_console.log | tail -2 | cut -c1-200 | tr '\n' ';')"; }
  [ $ru_dead = 0 ] && ! kill -0 $RU 2>/dev/null && { ru_dead=1; log "!! RU emulator exited: $(grep -E ' (ERR|FATAL) ' $RUN/ru_console.log | tail -2 | cut -c1-200 | tr '\n' ';')"; }
  sleep 1
done

# 8. stop: testMAC -> L1 -> RU emulator (the order of hybrid_run.sh; compose has already stopped the gNB on a normal down)
[ $stopping = 1 ] && log "stop requested" || log "the L1 exited by itself"
rm -f /tmp/dgist_l1_ready /tmp/dgist_tenant_ready
if [ -n "$TM" ] && kill -0 $TM 2>/dev/null; then kill -INT $TM; gone $TM 20 || { log "testMAC still alive after 20 s - SIGKILL"; kill -KILL $TM; }; fi
if kill -0 $L1 2>/dev/null; then kill -TERM $L1; gone $L1 15 || { log "L1 still alive after 15 s - SIGKILL"; kill -KILL $L1; }; fi
wait $L1; rc=$?
if kill -0 $RU 2>/dev/null; then kill -TERM $RU; gone $RU 20 || { log "RU emulator still alive after 20 s - SIGKILL"; kill -KILL $RU; }; fi
[ $MODE = baseline ] && echo quit | nvidia-cuda-mps-control 2>/dev/null

# 9. small per-run files from the console logs (CON/WRN/ERR/FATAL lines and the per-second lines are all in them; no pass over the full nvlog files)
C=$RUN/l1_console.log
grep -E ' (WRN|ERR|FATAL) |Send Err.ind|recovery|not responsive|Order kernel timeout|exit handler|pipeline failed|CONFIG.req received|START.req|STOP.req|L1 is ready|dApp sizer' $C > $RUN/phy_warn.log 2>/dev/null
grep -E "\] Cell +$PC \| DL" $C > $RUN/l1_persec.log 2>/dev/null
grep -E '\] Cell +[0-9]+ \| DL' $C | grep -v -E "\] Cell +$PC \| DL" > $RUN/l1_persec_emu.log 2>/dev/null
grep -E '\[RU\] \| ' $RUN/ru_console.log > $RUN/ru_table.txt 2>/dev/null
grep -c 'Payload Validation ERROR' $RUN/ru_console.log > $RUN/ru_payload_errors.txt 2>/dev/null
[ $MODE = ours ] && cp /tmp/dapp_tenant.log $RUN/ 2>/dev/null
chown -R -h $OWN $RUN $LOGD 2>/dev/null
late() { grep -E "\| $1 +\|" $RUN/ru_table.txt 2>/dev/null | tail -1 | cut -d'|' -f3- | tr -s ' ' | tr -d '\n'; }
log "logs: $RUN | L1 Err.ind $(grep -c 'Send Err.ind' $RUN/phy_warn.log 2>/dev/null), FATAL $(grep -c FATAL $RUN/phy_warn.log 2>/dev/null) | RU emulator DL U late slots per cell:$(late 'DL U LATE SLOTS') DL C late slots:$(late 'DL C LATE SLOTS') payload errors $(cat $RUN/ru_payload_errors.txt 2>/dev/null)"
log "full nvlog files: $HOSTLOGD ($(du -sh $LOGD 2>/dev/null | cut -f1))"
log "L1 exited ($rc)"; exit $rc
