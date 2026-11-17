#!/usr/bin/env python3
"""fake_dapp_agent.py (ICNL, 2026-11-11, test only): a stand-in for the DGIST dApp agent (TCP 9810, JSON lines + JPEG bytes,
dapp_hook/docs/XAPP_INTERFACE.md v1: hello / welcome, estimate push, run / result, get). One GPU worker serves the RUNs FIFO.

Server time of a model = decode + inference at the SM count YOLO gets. Inference at 80 SM and the decode are DGIST's real-L1
4K numbers (dapp_hook/cots/MANUAL.md:107: 26n 5.7, 26m 14.9, 26x 32.3 ms inference, about 6 ms decode); 26s and 26l are
interpolated (TEST ONLY). Other SM counts scale as (80 / sm)^0.7. The SM count YOLO gets follows the RAN load the UL simulator
writes (va_sim.py, --sim-file: "tb_slot_bits"), like the DGIST FAPI-aware split: PUSCH 16 .. 56 SM with the slot TB, PDSCH 24,
8 spare, YOLO the rest snapped down to the classes 24 .. 80.

  fake_dapp_agent.py [--port 9810] [--sim-file F] [--sm 80 (fixed when no sim file)]
"""
import argparse
import json
import queue
import socket
import threading
import time

INF80 = {"26n": 5.7, "26s": 9.0, "26m": 14.9, "26l": 21.0, "26x": 32.3}
DEC_MS = 6.0
CLASSES = [24, 40, 48, 56, 64, 72, 80]
GPU_SM = 132

ap = argparse.ArgumentParser()
ap.add_argument("--port", type=int, default=9810); ap.add_argument("--sim-file", default="")
ap.add_argument("--sm", type=int, default=80); ap.add_argument("--max-tb-bits", type=float, default=273 * 1000.0)
ap.add_argument("--max-queue", type=int, default=8)
a = ap.parse_args()

lock = threading.Lock()
clients = []          # dicts: sock, role, every, est_count, wlock
work = queue.Queue()
waiting = []          # models of the queued RUNs (for the deadline check)
busy_until = [0.0]    # wall s when the worker is free
state = {"sm": a.sm, "sim_t": 0.0}


def now_ns():
    return time.time_ns()


def yolo_sm():
    if not a.sim_file:
        return a.sm
    try:
        with open(a.sim_file) as f:
            s = json.load(f)
        tb = float(s.get("tb_slot_bits", 0.0))
    except (OSError, ValueError):
        return state["sm"]
    pusch = 16 + 40 * min(1.0, tb / a.max_tb_bits)
    free = GPU_SM - pusch - 24 - 8
    return max([c for c in CLASSES if c <= free] or [CLASSES[0]])


def server_ms(model, sm):
    return DEC_MS + INF80[model] * (80.0 / sm) ** 0.7


def send(c, obj):
    data = (json.dumps(obj, separators=(",", ":")) + "\n").encode()
    with c["wlock"]:
        try:
            c["sock"].sendall(data)
        except OSError:
            pass


def estimate_msg():
    sm = state["sm"]
    with lock:
        queued = sum(server_ms(q, sm) for q in waiting)
    wait = max(0.0, busy_until[0] - time.time()) * 1000.0 + queued
    return {"type": "estimate", "t_ns": now_ns(), "sfn": 0, "slot": 0, "ul_sm": GPU_SM - 24 - 8 - sm, "dl_sm": 24, "yolo_sm_now": sm,
            "yolo_sm_min": sm, "queue": work.qsize(), "inflight": 1 if busy_until[0] > time.time() else 0,
            "est_ms": {m: round(wait + server_ms(m, sm), 2) for m in INF80}, "est_sm": {m: sm for m in INF80}}


def pusher():
    slot = 0
    while True:
        time.sleep(0.0005)
        slot += 1
        if slot % 20 == 0:
            state["sm"] = yolo_sm()
        with lock:
            cs = list(clients)
        e = None
        for c in cs:
            if c.get("sub_est") and slot % c["every"] == 0:
                e = e or estimate_msg()
                send(c, e)


