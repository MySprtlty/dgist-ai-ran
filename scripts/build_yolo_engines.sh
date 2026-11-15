#!/bin/bash
# build_yolo_engines.sh [4k|fhd|fhd<N>] [sm ...] - YOLO26 n/s/m/l/x TensorRT engines for the in-process tenant, one per SM class (a TensorRT engine
#   only runs near the SM count its builder saw). Default profile = .env DAPP_INPUT (4k); default classes = the list the L1 loads for that profile:
#   .env DAPP_TENANT_CLASSES for 4k (24,...,80), .env DAPP_TENANT_CLASSES_FHD for fhd (24,...,88) - the list ./dgist.sh check looks for.
#   profile 4k : ONNX batch 8, 1280x1280, NMS-free head -> models/full26<m>/seg00_sm<sm>.engine  (4K frame = 6 native 1280 crops + 2 whole views)
#   profile fhd: ONNX batch 3, same network           -> models/fhd26<m>/seg00_sm<sm>.engine   (FHD frame = 2 native 1280x1080 crops + 1 whole view)
#   profile fhd<N> (branch demo-slot-level; fhd132 on the GH200): the ONE-ENGINE sets of the slot-level tenant -> models/fhd<N>_26<m>/seg00_sm<N>.engine,
#       one engine per model, same batch-3 ONNX, built with all N SMs of the GPU visible: no MPS daemon, no cap (the recipe of the research
#       engines of 2026-11-12). The tenant deserialises it once in the primary context and runs it on every SM class, so the class can change
#       inside one inference (.env DAPP_TENANT_ENGINE_SM=<N>, scripts/slot_level_env.sh). N must be the SM count of the GPU; class arguments are
#       ignored. 5 engines, 3-8 min each. With DAPP_TENANT_ENGINE_SM > 0 the L1 loads only these; the per-class fhd set is needed only for
#       DAPP_TENANT_ENGINE_SM=0.
#   (1) weights yolo26{n,s,m,l,x}.pt (downloaded by ultralytics on first use) -> ONNX (aerial-cuda-accelerated-ran/cuPHY-CP/dapp_hook/models/export_1280.py,
#       CPU, va_service/yolo_env - needs onnx + onnxslim from va_service/requirements.txt)
#   (2) trtexec inside a throwaway Aerial container under an MPS active-thread cap that grants exactly <sm> SMs (GH200: floor(pct/100*66 TPC)*2,
#       confirmed by models/mps_sm_probe.py)
#   GPU must be idle (./all.sh down): an MPS daemon runs inside the container (as root: pipe dir /var) for the build only. 3-8 min per engine; 5 models x 7 classes ~ 2-3 h (fhd: 8 classes).
#   fhd classes >= 80 of models s and l, and 88 of model x (2026-11-10/11): built with the timing cache of a fresh sm72 build of the same model (models/fhd26<m>/sm72.timing.cache,
#   made here when missing). With TensorRT 10.3 the sm80 tactics of those two engines cannot run in one process next to another class of the same
#   model ("Cask Gemm execution" at the first enqueue, reproduced with models/trt_pair.py); the sm72 tactics at 80 SMs cost 26s 3.6 / 26l 7.9 ms (same as native).
#   26x at 88 with native tactics hit the same "Cask Gemm execution" next to its other classes (2026-11-11 18:38), hence the cache for x >= 88 too.
#   class 88 (2026-11-11) = the (P132,N24) gap of the union layout: D slots with no UL work within ul_back 4 slots (DDDDDDSUUU slots 4-6); the
#   tenant uses it only when the engine exists. 88 is in the tracked .env DAPP_TENANT_CLASSES_FHD, so the default fhd build makes it
#   (./dgist.sh check asks for the last class of that list).
set -eu
D=$(cd "$(dirname "$0")/.." && pwd); . $D/.env
A=$D/aerial-cuda-accelerated-ran; M=$A/cuPHY-CP/dapp_hook/models; MODELS=${MODELS:-n s m l x}
INPUT=${DAPP_INPUT:-4k}; case "${1:-}" in 4k|fhd|fhd[1-9]*) INPUT=$1; shift ;; esac
ONE=0   # > 0 = one-engine profile fhd<N>
case $INPUT in fhd) B=3; SET=fhd26 ;; 4k) B=8; SET=full26 ;;
  fhd[1-9]*) ONE=${INPUT#fhd}; case $ONE in *[!0-9]*) echo "!! unknown profile '$INPUT' (4k|fhd|fhd<N>)"; exit 2 ;; esac; B=3; SET=fhd${ONE}_26 ;;
  *) echo "!! unknown profile '$INPUT' (4k|fhd|fhd<N>)"; exit 2 ;; esac
