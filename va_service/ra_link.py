"""ra_link.py (ICNL, 2026-11-12) - the VA service tells the IITP demo Resource Allocator (RA, iitp_demo/ra/ra_main.py) about every
phone frame and asks it which model to run. Local UDP through va_hook.py (a copy of iitp_demo/ra/va_hook.py), default 127.0.0.1:9822.
Off unless VA_RA_HOOK is set: the service then behaves exactly like the DGIST original.

  page / ue_sim.py   POST /detect_frame with headers X-Frame-Id, X-Ue-Id, X-T0-Ms (send time on the SERVER clock = client time + offset)
  RaMiddleware       request headers in       -> frame_start {ue_ip, ue_id, frame_id, bytes = Content-Length, t0_ms, t_hdr_ms}
  (pure ASGI, sees   body chunks arriving     -> progress {received}, at most every VA_RA_PROGRESS_MS (default 5 ms)
   the body as it    last body chunk          -> t1 = upload done (what the RA counts as the end of the UL time)
   arrives)          request over, no done    -> done {status fail, reason http_<code> | no_response} (HTTP error, client gone)
  detect_frame       before the RUN           -> decide, waits at most VA_RA_TIMEOUT_MS (default 5) -> {model, deadline_ms};
                                                 no answer: VA_MODEL and VA_DEADLINE_MS as before
                     after the agent's reply  -> done {status, model, sm, wait/decode/infer/latency ms, t_done_ms}
                     enable_yolo off          -> done {status no_ai}
The UE is named twice (client IP, X-Ue-Id / form ue_id); the RA maps either to an RNTI (ra_main.py --ue-ip / --ue-label).
Without X-Frame-Id the frame gets a service-side id from 1e9 on; without a usable X-T0-Ms the header arrival time stands in for t0
(the UL time the RA sees is then too short by the connection setup and the first bytes).
"""
import itertools
import sys
import threading
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
from va_hook import VaHook, wall_ms   # noqa: E402

PATH = "/detect_frame"
SCOPE_KEY = "icnl_ra"


def _num(s, cast=float):
    try:
        return cast(s)
    except (TypeError, ValueError):
        return None


