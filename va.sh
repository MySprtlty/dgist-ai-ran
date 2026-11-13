#!/bin/bash
# va.sh - the VA (video analytics) service of the 1-cell demo, run from /home/nvidia/_dgist (nvidia account, no sudo)
#   The phone (COTS UE) opens https://10.45.0.1:8000/ and sends 4K camera frames at 5 req/s per UE; this service forwards every frame
#   as one RUN request (model VA_MODEL or, with VA_MODEL_SELECT=1, a model drawn by the VA_MODEL_WEIGHTS rule; QoS deadline 200 ms) to the dApp agent inside the L1 container (TCP 9810) and returns the agent's
#   reply (result figures + detections) to the phone, which draws the boxes and logs the UL / AI / DL latency split.
#   Base-station viewer (2026-11-11): https://<host>:8000/viewer shows each UE's latest frame + boxes; the picture is updated only when
#   UL (measured) + AI (measured) + DL (fixed .env VA_VIEWER_DL_MS) <= VA_DEADLINE_MS, so QoS misses show as a frozen picture.
#   Code: va_service/ (hsw yolo_service + dapp_backend.py; the hsw original is in va_service/upstream_hsw/). Doc: va_service/README.md
# commands
#   start [local]        activate va_service/yolo_env and start uvicorn (HTTPS, certs/server.*) in the background
#                        'local' = inference with the hsw original (ultralytics in this process) instead of the dApp, for A/B tests
#   stop | restart
#   status               process, ports, dApp agent connection + counters + latest estimate, active UE sessions, last CSVs
#   logs                 follow the uvicorn log
#   test [ue] [n] [rate] UE stand-in (va_service/ue_sim.py): n frames (VA_CAMERA_WIDTH x HEIGHT) at rate req/s as ue1|ue2 -> the same CSV the phone writes
#   test2 [n] [rate]     ue1 and ue2 at the same time
#   bg start [--fps N] | bg stop | bg status      background virtual UE inside the service (bg_yolo.py, localhost control)
#   make-frame           (re)create the VA_CAMERA_WIDTH x VA_CAMERA_HEIGHT test JPEG from the Aerial tree's 4K images
# settings: .env VA_* | logs: runs/va_service/ (uvicorn) and runs/va_service_log/ (per-session CSV, phone and ue_sim.py)
set -u
[ "$(id -u)" = 0 ] && { echo "!! run this as the nvidia user (not root / sudo): processes and files would become root-owned and the other scripts could not stop them"; exit 1; }
D=$(cd "$(dirname "$0")" && pwd); cd $D
set -a; . $D/.env; set +a
V=$D/va_service; VENV=$V/yolo_env; R=$D/runs/va_service; PIDF=$R/uvicorn.pid
PORT=${VA_PORT:-8000}; AGENT_PORT=${DAPP_AGENT_PORT:-9810}; CW=${VA_CAMERA_WIDTH:-3840}; CH=${VA_CAMERA_HEIGHT:-2160}
FRAME=$V/test_frames/uhd_3840x2160_q90.jpg; [ "${CW}x${CH}" = 3840x2160 ] || FRAME=$V/test_frames/frame_${CW}x${CH}_q90.jpg   # the UE stand-in sends frames of the configured camera size
mkdir -p $R $D/runs/va_service_log
pid() { cat $PIDF 2>/dev/null; }
alive() { [ -n "$(pid)" ] && kill -0 $(pid) 2>/dev/null; }
port_open() { (echo > /dev/tcp/127.0.0.1/$1) 2>/dev/null; }
ue_ip() { ip -br addr show ogstun 2>/dev/null | awk '{print $3}' | cut -d/ -f1 | grep . || echo "10.45.0.1(ogstun down)"; }
lan_ip() { hostname -I 2>/dev/null | tr " " "\n" | grep -E "^10\.150\." | head -1 | grep . || hostname -I 2>/dev/null | awk '{print $1}'; }   # the host LAN address a PC browser uses for /viewer
need_up() { alive && port_open $PORT || { echo "VA service not running: $0 start"; exit 1; }; }
make_frame() {
  mkdir -p $V/test_frames
  SRC=$D/aerial-cuda-accelerated-ran/cuPHY-CP/dapp_hook/models/img4k/img4k_0.jpg
  $VENV/bin/python - "$SRC" "$FRAME" "$CW" "$CH" <<'EOF'
import sys
from PIL import Image
src, dst, W, H = sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4])
im = Image.open(src).convert("RGB")
w, h = im.size
if (w, h) != (W, H):
    # center crop/resize to the camera size the phone page captures (.env VA_CAMERA_WIDTH x VA_CAMERA_HEIGHT)
    s = max(W / w, H / h)
    im = im.resize((round(w * s), round(h * s)), Image.LANCZOS)
    w, h = im.size
    l, t = (w - W) // 2, (h - H) // 2
    im = im.crop((l, t, l + W, t + H))
im.save(dst, "JPEG", quality=90)
import os
print(f"test frame: {dst} {im.size[0]}x{im.size[1]} q90 {os.path.getsize(dst)/1024:.0f} KB (from {src})")
EOF
}

