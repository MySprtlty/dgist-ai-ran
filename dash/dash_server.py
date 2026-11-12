#!/usr/bin/env python3
"""_dgist experiment dashboard server (2026-11-12).

One page for the researchers: live metrics of the 1-cell demo (VA / non-VA UEs, GPU slots, radio, slicing), a measurement that is started and
stopped by name ("측정 시작" / "측정 종료") and written to runs/dash/<name>_<time>/, and a replay of such a measurement on the same page.

  uvicorn dash_server:app --host 0.0.0.0 --port 8010        (dash.sh does this inside the VA service's venv)

Plain HTTP on the LAN. Requests from the UE network (DASH_DENY_NETS, default 10.45.0.0/16) get 403: the page can never be loaded over the cell,
so it adds no downlink traffic. The server only READS the other components (docker logs / docker exec tail, the services' loopback status and
/metrics/frames feeds, the dApp agent's telemetry stream as role "dash", journalctl, files) - it sends nothing to a UE and controls nothing.

DASH_SIM=1 feeds the page from sim.py instead (made-up numbers, badged "SIMULATION" everywhere, recordings in runs/dash_sim/).
"""
import asyncio
import signal
import ipaddress
import json
import os
import socket
import sys
import threading
import time
import traceback
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, StreamingResponse

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import hub as hubmod          # noqa: E402
import sources                # noqa: E402

ROOT = Path(os.environ.get("DASH_ROOT") or HERE.parent).resolve()
SIM = os.environ.get("DASH_SIM", "0") not in ("", "0")
STATIC = HERE / "static"
cfg = sources.load_cfg(ROOT)
cfg["sim"] = SIM
if SIM and os.environ.get("DASH_SIM_MODE") in ("ours", "baseline"): cfg["mode"] = os.environ["DASH_SIM_MODE"]          # rehearse either mode with the stack down
cfg["host"] = socket.gethostname()
REC_BASE = Path(os.environ.get("DASH_REC_DIR") or (ROOT / "runs" / ("dash_sim" if SIM else "dash")))
MIN_FREE_GB = float(os.environ.get("DASH_MIN_FREE_GB", "2.0"))          # a measurement is refused / stopped below this much free disk
DENY = [ipaddress.ip_network(x.strip()) for x in os.environ.get("DASH_DENY_NETS", "10.45.0.0/16").split(",") if x.strip()]
J = dict(ensure_ascii=False, separators=(",", ":"))

hub = hubmod.Hub(cfg)
state = {"loop": None, "clients": set(), "stop": threading.Event(), "col": None, "sim": None, "thread": None, "rec_lock": threading.Lock(), "auto_stop": None}
app = FastAPI(title="dgist dashboard", docs_url=None, redoc_url=None, openapi_url=None)


def log(*a):
    print("[dash %s]" % time.strftime("%H:%M:%S"), *a, flush=True)


@app.middleware("http")
async def lan_only(request: Request, call_next):
    """the UE network never gets this page (no downlink traffic on the cell); everything else is no-store so a reload always shows the installed files"""
    host = request.client.host if request.client else ""
    try:
        ip = ipaddress.ip_address(host)
        if any(ip in n for n in DENY): return PlainTextResponse("not served on the UE network\n", status_code=403)
    except ValueError: pass
    resp = await call_next(request)
    if not request.url.path.startswith("/static/fonts/"): resp.headers["Cache-Control"] = "no-store"
    return resp


# ---------------------------------------------------------------------------------------------- push to the pages (Server-Sent Events)
def _fanout(msg):
    for q in list(state["clients"]):
        if q.full():
            try: q.get_nowait()                       # a page that cannot keep up loses its oldest message, never blocks the others
            except asyncio.QueueEmpty: pass
        q.put_nowait(msg)


def push(obj):
    loop = state["loop"]
    if loop is None or not state["clients"]: return
    try: loop.call_soon_threadsafe(_fanout, json.dumps(obj, **J))
    except RuntimeError: pass


def free_gb():
    try:
        REC_BASE.mkdir(parents=True, exist_ok=True); sv = os.statvfs(str(REC_BASE)); return sv.f_bavail * sv.f_frsize / 2.0 ** 30
    except OSError: return None


