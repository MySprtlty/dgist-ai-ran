#!/usr/bin/env python3
"""Viewer relay of the dashboard (2026-11-14): the four screens of https://<host>:8000/viewer inside the dashboard's "뷰어" tab.

The dashboard's page cannot read the VA / non-VA services itself (other origin, no CORS), and those services are not to be changed or
restarted for this. So this module reads, over LOOPBACK, exactly what one /viewer browser reads and hands it to the dashboard's pages:

  VA pane      GET https://127.0.0.1:<VA_PORT>/viewer_frame/<ue>?seq=&stat=&wait=1000      long poll: JPEG + X-VA-Meta, or 204 + X-VA-Meta
  non-VA pane  GET https://127.0.0.1:<NONVA_PORT>/video_feed?session_id=<ue>               MJPEG parts with X-NV-Meta (only while the UE sends)
               GET https://127.0.0.1:<NONVA_PORT>/receiver_stats?session_id=<ue>           once per second

The verdicts (met / missed, the frozen picture) are made by the services; the relay copies them unchanged. The upstream requests run only
while at least one dashboard page reads /api/viewer/stream (the tab is open or a capture includes the screens), are shared by all pages, and
stop a few seconds after the last page left. Nothing is sent to a UE: no radio downlink is added (the dashboard is not served on the UE net).

One page reads ONE multiplexed binary stream. Record = [u32 BE header length][header JSON utf-8][u32 BE body length][body (JPEG or empty)].
Header: {"k": "hello"|"ka"} or {"pane": "va1"|"va2"|"nv1"|"nv2", "k": kind, ...}:
  "c"  {"ok": bool, "why": text}     the relay reaches the VA service for this pane or not            (VA panes)
  "p"  {"meta": {...}} + JPEG        a picture with its status                                         (both)
  "m"  {"meta": {...}}               a status without a new picture (VA: 204; non-VA: part with pic=false) (both)
  "s"  {"d": {...}}                  receiver statistics, d.ok = false when there is no session / no service (non-VA panes)
A page that cannot keep up gets the newest record of each kind and pane, never a backlog.
"""
import asyncio
import http.client
import json
import socket
import ssl
import struct
import threading
import time

PANES = (("va1", "va", "ue1"), ("va2", "va", "ue2"), ("nv1", "nv", "ue1"), ("nv2", "nv", "ue2"))
KINDS = ("c", "p", "m", "s")
IDLE_S = 3.0                      # upstream requests stop this long after the last page left
J = dict(ensure_ascii=False, separators=(",", ":"))
if getattr(http.client, "_MAXLINE", 1 << 20) < (1 << 20): http.client._MAXLINE = 1 << 20      # X-VA-Meta carries every box of a crowded picture in one header line


def record(head, body=b""):
    h = json.dumps(head, **J).encode("utf-8")
    return struct.pack(">I", len(h)) + h + struct.pack(">I", len(body)) + body


def _addr(text, port):
    text = (text or "").strip()
    if not text: return ("127.0.0.1", int(port))
    h, _, p = text.rpartition(":")
    return (h or "127.0.0.1", int(p))


class Client:
    """one page: the newest record per (pane, kind)"""
    def __init__(self):
        self.slots = {}; self.ev = asyncio.Event(); self.closed = False

    def put(self, pane, kind, rec):
        if kind == "p": self.slots.pop((pane, "m"), None)          # the picture's own status is newer than a waiting status
        self.slots[(pane, kind)] = rec; self.ev.set()

    def take(self):
        out = []
        for pane, _, _ in PANES:
            for kind in KINDS:
                r = self.slots.pop((pane, kind), None)
                if r is not None: out.append(r)
        self.ev.clear()
        return out


class _Gen:
    """one period with pages attached: its threads end when stop is set"""
    def __init__(self):
        self.stop = threading.Event(); self.conns = set(); self.lock = threading.Lock(); self.want = {p: threading.Event() for p, _, _ in PANES}