case "${1:-status}" in
start)
  alive && { echo "VA service already running (pid $(pid)) - $0 status"; exit 0; }
  [ -x $VENV/bin/uvicorn ] || { echo "!! $VENV/bin/uvicorn missing (va_service/yolo_env)"; exit 1; }
  [ -f $V/certs/server.key ] && [ -f $V/certs/server.crt ] || { echo "!! va_service/certs/server.{key,crt} missing"; exit 1; }
  [ "${2:-}" = local ] && export VA_BACKEND=local
  port_open $AGENT_PORT || echo "   note: dApp agent port $AGENT_PORT closed (L1 not up: ./dgist.sh up) - frames fail with agent_unreachable until it is"
  ip -br addr show ogstun 2>/dev/null | grep -q 10.45.0.1 || echo "   note: ogstun 10.45.0.1 is down (core off: ./core.sh up) - a UE cannot reach the service"
  LOG=$R/uvicorn_$(date +%m%d_%H%M%S).log; ln -sfn $LOG $R/uvicorn.log
  echo "== VA service start $(date +%T): backend ${VA_BACKEND:-dapp}, model $([ "${VA_MODEL_SELECT:-0}" = 1 ] && echo "rule ${VA_MODEL_WEIGHTS:-unset} (requests per block for 26n,26s,26m,26l,26x)" || echo ${VA_MODEL:-26x}), QoS ${VA_DEADLINE_MS:-200} ms, ${VA_TARGET_FPS:-5} req/s per UE, ${VA_CAMERA_WIDTH:-3840}x${VA_CAMERA_HEIGHT:-2160} JPEG q${VA_JPEG_QUALITY:-0.90}, port $PORT"
  # the braces matter: '&' must background only uvicorn (not the whole && list), so $! is uvicorn's pid and nothing keeps our stdout open
  (cd $V && set +u && . $VENV/bin/activate && { nohup uvicorn app:app --host 0.0.0.0 --port $PORT --ssl-keyfile certs/server.key --ssl-certfile certs/server.crt > $LOG 2>&1 < /dev/null & echo $! > $PIDF; })
  for i in $(seq 1 90); do port_open $PORT && break; alive || break; sleep 1; done
  if port_open $PORT; then
    echo "   up: phone https://$(ue_ip):$PORT/  (the phone needs va_service/certs/ca.crt installed, or accept the warning) | log $LOG"
    echo "   viewer (base station, the latest QoS-met frame + boxes per UE): https://$(lan_ip):$PORT/viewer  (one UE: /viewer/ue1) - also from a UE at https://$(ue_ip):$PORT/viewer"
    $VENV/bin/python $V/va_status.py --port $PORT --short
  else echo "!! did not come up - log tail:"; tail -20 $LOG; exit 1; fi ;;
stop)
  alive || { echo "VA service not running"; rm -f $PIDF; exit 0; }
  P=$(pid); echo "== VA service stop (pid $P) $(date +%T)"; kill -TERM $P 2>/dev/null
  for i in $(seq 1 15); do kill -0 $P 2>/dev/null || break; sleep 1; done
  kill -0 $P 2>/dev/null && { echo "   still alive after 15 s: SIGKILL"; kill -KILL $P 2>/dev/null; }
  rm -f $PIDF; echo "   stopped" ;;
restart) bash $0 stop; bash $0 start "${2:-}" ;;
status)
  if alive; then echo "VA service: running (pid $(pid)), port $PORT $(port_open $PORT && echo open || echo CLOSED), log $(readlink -f $R/uvicorn.log 2>/dev/null)"; else echo "VA service: stopped"; fi
  echo "viewer: https://$(lan_ip):$PORT/viewer (PC on the lab LAN) | https://$(ue_ip):$PORT/viewer (from a UE)"
  echo "dApp agent port $AGENT_PORT: $(port_open $AGENT_PORT && echo open || echo closed) | ogstun: $(ue_ip) | L1 container: $(docker ps --format '{{.Names}} ({{.Status}})' 2>/dev/null | grep dgist-l1 || echo not running)"
  alive && port_open $PORT && $VENV/bin/python $V/va_status.py --port $PORT
  ls -t $D/runs/va_service_log/*.log 2>/dev/null | head -3 | sed 's/^/last CSV: /' ;;
logs) tail -n 50 -f $R/uvicorn.log ;;
test)
  need_up; ue=${2:-ue1}; n=${3:-25}; rate=${4:-${VA_TARGET_FPS:-5}}
  [ -f $FRAME ] || make_frame
  $VENV/bin/python $V/ue_sim.py --url https://127.0.0.1:$PORT --ue $ue --count $n --rate $rate --image $FRAME --inflight ${VA_MAX_INFLIGHT:-4} ;;
test2)
  need_up; n=${2:-25}; rate=${3:-${VA_TARGET_FPS:-5}}
  [ -f $FRAME ] || make_frame
  echo "== two UEs at once: $n frames each at $rate req/s (summaries at the end)"
  $VENV/bin/python $V/ue_sim.py --url https://127.0.0.1:$PORT --ue ue1 --count $n --rate $rate --image $FRAME --inflight ${VA_MAX_INFLIGHT:-4} --quiet &
  sleep 0.1
  $VENV/bin/python $V/ue_sim.py --url https://127.0.0.1:$PORT --ue ue2 --count $n --rate $rate --image $FRAME --inflight ${VA_MAX_INFLIGHT:-4} --quiet &
  wait ;;
bg) need_up; shift; VA_PORT=$PORT $VENV/bin/python $V/bg_yolo.py "$@" ;;
make-frame) make_frame ;;
*) sed -n '2,19p' "$0" | sed 's/^# \{0,1\}//'; exit 2 ;;
esac
