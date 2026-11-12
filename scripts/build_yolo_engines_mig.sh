#!/bin/bash
# scripts/build_yolo_engines_mig.sh [x|n|s|m|l ...] - Baseline engines (2026-11-12): the batch-3 FHD YOLO26 engine built INSIDE the MIG 3g instance
#   (60 SMs, no MPS) -> aerial-cuda-accelerated-ran/cuPHY-CP/dapp_hook/models/migfhd26<m>/seg00_sm60.engine, loaded by the naive Baseline YOLO
#   server (scripts/yolo_entrypoint_baseline.sh). A TensorRT engine only runs well near the SM count its builder saw, hence the build in the slice.
#   Needs: sudo -n scripts/mig.sh on (runs/mig.env), the batch-3 ONNX models/yolo26<m>_1280_b3_e2e.onnx (scripts/build_yolo_engines.sh fhd makes it),
#   and no other process on the 3g instance. ~7-15 min for 26x. Default model: .env VA_MODEL (26x).
#   (not --privileged and CUDA_VISIBLE_DEVICES pinned: a privileged container sees every MIG instance and CUDA device 0 would be the 4g one)
set -eu
D=$(cd "$(dirname "$0")/.." && pwd); . $D/.env; [ -f $D/runs/mig.env ] && . $D/runs/mig.env
A=$D/aerial-cuda-accelerated-ran; M=$A/cuPHY-CP/dapp_hook/models; SM=${BASELINE_YOLO_SM:-auto}
[ -n "${MIG_3G:-}" ] || { echo "!! runs/mig.env has no MIG_3G (sudo -n scripts/mig.sh on)"; exit 1; }
nvidia-smi -L | grep -q "$MIG_3G" || { echo "!! MIG instance $MIG_3G not present (scripts/mig.sh status)"; exit 1; }
MODELS=${*:-${VA_MODEL#26}}
for m in $MODELS; do [ -s $M/yolo26${m}_1280_b3_e2e.onnx ] || { echo "!! $M/yolo26${m}_1280_b3_e2e.onnx missing (scripts/build_yolo_engines.sh fhd exports it)"; exit 1; }; done
echo "== Baseline engines inside the MIG 3g instance ($MIG_3G, SM class $SM, no MPS): models $MODELS -> models/migfhd26<m>/seg00_sm$SM.engine $(date +%T)"
docker run --rm -u root --network host --ipc host -e NVIDIA_VISIBLE_DEVICES=$MIG_3G -e CUDA_VISIBLE_DEVICES=$MIG_3G -e NVIDIA_DRIVER_CAPABILITIES=all -v $A:/opt/nvidia/cuBB \
  -w /opt/nvidia/cuBB/cuPHY-CP/dapp_hook/models -e MODELS="$MODELS" -e SM=$SM --entrypoint bash $AERIAL_IMAGE -c '
  set -u; export CUDA_MPS_PIPE_DIRECTORY=/var; echo quit | nvidia-cuda-mps-control 2>/dev/null; sleep 1
  echo "   device: $(nvidia-smi -L | head -1)"; python3 - <<EOF
import ctypes
cu = ctypes.CDLL("libcuda.so.1"); cu.cuInit(0); dev = ctypes.c_int(); cu.cuDeviceGet(ctypes.byref(dev), 0); n = ctypes.c_int()
cu.cuDeviceGetAttribute(ctypes.byref(n), 16, dev); print("   multiprocessors visible:", n.value); open("/tmp/nsm", "w").write(str(n.value))
EOF
  NSM=$(cat /tmp/nsm 2>/dev/null || echo $SM); [ "$SM" = auto ] && SM=$NSM; [ "$SM" = "$NSM" ] || echo "   note: naming the engine sm$NSM (the instance SM count), not sm$SM"; SM=$NSM
  for m in $MODELS; do
    out=migfhd26$m/seg00_sm$SM.engine; mkdir -p migfhd26$m
    echo "== 26$m: trtexec --onnx=yolo26${m}_1280_b3_e2e.onnx --fp16 -> $out $(date +%T)"
    /usr/src/tensorrt/bin/trtexec --onnx=yolo26${m}_1280_b3_e2e.onnx --fp16 --saveEngine=$out --skipInference 2>&1 | grep -E "Engine built|PASSED|FAILED|rror" | tail -3
    ls -la $out 2>/dev/null | awk "{print \"   \" \$5 \" B \" \$9}"
  done
  chown -R $(stat -c %u:%g .) migfhd26* 2>/dev/null; true'
echo "== done $(date +%T)"; ls -la $M/migfhd26*/seg00_sm*.engine 2>/dev/null | awk '{print "   " $5, $9}'
