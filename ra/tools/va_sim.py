#!/usr/bin/env python3
"""va_sim.py (ICNL, 2026-11-11, test only): phones + VA service + the MAC's UL PRB split, in real time, for testing the Resource
Allocator with FlexRIC's emulated gNB (ra/test/emu/).

Every UL slot (600 per s, simulated in 5 ms steps) the PRBs are split as the lab's MAC does with the count quotas the emulated gNB
holds (EMU_STATE_FILE, from the Slice SM controls): per service up to its quota, first each UE's PRB floor (from the RC controls,
while valid), the rest shared equally (water filling) among the UEs with data. A PRB carries the TS 38.214 table 5.1.3.1-2 bits
of the UE's MCS (--chan changes MCS over time). Cumulative TBS / PRBs, MCS and buffer go to EMU_SIM_FILE, which the emulated gNB
reports as its MAC SM - so the RA sees what its floors did.

VA UEs upload a JPEG of --frame-bytes every --period-ms. With --policy ra each frame is announced to the RA (va_hook:
frame_start, progress every 10 ms, decide when complete, done after the result) and runs the model the RA picks; with
--policy fixed:26x nothing is sent to the RA and every frame runs 26x (today's DGIST VA service: VA_MODEL=26x, qos.deadline_ms =
the E2E target). Each RUN goes to the dApp agent (fake_dapp_agent.py) as role "va". Non-VA UEs keep a bulk backlog.

  va_sim.py --va 4601:ue1,7a3c:ue2,9b21:ue3 --nonva c0de --state-file S --sim-file F [--chan 3:4601:8,6:4601:20] [--policy ra]
"""
import argparse
import csv
import math
import os
import statistics
import sys
import threading
import time
from collections import deque

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
from ra_policy import MAP, bits_per_prb  # noqa: E402
from va_hook import VaHook  # noqa: E402
import json  # noqa: E402

INF80 = {"26n": 5.7, "26s": 9.0, "26m": 14.9, "26l": 21.0, "26x": 32.3}   # cost proxy (fake agent's 80 SM inference ms)

ap = argparse.ArgumentParser()
ap.add_argument("--va", required=True, help="rnti:label,..."); ap.add_argument("--nonva", default="")
ap.add_argument("--frame-bytes", type=int, default=100000); ap.add_argument("--period-ms", type=float, default=400.0)
ap.add_argument("--offsets", default="", help="ms per VA UE (default 0 = all together)")
ap.add_argument("--duration", type=float, default=15.0); ap.add_argument("--deadline-ms", type=float, default=100.0)
ap.add_argument("--t-dl-ms", type=float, default=5.0); ap.add_argument("--mcs", type=int, default=20)
ap.add_argument("--chan", default="", help="t_s:rnti:mcs,... MCS changes")
ap.add_argument("--state-file", required=True); ap.add_argument("--sim-file", required=True)
ap.add_argument("--agent", default="127.0.0.1:9810"); ap.add_argument("--dapp-client-dir", default=os.environ.get("DAPP_CLIENT_DIR") or os.path.join(HERE, "..", "..", "aerial-cuda-accelerated-ran", "cuPHY-CP", "dapp_hook", "python"))
ap.add_argument("--policy", default="ra", help="ra | fixed:<model>"); ap.add_argument("--hook", default="")
ap.add_argument("--nonva-backlog", type=int, default=2_000_000); ap.add_argument("--overhead", type=float, default=1.05)
ap.add_argument("--layers", type=int, default=1); ap.add_argument("--total-prb", type=int, default=273)
ap.add_argument("--out", default="va_sim_frames.csv"); ap.add_argument("--tag", default="")
a = ap.parse_args()

sys.path.insert(0, a.dapp_client_dir)
from xapp_client import DappAgent  # noqa: E402


def wall_ms():
    return time.time_ns() / 1e6


class Ue:
    def __init__(self, rnti, label, va):
        self.rnti, self.label, self.va = rnti, label, va
        self.mcs, self.buf, self.tbs, self.prb = a.mcs, 0.0, 0, 0
        self.frames = deque()      # [frame_id, total bytes on air, delivered, t0_ms, app bytes]