def ticker():
    """1 Hz tick + 5 Hz slot scope; the hub writes both into the replay file itself while a measurement runs"""
    nxt = time.time(); i = 0; last_meta = None
    while not state["stop"].is_set():
        nxt += 0.2; d = nxt - time.time()
        if d > 0: state["stop"].wait(d)
        elif d < -1.0: nxt = time.time()
        i += 1
        try:
            if i % 5 == 0:
                push(hub.tick())
                m = hub.meta(); key = json.dumps({k: v for k, v in m.items() if k != "t"}, sort_keys=True, default=str)
                if key != last_meta: last_meta = key; push(m)
                if i % 50 == 0 and hub.rec is not None:
                    g = free_gb()
                    if g is not None and g < MIN_FREE_GB / 2:
                        log("free disk %.1f GB: stopping the measurement" % g); state["auto_stop"] = "free disk %.1f GB" % g
                        threading.Thread(target=_stop_rec, daemon=True).start()
            sc = hub.scope()
            if sc is not None: push(sc)
        except Exception:
            log("ticker:", traceback.format_exc().strip().splitlines()[-1]); traceback.print_exc()


@app.on_event("startup")
async def _startup():
    state["loop"] = asyncio.get_running_loop()
    if SIM:
        import sim
        state["sim"] = sim.Sim(hub, cfg); state["sim"].start(); log("SIMULATION source (made-up numbers)")
    else:
        state["col"] = sources.start_collectors(hub, cfg); log("collectors:", " ".join(state["col"]))
    state["thread"] = threading.Thread(target=ticker, name="dash-ticker", daemon=True); state["thread"].start()
    # an open browser holds its event stream forever, and uvicorn's graceful shutdown waits for every connection before it runs the shutdown
    # hook below (which closes a running measurement): end the streams as soon as the stop signal arrives
    for sg in (signal.SIGTERM, signal.SIGINT):
        prev = signal.getsignal(sg)

        def on_exit(signum, frame, prev=prev):
            state["stopping"] = True
            if os.environ.get("DASH_DEBUG_SIGNALS"): log("signal %d (ppid %d, pgid %d, sid %d)" % (signum, os.getppid(), os.getpgid(0), os.getsid(0)))
            state["loop"].call_soon_threadsafe(_wake_clients)
            if callable(prev): prev(signum, frame)
        try: signal.signal(sg, on_exit)
        except ValueError: pass                               # not the main thread (embedded): --timeout-graceful-shutdown still bounds the wait
    log("root %s | recordings %s | deny %s" % (ROOT, REC_BASE, ",".join(str(n) for n in DENY)))


def _wake_clients():
    for q in list(state["clients"]):
        try: q.put_nowait(None)
        except asyncio.QueueFull: pass                        # that stream sees state["stopping"] at its next message


@app.on_event("shutdown")
async def _shutdown():
    state["stop"].set(); state["stopping"] = True
    if hub.rec is not None:
        try: s = hub.rec_stop(); log("measurement %s closed at shutdown" % s.get("id"))
        except Exception as e: log("closing the measurement failed: %r" % (e,))
    if state["sim"] is not None: state["sim"].stop()
    for c in (state["col"] or {}).values():
        try: c.stop()
        except Exception: pass
    _wake_clients()


@app.get("/api/stream")
async def api_stream(request: Request):
    q = asyncio.Queue(maxsize=600); state["clients"].add(q)

    async def gen():
        try:
            yield "retry: 1500\n\n"
            while True:
                try: msg = await asyncio.wait_for(q.get(), 10.0)
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"; continue
                if msg is None or state.get("stopping"): return
                yield "data: " + msg + "\n\n"
        finally:
            state["clients"].discard(q)
    return StreamingResponse(gen(), media_type="text/event-stream", headers={"X-Accel-Buffering": "no"})


@app.get("/api/init")
def api_init():
    return JSONResponse({"meta": hub.meta(), "ticks": hub.history(), "scope": hub.last_scope, "now": time.time(), "free_gb": free_gb(), "min_free_gb": MIN_FREE_GB})


# ---------------------------------------------------------------------------------------------- measurement start / stop
@app.post("/api/rec/start")
async def api_rec_start(request: Request):
    try: body = await request.json()
    except Exception: body = {}
    name = str((body or {}).get("name") or "").strip()
    if not name: raise HTTPException(400, "실험 이름이 필요합니다")
    g = free_gb()
    if g is not None and g < MIN_FREE_GB: raise HTTPException(507, "디스크 여유 공간 부족 (%.1f GB)" % g)
    with state["rec_lock"]:
        try: meta = hub.rec_start(name, REC_BASE)
        except RuntimeError as e: raise HTTPException(409, "이미 측정 중입니다 (%s)" % e)
        except OSError as e: raise HTTPException(500, "기록 폴더를 만들 수 없습니다: %s" % e)
    state["auto_stop"] = None
    log("measurement started: %s -> %s (by %s)" % (name, meta["rec"]["dir"], request.client.host if request.client else "?"))
    push(meta)
    return JSONResponse({"ok": True, "meta": meta})


