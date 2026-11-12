#!/bin/bash
# dash.sh - the experiment dashboard of the 1-cell demo, run from /home/nvidia/_dgist (nvidia account, no sudo)
#   One web page for the researchers (PC on the lab LAN): http://<host>:8010/
#   live metrics per UE / service / GPU slot / radio, "측정 시작" (asks for a name) ... "측정 종료" (summary), every measurement saved under
#   runs/dash/<name>_<time>/ and replayable on the same page (tab "기록 · Replay"). The page is READ-ONLY towards the stack: it starts, stops and
#   changes nothing, sends nothing to a UE, and refuses requests from the UE network (10.45.0.0/16) so it can never add downlink traffic.
#   Code: dash/ (dash_server.py, hub.py, sources.py, sim.py, static/). Doc: dash/README.md
# commands
#   start                start the dashboard (plain HTTP, port .env DASH_PORT, default 8010) in the background; works in both DEMO_MODEs
#   stop | restart       a running measurement is closed properly (summary + files) before the process exits
#   status               process, URL, data sources that are answering, the running measurement, saved measurements
#   logs                 follow the server log
#   list                 saved measurements (runs/dash/)
#   sim [ours|baseline]  a SECOND instance on port DASH_SIM_PORT (default 8011) fed with made-up numbers (badge "SIMULATION"), for rehearsing the
#                        page, the measurement flow and the replay with the stack down; recordings go to runs/dash_sim/
#   sim stop | sim logs
# settings: .env DASH_* (all optional, see dash/README.md) | logs: runs/dash_service/ | measurements: runs/dash/ (sim: runs/dash_sim/)
set -u
[ "$(id -u)" = 0 ] && { echo "!! run this as the nvidia user (not root / sudo): processes and files would become root-owned and the other scripts could not stop them"; exit 1; }
D=$(cd "$(dirname "$0")" && pwd); cd $D
set -a; . $D/.env; set +a
P=$D/dash; VENV=$D/va_service/yolo_env; R=$D/runs/dash_service
PORT=${DASH_PORT:-8010}; SIMPORT=${DASH_SIM_PORT:-8011}
mkdir -p $R
port_open() { (exec 3<>/dev/tcp/127.0.0.1/$1) 2>/dev/null; }      # connect only: a byte sent here would be logged by uvicorn as an invalid request
lan_ip() { hostname -I 2>/dev/null | tr " " "\n" | grep -E "^10\.150\." | head -1 | grep . || hostname -I 2>/dev/null | awk '{print $1}'; }
pid() { cat $R/$1.pid 2>/dev/null; }                      # $1 = uvicorn (real) | sim
alive() { [ -n "$(pid $1)" ] && kill -0 $(pid $1) 2>/dev/null; }
api() { $VENV/bin/python - "$1" "$2" <<'PY' 2>/dev/null
import json, sys, urllib.request
try: print(json.dumps(json.load(urllib.request.urlopen("http://127.0.0.1:%s%s" % (sys.argv[1], sys.argv[2]), timeout=3))))
except Exception: pass
PY
}
launch() {                                                # $1 = uvicorn|sim, $2 = port, the rest = environment assignments
  local tag=$1 port=$2; shift 2
  [ -x $VENV/bin/uvicorn ] || { echo "!! $VENV/bin/uvicorn missing (va_service/yolo_env)"; exit 1; }
  [ -f $P/dash_server.py ] || { echo "!! $P/dash_server.py missing"; exit 1; }
  port_open $port && { echo "!! port $port is already in use"; exit 1; }
  LOG=$R/${tag}_$(date +%m%d_%H%M%S).log; ln -sfn $LOG $R/$tag.log
  # the braces matter: '&' must background only uvicorn (not the whole && list), so $! is uvicorn's pid and nothing keeps our stdout open
  (cd $P && set +u && . $VENV/bin/activate && { env "$@" DASH_ROOT=$D nohup uvicorn dash_server:app --host 0.0.0.0 --port $port --log-level warning --timeout-graceful-shutdown 5 > $LOG 2>&1 < /dev/null & echo $! > $R/$tag.pid; })
  for i in $(seq 1 30); do port_open $port && break; alive $tag || break; sleep 1; done
  port_open $port || { echo "!! did not come up - log tail:"; tail -20 $LOG; exit 1; }
}
halt() {                                                  # $1 = uvicorn|sim
  alive $1 || { echo "dashboard ($1) not running"; rm -f $R/$1.pid; return 0; }
  local p=$(pid $1); echo "== dashboard ($1) stop (pid $p) $(date +%T)"; kill -TERM $p 2>/dev/null
  for i in $(seq 1 15); do kill -0 $p 2>/dev/null || break; sleep 1; done
  kill -0 $p 2>/dev/null && { echo "   still alive after 15 s: SIGKILL"; kill -KILL $p 2>/dev/null; }
  rm -f $R/$1.pid; echo "   stopped"
}
recs() {                                                  # $1 = port
  api $1 /api/recs | $VENV/bin/python -c '
import json, sys
try: d = json.load(sys.stdin)
except Exception: sys.exit(0)
print("measurements in %s (disk free %.1f GB): %d" % (d.get("dir"), d.get("free_gb") or 0, len(d.get("recs") or [])))
for r in (d.get("recs") or [])[:int(sys.argv[1])]:
    f = lambda v, u="": "-" if v is None else "%s%s" % (v, u)
    print("  %-40s %-19s %6s s  %-8s QoS %-7s E2E %-9s %s%s" % (r.get("id"), f(r.get("start")), f(r.get("duration_s")), f(r.get("mode")), f(r.get("qos"), " %"), f(r.get("e2e"), " ms"),
          "RECORDING" if r.get("running") else "", " (unfinished)" if r.get("unfinished") else ""))' "${2:-5}"
}