def worker():
    while True:
        c, req, t_recv = work.get()
        with lock:
            if req["model"] in waiting:
                waiting.remove(req["model"])
        sm = state["sm"]
        t_start = time.time()
        dur = server_ms(req["model"], sm) / 1000.0
        busy_until[0] = t_start + dur
        time.sleep(dur)
        t_end = time.time()
        send(c, {"type": "result", "req_id": req.get("req_id"), "status": "success", "model": req["model"], "sm": sm, "sm_mean": float(sm),
                 "t_recv_ns": int(t_recv * 1e9), "t_start_ns": int(t_start * 1e9), "t_end_ns": int(t_end * 1e9),
                 "wait_ms": round((t_start - t_recv) * 1000, 2), "decode_ms": DEC_MS, "infer_ms": round(dur * 1000 - DEC_MS, 2),
                 "latency_ms": round((t_end - t_recv) * 1000, 2), "detections": 2 if req.get("want_dets") else 0, "tiles": 8,
                 **({"dets": [{"cls": 0, "name": "person", "conf": 0.9, "xyxy": [600, 200, 1300, 1000]},
                              {"cls": 2, "name": "car", "conf": 0.6, "xyxy": [100, 600, 500, 900]}]} if req.get("want_dets") else {})})


def serve(sock):
    c = {"sock": sock, "role": "", "every": 1, "sub_est": False, "wlock": threading.Lock()}
    with lock:
        clients.append(c)
    buf = b""
    try:
        while True:
            chunk = sock.recv(1 << 16)
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                line, rest = buf.split(b"\n", 1)
                try:
                    m = json.loads(line)
                except ValueError:
                    buf = rest
                    continue
                if m.get("type") == "run":
                    n = int(m.get("bytes", 0))
                    while len(rest) < n:
                        more = sock.recv(1 << 16)
                        if not more:
                            return
                        rest += more
                    buf = rest[n:]
                    t_recv = time.time()
                    dl = float((m.get("qos") or {}).get("deadline_ms", 0) or 0)
                    sm = state["sm"]
                    with lock:   # like the DGIST agent: the waiting requests' own times, then this one
                        queued = sum(server_ms(q, sm) for q in waiting)
                    pred = max(0.0, busy_until[0] - t_recv) * 1000 + queued + server_ms(m.get("model", "26x"), sm)
                    if work.qsize() >= a.max_queue or (dl > 0 and pred > dl):
                        send(c, {"type": "result", "req_id": m.get("req_id"), "status": "fail", "reason": "overload", "model": m.get("model")})
                    else:
                        m.setdefault("model", "26x")
                        with lock:
                            waiting.append(m["model"])
                        work.put((c, m, t_recv))
                    continue
                buf = rest
                if m.get("type") == "hello":
                    c["role"], c["every"] = m.get("role", ""), max(1, int(m.get("every", 1)))
                    c["sub_est"] = "estimate" in m.get("subscribe", [])
                    with lock:   # a returning role replaces the older connection of the same role
                        for o in clients:
                            if o is not c and o["role"] == c["role"]:
                                try:
                                    o["sock"].shutdown(socket.SHUT_RDWR)
                                except OSError:
                                    pass
                    send(c, {"type": "welcome", "version": 1, "models": list(INF80), "slot_us": 500, "cells": 1, "tdd": "DDDDDDSUUU",
                             "gpu_sm": GPU_SM, "input": "4k"})
                elif m.get("type") == "get" and m.get("what") == "estimate":
                    send(c, estimate_msg())
    except OSError:
        pass
    finally:
        with lock:
            if c in clients:
                clients.remove(c)
        sock.close()


threading.Thread(target=pusher, daemon=True).start()
threading.Thread(target=worker, daemon=True).start()
srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
srv.bind(("127.0.0.1", a.port))
srv.listen(8)
print(f"[FAKE-DAPP] listening 127.0.0.1:{a.port}, sim file {a.sim_file or '-'}, server ms at 80 SM {{{', '.join(f'{m}: {server_ms(m, 80):.1f}' for m in INF80)}}}", flush=True)
while True:
    s, _ = srv.accept()
    s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    threading.Thread(target=serve, args=(s,), daemon=True).start()