class RaLink:
    def __init__(self, hook_addr, timeout_ms=5.0, progress_ms=5.0):
        self.addr = hook_addr
        self.hook = VaHook(hook_addr)
        self.timeout = max(0.0, float(timeout_ms)) / 1000.0
        self.progress_ms = float(progress_ms)
        self._ids = itertools.count(1_000_000_000)
        self.lock = threading.Lock()
        self.n = {"frames": 0, "no_frame_id": 0, "no_t0": 0, "progress": 0, "decide": 0, "answered": 0, "no_answer": 0, "done": 0,
                  "done_no_ai": 0, "done_failed_request": 0}
        self.last = None

    def _count(self, k, d=1):
        with self.lock:
            self.n[k] += d

    def _who(self, info):
        return {"ue_ip": info["ue_ip"], "ue_id": info["ue_id"]}

    # ---- request headers in (event loop) ------------------------------------------------------------------------------
    def begin(self, scope, t_hdr_ms):
        h = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers") or []}
        fid = _num(h.get("x-frame-id"), int)
        if fid is None:
            fid = next(self._ids)
            self._count("no_frame_id")
        t0, t0_src = _num(h.get("x-t0-ms")), "page"
        if t0 is None or t0 < t_hdr_ms - 10_000.0:          # missing, or a clock offset gone wrong
            t0, t0_src = t_hdr_ms, "hdr"
            self._count("no_t0")
        elif t0 > t_hdr_ms:                                  # a slightly optimistic offset: the frame cannot start after its headers
            t0 = t_hdr_ms
        client = scope.get("client") or ("", 0)
        info = {"frame_id": fid, "ue_ip": client[0] or "", "ue_id": h.get("x-ue-id", ""), "bytes": _num(h.get("content-length"), int) or 0,
                "t0_ms": t0, "t0_src": t0_src, "t_hdr_ms": t_hdr_ms, "received": 0, "t_prog": t_hdr_ms, "t1_ms": None,
                "decision": None, "decide_ms": None, "done": False}
        self._count("frames")
        self.hook.frame_start(fid, info["bytes"], t0, t_hdr_ms=t_hdr_ms, **self._who(info))
        return info

    # ---- body chunk (event loop) ------------------------------------------------------------------------------------
    def chunk(self, info, nbytes, last, now_ms):
        info["received"] += nbytes
        if last:
            info["t1_ms"] = now_ms
        elif now_ms - info["t_prog"] >= self.progress_ms:
            info["t_prog"] = now_ms
            self.hook.progress(info["frame_id"], info["received"], **self._who(info))
            self._count("progress")

    # ---- before the RUN (worker thread) ---------------------------------------------------------------------------
    def decide(self, info, ue_id, t1_fallback_ms):
        if ue_id and not info["ue_id"]:
            info["ue_id"] = ue_id
        t1 = info["t1_ms"] or t1_fallback_ms
        self._count("decide")
        t = wall_ms()
        d = self.hook.decide(info["frame_id"], info["bytes"], info["t0_ms"], t1, timeout=self.timeout, **self._who(info))
        info["decide_ms"] = round(wall_ms() - t, 3)
        if d is None or d.get("model") is None:
            self._count("no_answer")
            return None
        self._count("answered")
        info["decision"] = {"model": d.get("model"), "deadline_ms": d.get("deadline_ms"), "pred_ms": d.get("pred_ms"),
                            "feasible": d.get("feasible")}
        self.last = dict(info["decision"], frame_id=info["frame_id"], ue_id=info["ue_id"], ue_ip=info["ue_ip"], t_ms=t)
        return info["decision"]

    # ---- after the reply (worker thread) or when the request ends (event loop) -------------------------------------
    def done(self, info, dapp, status=None, kind="done"):
        if info["done"]:
            return
        info["done"] = True
        r = dict(dapp or {})
        if status:
            r["status"] = status
        self.hook.done(info["frame_id"], r, wall_ms(), **self._who(info))
        self._count(kind)

    # ---- what the phone gets back (response field "ra") -----------------------------------------------------------
    @staticmethod
    def public(info):
        if info is None:
            return None
        d = info["decision"] or {}
        t1 = info["t1_ms"]
        return {"frame_id": info["frame_id"], "answered": bool(info["decision"]), "model": d.get("model"),
                "deadline_ms": d.get("deadline_ms"), "pred_ms": d.get("pred_ms"), "feasible": d.get("feasible"),
                "t0_src": info["t0_src"], "ul_ms": round(t1 - info["t0_ms"], 3) if t1 else None, "decide_ms": info["decide_ms"]}

    def status(self):
        with self.lock:
            n = dict(self.n)
        return {"hook": self.addr, "timeout_ms": self.timeout * 1000.0, "progress_ms": self.progress_ms, "counters": n, "last": self.last}


class RaMiddleware:
    """pure ASGI (BaseHTTPMiddleware would hide the body chunks): only POST /detect_frame, everything else passes untouched"""

    def __init__(self, app, link):
        self.app, self.link = app, link

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http" or scope.get("path") != PATH or scope.get("method") != "POST":
            await self.app(scope, receive, send)
            return
        link = self.link
        info = link.begin(scope, wall_ms())
        scope[SCOPE_KEY] = info

        async def rx():
            m = await receive()
            if m.get("type") == "http.request":
                link.chunk(info, len(m.get("body") or b""), not m.get("more_body", False), wall_ms())
            return m

        async def tx(m):
            if m.get("type") == "http.response.start":
                info["http_status"] = m.get("status")
            await send(m)

        try:
            await self.app(scope, rx, tx)
        finally:
            if not info["done"]:
                st = info.get("http_status")
                link.done(info, {"reason": f"http_{st}" if st else "no_response"}, status="fail", kind="done_failed_request")