case "${1:-status}" in
start)
  alive uvicorn && { echo "dashboard already running (pid $(pid uvicorn)) - http://$(lan_ip):$PORT/"; exit 0; }
  echo "== dashboard start $(date +%T): mode ${DEMO_MODE:-ours}, port $PORT, measurements -> runs/dash/"
  launch uvicorn $PORT DASH_SIM=0
  echo "   up: http://$(lan_ip):$PORT/  (PC on the lab LAN; the UE network gets 403) | log $LOG"
  docker ps --format '{{.Names}}' 2>/dev/null | grep -q '^dgist-l1$' || echo "   note: the stack is down (./all.sh up <name>) - the page shows empty tiles until it is up; no restart of the dashboard is needed" ;;
stop) halt uvicorn ;;
restart) bash $0 stop; bash $0 start ;;
status)
  if alive uvicorn; then echo "dashboard: running (pid $(pid uvicorn)), http://$(lan_ip):$PORT/ port $(port_open $PORT && echo open || echo CLOSED), log $(readlink -f $R/uvicorn.log 2>/dev/null)"
    api $PORT /api/init | $VENV/bin/python -c '
import json, sys
try: d = json.load(sys.stdin)
except Exception: sys.exit(0)
m = d.get("meta") or {}; t = (d.get("ticks") or [{}])[-1]; s = t.get("sys") or {}; rec = m.get("rec")
print("  mode %s | measurement: %s" % (m.get("mode"), ("RECORDING \"%s\" (%d s) -> %s" % (rec.get("name"), t.get("el") or 0, rec.get("dir"))) if rec else "none"))
print("  up: " + " ".join("%s=%s" % (k, "yes" if v else "no") for k, v in (s.get("up") or {}).items()))
print("  sources: " + " ".join("%s=%s" % (k, "yes" if v else "no") for k, v in (s.get("src") or {}).items()))
col = s.get("col") or {}
if col: print("  collectors: " + " ".join("%s=%s" % (k, (v or {}).get("state")) for k, v in col.items()))
print("  UEs sending: " + " ".join("%s=%s" % (k, "yes" if (u or {}).get("on") else "no") for k, u in (t.get("ue") or {}).items()))'
    recs $PORT 5
  else echo "dashboard: stopped ($0 start)"; fi
  alive sim && echo "simulation instance: running (pid $(pid sim)), http://$(lan_ip):$SIMPORT/" ;;
logs) tail -n 50 -f $R/uvicorn.log ;;
list) if alive uvicorn; then recs $PORT 200; else ls -dt $D/runs/dash/*/ 2>/dev/null | head -50; fi ;;
sim)
  case "${2:-ours}" in
  stop) halt sim ;;
  logs) tail -n 50 -f $R/sim.log ;;
  ours|baseline)
    alive sim && { echo "simulation instance already running (pid $(pid sim)) - http://$(lan_ip):$SIMPORT/ ($0 sim stop)"; exit 0; }
    echo "== dashboard SIMULATION start $(date +%T): made-up numbers, mode ${2:-ours}, port $SIMPORT, recordings -> runs/dash_sim/"
    launch sim $SIMPORT DASH_SIM=1 DASH_SIM_MODE=${2:-ours}
    echo "   up: http://$(lan_ip):$SIMPORT/  | log $LOG | stop: $0 sim stop" ;;
  *) echo "usage: $0 sim [ours|baseline|stop|logs]"; exit 2 ;;
  esac ;;
*) sed -n '2,17p' "$0" | sed 's/^# \{0,1\}//'; exit 2 ;;
esac