va = [Ue(*x.split(":"), True) for x in a.va.split(",") if x]
nv = [Ue(x, x, False) for x in a.nonva.split(",") if x]
ues = va + nv
chan = sorted((float(t), r, int(m)) for t, r, m in (x.split(":") for x in a.chan.split(",") if x))
offs = [float(x) for x in a.offsets.split(",")] if a.offsets else [0.0] * len(va)
hook = VaHook(a.hook or None) if a.policy == "ra" else None
h, _, p = a.agent.rpartition(":")
agent = DappAgent(h, int(p), timeout=10.0)
print(f"[VA-SIM] agent welcome: {agent.hello('va', 'va-sim', ['estimate'], every=200)}", flush=True)
rows, rows_lock = [], threading.Lock()
payload = b"\xff\xd8" + b"\0" * 998       # the fake agent does not decode: a small stand-in for the JPEG


def read_state():
    try:
        with open(a.state_file) as f:
            s = json.load(f)
    except (OSError, ValueError):
        return (0, 0), {}
    now_us = time.time_ns() // 1000
    fl = {u["rnti"]: u["floor"] for u in s.get("ues", []) if u.get("floor", 0) > 0 and (u.get("until_us", 0) == 0 or u["until_us"] > now_us)}
    q = s.get("quota", {})
    return (int(q.get("va", 0)), int(q.get("nonva", 0))), fl


def split(group, cap, floors):
    """PRBs of one UL slot for the UEs of one service: floors first, then water filling"""
    act = [u for u in group if u.buf > 0]
    if not act or cap <= 0:
        return {}
    need = {u: math.ceil(u.buf * 8 / bits_per_prb(u.mcs, a.layers)) for u in act}
    alloc = {u: min(floors.get(u.rnti, 0), need[u]) for u in act}
    tot = sum(alloc.values())
    if tot > cap:      # floors above the quota: the MAC keeps the quota
        alloc = {u: int(v * cap / tot) for u, v in alloc.items()}
    left = cap - sum(alloc.values())
    while left > 0:
        unsat = [u for u in act if need[u] > alloc[u]]
        if not unsat:
            break
        share = max(1, left // len(unsat))
        for u in unsat:
            g = min(share, need[u] - alloc[u], left)
            alloc[u] += g
            left -= g
            if left <= 0:
                break
    return alloc


def complete(u, fr, t1):
    fid, nbytes, t0 = fr[0], fr[4], fr[3]
    if hook:
        d = hook.decide(fid, nbytes, t0, t1, ue_id=u.label)
        model = d["model"] if d else "26x"
        dl = d["deadline_ms"] if d else a.deadline_ms
    else:
        model, dl = a.policy.split(":", 1)[1], a.deadline_ms
    r = agent.run(payload, model=model, qos={"deadline_ms": dl})
    t_res = wall_ms()
    e2e = t_res - t0 + a.t_dl_ms
    if hook:
        hook.done(fid, r, t_res, ue_id=u.label)
    with rows_lock:
        rows.append({"tag": a.tag, "policy": a.policy, "rnti": u.rnti, "label": u.label, "frame_id": fid, "mcs_at_end": u.mcs,
                     "t0_ms": round(t0, 1), "net_ms": round(t1 - t0, 1), "model": model, "deadline_ms": dl, "status": r.get("status"),
                     "reason": r.get("reason", ""), "server_ms": r.get("latency_ms"), "sm": r.get("sm"), "e2e_ms": round(e2e, 1),
                     "on_time": r.get("status") == "success" and e2e <= a.deadline_ms})


t_start = time.time()
next_frame = {u.rnti: t_start + offs[i] / 1000.0 for i, u in enumerate(va)}
fid = {u.rnti: 0 for u in va}
step, last_progress, ci = 0.005, 0.0, 0
tick = t_start
while time.time() - t_start < a.duration:
    tick += step
    now = time.time()
    if tick > now:
        time.sleep(tick - now)
    t = time.time() - t_start
    while ci < len(chan) and chan[ci][0] <= t:
        for u in ues:
            if u.rnti == chan[ci][1]:
                u.mcs = chan[ci][2]
                print(f"[VA-SIM] t={t:.2f}s {u.rnti} MCS -> {u.mcs} ({bits_per_prb(u.mcs):.0f} bits/PRB)", flush=True)
        ci += 1
    for u in va:                              # new frames
        if time.time() >= next_frame[u.rnti]:
            next_frame[u.rnti] += a.period_ms / 1000.0
            fid[u.rnti] += 1
            on_air = a.frame_bytes * a.overhead
            t0 = wall_ms()
            u.frames.append([fid[u.rnti], on_air, 0.0, t0, a.frame_bytes])
            u.buf += on_air
            if hook:
                hook.frame_start(fid[u.rnti], a.frame_bytes, t0, ue_id=u.label)
    for u in nv:
        u.buf = max(u.buf, float(a.nonva_backlog))
    (q_va, q_nv), floors = read_state()
    groups = [(va, q_va), (nv, q_nv)] if (q_va or q_nv) else [(ues, a.total_prb)]
    tb_bits = 0.0
    for _ in range(3):                        # 3 UL slots per 5 ms (DDDDDDSUUU, 30 kHz)
        for group, cap in groups:
            for u, n in split(group, cap, floors).items():
                bits = n * bits_per_prb(u.mcs, a.layers)
                got = min(u.buf, bits / 8)
                u.buf -= got
                u.tbs += int(got)
                u.prb += n
                tb_bits += bits
                d = got
                while d > 0 and u.frames:     # FIFO: the oldest frame first
                    fr = u.frames[0]
                    take = min(d, fr[1] - fr[2])
                    fr[2] += take
                    d -= take
                    if fr[2] >= fr[1] - 1e-6:
                        u.frames.popleft()
                        threading.Thread(target=complete, args=(u, fr, wall_ms()), daemon=True).start()
    if hook and t - last_progress >= 0.010:
        last_progress = t
        for u in va:
            for fr in u.frames:
                hook.progress(fr[0], int(fr[2] / a.overhead), ue_id=u.label)
    tmp = a.sim_file + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"t_ms": wall_ms(), "tb_slot_bits": tb_bits / 3, "ues": [{"rnti": u.rnti, "tbs": u.tbs, "prb": u.prb, "rprb": 0, "mcs": u.mcs,
                                                                         "bsr": int(u.buf), "snr": 20.0 if u.mcs > 12 else 8.0, "bler": 0.0} for u in ues]}, f)
    os.replace(tmp, a.sim_file)

