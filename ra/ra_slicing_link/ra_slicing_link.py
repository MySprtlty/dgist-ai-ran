#!/usr/bin/env python3
"""ra_slicing_link.py (ICNL, 2026-11-11): the Network Slicing xApp <-> Resource Allocator link of the IITP 6G demo (INTERFACE.md, v1).

UDP/IPv4, one datagram = one JSON object.  Slicing -> RA: "budget", "bye".  RA -> Slicing: "ra_state" (10 ms), "ra_qos" (100 ms).

RA side (the Resource Allocator):
    link = RaSide()                                   # listen 127.0.0.1:9820, Slicing xApp at 127.0.0.1:9821
    msg = link.recv(timeout=0.01)                     # dict (budget / bye) or None
    ok, reason = check_budget(msg, total=273)
    link.send_state(run, budget_seq, "applied", 150, 123, [("4601", 1), ...])
    link.send_qos(100, va={...}, nonva={...}, ues=[...])

Slicing side in Python (tests and tools; the PPO xApp itself uses ra_slicing_link.h):
    s = SlicingSide(); s.send_budget(run, seq, 150, 123, ["4601", "7a3c"], ["b210", "c0de"]); s.recv(0.01)
"""
import json
import os
import socket
import time

VERSION = 1
TOTAL_PRB = 273
DEFAULT_RA = ("127.0.0.1", 9820)
DEFAULT_SLICING = ("127.0.0.1", 9821)
MAX_DGRAM = 1400


def wall_ms():
    return time.time_ns() // 1_000_000


def hostport(s, default):
    """'host:port' -> (host, port); None/'' -> default"""
    if not s:
        return default
    h, _, p = s.rpartition(":")
    return (h or default[0], int(p))


def _dumps(obj):
    b = json.dumps(obj, separators=(",", ":")).encode()
    if len(b) > MAX_DGRAM:
        raise ValueError(f"datagram {len(b)} B > {MAX_DGRAM} B")
    return b


def check_budget(msg, total=TOTAL_PRB):
    """INTERFACE.md 4.2: (True, '') if the budget can be applied, else (False, reason)."""
    try:
        va, nv, tot, mn = int(msg["va_prb"]), int(msg["nonva_prb"]), int(msg["total"]), int(msg.get("min_prb", 1))
        run, seq = int(msg["run"]), int(msg["seq"])
    except (KeyError, TypeError, ValueError) as e:
        return False, f"bad field: {e}"
    if seq < 1 or run < 0:
        return False, "seq must be >= 1"
    if tot != total:
        return False, f"total {tot} != cell {total}"
    if va + nv != tot:
        return False, f"va_prb + nonva_prb = {va + nv} != total {tot}"
    if va < mn or nv < mn:
        return False, f"a service below min_prb {mn}"
    rn = [str(r).lower() for r in list(msg.get("va_rntis", [])) + list(msg.get("nonva_rntis", []))]
    if len(rn) != len(set(rn)):
        return False, "duplicate RNTI"
    return True, ""


class _Udp:
    def __init__(self, listen, peer):
        self.peer = peer
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(listen)

    def _send(self, obj):
        obj.setdefault("v", VERSION)
        obj.setdefault("t_ms", wall_ms())
        self.sock.sendto(_dumps(obj), self.peer)

    def recv(self, timeout=0.0):
        """one message dict from the peer host, or None on timeout / foreign or malformed datagram"""
        self.sock.settimeout(timeout if timeout and timeout > 0 else None)
        if not timeout or timeout <= 0:
            self.sock.setblocking(False)
        try:
            data, src = self.sock.recvfrom(MAX_DGRAM + 1)
        except (BlockingIOError, socket.timeout):
            return None
        if src[0] != self.peer[0]:
            return None
        try:
            m = json.loads(data)
        except ValueError:
            return None
        if not isinstance(m, dict) or m.get("v") != VERSION or "type" not in m:
            return None
        m["_rx_ms"] = wall_ms()
        return m

    def fileno(self):
        return self.sock.fileno()

    def close(self):
        self.sock.close()


class RaSide(_Udp):
    """The Resource Allocator end: receives budget / bye, sends ra_state / ra_qos."""

    def __init__(self, listen=None, slicing=None):
        super().__init__(hostport(listen or os.environ.get("RA_SLICING_LISTEN"), DEFAULT_RA),
                         hostport(slicing or os.environ.get("RA_PPO_ADDR"), DEFAULT_SLICING))
        self.state_seq = 0

    def send_state(self, run, budget_seq, status, applied_va, applied_nonva, ues, reason=""):
        """ues: iterable of (rnti hex str, svc int). status: none | pending | applied | rejected."""
        self.state_seq += 1
        self._send({"type": "ra_state", "v": VERSION, "t_ms": wall_ms(), "src": "ra", "seq": self.state_seq,
                    "run": int(run), "budget_seq": int(budget_seq), "status": status, "reason": reason,
                    "applied": {"va": int(applied_va), "nonva": int(applied_nonva)},
                    "ues": [{"rnti": str(r).lower(), "svc": int(s)} for r, s in ues]})

    def send_qos(self, window_ms, va, nonva, ues=()):
        """va / nonva: dict with requests, on_time, deadline_ms, e2e_p50_ms, net_p50_ms, proc_p50_ms (None = null);
        ues: dicts with rnti, svc, model, floor_prb, sm."""
        self._send({"type": "ra_qos", "v": VERSION, "t_ms": wall_ms(), "src": "ra", "window_ms": int(window_ms),
                    "va": va, "nonva": nonva, "ues": list(ues)})


class SlicingSide(_Udp):
    """The Slicing xApp end in Python (tests, tools): sends budget / bye, receives ra_state / ra_qos."""

    def __init__(self, listen=None, ra=None):
        super().__init__(hostport(listen or os.environ.get("PPO_RA_LISTEN"), DEFAULT_SLICING),
                         hostport(ra or os.environ.get("PPO_RA_ADDR"), DEFAULT_RA))

    def send_budget(self, run, seq, va_prb, nonva_prb, va_rntis, nonva_rntis, total=TOTAL_PRB, min_prb=5):
        self._send({"type": "budget", "v": VERSION, "t_ms": wall_ms(), "src": "ppo", "run": int(run), "seq": int(seq),
                    "va_prb": int(va_prb), "nonva_prb": int(nonva_prb), "total": int(total), "min_prb": int(min_prb),
                    "va_rntis": [str(r).lower() for r in va_rntis], "nonva_rntis": [str(r).lower() for r in nonva_rntis]})

    def send_bye(self, run):
        self._send({"type": "bye", "v": VERSION, "t_ms": wall_ms(), "src": "ppo", "run": int(run)})
