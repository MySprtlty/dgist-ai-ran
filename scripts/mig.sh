#!/bin/bash
# scripts/mig.sh on|off|status - GPU 0 MIG partition for the Baseline mode (2026-11-12): 4g.48gb (Aerial, 72 SMs) + 3g.48gb (YOLO, 60 SMs).
#   Needs root (nvidia-smi -mig / mig -cgi): run as  sudo -n scripts/mig.sh on   (the troy account has sudo; the nvidia account does not).
#   on     : refuses while any process uses the GPU; MPS off; -mig 1 (gpu-reset when pending); creates the two instances; writes runs/mig.env
#            (MIG_4G=<uuid> MIG_3G=<uuid>) that docker-compose.baseline.yaml and dgist.sh read
#   off    : destroys the instances, -mig 0 (gpu-reset when pending), removes runs/mig.env -> back to the Ours mode (whole GPU, green contexts)
#   status : mode, instances, uuids
set -u
D=$(cd "$(dirname "$0")/.." && pwd); GPU=${GPU:-0}; ENVF=$D/runs/mig.env
OWN=$(stat -c %u:%g $D)
smi() { nvidia-smi "$@"; }
mode() { smi -i $GPU --query-gpu=mig.mode.current,mig.mode.pending --format=csv,noheader; }
busy() { smi --query-compute-apps=pid,process_name --format=csv,noheader | grep -v nvidia-cuda-mps-server | grep -c . ; }
uuids() { echo "MIG_4G=$(smi -L | grep 'MIG 4g' | grep -o 'MIG-[0-9a-f-]*' | head -1)"; echo "MIG_3G=$(smi -L | grep 'MIG 3g' | grep -o 'MIG-[0-9a-f-]*' | head -1)"; }
status() { echo "-- $(date +%T) GPU $GPU MIG mode: $(mode) | instances: $(smi -L | grep -c 'MIG ') | GPU processes: $(busy)"; smi -L | sed 's/^/   /'; [ -f $ENVF ] && echo "   $ENVF: $(tr '\n' ' ' < $ENVF)"; }
need_root() { [ "$(id -u)" = 0 ] || { echo "!! needs root: sudo -n $0 $1"; exit 1; }; }
stop_mps() { for c in $(docker ps --format '{{.Names}}' | grep -E '^dgist-'); do docker exec -u root $c bash -c 'export CUDA_MPS_PIPE_DIRECTORY=/var; echo quit | nvidia-cuda-mps-control 2>/dev/null' 2>/dev/null; done; pkill -f nvidia-cuda-mps-control 2>/dev/null; sleep 1; }
case "${1:-status}" in
status) status ;;
on)
  need_root on
  docker ps --format '{{.Names}}' | grep -qE '^(dgist-l1|dgist-gnb|dgist-yolo|nv-cubb|oai-gnb.*)$' && { echo "!! an L1/gNB container is running: $(docker ps --format '{{.Names}}' | grep -E 'dgist-|nv-cubb|oai-gnb' | tr '\n' ' ') - ./all.sh down first"; exit 5; }
  stop_mps
  [ "$(busy)" = 0 ] || { echo "!! processes still on the GPU:"; smi --query-compute-apps=pid,process_name,used_memory --format=csv,noheader; exit 5; }
  if smi -L | grep -q 'MIG 4g' && smi -L | grep -q 'MIG 3g'; then echo "-- MIG already partitioned"; else
    smi -i $GPU -mig 1 | tail -1
    m=$(mode); if [ "$m" != "Enabled, Enabled" ]; then echo "-- mode $m -> gpu reset"; smi --gpu-reset -i $GPU | tail -1; m=$(mode); fi
    [ "$m" = "Enabled, Enabled" ] || { echo "!! MIG mode not enabled ($m) -> reverting"; smi -i $GPU -mig 0 | tail -1; exit 6; }
    smi mig -i $GPU -cgi 4g.48gb,3g.48gb -C | tail -3
    smi -L | grep -q 'MIG 4g' && smi -L | grep -q 'MIG 3g' || { echo "!! instance creation failed; profiles:"; smi mig -i $GPU -lgip | head -20; exit 7; }
  fi
  mkdir -p $D/runs; uuids > $ENVF; chown $OWN $ENVF $D/runs 2>/dev/null; chmod 644 $ENVF
  status; echo "== MIG on: $(tr '\n' ' ' < $ENVF)" ;;
off)
  need_root off
  docker ps --format '{{.Names}}' | grep -qE '^(dgist-l1|dgist-gnb|dgist-yolo)$' && { echo "!! dgist containers running - ./all.sh down first"; exit 5; }
  stop_mps
  [ "$(busy)" = 0 ] || { echo "!! processes still on the GPU:"; smi --query-compute-apps=pid,process_name,used_memory --format=csv,noheader; exit 5; }
  if [ "$(mode)" != "Disabled, Disabled" ]; then
    smi mig -i $GPU -dci > /dev/null 2>&1; smi mig -i $GPU -dgi > /dev/null 2>&1
    smi -i $GPU -mig 0 | tail -1
    m=$(mode); if [ "$m" != "Disabled, Disabled" ]; then echo "-- mode $m -> gpu reset"; smi --gpu-reset -i $GPU | tail -1; fi
  fi
  rm -f $ENVF; status; echo "== MIG off: $(mode)" ;;
*) sed -n '2,8p' "$0"; exit 2 ;;
esac
