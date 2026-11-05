#!/bin/bash
# nonva.sh - the non-VA (live video streaming) service of the hsw setup, run from /home/nvidia/_dgist (nvidia account, no sudo)
#   The second phone opens https://10.45.0.1:8001/ and uploads 720p camera frames at 20 fps (JPEG 0.80); the server keeps the latest
#   frame per UE and relays it as MJPEG to https://10.45.0.1:8001/viewer/<ue1|ue2> (any browser on the host or a UE). No inference.
#   Per-frame UL latency -> CSV at Stop; telemetry "nonva" (/tmp/oai-service-telemetry/nonva_ue*.json) for the hsw PPO xApp.
#   Code: nonva_service/ (hsw video_stream_service; original in nonva_service/upstream_hsw/). Shares the venv va_service/yolo_env.
# commands
#   start | stop | restart | status | logs
#   test [ue] [n] [fps]   UE stand-in (nonva_service/nonva_sim.py): n 720p frames at fps as ue1|ue2 -> the same CSV the phone writes
#   make-frame            (re)create the 1280x720 q80 test JPEG from the Aerial tree's 4K images
# settings: .env NONVA_* | logs: runs/nonva_service/ (uvicorn) and runs/nonva_service_log/ (per-session CSV)
set -u
[ "$(id -u)" = 0 ] && { echo "!! run this as the nvidia user (not root / sudo): processes and files would become root-owned and the other scripts could not stop them"; exit 1; }
D=$(cd "$(dirname "$0")" && pwd); cd $D
set -a; . $D/.env; set +a
V=$D/nonva_service; VENV=$D/va_service/yolo_env; R=$D/runs/nonva_service; PIDF=$R/uvicorn.pid
PORT=${NONVA_PORT:-8001}; FRAME=$V/test_frames/hd_1280x720_q80.jpg
mkdir -p $R $D/runs/nonva_service_log
pid() { cat $PIDF 2>/dev/null; }
alive() { [ -n "$(pid)" ] && kill -0 $(pid) 2>/dev/null; }
port_open() { (echo > /dev/tcp/127.0.0.1/$1) 2>/dev/null; }
ue_ip() { ip -br addr show ogstun 2>/dev/null | awk '{print $3}' | cut -d/ -f1 | grep . || echo "10.45.0.1(ogstun down)"; }
need_up() { alive && port_open $PORT || { echo "non-VA service not running: $0 start"; exit 1; }; }
make_frame() {
  mkdir -p $V/test_frames
  $VENV/bin/python - "$D/aerial-cuda-accelerated-ran/cuPHY-CP/dapp_hook/models/img4k/img4k_1.jpg" "$FRAME" "${NONVA_WIDTH:-1280}" "${NONVA_HEIGHT:-720}" <<'EOF'
import os, sys
from PIL import Image
src, dst, W, H = sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4])
im = Image.open(src).convert("RGB"); w, h = im.size
s = max(W / w, H / h); im = im.resize((round(w * s), round(h * s)), Image.LANCZOS); w, h = im.size
im = im.crop(((w - W) // 2, (h - H) // 2, (w - W) // 2 + W, (h - H) // 2 + H))
im.save(dst, "JPEG", quality=80)
print(f"test frame: {dst} {W}x{H} q80 {os.path.getsize(dst)/1024:.0f} KB")
EOF
}

case "${1:-status}" in
start)
  alive && { echo "non-VA service already running (pid $(pid)) - $0 status"; exit 0; }
  [ -x $VENV/bin/uvicorn ] || { echo "!! $VENV/bin/uvicorn missing (va_service/yolo_env)"; exit 1; }
  [ -f $V/certs/server.key ] && [ -f $V/certs/server.crt ] || { echo "!! nonva_service/certs/server.{key,crt} missing"; exit 1; }
  ip -br addr show ogstun 2>/dev/null | grep -q 10.45.0.1 || echo "   note: ogstun 10.45.0.1 is down (core off: ./core.sh up) - a UE cannot reach the service"
  LOG=$R/uvicorn_$(date +%m%d_%H%M%S).log; ln -sfn $LOG $R/uvicorn.log
  echo "== non-VA service start $(date +%T): ${NONVA_WIDTH:-1280}x${NONVA_HEIGHT:-720} @ ${NONVA_FPS:-20} fps JPEG q${NONVA_JPEG_QUALITY:-0.80}, viewer ${NONVA_VIEWER_FPS:-20} fps, port $PORT"
  (cd $V && set +u && . $VENV/bin/activate && { nohup uvicorn app:app --host 0.0.0.0 --port $PORT --ssl-keyfile certs/server.key --ssl-certfile certs/server.crt > $LOG 2>&1 < /dev/null & echo $! > $PIDF; })
  for i in $(seq 1 60); do port_open $PORT && break; alive || break; sleep 1; done
  if port_open $PORT; then
    echo "   up: sender https://$(ue_ip):$PORT/  (phone) | viewer https://$(ue_ip):$PORT/viewer/ue1 (or ue2) | log $LOG"
  else echo "!! did not come up - log tail:"; tail -20 $LOG; exit 1; fi ;;
stop)
  alive || { echo "non-VA service not running"; rm -f $PIDF; exit 0; }
  P=$(pid); echo "== non-VA service stop (pid $P) $(date +%T)"; kill -TERM $P 2>/dev/null
  for i in $(seq 1 15); do kill -0 $P 2>/dev/null || break; sleep 1; done
  kill -0 $P 2>/dev/null && { echo "   still alive after 15 s: SIGKILL"; kill -KILL $P 2>/dev/null; }
  rm -f $PIDF; echo "   stopped" ;;
restart) bash $0 stop; bash $0 start ;;
status)
  if alive; then echo "non-VA service: running (pid $(pid)), port $PORT $(port_open $PORT && echo open || echo CLOSED), log $(readlink -f $R/uvicorn.log 2>/dev/null)"; else echo "non-VA service: stopped"; fi
  echo "ogstun: $(ue_ip) | sender https://$(ue_ip):$PORT/ | viewer https://$(ue_ip):$PORT/viewer/ue1"
  alive && port_open $PORT && $VENV/bin/python $V/nonva_status.py --port $PORT
  ls -t $D/runs/nonva_service_log/*.log 2>/dev/null | head -3 | sed 's/^/last CSV: /' ;;
logs) tail -n 50 -f $R/uvicorn.log ;;
test)
  need_up; ue=${2:-ue1}; n=${3:-100}; fps=${4:-${NONVA_FPS:-20}}
  [ -f $FRAME ] || make_frame
  $VENV/bin/python $V/nonva_sim.py --url https://127.0.0.1:$PORT --ue $ue --count $n --fps $fps --image $FRAME ;;
make-frame) make_frame ;;
*) sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'; exit 2 ;;
esac
