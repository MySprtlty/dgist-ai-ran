#!/bin/bash
# vcam.sh - virtual cameras: extra YOLO load for the demo without any uplink (run as nvidia from /home/nvidia/_dgist). 2026-11-12
#   N FHD JPEG streams go straight to the dApp agent (TCP 9810, role "vcam") on the same RUN path as the phones' frames, so only the
#   GPU request rate rises; the cell, the phones and the VA service's own agent connection are untouched. Open loop: each camera
#   offers its fps whatever the latency is (inflight cap like the phone page, skipped frames counted).
#   start <N> [fps] [model]   start N cameras (fps default VCAM_FPS=10, model default VA_MODEL) -> runs/vcam/vcam.log, runs/vcam/status.json
#   stop | restart <N> ...    stop removes the load within a second
#   status                    offered/sent/skipped/fail, achieved req/s, RUN round trip p50/p90/p99, met-% (success within VA_DEADLINE_MS)
#   logs                      follow runs/vcam/vcam.log
#   make-frames <dir|files...> [count]   build va_service/vcam_frames/*.jpg (FHD, JPEG q VCAM_JPEG_QUALITY=0.70) from images or a video
#   dry-run <N> [fps] [sec]   pacing test without the agent (no GPU load)
# settings (.env, optional): VCAM_FPS, VCAM_INFLIGHT (4), VCAM_FRAMES (va_service/vcam_frames), VCAM_JPEG_QUALITY (0.70); VA_MODEL, VA_DEADLINE_MS, VA_AGENT are shared with the VA service
set -u
[ "$(id -u)" = 0 ] && { echo "!! run this as the nvidia user (not root / sudo)"; exit 1; }
D=$(cd "$(dirname "$0")" && pwd); cd $D
set -a; . $D/.env; set +a
V=$D/va_service; VENV=$V/yolo_env; R=$D/runs/vcam; PIDF=$R/vcam.pid; mkdir -p $R
FR=${VCAM_FRAMES:-$V/vcam_frames}
export DAPP_CLIENT_DIR=$D/aerial-cuda-accelerated-ran/cuPHY-CP/dapp_hook/python
pid() { cat $PIDF 2>/dev/null; }
alive() { [ -n "$(pid)" ] && kill -0 $(pid) 2>/dev/null; }
case "${1:-status}" in
start)
  alive && { echo "vcam already running (pid $(pid)) - $0 status | $0 stop"; exit 0; }
  N=${2:?usage: $0 start <N cameras> [fps] [model]}; FPS=${3:-${VCAM_FPS:-10}}; M=${4:-${VA_MODEL:-26x}}
  (echo > /dev/tcp/127.0.0.1/${DAPP_AGENT_PORT:-9810}) 2>/dev/null || { echo "!! dApp agent port ${DAPP_AGENT_PORT:-9810} closed (L1 not up)"; exit 1; }
  LOG=$R/vcam_$(date +%m%d_%H%M%S).log; ln -sfn $LOG $R/vcam.log
  echo "== vcam start $(date +%T): $N cameras x $FPS fps = $(python3 -c "print($N*$FPS)") req/s, model $M, deadline ${VA_DEADLINE_MS:-200} ms, inflight ${VCAM_INFLIGHT:-4}, frames $FR"
  { nohup $VENV/bin/python $V/vcam.py --cams $N --fps $FPS --model $M --deadline ${VA_DEADLINE_MS:-200} --inflight ${VCAM_INFLIGHT:-4} --frames $FR --agent ${VA_AGENT:-127.0.0.1:9810} --status $R/status.json > $LOG 2>&1 < /dev/null & echo $! > $PIDF; }
  sleep 2; alive && { head -2 $LOG | cut -c1-160; echo "   log $LOG | $0 status | $0 stop"; } || { echo "!! did not start:"; tail -5 $LOG; rm -f $PIDF; exit 1; } ;;
stop)
  alive || { echo "vcam not running"; rm -f $PIDF; exit 0; }
  P=$(pid); kill -TERM $P 2>/dev/null; for i in 1 2 3 4 5; do kill -0 $P 2>/dev/null || break; sleep 1; done; kill -0 $P 2>/dev/null && kill -KILL $P
  rm -f $PIDF; echo "== vcam stopped $(date +%T): $(tail -1 $R/vcam.log 2>/dev/null | cut -c1-140)" ;;
