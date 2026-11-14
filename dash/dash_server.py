#!/usr/bin/env python3
"""_dgist experiment dashboard server (2026-11-12).

One page for the researchers: live metrics of the 1-cell demo (VA / non-VA UEs, GPU slots, radio, slicing), a measurement that is started and
stopped by name ("측정 시작" / "측정 종료") and written to runs/dash/<name>_<time>/, and a replay of such a measurement on the same page.

  uvicorn dash_server:app --host 0.0.0.0 --port 8010        (dash.sh does this inside the VA service's venv)

Plain HTTP on the LAN, and the same page over HTTPS on DASH_TLS_PORT (a TLS front in this process; browsers allow the video encoder that
"Capture" uses only on HTTPS pages). Requests from the UE network (DASH_DENY_NETS, default 10.45.0.0/16) get 403 on the plain port and are
closed before the TLS handshake on the HTTPS port: the page can never be loaded over the cell, so it adds no downlink traffic. The server
READS the other components (docker logs / docker exec tail, the services' loopback status and /metrics/frames feeds, the dApp agent's
telemetry stream as role "dash", journalctl, files) and sends nothing to a UE.
  viewer.py   the four /viewer screens for the "뷰어" tab: a loopback relay of the services' viewer feeds (read only, on demand)
  control.py  the "제어" tab: On / Off / restart of the components through the operator scripts (the only part that changes anything;
              two-step confirmation, one action at a time, logged in runs/dash/control.jsonl; DASH_CTL=0 switches it off)

DASH_SIM=1 feeds the page from sim.py instead (made-up numbers, badged "SIMULATION" everywhere, recordings in runs/dash_sim/).
"""
import asyncio
import signal
import ipaddress
import json
import os
import socket
import ssl
import sys
import threading
import time
import traceback
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, StreamingResponse
from starlette.concurrency import run_in_threadpool

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import hub as hubmod          # noqa: E402
import sources                # noqa: E402
import viewer as viewermod    # noqa: E402
import control as controlmod  # noqa: E402

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


PORT = int(os.environ.get("DASH_PORT") or (8011 if SIM else 8010))          # the plain port uvicorn listens on (dash.sh passes it): the HTTPS front forwards to it
TLS_PORT = int(os.environ.get("DASH_TLS_PORT") or (8013 if SIM else 8012))   # 0 = no HTTPS port
TLS_CERT = Path(os.environ.get("DASH_TLS_CERT") or (ROOT / "va_service" / "certs" / "server.crt"))        # default: the certificate the VA service already shows this PC
TLS_KEY = Path(os.environ.get("DASH_TLS_KEY") or (ROOT / "va_service" / "certs" / "server.key"))
TLS_TASKS = set()                                                            # HTTPS front: its connection tasks (strong references)
PEERS = {}                                                                   # HTTPS front: source port of its loopback connection -> the real client address
cfg["tls_port"] = TLS_PORT if TLS_PORT > 0 and TLS_CERT.is_file() and TLS_KEY.is_file() else 0
cfg["ctl_dry"] = bool(cfg.get("ctl_dry")) or SIM                              # the simulation never runs a real command
relay = viewermod.Relay(cfg, log, SIM)
cfg["viewer"].update(relay=relay.on, va=relay.va is not None, nv=relay.nv is not None)
ctl = controlmod.Control(ROOT, hub, cfg, REC_BASE, log, dry=cfg["ctl_dry"]) if cfg.get("ctl") else None


def _denied(host):
    try: ip = ipaddress.ip_address(host)
    except ValueError: return False
    if ip.version == 6 and ip.ipv4_mapped is not None: ip = ip.ipv4_mapped
    return any(ip in n for n in DENY)


@app.middleware("http")
async def lan_only(request: Request, call_next):
    """the UE network never gets this page (no downlink traffic on the cell); everything else is no-store so a reload always shows the installed files"""
    host = request.client.host if request.client else ""
    if request.client and host in ("127.0.0.1", "::1") and request.client.port in PEERS: host = PEERS[request.client.port]       # came in through the HTTPS front
    if _denied(host): return PlainTextResponse("not served on the UE network\n", status_code=403)
    request.state.peer = host
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
            fr = hub.frames()                         # demo tab: per-frame latency feed (message kind "fr")
            if fr is not None: push(fr)
        except Exception:
            log("ticker:", traceback.format_exc().strip().splitlines()[-1]); traceback.print_exc()


@app.on_event("startup")
async def _startup():
    state["loop"] = relay.loop = asyncio.get_running_loop()
    await _tls_start()
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
    log("root %s | recordings %s | deny %s | https %s | viewer relay %s | control %s" % (ROOT, REC_BASE, ",".join(str(n) for n in DENY), cfg["tls_port"] or "off",
        "on" if relay.on else "off", "off" if ctl is None else ("dry-run" if ctl.dry else "on")))


def _wake_clients():
    for q in list(state["clients"]):
        try: q.put_nowait(None)
        except asyncio.QueueFull: pass                        # that stream sees state["stopping"] at its next message
    relay.close()


# ---------------------------------------------------------------------------------------------- HTTPS front (the page on a secure origin, for Capture)
async def _pump(r, w):
    try:
        while True:
            b = await r.read(65536)
            if not b: break
            w.write(b); await w.drain()
    except Exception: pass


