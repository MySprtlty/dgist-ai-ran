#!/usr/bin/env python3
"""ra_stub.py (ICNL, 2026-11-11): stand-in for the Resource Allocator on the Slicing link (INTERFACE.md), for testing the Slicing
xApp (PPO, PPO_OUTPUT=ra) without the RA and without touching the MAC. It reports the connected UEs given on the command line (or
VA_RNTIS / NONVA_RNTIS as xapp.sh exports them), "applies" every valid budget after --apply-ms and associates the budget's RNTIs,
sends ra_state every 10 ms and a synthetic ra_qos every 100 ms, and logs every budget to csv.

  ra_stub.py [--va-rntis 4601,7a3c --nonva-rntis b210,c0de] [--apply-ms 12] [--mode normal|reject|silent|pending] [--csv F]
             [--duration S] [--listen 127.0.0.1:9820] [--slicing 127.0.0.1:9821]
  modes: normal  = apply after --apply-ms (what the RA should do)
         reject  = every budget after the first is rejected (the Slicing xApp must log NOT_CONFIRMED and keep the old quota)
         pending = budgets after the first stay pending (never confirmed)
         silent  = stop sending ra_state after the first budget is applied (the Slicing xApp must HOLD after 200 ms)
"""
import argparse
import csv
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ra_slicing_link import RaSide, check_budget, wall_ms  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--va-rntis", default=os.environ.get("VA_RNTIS", ""))
ap.add_argument("--nonva-rntis", default=os.environ.get("NONVA_RNTIS", ""))
ap.add_argument("--apply-ms", type=float, default=12.0, help="pending -> applied delay (the MAC's next Slice SM report)")
ap.add_argument("--mode", default="normal", choices=["normal", "reject", "silent", "pending"])
ap.add_argument("--csv"); ap.add_argument("--duration", type=float, default=0.0)
ap.add_argument("--listen"); ap.add_argument("--slicing")
ap.add_argument("--quiet", action="store_true")
a = ap.parse_args()

ues = {r.lower(): 0 for r in (a.va_rntis.split(",") + a.nonva_rntis.split(",")) if r}   # rnti -> svc (0 until associated)
link = RaSide(a.listen, a.slicing)
print(f"[RA-STUB] listening {link.sock.getsockname()}, Slicing xApp at {link.peer}, mode {a.mode}, apply {a.apply_ms} ms, "
      f"UEs {list(ues) or 'none (taken from the first budget)'}", flush=True)
w = None
if a.csv:
    f = open(a.csv, "w", newline="")
    w = csv.writer(f)
    w.writerow(["rx_ms", "run", "seq", "va_prb", "nonva_prb", "status", "reason", "applied_ms"])

run = seq = 0; status = "none"; reason = ""
applied = [0, 0]; pending = None   # (apply_at_ms, va, nonva, budget msg)
last_seq = {}                      # run -> last processed seq
n_budget = 0; t0 = time.time(); next_state = next_qos = 0.0; silent = False


def log(*x):
    if not a.quiet:
        print(*x, flush=True)


while a.duration <= 0 or time.time() - t0 < a.duration:
    now = time.time()
    wake = min(next_state, next_qos, pending[0] / 1000.0 if pending else float("inf"))
    m = link.recv(timeout=max(0.0005, wake - now) if next_state else 0.001)
    if silent:   # mode silent: the RA has gone quiet - read and drop everything
        continue
    if m and m["type"] == "budget":
        r, s = int(m.get("run", 0)), int(m.get("seq", 0))
        if s <= last_seq.get(r, 0):
            log(f"[RA-STUB] old budget run {r} seq {s} ignored")
        else:
            last_seq[r] = s; run, seq = r, s; n_budget += 1
            ok, why = check_budget(m)
            if ok and a.mode == "reject" and n_budget > 1:
                ok, why = False, "stub mode reject"
            if not ok:
                status, reason = "rejected", why
            else:
                status, reason = "pending", ""
                apply_at = float("inf") if (a.mode == "pending" and n_budget > 1) else wall_ms() + a.apply_ms
                pending = (apply_at, int(m["va_prb"]), int(m["nonva_prb"]), m)
            log(f"[RA-STUB] budget run {r} seq {s} {m.get('va_prb')}:{m.get('nonva_prb')} -> {status} {reason}")
            if w:
                w.writerow([m["_rx_ms"], r, s, m.get("va_prb"), m.get("nonva_prb"), status, reason, ""]); f.flush()
            if not silent:
                link.send_state(run, seq, status, applied[0], applied[1], ues.items(), reason)
    elif m and m["type"] == "bye":
        log(f"[RA-STUB] bye from run {m.get('run')}: keeping {applied[0]}:{applied[1]}")
    if pending and wall_ms() >= pending[0]:
        _, va, nv, b = pending; pending = None
        applied = [va, nv]
        for r in b.get("va_rntis", []): ues[r.lower()] = 1
        for r in b.get("nonva_rntis", []): ues[r.lower()] = 2
        status = "applied"
        log(f"[RA-STUB] applied {va}:{nv} (seq {seq})")
        if w:
            w.writerow([wall_ms(), run, seq, va, nv, "applied", "", wall_ms() - b["_rx_ms"]]); f.flush()
        link.send_state(run, seq, status, applied[0], applied[1], ues.items())   # right after processing (INTERFACE.md 4.2)
        if a.mode == "silent":
            silent = True
            log("[RA-STUB] silent from now on")
            continue
    now = time.time()
    if now >= next_state:
        next_state = now + 0.010
        if not silent:
            link.send_state(run, seq, status, applied[0], applied[1], ues.items(), reason)
    if now >= next_qos:
        next_qos = now + 0.100
        if not silent:
            link.send_qos(100, va={"requests": 0, "on_time": 0, "deadline_ms": 100, "e2e_p50_ms": None, "net_p50_ms": None, "proc_p50_ms": None},
                          nonva={"requests": 0, "on_time": 0, "deadline_ms": 0, "e2e_p50_ms": None, "net_p50_ms": None, "proc_p50_ms": None},
                          ues=[{"rnti": r, "svc": s, "model": None, "floor_prb": 0, "sm": None} for r, s in ues.items()])