DEF=${DAPP_TENANT_CLASSES:-24,40,48,56,64,72,80}; [ $INPUT = fhd ] && DEF=${DAPP_TENANT_CLASSES_FHD:-24,40,48,56,64,72,80,88}   # the class list of the profile (as scripts/l1_entrypoint.sh)
CLASSES=${*:-$(echo $DEF | tr ',' ' ')}; [ $ONE -gt 0 ] && CLASSES=$ONE
PY=$D/va_service/yolo_env/bin/python; [ -x $PY ] || { echo "!! va_service/yolo_env missing (scripts/setup_venvs.sh)"; exit 1; }
need=""; for m in $MODELS; do [ -s $M/yolo26${m}_1280_b${B}_e2e.onnx ] || need="$need yolo26$m"; done
if [ -n "$need" ]; then echo "== ONNX export, batch $B (CPU, a few minutes per model):$need"; (cd $M && $PY export_1280.py --models $need --batches $B --nms-free | grep -E 'onnx:|rror' ); fi
apps=$(nvidia-smi --query-compute-apps=pid --format=csv,noheader | grep -c . || true); [ "$apps" = 0 ] || { echo "!! $apps process(es) on the GPU - stop the L1 first (./all.sh down)"; exit 1; }
echo "== profile $INPUT (batch $B -> models/$SET<m>): engines for SM classes $CLASSES (models: $MODELS) $(date +%T)"
docker run --rm -u root --privileged --network host --ipc host -e NVIDIA_VISIBLE_DEVICES=all -e NVIDIA_DRIVER_CAPABILITIES=all -v $A:/opt/nvidia/cuBB \
  -w /opt/nvidia/cuBB/cuPHY-CP/dapp_hook/models -e MODELS="$MODELS" -e CLASSES="$CLASSES" -e B=$B -e SET=$SET -e ONE=$ONE --entrypoint bash $AERIAL_IMAGE -c '
  set -u
  if [ $ONE -gt 0 ]; then   # one-engine sets: the whole GPU, no MPS daemon, no cap
    own=$(stat -c %u:%g .)
    got=$(python3 -c "import ctypes; cu=ctypes.CDLL(\"libcuda.so.1\"); cu.cuInit(0); d=ctypes.c_int(); cu.cuDeviceGet(ctypes.byref(d),0); n=ctypes.c_int(); cu.cuDeviceGetAttribute(ctypes.byref(n),16,d); print(n.value)" 2>/dev/null || echo 0)
    [ "$got" = "$ONE" ] || { echo "!! this GPU shows $got SMs, not $ONE - the profile is fhd<SM count of the GPU> (and .env DAPP_TENANT_ENGINE_SM the same number)"; exit 1; }
    for m in $MODELS; do out=$SET$m/seg00_sm$ONE.engine; [ -s $out ] && { echo "have $out"; continue; }; mkdir -p $SET$m
      echo "== $out (all $ONE SMs, no MPS cap) $(date +%T)"
      /usr/src/tensorrt/bin/trtexec --onnx=yolo26${m}_1280_b${B}_e2e.onnx --fp16 --saveEngine=$out --skipInference 2>&1 | grep -E "Engine built|PASSED|FAILED|rror"
      chown -R $own $SET$m
    done
    exit 0
  fi
  export CUDA_MPS_PIPE_DIRECTORY=/var CUDA_MPS_LOG_DIRECTORY=/var
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
