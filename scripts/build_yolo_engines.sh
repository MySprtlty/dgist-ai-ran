#!/bin/bash
# build_yolo_engines.sh [4k|fhd] [sm ...] - YOLO26 n/s/m/l/x TensorRT engines for the in-process tenant, one per SM class (a TensorRT engine
#   only runs near the SM count its builder saw). Default profile = .env DAPP_INPUT (4k), default classes = .env DAPP_TENANT_CLASSES (24,...,80).
#   profile 4k : ONNX batch 8, 1280x1280, NMS-free head -> models/full26<m>/seg00_sm<sm>.engine  (4K frame = 6 native 1280 crops + 2 whole views)
#   profile fhd: ONNX batch 3, same network           -> models/fhd26<m>/seg00_sm<sm>.engine   (FHD frame = 2 native 1280x1080 crops + 1 whole view)
#   (1) weights yolo26{n,s,m,l,x}.pt (downloaded by ultralytics on first use) -> ONNX (aerial-cuda-accelerated-ran/cuPHY-CP/dapp_hook/models/export_1280.py,
#       CPU, va_service/yolo_env - needs onnx + onnxslim from va_service/requirements.txt)
#   (2) trtexec inside a throwaway Aerial container under an MPS active-thread cap that grants exactly <sm> SMs (GH200: floor(pct/100*66 TPC)*2,
#       confirmed by models/mps_sm_probe.py)
#   GPU must be idle (./all.sh down): an MPS daemon runs inside the container (as root: pipe dir /var) for the build only. 3-8 min per engine; 5 models x 7 classes ~ 2-3 h.
#   fhd classes >= 80 of models s and l, and 88 of model x (2026-11-10/11): built with the timing cache of a fresh sm72 build of the same model (models/fhd26<m>/sm72.timing.cache,
#   made here when missing). With TensorRT 10.3 the sm80 tactics of those two engines cannot run in one process next to another class of the same
#   model ("Cask Gemm execution" at the first enqueue, reproduced with models/trt_pair.py); the sm72 tactics at 80 SMs cost 26s 3.6 / 26l 7.9 ms (same as native).
#   26x at 88 with native tactics hit the same "Cask Gemm execution" next to its other classes (2026-11-11 18:38), hence the cache for x >= 88 too.
#   class 88 (2026-11-11) = the (P132,N24) gap of the union layout: D slots with no UL work within ul_back 4 slots (DDDDDDSUUU slots 4-6); the
#   tenant uses it only when the engine exists, so build it with `build_yolo_engines.sh fhd 88` and then add 88 to .env DAPP_TENANT_CLASSES_FHD.
set -eu
D=$(cd "$(dirname "$0")/.." && pwd); . $D/.env
A=$D/aerial-cuda-accelerated-ran; M=$A/cuPHY-CP/dapp_hook/models; MODELS=${MODELS:-n s m l x}
INPUT=${DAPP_INPUT:-4k}; case "${1:-}" in 4k|fhd) INPUT=$1; shift ;; esac
case $INPUT in fhd) B=3; SET=fhd26 ;; 4k) B=8; SET=full26 ;; *) echo "!! unknown profile '$INPUT' (4k|fhd)"; exit 2 ;; esac
CLASSES=${*:-$(echo ${DAPP_TENANT_CLASSES:-24,40,48,56,64,72,80} | tr ',' ' ')}
PY=$D/va_service/yolo_env/bin/python; [ -x $PY ] || { echo "!! va_service/yolo_env missing (scripts/setup_venvs.sh)"; exit 1; }
need=""; for m in $MODELS; do [ -s $M/yolo26${m}_1280_b${B}_e2e.onnx ] || need="$need yolo26$m"; done
if [ -n "$need" ]; then echo "== ONNX export, batch $B (CPU, a few minutes per model):$need"; (cd $M && $PY export_1280.py --models $need --batches $B --nms-free | grep -E 'onnx:|rror' ); fi
apps=$(nvidia-smi --query-compute-apps=pid --format=csv,noheader | grep -c . || true); [ "$apps" = 0 ] || { echo "!! $apps process(es) on the GPU - stop the L1 first (./all.sh down)"; exit 1; }
echo "== profile $INPUT (batch $B -> models/$SET<m>): engines for SM classes $CLASSES (models: $MODELS) $(date +%T)"
docker run --rm -u root --privileged --network host --ipc host -e NVIDIA_VISIBLE_DEVICES=all -e NVIDIA_DRIVER_CAPABILITIES=all -v $A:/opt/nvidia/cuBB \
  -w /opt/nvidia/cuBB/cuPHY-CP/dapp_hook/models -e MODELS="$MODELS" -e CLASSES="$CLASSES" -e B=$B -e SET=$SET --entrypoint bash $AERIAL_IMAGE -c '
  set -u; export CUDA_MPS_PIPE_DIRECTORY=/var CUDA_MPS_LOG_DIRECTORY=/var
  nvidia-cuda-mps-control -d && echo start_server -uid 0 | nvidia-cuda-mps-control
  trap "echo quit | nvidia-cuda-mps-control; sleep 1" EXIT
  own=$(stat -c %u:%g .)
  for sm in $CLASSES; do
    pct=$(( (sm / 2 * 100 + 65) / 66 )); [ $pct -gt 100 ] && pct=100
    cap=""; [ $pct -lt 100 ] && cap="CUDA_MPS_ACTIVE_THREAD_PERCENTAGE=$pct"
    got=$(env $cap python3 mps_sm_probe.py)
    [ "$got" = "$sm" ] || { echo "!! SM $sm: cap $pct % grants $got SMs - skipped"; continue; }
    for m in $MODELS; do out=$SET$m/seg00_sm$sm.engine; [ -s $out ] && { echo "have $out"; continue; }; mkdir -p $SET$m
      tc=""
      if [ $SET = fhd26 ] && { { [ $sm -ge 80 ] && { [ $m = s ] || [ $m = l ]; }; } || { [ $sm -ge 88 ] && [ $m = x ]; }; }; then   # sm72 tactics: fhd s/l >= 80, x >= 88 (see the header)
        cache=$SET$m/sm72.timing.cache
        if [ ! -s $cache ]; then pct72=$(( (72 / 2 * 100 + 65) / 66 )); got72=$(env CUDA_MPS_ACTIVE_THREAD_PERCENTAGE=$pct72 python3 mps_sm_probe.py)
          [ "$got72" = 72 ] || { echo "!! $out: sm72 cap $pct72 % grants $got72 SMs - cannot make the timing cache, skipped"; continue; }
          echo "== $cache (fresh sm72 build of yolo26$m for the tactics) $(date +%T)"
          env CUDA_MPS_ACTIVE_THREAD_PERCENTAGE=$pct72 /usr/src/tensorrt/bin/trtexec --onnx=yolo26${m}_1280_b${B}_e2e.onnx --fp16 --saveEngine=/tmp/tc72_$m.engine --skipInference --timingCacheFile=$cache 2>&1 | grep -E "Engine built|PASSED|FAILED|rror"
          rm -f /tmp/tc72_$m.engine; fi
        tc="--timingCacheFile=$cache"
      fi
      echo "== $out (cap $pct % = $sm SM${tc:+, sm72 timing cache}) $(date +%T)"
      env $cap /usr/src/tensorrt/bin/trtexec --onnx=yolo26${m}_1280_b${B}_e2e.onnx --fp16 --saveEngine=$out --skipInference $tc 2>&1 | grep -E "Engine built|PASSED|FAILED|rror"
      chown -R $own $SET$m
    done
  done'
echo "ENGINES_DONE $INPUT $(date +%T)"; ls $M/$SET*/*.engine | wc -l | sed "s/\$/ engines present in models\/$SET*/"