async def _tls_conn(sock, addr, ctx):
    """one browser connection: TLS ends here, the bytes go to the plain port on loopback and back"""
    loop = asyncio.get_running_loop(); r = asyncio.StreamReader(limit=1 << 16); proto = asyncio.StreamReaderProtocol(r); w = uw = None; lport = None
    try:
        tr, _ = await loop.connect_accepted_socket(lambda: proto, sock, ssl=ctx, ssl_handshake_timeout=10.0)
        w = asyncio.StreamWriter(tr, proto, r, loop)
        ur, uw = await asyncio.open_connection("127.0.0.1", PORT)
        lport = uw.get_extra_info("sockname")[1]; PEERS[lport] = addr[0]
        a = asyncio.ensure_future(_pump(r, uw)); b = asyncio.ensure_future(_pump(ur, w))
        try: await asyncio.wait({a, b}, return_when=asyncio.FIRST_COMPLETED)
        finally: a.cancel(); b.cancel()
    except Exception: pass
    finally:
        if lport is not None: PEERS.pop(lport, None)
        for x in (w, uw):
            if x is not None:
                try: x.close()
                except Exception: pass
        if w is None:
            try: sock.close()
            except Exception: pass


async def _tls_accept(srv, ctx):
    loop = asyncio.get_running_loop()
    while True:
        try: sock, addr = await loop.sock_accept(srv)
        except OSError:
            await asyncio.sleep(0.2); continue
        if _denied(addr[0]):                                  # the UE network: closed before a single TLS byte goes out
            sock.close(); continue
        sock.setblocking(False)
        t = asyncio.ensure_future(_tls_conn(sock, addr, ctx)); TLS_TASKS.add(t); t.add_done_callback(TLS_TASKS.discard)


async def _tls_start():
    if not cfg["tls_port"]:
        log("https: off (%s)" % ("DASH_TLS_PORT=0" if TLS_PORT <= 0 else "no certificate at %s" % TLS_CERT)); return
    try:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); ctx.load_cert_chain(str(TLS_CERT), str(TLS_KEY)); ctx.set_alpn_protocols(["http/1.1"])
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM); srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("0.0.0.0", TLS_PORT)); srv.listen(64); srv.setblocking(False)
        state["tls"] = (srv, asyncio.ensure_future(_tls_accept(srv, ctx)))
    except Exception as e:
        cfg["tls_port"] = 0; log("https: off (%r)" % (e,))


@app.on_event("shutdown")
async def _shutdown():
    state["stop"].set(); state["stopping"] = True
    t = state.get("tls")
    if t:
        t[1].cancel()
        try: t[0].close()
        except Exception: pass
        for x in list(TLS_TASKS): x.cancel()
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


# ---------------------------------------------------------------------------------------------- viewer screens (viewer.py)
@app.get("/api/viewer/stream")
async def api_viewer_stream():
    if not relay.on: raise HTTPException(404, "뷰어 중계가 꺼져 있습니다")

    async def gen():
        c = relay.attach()
        try:
            yield viewermod.record({"k": "hello", "va": relay.va is not None, "nv": relay.nv is not None})
            while True:
                try: await asyncio.wait_for(c.ev.wait(), 5.0)
                except asyncio.TimeoutError:
                    yield viewermod.record({"k": "ka"}); continue
                if c.closed or state.get("stopping"): return
                out = c.take()
                if out: yield b"".join(out)
        finally:
            relay.detach(c)
    return StreamingResponse(gen(), media_type="application/octet-stream", headers={"X-Accel-Buffering": "no"})


# ---------------------------------------------------------------------------------------------- component control (control.py)
async def _ctl_body(request):
    if ctl is None: raise HTTPException(404, "제어 기능이 꺼져 있습니다 (.env DASH_CTL=0)")
    if not request.headers.get("content-type", "").startswith("application/json"): raise HTTPException(415, "JSON만 받습니다")
    origin = request.headers.get("origin")
    if origin and origin.split("://", 1)[-1] != request.headers.get("host"): raise HTTPException(403, "다른 사이트에서 온 요청입니다")
    try: body = await request.json()
    except Exception: body = None
    if not isinstance(body, dict): raise HTTPException(400, "잘못된 요청")
    return body


@app.get("/api/ctl/state")
def api_ctl_state():
    return JSONResponse({"on": False} if ctl is None else ctl.state())


@app.post("/api/ctl/plan")
async def api_ctl_plan(request: Request):
    b = await _ctl_body(request)
    try: return JSONResponse(await run_in_threadpool(ctl.plan, b.get("comp"), b.get("action"), b.get("arg")))
    except controlmod.Refused as e: raise HTTPException(e.code, str(e))


@app.post("/api/ctl/run")
async def api_ctl_run(request: Request):
    b = await _ctl_body(request)
    try: return JSONResponse({"ok": True, "job": await run_in_threadpool(ctl.run, b.get("token"), getattr(request.state, "peer", "?"))})
    except controlmod.Refused as e: raise HTTPException(e.code, str(e))


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
    return {"ok": True, "sim": SIM, "recording": hub.rec_info(), "clients": len(state["clients"]), "https": cfg["tls_port"], "viewer": relay.info(),
            "control": None if ctl is None else {"dry": ctl.dry, "busy": ctl.busy is not None}}
