#!/usr/bin/env python3
"""sim.py - a SIMULATED data source for the dashboard (DASH_SIM=1 / ./dash.sh sim). It feeds the hub through the same on_* calls as the real
collectors, so the page, the recorder and the replay can be developed and rehearsed with the stack down. Every number it produces is made up;
the page shows a SIMULATION badge and recordings carry "sim": true. DASH_SIM_MODE=baseline simulates the Baseline stack instead: no slot CSV, no
xApp / slice quota, PRBs only from the gNB's sampled grant lines, one model (26x) on a fixed 60-SM MIG slice without deadline admission."""
import heapq, math, random, threading, time

MODELS = ("26n", "26s", "26m", "26l", "26x")
INF_MS = {"26n": 4.5, "26s": 6.5, "26m": 10.5, "26l": 13.5, "26x": 21.0}        # rough FHD batch-3 inference times at 80 SMs (ms)


class Sim(threading.Thread):
    def __init__(self, hub, cfg, seed=7, select=True):
        super().__init__(name="sim", daemon=True); self.hub = hub; self.cfg = cfg; self.rng = random.Random(seed); self.base = cfg.get("mode") == "baseline"; self.select = select and not self.base
        self.stop_ev = threading.Event(); self.q = []; self.n = 0; self.t0 = None
        self.va = [{"sid": "ue1", "ip": "10.45.0.11", "rnti": "1c69", "cu": 1, "snr": 19.0, "next": 0.0, "fid": 0, "tok": "a1b2c3d4", "rx": 0, "tx": 0, "ul0": 0, "ul1": 0, "dl0": 0, "dl1": 0, "back": 0.0},
                   {"sid": "ue2", "ip": "10.45.0.12", "rnti": "398f", "cu": 2, "snr": 22.0, "next": 0.03, "fid": 0, "tok": "e5f6a7b8", "rx": 0, "tx": 0, "ul0": 0, "ul1": 0, "dl0": 0, "dl1": 0, "back": 0.0}]
        self.nv = [{"sid": "ue1", "ip": "10.45.0.13", "rnti": "1a60", "cu": 3, "snr": 21.0, "next": 0.01, "fid": 0, "rx": 0, "tx": 0, "ul0": 0, "ul1": 0, "dl0": 0, "dl1": 0},
                   {"sid": "ue2", "ip": "10.45.0.14", "rnti": "3656", "cu": 4, "snr": 20.0, "next": 0.04, "fid": 0, "rx": 0, "tx": 0, "ul0": 0, "ul1": 0, "dl0": 0, "dl1": 0}]
        self.quota = [200, 73]; self.gpu_free_at = 0.0; self.slot = 0; self.slot_t = None; self.runs = []      # runs: (slot_from, slot_to, cls, model)
        self.t_stats = 0.0; self.t_ppo = 0.0; self.t_sec = 0.0; self.t_quota = 0.0; self.t_est = 0.0; self.win_req = 0; self.win_sm = []; self.last = None
        self.prb = {}; self.tp = {}; self.queue = 0

    def stop(self): self.stop_ev.set()

    def _push(self, t, fn): self.n += 1; heapq.heappush(self.q, (t, self.n, fn))

    def setup(self, now):
        h = self.hub; c = self.cfg
        h.sim = True
        va = c.get("va_imsis") or ["999700000177992", "999700000177998"]; nv = c.get("nonva_imsis") or ["999700000177994", "999700000177996"]
        c["va_imsis"], c["nonva_imsis"] = va, nv
        ues = self.va + self.nv; ims = va[:2] + nv[:2]
        h.set_core({u["cu"]: im for u, im in zip(ues, ims)}, {im: u["ip"] for u, im in zip(ues, ims)}, fixed=True)
        h.on_agent({"type": "welcome", "version": "sim", "models": ["26x"] if self.base else list(MODELS), "slot_us": 500, "cells": 1, "tdd": "DDDDDDSUUU", "gpu_sm": 60 if self.base else 132, "input": "fhd"}, now)
        if not self.base: h.on_quota(self.quota[0], self.quota[1], "gnb", now)
        self.t0 = now; self.slot_t = now; self.slot = int(now * 2000) % 10 ** 7 // 20 * 20
        for u in ues: u["next"] += now

    # the "story": calm -> uplink congestion on the VA slice (~70-110 s) -> extra GPU load from virtual cameras (~150-210 s), repeating every 240 s
    def phase(self, t):
        x = (t - self.t0) % 240.0
        return {"ulc": 1.0 if 70 <= x < 110 else 0.0, "vcam": 150 <= x < 210, "x": x}

    def step(self, now):
        h = self.hub; r = self.rng; ph = self.phase(now)
        while self.q and self.q[0][0] <= now: _, _, fn = heapq.heappop(self.q); fn()
        # --- VA phones: capture -> uplink -> GPU -> reply -> E2E report
        for i, u in enumerate(self.va):
            while u["next"] <= now:
                t_cap = u["next"]; u["next"] += 1.0 / 15.0 * r.uniform(0.97, 1.03)
                if self.base and self.gpu_free_at - now > 0.30: continue          # Baseline: the phone holds new frames while its earlier ones are still unanswered
                u["fid"] += 1; fid = u["fid"]
                kb = r.gauss(185, 14) if i == 0 else r.gauss(172, 12)
                share = self.quota[0] / 2.0; cong = ph["ulc"] * (1.0 if i == 1 else 0.55)
                ul = max(8.0, r.lognormvariate(math.log(30 + 2200.0 / max(40.0, share)), 0.22) + cong * r.uniform(60, 190) * (0.4 + 0.6 * math.sin((now - self.t0) / 6.0) ** 2))
                t_arr = t_cap + ul / 1000.0
                self._push(t_arr, lambda u=u, fid=fid, ul=ul, kb=kb, t_arr=t_arr: self._va_serve(u, fid, ul, kb, t_arr))
                u["rx"] += int(kb * 1024 * 1.04)
        # --- non-VA phones: frames up, nothing back
        for i, u in enumerate(self.nv):
            while u["next"] <= now:
                t_cap = u["next"]; u["next"] += 1.0 / 15.0; u["fid"] += 1; kb = r.gauss(118, 10)
                ul = max(6.0, r.lognormvariate(math.log(24 + 1500.0 / max(20.0, self.quota[1] / 2.0)), 0.25))
                if r.random() < (0.30 if ph["ulc"] else 0.02): ul += r.uniform(180.0, 420.0)        # late frames: over the non-VA deadline
                self._push(t_cap + ul / 1000.0, lambda u=u, fid=u["fid"], ul=ul, kb=kb, t=t_cap: h.on_nv_event({"k": "f", "t": (t + ul / 1000.0) * 1000.0, "ue": u["sid"], "fid": fid, "ip": u["ip"], "ul": ul, "kb": kb}, now))
                u["rx"] += int(kb * 1024 * 1.04)
        # --- virtual cameras: extra requests straight to the GPU (no radio)
        if ph["vcam"]:
            k = int((now - self.t0) * 20.0) - int((self.t_prev - self.t0) * 20.0) if getattr(self, "t_prev", None) else 0
            for _ in range(max(0, k)):
                if not (self.base and self.gpu_free_at - now > 0.30): self._gpu_run(now, "26x", None)
        self.t_prev = now
        # --- slots (500 us each)
        rows = []
        while self.slot_t <= now:
            s = self.slot; pos = s % 10; t_ns = int(self.slot_t * 1e9)
            if pos < 6: P, N = 132, (40 if (s // 10) % 7 == 3 else 24)
            elif pos == 6: P, N = 132, 24
            else: P, N = (104 if ((s // 10) % 5 == 2 or ph["ulc"] > 0) else 112), 24
            free = max(0, 64 - N) + max(0, min(P, 120) - 72); run = 0; m = ""
            self.runs = [x for x in self.runs if x[1] > s]
            for a, b, cls, mod in self.runs:
                if a <= s < b: run = min(cls, free); m = mod; break
            rows.append((s, pos, P, N, run, self.queue, m, t_ns)); self.slot += 1; self.slot_t += 0.0005
        if rows and not self.base: h.on_slots(rows, now)
        # --- gNB statistics block (every 1.28 s) + PPO-like 100 ms records + per-second things
        if now - self.t_ppo >= 0.1:
            self.t_ppo = now; ues = {}
            act = {"yolo": [1.0, 1.0], "live": [1.0, 1.0]}
            for pre, lst, q in (("yolo", self.va, self.quota[0]), ("live", self.nv, self.quota[1])):
                w = [r.uniform(0.75, 1.25) for _ in lst]; sw = sum(w)
                for j, u in enumerate(lst):
                    use = q * (0.93 if pre == "yolo" else 0.62) * w[j] / sw
                    self.prb[u["rnti"]] = use; tp = use * (0.50 + 0.012 * (u["snr"] - 15)) * r.uniform(0.9, 1.1); self.tp[u["rnti"]] = tp
                    ues["%s_%d" % (pre, j + 1)] = {"ul_buffer_bytes": max(0.0, r.gauss(220000, 60000)) if pre == "yolo" else 0.0, "ul_snr_db": u["snr"] + r.uniform(-1, 1), "ul_tp_mbps": tp, "ul_prb_avg": use}
            if not self.base: h.on_ppo({"wall_ms": now * 1000.0, "input": {"ues": ues, "service_prbs": {"yolo": self.quota[0], "live": self.quota[1]}}}, now)
        if self.base and now - self.t_quota >= 1.0:
            # Baseline: no xApp and no slice quota - only the gNB's grant lines of one sampled frame a second (6 UL slots)
            self.t_quota = now; fr = int(now) % 1024
            for sl in (7, 8, 9, 17, 18, 19):
                for svc, lst in ((1, self.va), (2, self.nv)):
                    for u in lst: h.on_gnb_grant(u["rnti"], (fr, sl), int(max(0, self.prb.get(u["rnti"], 0) * r.uniform(0.7, 1.3))), svc, 0, now)
        elif not self.base and now - self.t_quota >= 0.5:
            self.t_quota = now
            tgt = 228 if ph["ulc"] > 0 and ph["x"] > 78 else 200
            self.quota[0] += max(-6, min(6, tgt - self.quota[0])) + r.choice((-2, -1, 0, 0, 1, 2)); self.quota[0] = max(120, min(250, self.quota[0])); self.quota[1] = 273 - self.quota[0]
            h.on_quota(self.quota[0], self.quota[1], "gnb", now)
        if now - self.t_stats >= 1.28:
            dt = now - self.t_stats if self.t_stats else 1.28; self.t_stats = now; tb = now % 86400.0
            for u in self.va + self.nv:
                u["snr"] += r.uniform(-0.4, 0.4) + 0.05 * ((19.0 if u is self.va[0] else 21.0) - u["snr"])
                bl = max(0.01, min(0.3, r.gauss(0.085, 0.02) + (0.05 if ph["ulc"] > 0 and u in self.va else 0.0)))
                n0 = int(600 * dt * r.uniform(0.6, 0.9)); u["ul0"] += n0; u["ul1"] += int(n0 * bl); u["dl0"] += int(900 * dt); u["dl1"] += int(9 * dt * r.random())
                u["tx"] += int(140000 * dt)
                mcs = int(max(5, min(27, 9 + (u["snr"] - 10) * 1.45 + r.uniform(-1, 1))))
                h.on_gnb_ue(u["rnti"], {"cu": u["cu"], "sync": True, "ph": int(r.gauss(50, 2)), "pcmax": 22, "rsrp": int(-70 - (22 - u["snr"]) * 4 + r.uniform(-1, 1))}, tb, now)
                h.on_gnb_ue(u["rnti"], {"dl0": u["dl0"], "dl1": u["dl1"], "dlbler": round(r.uniform(0.0, 0.012), 5), "dlmcs": int(r.uniform(7, 10))}, None, now)
                h.on_gnb_ue(u["rnti"], {"ul0": u["ul0"], "ul1": u["ul1"], "ulerr": 0, "bler": round(bl, 5), "mcs": mcs, "qm": 8 if mcs > 19 else 6, "snr": round(u["snr"] * 2) / 2.0}, None, now)
                h.on_gnb_ue(u["rnti"], {"tx": u["tx"], "rx": u["rx"]}, tb, now)
        if now - self.t_est >= 0.1:
            self.t_est = now; back = max(0.0, self.gpu_free_at - now) * 1000.0; self.queue = int(back / 22.0)
            sm0 = 60 if self.base else 80
            h.on_agent({"type": "estimate", "t_ns": int(now * 1e9), "ul_sm": 0 if self.base else 20, "dl_sm": 0 if self.base else 24, "yolo_sm_now": sm0, "yolo_sm_min": sm0 if self.base else 24, "queue": self.queue, "inflight": min(4, self.queue + (1 if back > 0 else 0)),
                        "est_ms": {m: round(back + INF_MS[m] * (80.0 / sm0) ** 0.55 + 6.0, 1) for m in (("26x",) if self.base else MODELS)}, "est_sm": {m: sm0 for m in (("26x",) if self.base else MODELS)}}, now)
        if now - self.t_sec >= 1.0:
            self.t_sec = now
            h.on_agent({"type": "sm_report", "t_ns": int(now * 1e9), "window_ms": 1000, "requests": self.win_req, "sm_mean": (sum(self.win_sm) / len(self.win_sm)) if self.win_sm else 0.0,
                        "yolo_sm_time_mean": 60.0 if self.base else 83.0, "ran_sm_time_mean": 0.0 if self.base else 44.0, "last": self.last}, now)
            self.win_req = 0; self.win_sm = []
            mac = sum(self.tp.values())
            h.on_l1({"dl": round(r.uniform(3.5, 4.5), 2), "dl_slots": 1200, "ul": round(mac * 0.97, 2), "ul_slots": 600, "crc": int(r.uniform(20, 60) + ph["ulc"] * 40), "crc_tot": 0}, now)
            h.set_sys({"mode": self.cfg.get("mode", "ours"), "up": {"l1": True, "gnb": True, "yolo": self.base, "va": True, "nonva": True, "xapp": not self.base, "moni": False, "vcam": ph["vcam"], "ric": True, "core": True, "tgen": True},
                       "tgen": {"on": True, "run": True, "load": "auto", "sel": "auto"},
                       "col": {}, "disk_gb": 56.0, "rntis": "VA_RNTIS=1c69,398f NONVA_RNTIS=1a60,3656"})
            util = min(99.0, 38 + 22 * ph["ulc"] + (30 if ph["vcam"] else 0) + r.uniform(-4, 4))
            h.set_nvml({"util": round(util, 1), "power": round(240 + util * 2.1, 1), "temp": round(52 + util * 0.12, 1), "mem": 21.4})
            h.set_vcam({"seen": time.time(), "cams": 2, "fps": 10.0, "model": "26x", "deadline_ms": 200, "inflight": 4, "dry": False, "req_s": round(20.0 + r.uniform(-0.6, 0.6), 1), "met_pct": round(r.uniform(96, 100), 1),
                        "rt": [round(r.uniform(28, 34), 1), round(r.uniform(45, 60), 1), round(r.uniform(70, 110), 1)], "offered": 0, "sent": 0, "skipped": 0, "success": 0, "met": 0, "uptime_s": int(ph["x"] - 150),
                        "jpeg_quality": 0.7, "frames": "", "per": [{"cam": 1, "req_s": 10.1, "met_pct": 99.0, "rt_p50": 30.2, "rt_p99": 80.0}, {"cam": 2, "req_s": 9.9, "met_pct": 98.0, "rt_p50": 31.0, "rt_p99": 92.0}]} if ph["vcam"] else None)

    def _gpu_run(self, t_arr, model, sm_pref):
        """one request on the GPU: waits for the previous one, then runs; returns (wait_ms, infer_ms, model, sm)"""
        r = self.rng; start = max(t_arr, self.gpu_free_at); wait = (start - t_arr) * 1000.0
        if self.select and sm_pref is None and model == "26x": pass
        if self.select and sm_pref == "auto":
            # the (simulated) model selection: the largest model whose estimate still fits the budget left for the GPU
            budget = 95.0 - wait
            for m in ("26x", "26l", "26m", "26s", "26n"):
                if INF_MS[m] * 1.15 + 6.0 <= budget or m == "26n": model = m; break
        sm = 60 if self.base else r.choices((80, 72, 64, 88), weights=(70, 16, 6, 8))[0]
        inf = INF_MS[model] * (80.0 / sm) ** 0.55 * r.uniform(0.93, 1.12)
        self.gpu_free_at = start + inf / 1000.0
        a = self.slot + int((start - self.slot_t) * 2000.0) if self.slot_t else 0
        self.runs.append((a, a + max(1, int(inf * 2.0)), sm, model)); self.win_req += 1; self.win_sm.append(sm)
        self.last = {"model": model, "status": "success", "sm": sm, "sm_mean": float(sm), "infer_ms": round(inf, 2), "latency_ms": round(wait + inf, 2)}
        return wait, inf, model, sm

    def _va_serve(self, u, fid, ul, kb, t_arr):
        h = self.hub; r = self.rng
        wait, inf, model, sm = self._gpu_run(t_arr, "26x", "auto" if self.select else None)
        dec = r.uniform(4.2, 6.5); oth = r.uniform(1.2, 2.6); ai = wait + dec + inf + r.uniform(0.3, 0.8)
        ok = (self.base or not (wait > 105.0 and r.random() < 0.5)) and r.random() > 0.0005          # the agent's deadline admission refuses when the queue is too long
        t_send = t_arr + (ai + oth) / 1000.0
        ev = {"k": "f", "t": t_send * 1000.0, "ue": u["sid"], "tok": u["tok"], "fid": fid, "ip": u["ip"], "yolo": True, "ul": ul, "ai": ai, "oth": oth, "wait": wait, "dec": dec, "inf": inf,
              "srv": wait + dec + inf, "sm": sm, "smm": float(sm), "model": model, "st": "success" if ok else "rejected", "why": None if ok else "deadline", "kb": kb, "w": 1920, "h": 1080, "det": int(r.uniform(3, 12))}
        self._push(t_send, lambda: h.on_va_event(ev, t_send))
        dl = max(6.0, r.gauss(17.0, 4.0)); e2e = ul + ai + oth + dl
        if r.random() > 0.01:
            self._push(t_send + dl / 1000.0, lambda: h.on_va_event({"k": "e", "t": (t_send + dl / 1000.0) * 1000.0, "ue": u["sid"], "tok": u["tok"], "fid": fid, "ok": ok, "e2e": e2e if ok else None}, t_send + dl / 1000.0))

    def run(self):
        self.setup(time.time())
        while not self.stop_ev.is_set():
            self.step(time.time()); self.stop_ev.wait(0.02)