time.sleep(1.0)    # let the last RUNs finish
with rows_lock:
    done = list(rows)
if done:
    with open(a.out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(done[0]))
        w.writeheader()
        w.writerows(done)
ok = [r for r in done if r["status"] == "success"]
e2e = sorted(r["e2e_ms"] for r in ok)
q = lambda v, x: v[min(len(v) - 1, int(x * len(v)))] if v else float("nan")
mods = {m: sum(1 for r in done if r["model"] == m) for m in INF80}
cost = sum(INF80[r["model"]] for r in done)
print(f"[VA-SIM] {a.tag} policy {a.policy}: frames {len(done)} ok {len(ok)} on time {sum(r['on_time'] for r in done)} "
      f"({100.0 * sum(r['on_time'] for r in done) / max(1, len(done)):.1f} %) | e2e p50 {q(e2e, .5):.1f} p95 {q(e2e, .95):.1f} ms | "
      f"net p50 {statistics.median([r['net_ms'] for r in done]) if done else float('nan'):.1f} ms | models {mods} | mean mAP "
      f"{statistics.mean(MAP[r['model']] for r in done) if done else float('nan'):.2f} | cost vs all-26x {100.0 * (1 - cost / max(1e-9, INF80['26x'] * len(done))):.1f} % less",
      flush=True)
for u in va:
    rs = [r for r in done if r["rnti"] == u.rnti]
    if rs:
        print(f"   {u.label} {u.rnti}: frames {len(rs)} on time {sum(r['on_time'] for r in rs)} | net p50 {statistics.median(r['net_ms'] for r in rs):.1f} ms"
              f" | models {dict((m, sum(1 for r in rs if r['model'] == m)) for m in INF80 if any(r['model'] == m for r in rs))}", flush=True)
os._exit(0)