class Relay:
    def __init__(self, cfg, log, sim=False):
        v = cfg.get("viewer") or {}
        self.log = log; self.loop = None; self.clients = set(); self.last = {}; self.gen = None; self.idle = None; self.n = 0
        # the simulation never reads the real services: there the relay runs only against addresses given on purpose (tests)
        self.va = _addr(v.get("va_addr"), cfg["va"]["port"]) if (v.get("va_addr") or not sim) and v.get("on", True) else None
        self.nv = _addr(v.get("nv_addr"), cfg["nonva"]["port"]) if (v.get("nv_addr") or not sim) and v.get("nonva", True) else None
        self.on = bool(v.get("relay", True)) and (self.va is not None or self.nv is not None)
        self.ctx = ssl.create_default_context(); self.ctx.check_hostname = False; self.ctx.verify_mode = ssl.CERT_NONE      # loopback, self-signed (as sources.FeedPoller)

    def info(self):
        return {"on": self.on, "va": self.va is not None, "nv": self.nv is not None, "pages": len(self.clients), "reading": self.gen is not None}

    # ------------------------------------------------------------------ event-loop side
    def attach(self):
        c = Client(); self.clients.add(c)
        if self.idle is not None: self.idle.cancel(); self.idle = None
        if self.gen is None: self._start()
        else:                                                      # a page that joins later: what the others already show
            now = time.monotonic()
            for pane, _, _ in PANES:
                p = self.last.get((pane, "p")); m = self.last.get((pane, "m"))
                for kind in KINDS:
                    x = self.last.get((pane, kind))
                    if x is None or (kind == "m" and p is not None and m[0] < p[0]): continue
                    c.put(pane, kind, record(dict(x[1], age=round((now - x[3]) * 1000.0)), x[2]))
        return c

    def detach(self, c):
        self.clients.discard(c)
        if not self.clients and self.gen is not None and self.idle is None:
            self.idle = self.loop.call_later(IDLE_S, self._idle)

    def _idle(self):
        self.idle = None
        if not self.clients: self._stop()

    def _start(self):
        g = self.gen = _Gen(); self.last = {}
        for pane, kind, ue in PANES:
            if kind == "va" and self.va is not None: fns = (self._va_loop,)
            elif kind == "nv" and self.nv is not None: fns = (self._nv_stats, self._nv_stream)
            else: fns = ()
            for fn in fns: threading.Thread(target=fn, args=(g, pane, ue), name="viewer-%s" % pane, daemon=True).start()
        self.log("viewer relay: reading (VA %s, non-VA %s)" % (self.va and "%s:%d" % self.va, self.nv and "%s:%d" % self.nv))

    def _stop(self):
        g, self.gen = self.gen, None
        if g is None: return
        g.stop.set()
        with g.lock: conns = list(g.conns)
        for c in conns:                                            # wakes a thread that waits in a read
            try: c.sock.shutdown(socket.SHUT_RDWR)
            except Exception: pass
        self.last = {}
        self.log("viewer relay: stopped (no page reads it)")

    def close(self):
        if self.idle is not None: self.idle.cancel(); self.idle = None
        self._stop()
        for c in list(self.clients): c.closed = True; c.ev.set()

    def _deliver(self, g, pane, kind, head, body, rec):
        if g is not self.gen: return
        self.n += 1; self.last[(pane, kind)] = (self.n, head, body, time.monotonic())
        for c in self.clients: c.put(pane, kind, rec)

    # ------------------------------------------------------------------ upstream threads
    def _pub(self, g, pane, kind, head, body=b""):
        head = dict(head, pane=pane, k=kind)
        try: self.loop.call_soon_threadsafe(self._deliver, g, pane, kind, head, body, record(head, body))
        except RuntimeError: pass                                  # the loop is closing

    def _open(self, g, addr, timeout):
        c = http.client.HTTPSConnection(addr[0], addr[1], timeout=timeout, context=self.ctx)
        with g.lock: g.conns.add(c)
        return c

    def _drop(self, g, c):
        if c is None: return
        with g.lock: g.conns.discard(c)
        try: c.close()
        except Exception: pass

    def _va_loop(self, g, pane, ue):
        """the viewer page's loop(): long poll, 200 = picture + status, 204 = status"""
        conn = None; seq = stat = 0; ok = None
        while not g.stop.is_set():
            try:
                if conn is None: conn = self._open(g, self.va, 8.0)
                conn.request("GET", "/viewer_frame/%s?seq=%d&stat=%d&wait=1000" % (ue, seq, stat), headers={"Cache-Control": "no-store"})
                r = conn.getresponse(); body = r.read()
                if r.status not in (200, 204): raise OSError("HTTP %d" % r.status)
                meta = json.loads(r.getheader("X-VA-Meta") or "{}")
                if ok is not True: ok = True; self._pub(g, pane, "c", {"ok": True, "why": ""})
                if "stat_seq" in meta: stat = int(meta["stat_seq"])
                if r.status == 200 and body:
                    seq = int(meta.get("seq") or 0); self._pub(g, pane, "p", {"meta": meta}, body)
                else: self._pub(g, pane, "m", {"meta": meta})
            except Exception as e:
                self._drop(g, conn); conn = None; seq = stat = 0   # a restarted service counts from 0 again
                if g.stop.is_set(): break
                if ok is not False: ok = False; self._pub(g, pane, "c", {"ok": False, "why": str(e)[:80]})
                g.stop.wait(0.7)
        self._drop(g, conn)

    def _nv_stats(self, g, pane, ue):
        """the viewer page's nonvaLoop(): statistics once per second; an active UE asks for the picture stream"""
        conn = None
        while not g.stop.is_set():
            t = time.monotonic()
            try:
                if conn is None: conn = self._open(g, self.nv, 1.5)
                conn.request("GET", "/receiver_stats?session_id=" + ue)
                r = conn.getresponse(); body = r.read()
                if r.status != 200: d = {"ok": False, "reason": "HTTP %d" % r.status}          # 404: no session yet
                else: d = json.loads(body); d["ok"] = True
            except Exception as e:
                self._drop(g, conn); conn = None; d = {"ok": False, "reason": str(e)[:120]}
                if g.stop.is_set(): break
            if d.get("ok") and d.get("active"): g.want[pane].set()
            self._pub(g, pane, "s", {"d": d})
            g.stop.wait(max(0.05, 1.0 - (time.monotonic() - t)))
        self._drop(g, conn)

    def _nv_stream(self, g, pane, ue):
        """the viewer page's nonvaStream(): the MJPEG parts. The service sends a part only for a new picture or a changed verdict, so the
        stream is silent while every frame misses its deadline; it ends when the UE stops. Opened again at most once per 2 s."""
        want = g.want[pane]
        while not g.stop.is_set():
            if not want.wait(1.0): continue
            conn = None; t0 = time.monotonic()
            try:
                conn = self._open(g, self.nv, 20.0)
                conn.request("GET", "/video_feed?session_id=" + ue)
                r = conn.getresponse()
                if r.status != 200: r.read(); raise OSError("HTTP %d" % r.status)
                while not g.stop.is_set():
                    line = r.readline(1 << 16)
                    if not line: break                             # the stream ended
                    if not line.strip().startswith(b"--"): continue
                    n = -1; meta = None
                    while True:
                        h = r.readline(1 << 20)
                        if not h: raise EOFError("stream ended inside a part")
                        h = h.strip()
                        if not h: break
                        k, _, v = h.partition(b":"); k = k.strip().lower()
                        if k == b"content-length": n = int(v)
                        elif k == b"x-nv-meta":
                            try: meta = json.loads(v.decode("ascii", "replace"))
                            except ValueError: meta = None
                    if n < 0: raise ValueError("part without Content-Length")
                    jpeg = r.read(n)
                    if len(jpeg) < n: break
                    if meta is not None and meta.get("pic") is False: self._pub(g, pane, "m", {"meta": meta})
                    else: self._pub(g, pane, "p", {"meta": meta}, jpeg)
            except Exception:
                pass                                               # read timeout during a long freeze, service restart: the next round opens it again
            finally:
                self._drop(g, conn)
            want.clear(); g.stop.wait(max(0.0, 2.0 - (time.monotonic() - t0)))