def _stop_rec():
    with state["rec_lock"]:
        summ = hub.rec_stop()
    if state["auto_stop"]: summ.setdefault("notes", []).append("stopped automatically: " + state["auto_stop"])
    log("measurement stopped: %s (%.0f s, %d ticks)" % (summ.get("id"), summ.get("duration_s") or 0, summ.get("ticks") or 0))
    push({"k": "end", "t": time.time(), "summary": summ}); push(hub.meta())
    return summ


@app.post("/api/rec/stop")
def api_rec_stop():
    try: return JSONResponse({"ok": True, "summary": _stop_rec()})
    except RuntimeError: raise HTTPException(409, "측정 중이 아닙니다")


# ---------------------------------------------------------------------------------------------- saved measurements
def _rec_dir(rid):
    if not rid or "/" in rid or "\\" in rid or rid.startswith("."): raise HTTPException(404)
    d = REC_BASE / rid
    if not d.is_dir(): raise HTTPException(404)
    return d


def _one(d, suffix):
    xs = sorted(d.glob("*" + suffix))
    return xs[0] if xs else None


@app.get("/api/recs")
def api_recs():
    out = []; cur = (hub.rec or {}).get("id")
    if REC_BASE.is_dir():
        for d in sorted((x for x in REC_BASE.iterdir() if x.is_dir()), key=lambda x: x.stat().st_mtime, reverse=True)[:300]:
            rp = _one(d, ".replay.jsonl")
            if rp is None: continue
            e = {"id": d.name, "running": d.name == cur, "files": [{"name": f.name, "bytes": f.stat().st_size} for f in sorted(d.iterdir()) if f.is_file()],
                 "mtime": rp.stat().st_mtime, "name": d.name}
            sp = _one(d, ".summary.json")
            if sp is not None:
                try:
                    s = json.loads(sp.read_text()); va = (s.get("svc") or {}).get("va") or {}; g = s.get("gpu") or {}
                    e.update(name=s.get("name"), start=s.get("start"), end=s.get("end"), duration_s=s.get("duration_s"), mode=s.get("mode"), sim=s.get("sim"),
                             qos=va.get("qos"), e2e=va.get("e2e"), req=g.get("req"), busy=g.get("busy"), map=(s.get("accuracy") or {}).get("retention_pct"), frames=va.get("n"))
                except (OSError, ValueError): e["broken"] = True
            else: e["unfinished"] = not e["running"]
            out.append(e)
    return JSONResponse({"recs": out, "dir": str(REC_BASE), "free_gb": free_gb()})


@app.get("/api/recs/{rid}/replay")
def api_replay(rid: str):
    p = _one(_rec_dir(rid), ".replay.jsonl")
    if p is None: raise HTTPException(404)
    return FileResponse(str(p), media_type="application/x-ndjson")


@app.get("/api/recs/{rid}/summary")
def api_summary(rid: str):
    p = _one(_rec_dir(rid), ".summary.json")
    if p is None: raise HTTPException(404)
    return FileResponse(str(p), media_type="application/json")


@app.get("/api/recs/{rid}/file/{fname}")
def api_file(rid: str, fname: str):
    d = _rec_dir(rid); p = d / fname
    if "/" in fname or fname.startswith(".") or not p.is_file(): raise HTTPException(404)
    return FileResponse(str(p), filename=fname)


# ---------------------------------------------------------------------------------------------- the page
@app.get("/")
def index():
    return FileResponse(str(STATIC / "index.html"), media_type="text/html; charset=utf-8")


@app.get("/static/{path:path}")
def static_file(path: str):
    p = (STATIC / path).resolve()
    if STATIC not in p.parents or not p.is_file(): raise HTTPException(404)
    resp = FileResponse(str(p))
    if "/fonts/" in str(p): resp.headers["Cache-Control"] = "public, max-age=86400"
    return resp


@app.get("/healthz")
def healthz():
    return {"ok": True, "sim": SIM, "recording": hub.rec_info(), "clients": len(state["clients"])}
