#!/bin/bash
# build_yolo_engines.sh [sm ...] - YOLO26 n/s/m/l/x TensorRT engines for the in-process tenant, one per SM class (a TensorRT engine only
#   runs near the SM count its builder saw). Default classes = .env DAPP_TENANT_CLASSES (24,40,48,56,64,72,80).
#   (1) weights yolo26{n,s,m,l,x}.pt (downloaded by ultralytics on first use) -> ONNX, batch 8, 1280x1280, NMS-free head
#       (aerial-cuda-accelerated-ran/cuPHY-CP/dapp_hook/models/export_1280.py, CPU, va_service/yolo_env)
#   (2) trtexec inside a throwaway Aerial container under an MPS active-thread cap that grants exactly <sm> SMs (GH200: floor(pct/100*66 TPC)*2,
#       confirmed by models/mps_sm_probe.py) -> models/full26<m>/seg00_sm<sm>.engine
#   GPU must be idle (./all.sh down): an MPS daemon runs inside the container for the build only. 3-8 min per engine; 5 models x 7 classes ~ 3 h.
set -eu
D=$(cd "$(dirname "$0")/.." && pwd); . $D/.env
A=$D/aerial-cuda-accelerated-ran; M=$A/cuPHY-CP/dapp_hook/models; MODELS=${MODELS:-n s m l x}
CLASSES=${*:-$(echo ${DAPP_TENANT_CLASSES:-24,40,48,56,64,72,80} | tr ',' ' ')}
PY=$D/va_service/yolo_env/bin/python; [ -x $PY ] || { echo "!! va_service/yolo_env missing (scripts/setup_venvs.sh)"; exit 1; }
need=""; for m in $MODELS; do [ -s $M/yolo26${m}_1280_b8_e2e.onnx ] || need="$need yolo26$m"; done
if [ -n "$need" ]; then echo "== ONNX export (CPU, a few minutes per model):$need"; (cd $M && $PY export_1280.py --models $need --batches 8 --nms-free | grep -E 'onnx:|rror' ); fi
apps=$(nvidia-smi --query-compute-apps=pid --format=csv,noheader | grep -c . || true); [ "$apps" = 0 ] || { echo "!! $apps process(es) on the GPU - stop the L1 first (./all.sh down)"; exit 1; }
echo "== engines for SM classes: $CLASSES (models: $MODELS) $(date +%T)"
docker run --rm --privileged --network host --ipc host -e NVIDIA_VISIBLE_DEVICES=all -e NVIDIA_DRIVER_CAPABILITIES=all -v $A:/opt/nvidia/cuBB \
  -w /opt/nvidia/cuBB/cuPHY-CP/dapp_hook/models -e MODELS="$MODELS" -e CLASSES="$CLASSES" --entrypoint bash $AERIAL_IMAGE -c '
  set -u; export CUDA_MPS_PIPE_DIRECTORY=/var CUDA_MPS_LOG_DIRECTORY=/var
  nvidia-cuda-mps-control -d && echo start_server -uid 0 | nvidia-cuda-mps-control
  trap "echo quit | nvidia-cuda-mps-control; sleep 1" EXIT
  own=$(stat -c %u:%g .)
  for sm in $CLASSES; do
    pct=$(( (sm / 2 * 100 + 65) / 66 )); [ $pct -gt 100 ] && pct=100
    cap=""; [ $pct -lt 100 ] && cap="CUDA_MPS_ACTIVE_THREAD_PERCENTAGE=$pct"
    got=$(env $cap python3 mps_sm_probe.py)
    [ "$got" = "$sm" ] || { echo "!! SM $sm: cap $pct % grants $got SMs - skipped"; continue; }
    for m in $MODELS; do out=full26$m/seg00_sm$sm.engine; [ -s $out ] && { echo "have $out"; continue; }; mkdir -p full26$m
      echo "== $out (cap $pct % = $sm SM) $(date +%T)"
      env $cap /usr/src/tensorrt/bin/trtexec --onnx=yolo26${m}_1280_b8_e2e.onnx --fp16 --saveEngine=$out --skipInference 2>&1 | grep -E "Engine built|PASSED|FAILED|rror"
      chown -R $own full26$m
    done
  done'
echo "ENGINES_DONE $(date +%T)"; ls $M/full26*/*.engine | wc -l | sed 's/$/ engines present/'