restart) bash $0 stop; shift; bash $0 start "$@" ;;
status)
  if alive; then echo "vcam: running (pid $(pid)) | log $(readlink -f $R/vcam.log)"; else echo "vcam: stopped"; fi
  [ -f $R/status.json ] && python3 - $R/status.json <<'EOF'
import json, sys, time
d = json.load(open(sys.argv[1])); t = d["total"]
age = time.time() - t.get("ts", 0)
print(f"   {'(stale %.0f s) ' % age if age > 5 else ''}{t['cams']} cams x {t['fps']:g} fps = {t['cams']*t['fps']:g} req/s offered | model {t['model']} deadline {t['deadline_ms']} ms | up {t['uptime_s']} s"
      + (" | DRY RUN" if t.get("dry_run") else ""))
print(f"   achieved {t['req_s_60']} req/s | sent {t['sent']} skipped {t['skipped']} success {t['success']} fail {t['fail'] or 0} | RUN round trip p50/p90/p99 {t.get('rt_p50')}/{t.get('rt_p90')}/{t.get('rt_p99')} ms | met {t.get('met_pct_60')} %")
for c in d["cams"]:
    print(f"   cam{c['cam']}: {c['req_s_60']} req/s sent {c['sent']} skipped {c['skipped']} fail {sum(c['fail'].values())} rt p50/p90 {c['rt_p50']}/{c['rt_p90']} met {c['met_pct_60']} % inflight {c['inflight']}")
EOF
  ;;
logs) tail -n 20 -f $R/vcam.log ;;
dry-run) N=${2:-2}; FPS=${3:-10}; SEC=${4:-12}; $VENV/bin/python $V/vcam.py --cams $N --fps $FPS --duration $SEC --report 4 --dry-run --status $R/status_dry.json --frames $FR ;;
make-frames)
  shift; COUNT=${!#}; case "$COUNT" in ''|*[!0-9]*) COUNT=60 ;; *) set -- "${@:1:$(($#-1))}" ;; esac
  [ $# -ge 1 ] || { echo "usage: $0 make-frames <image dir | image files | video file> [count]"; exit 2; }
  mkdir -p $FR
  $VENV/bin/python - "$FR" "$COUNT" "${VCAM_JPEG_QUALITY:-0.70}" "$@" <<'EOF'
import sys, glob, os
from pathlib import Path
from PIL import Image
out, count, q = Path(sys.argv[1]), int(sys.argv[2]), float(sys.argv[3]); srcs = sys.argv[4:]
W, H = 1920, 1080
def fit(im):
    im = im.convert("RGB"); w, h = im.size; s = max(W / w, H / h)
    im = im.resize((round(w * s), round(h * s)), Image.LANCZOS); w, h = im.size
    return im.crop(((w - W) // 2, (h - H) // 2, (w - W) // 2 + W, (h - H) // 2 + H))
files = []
for s in srcs:
    p = Path(s)
    if p.is_dir(): files += sorted(x for x in p.iterdir() if x.suffix.lower() in (".jpg", ".jpeg", ".png"))
    elif p.suffix.lower() in (".mp4", ".mov", ".mkv", ".avi"):
        import cv2
        cap = cv2.VideoCapture(str(p)); n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)); step = max(1, n // count)
        i = k = 0
        while k < count:
            cap.set(cv2.CAP_PROP_POS_FRAMES, i); ok, fr = cap.read()
            if not ok: break
            Image.fromarray(fr[:, :, ::-1]).pipe = None
            fit(Image.fromarray(fr[:, :, ::-1])).save(out / f"v{k:04d}.jpg", "JPEG", quality=int(q * 100)); k += 1; i += step
        print(f"{k} frames from video {p.name}")
    else: files.append(p)
k = 0
for f in files[:count]:
    try: fit(Image.open(f)).save(out / f"i{k:04d}.jpg", "JPEG", quality=int(q * 100)); k += 1
    except Exception as e: print(f"skip {f}: {e}")
sizes = [os.path.getsize(x) for x in glob.glob(str(out / "*.jpg"))]
print(f"{out}: {len(sizes)} frames, FHD q{q:g}, mean {sum(sizes)/max(1,len(sizes))/1024:.0f} KB")
EOF
  ;;
*) sed -n '2,12p' "$0"; exit 2 ;;
esac
