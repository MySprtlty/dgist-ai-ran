#!/usr/bin/env python3
"""va_hook.py (ICNL, 2026-11-11): what the VA service (or a stand-in) tells the Resource Allocator about each frame, over local
UDP to ra_main.py (127.0.0.1:9822, one JSON object per datagram). docs/iitp_demo.md 7.3.

    hook = VaHook()
    hook.frame_start(frame_id, nbytes, t0_ms, ue_ip="10.45.0.2", ue_id="ue1")    # request headers in: the upload has begun
    hook.progress(frame_id, received_bytes, ue_ip=..., ue_id=...)                # optional, while the body arrives
    d = hook.decide(frame_id, nbytes, t0_ms, t1_ms, ue_ip=..., ue_id=...)        # whole frame in: {"model": "26n", "deadline_ms": 41.2, ...}
    ... RUN to the dApp agent with d["model"] and qos.deadline_ms = d["deadline_ms"] ...
    hook.done(frame_id, result, t_done_ms, ue_ip=..., ue_id=...)                 # the agent's result dict

decide() waits at most `timeout` (default 5 ms) and returns None without an answer: the caller keeps its default model.
t0_ms is the UE's send time on the server clock (the page's clock offset applied). rnti (hex, optional): when the sender
knows the UE's C-RNTI (the VA receiver learns it from the gNB tap's BSR / MAC reports), the RA uses it if its --ue-ip /
--ue-label maps do not name the UE. Thread-safe.
"""
import json
import os
import socket
import threading
import time


def wall_ms():
    return time.time_ns() / 1e6


class VaHook:
    def __init__(self, ra=None):
        h, _, p = (ra or os.environ.get("RA_HOOK") or "127.0.0.1:9822").rpartition(":")
        self.ra = (h, int(p))
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.lock = threading.Lock()
        self.req = 0

    def _send(self, obj):
        obj["v"] = 1
        try:
            self.sock.sendto(json.dumps(obj, separators=(",", ":")).encode(), self.ra)
        except OSError:
            pass

    def frame_start(self, frame_id, nbytes, t0_ms, ue_ip="", ue_id="", t_hdr_ms=None, rnti=""):
        self._send({"type": "frame_start", "frame_id": int(frame_id), "bytes": int(nbytes), "t0_ms": float(t0_ms),
                    "t_hdr_ms": float(t_hdr_ms if t_hdr_ms is not None else wall_ms()), "ue_ip": ue_ip, "ue_id": ue_id, "rnti": rnti})

    def progress(self, frame_id, received, ue_ip="", ue_id="", rnti=""):
        self._send({"type": "progress", "frame_id": int(frame_id), "received": int(received), "ue_ip": ue_ip, "ue_id": ue_id, "rnti": rnti})

    def decide(self, frame_id, nbytes, t0_ms, t1_ms, ue_ip="", ue_id="", timeout=0.005, rnti=""):
        with self.lock:
            self.req += 1
            req = self.req
            self._send({"type": "decide", "req": req, "frame_id": int(frame_id), "bytes": int(nbytes), "t0_ms": float(t0_ms),
                        "t1_ms": float(t1_ms), "ue_ip": ue_ip, "ue_id": ue_id, "rnti": rnti})
            end = time.time() + timeout
            while True:
                left = end - time.time()
                if left <= 0:
                    return None
                self.sock.settimeout(left)
                try:
                    data, _ = self.sock.recvfrom(2048)
                except (socket.timeout, OSError):
                    return None
                try:
                    m = json.loads(data)
                except ValueError:
                    continue
                if m.get("type") == "decision" and m.get("req") == req:
                    return m

    def done(self, frame_id, result, t_done_ms, ue_ip="", ue_id="", rnti=""):
        r = result or {}
        self._send({"type": "done", "frame_id": int(frame_id), "status": r.get("status"), "reason": r.get("reason", ""),
                    "model": r.get("model"), "sm": r.get("sm"), "wait_ms": r.get("wait_ms"), "decode_ms": r.get("decode_ms"),
                    "infer_ms": r.get("infer_ms"), "latency_ms": r.get("latency_ms"), "t_done_ms": float(t_done_ms),
                    "ue_ip": ue_ip, "ue_id": ue_id, "rnti": rnti})
