#!/usr/bin/env python3
"""hub.py - state, per-second aggregation and the measurement recorder of the DGIST measurement dashboard (2026-11-12).

Collectors (sources.py) or the simulator (sim.py) call the on_* methods; the ticker (dash_server.py) calls tick() once a second and scope()
five times a second. A tick is self-contained: the values of the last second per UE / service / cell / GPU and, while a measurement is
recording, the cumulative values since "측정 시작" under the key "c". The same messages go to the browsers and into <name>.replay.jsonl,
so the page replays a recording through the code path it uses live.
"""
import csv, gzip, json, math, os, re, threading, time
from collections import deque
from pathlib import Path

MODELS = ("26n", "26s", "26m", "26l", "26x")
# COCO val2017 mAP50-95 at 640 px as published by Ultralytics (docs.ultralytics.com/models/yolo26, read 2026-11-12) - not measured on our FHD pipeline
MAP_DEFAULT = {"26n": 40.9, "26s": 48.6, "26m": 53.1, "26l": 55.0, "26x": 57.5}
NSM, ORDER = 132, 8
UE_KEYS = ("va1", "va2", "nv1", "nv2")
CORE_SETTLE_S = 1.5          # a core table must have been read this long after an RNTI appeared on a gNB UE id to name its phone
UE_NAMES = {"va1": "VA UE1", "va2": "VA UE2", "nv1": "Non-VA UE1", "nv2": "Non-VA UE2"}
JOIN_WAIT_S = 1.5          # how long a served frame waits for the E2E latency its phone reports (POST /telemetry/result)


def rnd(v, nd=2):
    if v is None or isinstance(v, bool): return v
    try:
        if v != v or v in (float("inf"), float("-inf")): return None
        return round(float(v), nd)
    except (TypeError, ValueError):
        return None


def mean(xs):
    s = 0.0; n = 0
    for x in xs:
        if x is not None: s += x; n += 1
    return s / n if n else None


class Stat:
    """count / mean / max and, with hist=True, percentiles from a histogram of `res`-wide bins (values past the top land in the last bin)"""
    __slots__ = ("n", "s", "mx", "h", "res")

    def __init__(self, hist=False, res=1.0, bins=4000):
        self.n = 0; self.s = 0.0; self.mx = None; self.h = [0] * (bins + 1) if hist else None; self.res = res

    def add(self, v, w=1):
        if v is None or v != v: return
        self.n += w; self.s += v * w
        if self.mx is None or v > self.mx: self.mx = v
        if self.h is not None:
            i = int(v / self.res); self.h[0 if i < 0 else (len(self.h) - 1 if i >= len(self.h) else i)] += w

    def mean(self): return self.s / self.n if self.n else None

    def pct(self, ps):
        if not self.n or self.h is None: return [None] * len(ps)
        out = []; acc = 0; it = iter(sorted(ps)); p = next(it, None); tgt = self.n * p / 100.0 if p is not None else None
        for i, c in enumerate(self.h):
            if not c: continue
            acc += c
            while p is not None and acc >= tgt:
                out.append((i + 0.5) * self.res); p = next(it, None); tgt = self.n * p / 100.0 if p is not None else None
            if p is None: break
        while len(out) < len(ps): out.append(self.mx)
        return out


VA_LAT = ("ul", "ai", "inf", "wait", "dec", "oth", "e2e", "dl")      # per-frame latency parts of a VA frame (ms)
RADIO = ("snr", "mcs", "bler", "prb", "mac", "dlmac", "rsrp", "ph", "buf", "dlmcs", "dlbler")


class UE:
    def __init__(self, key):
        self.key = key; self.svc = "va" if key.startswith("va") else "nv"; self.sid = "ue" + key[-1]
        self.ip = None; self.imsi = None; self.rnti = None; self.map_src = None
        self.last_t = 0.0; self.w = None; self.h = None; self.model = None; self.sm = None; self.last_model_t = 0.0
        self.win = None; self.cum = None; self.reset_win(); self.reset_cum()

    def reset_win(self):
        self.win = {k: [] for k in VA_LAT + ("kb",)}
        self.win.update(n=0, ok=0, met=0, fail=0, est=0, bytes=0.0, map=[], models={})

    def reset_cum(self):
        c = {k: Stat(hist=k in ("ul", "ai", "inf", "e2e", "dl")) for k in VA_LAT + ("kb",)}
        c.update({k: Stat() for k in RADIO})
        c.update(n=0, ok=0, met=0, fail=0, est=0, bytes=0.0, map=Stat(), models={}, combo={}, fps=Stat(), mbps=Stat(), retx0=0, retx1=0)
        self.cum = c


class Hub:
    def __init__(self, cfg):
        self.cfg = cfg; self.lock = threading.RLock()
        self.map = dict(MAP_DEFAULT); self.map.update(cfg.get("map") or {})
        self.deadline = float(cfg.get("deadline_ms", 200.0)); self.dl_fixed = float(cfg.get("dl_fixed_ms", 20.0))
        self.ue = {k: UE(k) for k in UE_KEYS}
        self.pending = {}                    # (sid, tok, fid) -> [frame event, arrival time]
        self.radio = {}                      # rnti -> latest gNB stats of that UE
        self.gf = deque(maxlen=10); self.gf_cur = None; self.cap_tot = []; self.quota_ev = []
        self.quota = {"va": None, "nv": None, "src": None, "t": 0.0}; self.need_remap = False
        self.ppo = []; self.ppo_t = 0.0; self.ppo_keys = {}; self.ppo_last = None
        self.moni = {}; self.moni_t = 0.0
        self.slot_ring = deque(maxlen=200); self.slot_t = 0.0; self.slot_win = self._slot_acc(); self.slot_cum = self._slot_acc()
        self.est = None; self.est_t = 0.0; self.rep = None; self.rep_t = 0.0; self.rep_new = False; self.welcome = None
        self.gpu_cum = self._gpu_acc()
        self.l1 = None; self.l1_t = 0.0; self.sysd = {}; self.vcam = None; self.nvml = None
        self.core = {"imsi_ip": {}, "cu_imsi": {}, "t": 0.0, "fixed": False}
        self.rec = None; self.ticks = deque(maxlen=int(cfg.get("live_ticks", 300))); self.rec_n = 0; self.rec_last = {}
        self.last_tick_t = time.time(); self.last_scope = None; self.sim = bool(cfg.get("sim"))
        self.cum_svc = {s: self._svc_acc() for s in ("va", "nv")}; self.cum_cell = {k: Stat() for k in ("prb", "unused", "ul", "dl", "mac")}
        self.rec_frames = None; self.rec_slots = None; self.listeners = []
        self.frames_total = 0; self.e2e_late = 0

    # ------------------------------------------------------------------ accumulators
    @staticmethod
    def _slot_acc():
        return dict(n=0, free=0, run=0, ran=0, unus=0, busy=0, q=0, pusch=0, pdsch=0, idle=0, strand=0, models={}, combo={})

    @staticmethod
    def _gpu_acc():
        return dict(req=Stat(), busy=Stat(), queue=Stat(), wait=Stat(), cap=Stat(), util=Stat(), power=Stat(), free=Stat(), run=Stat(), ran=Stat(),
                    strand=Stat(), n_req=0, n_rej=0, sat_s=0, warn_s=0, ok_s=0, map=Stat())

    @staticmethod
    def _svc_acc():
        return dict(quota=Stat(), prb=Stat(), mac=Stat(), mbps=Stat())

    # ------------------------------------------------------------------ inputs: services
    def on_va_event(self, ev, now=None):
        now = now or time.time()
        with self.lock:
            sid = ev.get("ue"); key = (sid, ev.get("tok"), ev.get("fid"))
            if ev.get("k") == "f":
                if ev.get("fid") is None or not ev.get("yolo", True): self._va_final(ev, None, now)
                else: self.pending[key] = [ev, now]
            elif ev.get("k") == "e":
                p = self.pending.pop(key, None)
                if p is not None: self._va_final(p[0], ev, now)
                elif not ev.get("ok", True):     # the phone gave up on a frame the service never finished (upload timeout): a failed frame
                    self._va_final({"k": "f", "t": ev.get("t"), "ue": sid, "fid": ev.get("fid"), "st": "fail", "why": "upload", "yolo": True}, ev, now)
                else: self.e2e_late += 1

    def _ue_of(self, svc, sid):
        k = ("va" if svc == "va" else "nv") + str(sid or "")[-1:]
        return self.ue.get(k)

    def _va_final(self, f, e, now):
        u = self._ue_of("va", f.get("ue"))
        if u is None: return
        ok = (f.get("st") == "success") or (not f.get("yolo", True) and f.get("st") is None)
        if e is not None and not e.get("ok", True): ok = False
        ul, ai, oth = f.get("ul"), f.get("ai"), f.get("oth") or 0.0
        e2e = e.get("e2e") if e is not None and e.get("ok", True) else None
        src = "phone"
        if e2e is None:
            src = "est"; e2e = (ul or 0.0) + (ai or 0.0) + oth + self.dl_fixed if ai is not None else None
        dl = max(0.0, e2e - (ul or 0.0) - (ai or 0.0) - oth) if src == "phone" and ai is not None else None
        met = bool(ok and e2e is not None and e2e <= self.deadline)
        w = u.win; c = u.cum; vals = dict(ul=ul, ai=ai, inf=f.get("inf"), wait=f.get("wait"), dec=f.get("dec"), oth=f.get("oth"), e2e=e2e, dl=dl, kb=f.get("kb"))
        w["n"] += 1; w["ok"] += ok; w["met"] += met; w["fail"] += (not ok); w["est"] += (src == "est"); w["bytes"] += (f.get("kb") or 0.0) * 1024.0
        for k, v in vals.items():
            if v is not None and (ok or k in ("ul", "kb")): w[k].append(v)
        m = f.get("model"); sm = f.get("sm")
        if ok and m:
            w["models"][m] = w["models"].get(m, 0) + 1; w["map"].append(self.map.get(m))
            u.model = m; u.sm = sm; u.last_model_t = now
        if f.get("w"): u.w, u.h = f.get("w"), f.get("h")
        if f.get("ip") and f.get("ip") != u.ip: u.ip = f.get("ip"); self._remap()
        u.last_t = now; self.frames_total += 1
        if self.rec is not None:
            c["n"] += 1; c["ok"] += ok; c["met"] += met; c["fail"] += (not ok); c["est"] += (src == "est"); c["bytes"] += (f.get("kb") or 0.0) * 1024.0
            for k, v in vals.items():
                if v is not None and (ok or k in ("ul", "kb")): c[k].add(v)
            if ok and m:
                c["models"][m] = c["models"].get(m, 0) + 1; c["map"].add(self.map.get(m))
                if sm is not None: ck = "%s@%s" % (m, sm); c["combo"][ck] = c["combo"].get(ck, 0) + 1
            if self.rec_frames is not None:
                self.rec_frames.writerow([rnd((f.get("t") or now * 1000.0) / 1000.0, 3), u.key, f.get("ue"), f.get("fid"), f.get("ip"), u.rnti, rnd(ul), rnd(ai), rnd(f.get("wait")),
                                          rnd(f.get("dec")), rnd(f.get("inf")), rnd(f.get("oth")), rnd(e2e), src, rnd(dl), int(met), f.get("st"), f.get("why"), m, sm,
                                          rnd(f.get("smm"), 1), rnd(f.get("kb"), 1), f.get("w"), f.get("h"), f.get("det")])

    def on_nv_event(self, ev, now=None):
        now = now or time.time()
        with self.lock:
            u = self._ue_of("nv", ev.get("ue"))
            if u is None: return
            w = u.win; w["n"] += 1; w["ok"] += 1; w["bytes"] += (ev.get("kb") or 0.0) * 1024.0
            if ev.get("ul") is not None: w["ul"].append(ev["ul"])
            if ev.get("kb") is not None: w["kb"].append(ev["kb"])
            if ev.get("ip") and ev.get("ip") != u.ip: u.ip = ev.get("ip"); self._remap()
            u.last_t = now
            if self.rec is not None:
                c = u.cum; c["n"] += 1; c["ok"] += 1; c["bytes"] += (ev.get("kb") or 0.0) * 1024.0; c["ul"].add(ev.get("ul")); c["kb"].add(ev.get("kb"))

    # ------------------------------------------------------------------ inputs: gNB
    def on_gnb_ue(self, rnti, fields, t_blk=None, now=None):
        """one stats line of a UE (fields merge); rx/tx = cumulative MAC bytes, rounds = (tx0, retx1) -> rates against the block time"""
        now = now or time.time()
        with self.lock:
            if rnti not in self.radio: self.need_remap = True
            r = self.radio.setdefault(rnti, {"rnti": rnti}); r["t"] = now
            if fields.get("cu") is not None and fields["cu"] != r.get("cu"): r["t_cu"] = now; self.need_remap = True     # this RNTI on this gNB UE id: first seen now
            if "rx" in fields and t_blk is not None:
                if r.get("rx") is not None and r.get("t_blk") is not None:
                    dt = (t_blk - r["t_blk"]) % 86400.0
                    if 0.2 < dt < 30.0 and fields["rx"] >= r["rx"]:
                        r["mac"] = (fields["rx"] - r["rx"]) * 8.0 / dt / 1e6; r["dlmac"] = max(0, fields["tx"] - r.get("tx", fields["tx"])) * 8.0 / dt / 1e6
                r["t_blk"] = t_blk
            if "ul0" in fields:
                if r.get("ul0") is not None and fields["ul0"] >= r["ul0"]:
                    d0 = fields["ul0"] - r["ul0"]; d1 = fields["ul1"] - r.get("ul1", fields["ul1"])
                    r["bler_w"] = d1 / d0 if d0 > 0 else None; r["d0"] = d0; r["d1"] = d1; r["d_new"] = True
            r.update(fields)

    def on_gnb_grant(self, rnti, slot_key, grant, service=None, quota=None, now=None):
        """the MAC logs the UL grants of every 100th frame (~one frame a second = a 1 % sample): keep the last 10 sampled frames"""
        with self.lock:
            now = now or time.time(); fr = slot_key[0]
            if self.gf_cur is None or self.gf_cur[0] != fr:
                if self.gf_cur is not None: self.gf.append(self.gf_cur)
                self.gf_cur = (fr, now, {})
            g = self.gf_cur[2]; g[rnti] = g.get(rnti, 0) + grant

    def on_gnb_cap(self, total, cap):
        with self.lock: self.cap_tot.append(total)

    def on_quota(self, va, nv, src, now=None):
        """UL PRB quota of the two slices. The gNB's own E2_SLICE_TRANSITION_DONE is authoritative and stays valid until the next one;
        the xApp logs (ppo / moni) are used only while the gNB log has reported none. (None, None, None) = forget (gNB restarted)."""
        with self.lock:
            if src is not None and src != "gnb" and self.quota["src"] == "gnb": return
            self.quota.update(va=va, nv=nv, src=src, t=now or time.time())
            if va is not None: self.quota_ev.append((va, nv))

    def on_rnti_gone(self, rnti):
        with self.lock: self.radio.pop(rnti, None)

    # ------------------------------------------------------------------ inputs: xApp logs
    def on_ppo(self, rec, now=None):
        with self.lock:
            self.ppo.append(rec); self.ppo_t = now or time.time(); self.ppo_last = rec
            sp = (rec.get("input") or {}).get("service_prbs") or {}
            if sp.get("yolo") is not None: self.on_quota(sp.get("yolo"), sp.get("live"), "ppo", now)

    def on_moni(self, ues, now=None):
        """ues: {rnti: {prb, mac, buf, snr, budget, service}} from the lab monitor xApp log"""
        with self.lock:
            self.moni = ues; self.moni_t = now or time.time()
            b = {}
            for x in ues.values():
                if x.get("budget") is not None: b[x.get("service")] = x["budget"]
            if 1 in b and 2 in b: self.on_quota(b[1], b[2], "moni", now)

    # ------------------------------------------------------------------ inputs: L1 / tenant
    def on_slots(self, rows, now=None):
        """rows: (abs_slot, pos, gap_P, gap_N, run_cls, queue, model, t_ns) of the tenant's per-slot CSV"""
        if not rows: return                                   # an error line of the follower is not slot data
        with self.lock:
            self.slot_t = now or time.time()
            for row in rows:
                s, pos, P, N, run, q, m, tn = row
                if P <= 0 or N < 0: P, N = NSM, 24
                free = max(0, 64 - N) + max(0, min(P, 120) - 72); ran = N + ORDER + (NSM - P); unus = max(0, NSM - ran - free)
                run = min(run, free) if run > 0 else 0
                self.slot_ring.append((s, pos, P, N, run, q, m))
                for a in ((self.slot_win, self.slot_cum) if self.rec is not None else (self.slot_win,)):
                    a["n"] += 1; a["free"] += free; a["run"] += run; a["ran"] += ran; a["unus"] += unus; a["q"] += q; a["pusch"] += NSM - P; a["pdsch"] += N
                    if run > 0:
                        a["busy"] += 1; a["strand"] += unus + (free - run); a["models"][m] = a["models"].get(m, 0) + 1
                        ck = "%s@%d" % (m, run); a["combo"][ck] = a["combo"].get(ck, 0) + 1
                    else: a["idle"] += free; a["strand"] += unus
            if self.rec_slots is not None:
                for row in rows: self.rec_slots.write(("%d,%d,%d,%d,%d,%d,%s,%d\n" % row).encode())

    def on_agent(self, msg, now=None):
        with self.lock:
            t = msg.get("type"); now = now or time.time()
            if t == "welcome": self.welcome = msg
            elif t == "estimate": self.est = msg; self.est_t = now
            elif t == "sm_report": self.rep = msg; self.rep_t = now; self.rep_new = True

    def on_l1(self, d, now=None):
        with self.lock: self.l1 = d; self.l1_t = now or time.time()

    def set_sys(self, d):
        with self.lock: self.sysd.update(d)

    def set_vcam(self, d):
        with self.lock: self.vcam = d

    def set_nvml(self, d):
        with self.lock: self.nvml = d

    def set_core(self, cu_imsi, imsi_ip, t=None, fixed=False):
        """t = when the core journals were read; fixed = a table that does not come from a journal (simulation, tests)"""
        with self.lock:
            self.core = {"cu_imsi": cu_imsi, "imsi_ip": imsi_ip, "t": t or time.time(), "fixed": fixed}; self._remap()

    # ------------------------------------------------------------------ UE <-> IP <-> IMSI <-> RNTI
    def _remap(self):
        ip_imsi = {ip: im for im, ip in self.core["imsi_ip"].items()}
        imsi_rnti = {}
        for rnti, r in sorted(self.radio.items(), key=lambda kv: kv[1].get("t", 0)):
            # the gNB reuses its UE ids: a table read before this RNTI showed up on the id may still name the id's previous phone.
            # Such an RNTI stays unknown until the next read (the gNB parser asks for one 2.5 s after every new RNTI).
            if not self.core.get("fixed") and r.get("t_cu", 0.0) > self.core["t"] - CORE_SETTLE_S: continue
            im = self.core["cu_imsi"].get(r.get("cu"))
            if im: imsi_rnti[im] = rnti
        for svc, lst in (("va", self.cfg.get("va_imsis") or []), ("nv", self.cfg.get("nonva_imsis") or [])):
            ues = [u for u in self.ue.values() if u.svc == svc]; used = set()
            for u in ues:                      # 1) the phone that really uses this service session (its IP -> IMSI)
                u.imsi = ip_imsi.get(u.ip) if u.ip else None; u.map_src = "ip" if u.imsi else None
                if u.imsi: used.add(u.imsi)
            rest = [im for im in lst if im not in used]
            for u in ues:                      # 2) otherwise the .env IMSI order (what xapp.sh uses for yolo_1/2, live_1/2)
                if u.imsi is None and rest: u.imsi = rest.pop(0); u.map_src = "order"
            for u in ues: u.rnti = imsi_rnti.get(u.imsi) if u.imsi else None
        self.ppo_keys = {}
        for pre, lst in (("yolo", self.cfg.get("va_imsis") or []), ("live", self.cfg.get("nonva_imsis") or [])):
            for i, im in enumerate(lst): self.ppo_keys[im] = "%s_%d" % (pre, i + 1)

    # ------------------------------------------------------------------ measurement recording
    def rec_start(self, name, base, now=None):
        with self.lock:
            if self.rec is not None: raise RuntimeError("already recording")
            safe = re.sub(r"[^0-9A-Za-z가-힣_.-]+", "_", (name or "").strip()).strip("._") or "experiment"
            safe = safe[:60]; ts = time.strftime("%Y%m%d_%H%M%S"); d = Path(base) / ("%s_%s" % (safe, ts)); d.mkdir(parents=True, exist_ok=False)
            for u in self.ue.values(): u.reset_cum()
            self.slot_cum = self._slot_acc(); self.gpu_cum = self._gpu_acc(); self.cum_svc = {s: self._svc_acc() for s in ("va", "nv")}
            self.cum_cell = {k: Stat() for k in self.cum_cell}; self.rec_n = 0; self.rec_last = {}
            fj = open(d / (safe + ".replay.jsonl"), "w", buffering=1 << 16)
            ff = open(d / (safe + ".frames.csv"), "w", newline="", buffering=1 << 16); fw = csv.writer(ff)
            fw.writerow("t_s,ue,service_ue,frame_id,ip,rnti,ul_ms,ai_ms,wait_ms,decode_ms,infer_ms,server_other_ms,e2e_ms,e2e_src,dl_ms,qos_met,status,reason,model,sm,sm_mean,kb,w,h,detections".split(","))
            fs = gzip.open(d / (safe + ".slots.csv.gz"), "wb", compresslevel=3) if self.cfg.get("rec_slots", True) else None
            if fs is not None: fs.write(b"abs_slot,pos,gap_P,gap_N,run_cls,queue,model,t_ns\n")
            self.rec = dict(name=name.strip() or safe, safe=safe, dir=str(d), id=d.name, t0=now or time.time(), fj=fj, ff=ff, fs=fs, n_msg=0)
            self.rec_frames = fw; self.rec_slots = fs
            meta = self.meta(); fj.write(json.dumps(meta, ensure_ascii=False, separators=(",", ":")) + "\n")
            return meta

    def rec_write(self, msg):
        r = self.rec
        if r is not None:
            r["fj"].write(json.dumps(msg, ensure_ascii=False, separators=(",", ":")) + "\n"); r["n_msg"] += 1

    def rec_stop(self, now=None):
        with self.lock:
            r = self.rec
            if r is None: raise RuntimeError("not recording")
            summ = self.summary(now); d = Path(r["dir"]); safe = r["safe"]
            self.rec_write({"k": "end", "t": rnd(now or time.time(), 3), "summary": summ})
            self.rec = None; self.rec_frames = None; self.rec_slots = None
            for f in (r["fj"], r["ff"], r["fs"]):
                try:
                    if f is not None: f.close()
                except OSError: pass
        # the slow part (a second file made from the replay file) runs without the lock: the collectors keep feeding the live view
        (d / (safe + ".summary.txt")).write_text(summary_text(summ))
        try: write_timeseries(d / (safe + ".timeseries.csv"), d / (safe + ".replay.jsonl"))
        except Exception as e: summ.setdefault("notes", []).append("timeseries.csv 를 쓰지 못했습니다: %r" % (e,))
        summ["files"] = sorted(p.name for p in d.iterdir()) + [safe + ".summary.json"]; summ["files"] = sorted(set(summ["files"])); summ["id"] = d.name
        (d / (safe + ".summary.json")).write_text(json.dumps(summ, ensure_ascii=False, indent=1))
        return summ

    # ------------------------------------------------------------------ meta
    def meta(self):
        with self.lock:
            c = self.cfg; w = self.welcome or {}; mode = self.sysd.get("mode") or c.get("mode")
            return {"k": "meta", "t": rnd(time.time(), 3), "ver": 1, "sim": self.sim, "mode": mode,
                    "rec": self.rec_info(), "deadline_ms": self.deadline, "dl_fixed_ms": self.dl_fixed,
                    "map": self.map, "map_src": c.get("map_src"), "models": list(MODELS), "base_model": c.get("base_model", "26x"),
                    "gpu_sm": NSM if mode == "baseline" else (w.get("gpu_sm") or NSM), "agent_sm": w.get("gpu_sm"), "order_sm": ORDER, "tdd": w.get("tdd", c.get("tdd", "DDDDDDSUUU")), "input": w.get("input", c.get("input")),
                    "classes": c.get("classes"), "pusch_profiles": c.get("pusch_profiles"), "pdsch_profiles": c.get("pdsch_profiles"),
                    "baseline": {"ran_sm": c.get("baseline_ran_sm"), "yolo_sm": c.get("baseline_yolo_sm")}, "prb_total": c.get("prb_total", 273),
                    "va": c.get("va"), "nonva": c.get("nonva"), "host": c.get("host"),
                    "ues": {k: {"svc": u.svc, "sid": u.sid, "imsi": u.imsi, "ip": u.ip, "rnti": u.rnti, "map": u.map_src} for k, u in self.ue.items()}}

    def history(self, max_n=1500):
        """what a page needs when it opens: every tick since the measurement started (thinned when long), else the live window"""
        with self.lock:
            r = self.rec
            if r is None: return list(self.ticks)
            r["fj"].flush(); path = Path(r["dir"]) / (r["safe"] + ".replay.jsonl")
        try: return thin(list(iter_ticks(path)), max_n)
        except OSError: return list(self.ticks)

    def rec_info(self):
        r = self.rec
        return None if r is None else {"name": r["name"], "id": r["id"], "t0": rnd(r["t0"], 3), "dir": r["dir"]}

    # ------------------------------------------------------------------ the per-second tick
    def tick(self, now=None):
        now = now or time.time()
        with self.lock:
            dt = max(0.2, now - self.last_tick_t); self.last_tick_t = now; rec = self.rec is not None
            for rnti in [x for x, r in self.radio.items() if now - r.get("t", 0) > 20.0]: del self.radio[rnti]; self.need_remap = True
            if self.need_remap: self.need_remap = False; self._remap()
            for key, p in list(self.pending.items()):           # frames whose phone never reported an E2E: use the server-side estimate
                if now - p[1] > JOIN_WAIT_S: del self.pending[key]; self._va_final(p[0], None, now)
            t = {"k": "tick", "t": rnd(now, 3), "dt": rnd(dt, 3), "rec": rec, "el": rnd(now - self.rec["t0"], 1) if rec else None, "ue": {}, "svc": {}}
            ppo_ok = now - self.ppo_t < 2.0 and self.ppo; moni_ok = now - self.moni_t < 3.0 and self.moni
            gfs = [f for f in list(self.gf) + ([self.gf_cur] if self.gf_cur is not None and now - self.gf_cur[1] > 0.05 else []) if now - f[1] < 12.0]
            nslot = 6 * len(gfs)                                       # a frame has 6 UL slots (DDDDDDSUUU twice); slots without a grant print nothing
            for k, u in self.ue.items():
                w = u.win; d = {"on": now - u.last_t < 3.0, "sid": u.sid, "rnti": u.rnti, "n": w["n"], "fps": rnd(w["n"] / dt, 1), "fail": w["fail"],
                                "ul": rnd(mean(w["ul"])), "kb": rnd(mean(w["kb"]), 1), "mbps": rnd(w["bytes"] * 8.0 / dt / 1e6, 3)}
                if u.svc == "va":
                    for f in VA_LAT[1:]: d[f] = rnd(mean(w[f]))
                    d.update(qos=rnd(100.0 * w["met"] / w["n"], 1) if w["n"] else None, est=w["est"], src="est" if w["est"] and w["est"] == w["n"] else "phone",
                             model=u.model if now - u.last_model_t < 5.0 else None, sm=u.sm if now - u.last_model_t < 5.0 else None,
                             map=rnd(mean(w["map"])), models=dict(w["models"]), w=u.w, h=u.h)
                # radio: PRB / throughput from the PPO xApp input log, else the lab monitor xApp, else the gNB log itself
                r = self.radio.get(u.rnti) if u.rnti else None
                rd = dict.fromkeys(RADIO); psrc = None
                if r is not None and now - r.get("t", 0) < 4.0:
                    rd.update(snr=r.get("snr"), mcs=r.get("mcs"), bler=r.get("bler"), rsrp=r.get("rsrp"), ph=r.get("ph"), mac=r.get("mac"), dlmac=r.get("dlmac"),
                              dlmcs=r.get("dlmcs"), dlbler=r.get("dlbler"))
                    d["sync"] = r.get("sync"); d["blerw"] = rnd(r.get("bler_w"), 4); d["qm"] = r.get("qm"); d["pcmax"] = r.get("pcmax")
                    if rec and r.pop("d_new", False): u.cum["retx0"] += r.get("d0") or 0; u.cum["retx1"] += r.get("d1") or 0
                pk = self.ppo_keys.get(u.imsi)
                if ppo_ok and pk:
                    xs = [((p.get("input") or {}).get("ues") or {}).get(pk) for p in self.ppo]; xs = [x for x in xs if x]
                    if xs:
                        rd["prb"] = mean([x.get("ul_prb_avg") for x in xs]); rd["buf"] = mean([x.get("ul_buffer_bytes") for x in xs]); psrc = "ppo"
                        if rd["mac"] is None: rd["mac"] = mean([x.get("ul_tp_mbps") for x in xs])
                        if rd["snr"] is None: rd["snr"] = mean([x.get("ul_snr_db") for x in xs])
                if psrc is None and moni_ok and u.rnti in self.moni:
                    m = self.moni[u.rnti]; rd["prb"] = m.get("prb"); rd["buf"] = m.get("buf"); psrc = "moni"
                    if rd["mac"] is None: rd["mac"] = m.get("mac")
                if psrc is None and u.rnti and nslot:
                    rd["prb"] = sum(f[2].get(u.rnti, 0) for f in gfs) / float(nslot); psrc = "gnb"
                for f in RADIO: d[f] = rnd(rd[f], 5 if f in ("bler", "dlbler") else 2)
                d["prbsrc"] = psrc
                if rec:
                    c = u.cum
                    for f in RADIO: c[f].add(rd[f])
                    c["fps"].add(w["n"] / dt); c["mbps"].add(w["bytes"] * 8.0 / dt / 1e6)
                    el = max(1e-3, now - self.rec["t0"])
                    cd = {"n": c["n"], "fail": c["fail"], "fps": rnd(c["n"] / el, 2), "ul": rnd(c["ul"].mean()), "kb": rnd(c["kb"].mean(), 1), "mbps": rnd(c["bytes"] * 8.0 / el / 1e6, 3)}
                    cd["ulp"] = [rnd(x, 1) for x in c["ul"].pct((50, 90, 99))]
                    if u.svc == "va":
                        for f in VA_LAT[1:]: cd[f] = rnd(c[f].mean())
                        cd.update(qos=rnd(100.0 * c["met"] / c["n"], 2) if c["n"] else None, met=c["met"], est=c["est"], map=rnd(c["map"].mean(), 2),
                                  models=dict(c["models"]), combo=dict(c["combo"]),
                                  e2ep=[rnd(x, 1) for x in c["e2e"].pct((50, 90, 99))], aip=[rnd(x, 1) for x in c["ai"].pct((50, 90, 99))])
                    for f in RADIO: cd[f] = rnd(c[f].mean(), 5 if f in ("bler", "dlbler") else 2)
                    cd["blerx"] = rnd(c["retx1"] / c["retx0"], 5) if c["retx0"] else None
                    d["c"] = cd
                t["ue"][k] = d
            # services (slices)
            if self.quota["src"] in ("ppo", "moni") and now - self.quota["t"] > 5.0: self.quota.update(va=None, nv=None, src=None)     # that xApp stopped
            qv, qn = self.quota["va"], self.quota["nv"]
            if qv is not None and self.quota_ev: qv = mean([x[0] for x in self.quota_ev]); qn = mean([x[1] for x in self.quota_ev])    # a PPO xApp moves it several times a second
            self.quota_ev = []
            tot_prb = 0.0; tot_mac = 0.0; any_prb = False
            for s, q in (("va", qv), ("nv", qn)):
                us = [t["ue"][k] for k in UE_KEYS if self.ue[k].svc == s]
                prb = [x["prb"] for x in us if x["prb"] is not None]; mac = [x["mac"] for x in us if x["mac"] is not None]
                sd = {"quota": rnd(q, 1), "prb": rnd(sum(prb), 2) if prb else None, "mac": rnd(sum(mac), 2) if mac else None,
                      "mbps": rnd(sum(x["mbps"] or 0 for x in us), 3), "fps": rnd(sum(x["fps"] or 0 for x in us), 1), "n": sum(x["n"] for x in us)}
                if prb: tot_prb += sum(prb); any_prb = True
                if mac: tot_mac += sum(mac)
                if s == "va":
                    n = sum(self.ue[k].win["n"] for k in ("va1", "va2")); met = sum(self.ue[k].win["met"] for k in ("va1", "va2"))
                    sd["qos"] = rnd(100.0 * met / n, 1) if n else None
                    sd["e2e"] = rnd(mean(self.ue["va1"].win["e2e"] + self.ue["va2"].win["e2e"])); sd["ul"] = rnd(mean(self.ue["va1"].win["ul"] + self.ue["va2"].win["ul"]))
                    sd["ai"] = rnd(mean(self.ue["va1"].win["ai"] + self.ue["va2"].win["ai"])); sd["map"] = rnd(mean(self.ue["va1"].win["map"] + self.ue["va2"].win["map"]))
                else: sd["ul"] = rnd(mean(self.ue["nv1"].win["ul"] + self.ue["nv2"].win["ul"]))
                if rec:
                    a = self.cum_svc[s]; a["quota"].add(q); a["prb"].add(sd["prb"]); a["mac"].add(sd["mac"]); a["mbps"].add(sd["mbps"])
                    cd = {"quota": rnd(a["quota"].mean(), 1), "prb": rnd(a["prb"].mean()), "mac": rnd(a["mac"].mean()), "mbps": rnd(a["mbps"].mean(), 3)}
                    if s == "va":
                        c1, c2 = self.ue["va1"].cum, self.ue["va2"].cum; n = c1["n"] + c2["n"]
                        cd["n"] = n; cd["qos"] = rnd(100.0 * (c1["met"] + c2["met"]) / n, 2) if n else None
                        for f in ("e2e", "ul", "ai", "inf", "dl"):
                            nn = c1[f].n + c2[f].n; cd[f] = rnd((c1[f].s + c2[f].s) / nn) if nn else None
                        nn = c1["map"].n + c2["map"].n; cd["map"] = rnd((c1["map"].s + c2["map"].s) / nn, 2) if nn else None
                    else:
                        c1, c2 = self.ue["nv1"].cum, self.ue["nv2"].cum; cd["n"] = c1["n"] + c2["n"]
                        nn = c1["ul"].n + c2["ul"].n; cd["ul"] = rnd((c1["ul"].s + c2["ul"].s) / nn) if nn else None
                    sd["c"] = cd
                t["svc"][s] = sd
            t["quota_src"] = self.quota["src"] if qv is not None else None
            # cell
            l1 = self.l1 if self.l1 is not None and now - self.l1_t < 3.0 else None
            cell = {"prb": rnd(tot_prb) if any_prb else None, "unused": rnd(max(0.0, self.cfg.get("prb_total", 273) - tot_prb)) if any_prb else None,
                    "mac": rnd(tot_mac) if tot_mac else None, "ul": l1.get("ul") if l1 else None, "dl": l1.get("dl") if l1 else None,
                    "crc": l1.get("crc") if l1 else None, "cap": rnd(mean(self.cap_tot), 1), "ues": len([1 for r in self.radio.values() if now - r.get("t", 0) < 4.0])}
            if rec:
                for f in ("prb", "unused", "ul", "dl", "mac"): self.cum_cell[f].add(cell[f])
                cell["c"] = {f: rnd(self.cum_cell[f].mean()) for f in self.cum_cell}
            t["cell"] = cell
            t["gpu"] = self._gpu_tick(now, dt, rec, t)
            t["vcam"] = self.vcam if self.vcam and now - self.vcam.get("seen", 0) < 5.0 else None
            t["sys"] = dict(self.sysd); t["sys"]["src"] = {"ppo": bool(ppo_ok), "moni": bool(moni_ok), "gnb": any(now - r.get("t", 0) < 4.0 for r in self.radio.values()),
                                                         "slots": now - self.slot_t < 2.0, "agent": now - max(self.est_t, self.rep_t) < 3.0, "l1": l1 is not None}
            for u in self.ue.values(): u.reset_win()
            self.cap_tot = []; self.ppo = []
            self.ticks.append(t)
            if rec: self.rec_n += 1; self.rec_last = t; self.rec_write(t); self.rec["fj"].flush()
            return t

    def _gpu_tick(self, now, dt, rec, t):
        g = {}; sw = self.slot_win; slots_ok = sw["n"] > 0 and now - self.slot_t < 2.0
        est = self.est if self.est is not None and now - self.est_t < 3.0 else None
        rep = self.rep if self.rep is not None and now - self.rep_t < 3.0 else None
        va_n = sum(t["ue"][k]["n"] for k in ("va1", "va2")); vc = self.vcam if self.vcam and now - self.vcam.get("seen", 0) < 5.0 else None
        g["va_req"] = rnd(va_n / dt, 1); g["vcam_req"] = rnd(vc.get("req_s"), 1) if vc else None
        req = None
        if rep is not None and rep.get("window_ms"):
            if self.rep_new: self._rep_req = (rep.get("requests") or 0) * 1000.0 / float(rep["window_ms"]); self.rep_new = False
            req = getattr(self, "_rep_req", None)
        if req is None: req = va_n / dt + ((vc or {}).get("req_s") or 0.0)
        g["req"] = rnd(req, 1); g["req_src"] = "agent" if rep is not None else "services"
        g["queue"] = est.get("queue") if est else (rnd(sw["q"] / sw["n"], 2) if slots_ok else None); g["inflight"] = est.get("inflight") if est else None
        if est: g["est_ms"] = est.get("est_ms"); g["est_sm"] = est.get("est_sm"); g["yolo_now"] = est.get("yolo_sm_now"); g["ul_sm"] = est.get("ul_sm"); g["dl_sm"] = est.get("dl_sm")
        if rep: g["rep_sm"] = rnd(rep.get("sm_mean"), 1); g["share_yolo"] = rnd(rep.get("yolo_sm_time_mean"), 1); g["share_ran"] = rnd(rep.get("ran_sm_time_mean"), 1)
        waits = self.ue["va1"].win["wait"] + self.ue["va2"].win["wait"]; infs = self.ue["va1"].win["inf"] + self.ue["va2"].win["inf"]
        g["wait"] = rnd(mean(waits)); g["inf"] = rnd(mean(infs)); rej = sum(t["ue"][k]["fail"] for k in ("va1", "va2")); g["rej"] = rej
        busy = None
        if slots_ok:
            n = float(sw["n"]); busy = sw["busy"] / n
            g["sm"] = {"free": rnd(sw["free"] / n, 1), "run": rnd(sw["run"] / n, 1), "ran": rnd(sw["ran"] / n, 1), "unus": rnd(sw["unus"] / n, 1), "idle": rnd(sw["idle"] / n, 1),
                       "strand": rnd(sw["strand"] / n, 1), "pusch": rnd(sw["pusch"] / n, 1), "pdsch": rnd(sw["pdsch"] / n, 1), "n": sw["n"]}
            g["models"] = dict(sw["models"]); g["combo"] = dict(sw["combo"]); g["busy_src"] = "slots"
        else:
            inf = mean(infs)
            if inf is None and est and (est.get("est_ms") or {}): inf = (est["est_ms"] or {}).get(self.cfg.get("va", {}).get("model") or "26x")
            if inf is not None and req is not None: busy = min(1.0, req * inf / 1000.0); g["busy_src"] = "req*infer"
        g["busy"] = rnd(100.0 * busy, 1) if busy is not None else None
        cap = req / busy if busy is not None and busy >= 0.05 and req else None
        g["cap"] = rnd(cap, 1)
        v = "idle"
        if req and req > 0.2:
            w_ = mean(waits) or 0.0; q = g["queue"] or 0
            if (busy is not None and busy >= 0.90) or rej > 0 or q >= 3 or w_ > 0.5 * self.deadline: v = "sat"
            elif (busy is not None and busy >= 0.70) or w_ > 0.2 * self.deadline or q >= 2: v = "warn"
            else: v = "ok"
        g["verdict"] = v
        if self.nvml: g["nvml"] = self.nvml
        maps = self.ue["va1"].win["map"] + self.ue["va2"].win["map"]; g["map"] = rnd(mean(maps))
        if rec:
            a = self.gpu_cum; a["req"].add(req); a["busy"].add(busy * 100.0 if busy is not None else None); a["queue"].add(g["queue"]); a["cap"].add(cap)
            for x in waits: a["wait"].add(x)
            for x in maps: a["map"].add(x)
            if self.nvml: a["util"].add(self.nvml.get("util")); a["power"].add(self.nvml.get("power"))
            a["n_req"] += req * dt if req else 0; a["n_rej"] += rej; a[{"sat": "sat_s", "warn": "warn_s"}.get(v, "ok_s")] += 1
            c = {"req": rnd(a["req"].mean(), 1), "busy": rnd(a["busy"].mean(), 1), "queue": rnd(a["queue"].mean(), 2), "wait": rnd(a["wait"].mean()), "cap": rnd(a["cap"].mean(), 1),
                 "n_req": int(a["n_req"]), "n_rej": a["n_rej"], "sat_s": a["sat_s"], "warn_s": a["warn_s"], "map": rnd(a["map"].mean(), 2),
                 "util": rnd(a["util"].mean(), 1), "power": rnd(a["power"].mean(), 1)}
            sc = self.slot_cum
            if sc["n"]:
                n = float(sc["n"])
                c["sm"] = {"free": rnd(sc["free"] / n, 1), "run": rnd(sc["run"] / n, 1), "ran": rnd(sc["ran"] / n, 1), "unus": rnd(sc["unus"] / n, 1), "idle": rnd(sc["idle"] / n, 1),
                           "strand": rnd(sc["strand"] / n, 1), "pusch": rnd(sc["pusch"] / n, 1), "pdsch": rnd(sc["pdsch"] / n, 1), "n": sc["n"], "busy": rnd(100.0 * sc["busy"] / n, 1)}
                c["models"] = dict(sc["models"]); c["combo"] = dict(sc["combo"])
            # request-weighted model x SM of the VA frames (always available, also in Baseline)
            cr = {}; mr = {}
            for k in ("va1", "va2"):
                for ck, n in self.ue[k].cum["combo"].items(): cr[ck] = cr.get(ck, 0) + n
                for m, n in self.ue[k].cum["models"].items(): mr[m] = mr.get(m, 0) + n
            c["combo_req"] = cr; c["models_req"] = mr
            g["c"] = c
        self.slot_win = self._slot_acc()
        return g

    # ------------------------------------------------------------------ the slot scope (5 Hz)
    def scope(self, now=None, n=80):
        now = now or time.time()
        with self.lock:
            if not self.slot_ring or now - self.slot_t > 2.0: s = None
            else:
                rows = list(self.slot_ring)[-n:]
                s = {"k": "scope", "t": rnd(now, 3), "s0": rows[0][0], "pos": [r[1] for r in rows], "P": [r[2] for r in rows], "N": [r[3] for r in rows],
                     "run": [r[4] for r in rows], "q": [r[5] for r in rows], "m": [r[6] for r in rows]}
            if s is None and (self.est is None or now - self.est_t > 3.0): return None
            if s is None: s = {"k": "scope", "t": rnd(now, 3)}
            last = max(self.ue.values(), key=lambda u: u.last_model_t)
            s["last"] = {"model": last.model, "sm": last.sm, "age": rnd(now - last.last_model_t, 1)} if last.last_model_t else None
            if self.est is not None and now - self.est_t < 3.0: s["est"] = {"queue": self.est.get("queue"), "inflight": self.est.get("inflight"), "yolo": self.est.get("yolo_sm_now")}
            if self.rec is not None: self.rec_write(s)
            self.last_scope = s
            return s

    # ------------------------------------------------------------------ summary of a measurement
    def summary(self, now=None):
        r = self.rec; now = now or time.time(); el = now - r["t0"]; last = self.rec_last or {}
        s = {"name": r["name"], "id": r["id"], "dir": r["dir"], "start": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(r["t0"])),
             "end": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)), "t0": rnd(r["t0"], 3), "duration_s": rnd(el, 1), "ticks": self.rec_n,
             "mode": self.sysd.get("mode") or self.cfg.get("mode"), "sim": self.sim, "deadline_ms": self.deadline, "map_table": self.map, "base_model": self.cfg.get("base_model", "26x"),
             "va_cfg": self.cfg.get("va"), "nonva_cfg": self.cfg.get("nonva"), "ue": {}, "svc": {}, "notes": []}
        for k in UE_KEYS:
            u = self.ue[k]; c = (last.get("ue") or {}).get(k, {}).get("c") or {}
            d = dict(c); d.update(svc=u.svc, sid=u.sid, rnti=u.rnti, imsi=u.imsi, ip=u.ip, w=u.w, h=u.h)
            if u.svc == "va" and u.cum["n"] and u.cum["est"]: s["notes"].append("%s: %d / %d 프레임은 폰의 E2E 보고가 없어 UL + Inference + 고정 DL %.0f ms 로 추정했습니다" % (UE_NAMES.get(k, k), u.cum["est"], u.cum["n"], self.dl_fixed))
            s["ue"][k] = d
        for sv in ("va", "nv"): s["svc"][sv] = ((last.get("svc") or {}).get(sv) or {}).get("c") or {}
        s["cell"] = (last.get("cell") or {}).get("c") or {}; s["gpu"] = (last.get("gpu") or {}).get("c") or {}
        base = self.map.get(self.cfg.get("base_model", "26x")) or 57.5; m = (s["svc"].get("va") or {}).get("map")
        s["accuracy"] = {"map_mean": m, "map_base": base, "retention_pct": rnd(100.0 * m / base, 2) if m else None, "loss_pct": rnd(100.0 * (1 - m / base), 2) if m else None}
        s["vcam"] = last.get("vcam"); s["quota_src"] = last.get("quota_src")
        s["prb_src"] = {k: (last.get("ue") or {}).get(k, {}).get("prbsrc") for k in UE_KEYS}
        return s


def summary_text(s):
    def f(v, unit="", nd=1):
        return "-" if v is None else (("%." + str(nd) + "f") % v + unit if isinstance(v, (int, float)) else str(v))
    L = ["Measurement %s  (%s)" % (s["name"], s["id"]), "  %s -> %s  (%.0f s)  mode %s%s" % (s["start"], s["end"], s["duration_s"], s["mode"], "  [SIMULATION]" if s.get("sim") else ""), ""]
    L.append("VA service (deadline %.0f ms)" % s["deadline_ms"])
    L.append("  %-5s %-5s %8s %7s %8s %8s %8s %8s %8s %8s %8s %7s %8s %7s %7s %7s" % ("UE", "RNTI", "frames", "fps", "QoS %", "UL ms", "AI ms", "infer", "E2E ms", "E2E p99", "DL ms", "PRB", "MAC Mbps", "SNR", "MCS", "BLER"))
    for k in ("va1", "va2"):
        u = s["ue"].get(k) or {}; p = u.get("e2ep") or [None] * 3
        L.append("  %-5s %-5s %8s %7s %8s %8s %8s %8s %8s %8s %8s %7s %8s %7s %7s %7s" % (k, u.get("rnti") or "-", u.get("n", 0), f(u.get("fps")), f(u.get("qos"), nd=2), f(u.get("ul")), f(u.get("ai")),
                 f(u.get("inf")), f(u.get("e2e")), f(p[2]), f(u.get("dl")), f(u.get("prb")), f(u.get("mac"), nd=2), f(u.get("snr")), f(u.get("mcs")), f(u.get("bler"), nd=4)))
    v = s["svc"].get("va") or {}
    L.append("  service: QoS %s %%  E2E %s ms  UL %s ms  AI %s ms  quota %s PRB  used %s PRB  MAC %s Mbps" % (f(v.get("qos"), nd=2), f(v.get("e2e")), f(v.get("ul")), f(v.get("ai")), f(v.get("quota")), f(v.get("prb")), f(v.get("mac"), nd=2)))
    L += ["", "Non-VA service"]
    L.append("  %-5s %-5s %8s %7s %8s %8s %8s %7s %8s %7s %7s %7s" % ("UE", "RNTI", "frames", "fps", "UL ms", "UL p99", "app Mbps", "PRB", "MAC Mbps", "SNR", "MCS", "BLER"))
    for k in ("nv1", "nv2"):
        u = s["ue"].get(k) or {}; p = u.get("ulp") or [None] * 3
        L.append("  %-5s %-5s %8s %7s %8s %8s %8s %7s %8s %7s %7s %7s" % (k, u.get("rnti") or "-", u.get("n", 0), f(u.get("fps")), f(u.get("ul")), f(p[2]), f(u.get("mbps"), nd=2), f(u.get("prb")),
                 f(u.get("mac"), nd=2), f(u.get("snr")), f(u.get("mcs")), f(u.get("bler"), nd=4)))
    n = s["svc"].get("nv") or {}
    L.append("  service: quota %s PRB  used %s PRB  MAC %s Mbps" % (f(n.get("quota")), f(n.get("prb")), f(n.get("mac"), nd=2)))
    g = s.get("gpu") or {}; sm = g.get("sm") or {}
    L += ["", "GPU", "  YOLO requests %s req/s (total %s, rejected/failed %s)  load %s %%  sustainable ~%s req/s  queue %s  wait %s ms  saturated %s s / warning %s s" % (
        f(g.get("req")), g.get("n_req", "-"), g.get("n_rej", "-"), f(g.get("busy")), f(g.get("cap")), f(g.get("queue"), nd=2), f(g.get("wait")), g.get("sat_s", "-"), g.get("warn_s", "-"))]
    if sm: L.append("  SMs per slot (mean of %d slots): RAN reserved %s, offered to YOLO %s, YOLO running %s, stranded %s" % (sm.get("n", 0), f(sm.get("ran")), f(sm.get("free")), f(sm.get("run")), f(sm.get("strand"))))
    if g.get("combo"): L.append("  model x SM (share of busy slots): " + ", ".join("%s %.1f %%" % (k, 100.0 * v_ / max(1, sum(g["combo"].values()))) for k, v_ in sorted(g["combo"].items(), key=lambda kv: -kv[1])))
    if g.get("combo_req"): L.append("  model x SM (share of VA requests): " + ", ".join("%s %.1f %%" % (k, 100.0 * v_ / max(1, sum(g["combo_req"].values()))) for k, v_ in sorted(g["combo_req"].items(), key=lambda kv: -kv[1])))
    a = s.get("accuracy") or {}
    L += ["", "Accuracy (published COCO mAP50-95 of the models that ran, request-weighted; not measured on this pipeline)",
          "  mean mAP %s vs %s (%s fixed) -> retention %s %%, loss %s %%" % (f(a.get("map_mean"), nd=2), f(a.get("map_base")), s.get("base_model"), f(a.get("retention_pct"), nd=2), f(a.get("loss_pct"), nd=2))]
    c = s.get("cell") or {}
    L += ["", "Cell: UL %s Mbps (L1), MAC sum %s Mbps, PRB used %s / unused %s per UL slot" % (f(c.get("ul"), nd=2), f(c.get("mac"), nd=2), f(c.get("prb")), f(c.get("unused")))]
    L.append("Sources: quota %s, PRB per UE %s" % (s.get("quota_src") or "-", " ".join("%s=%s" % (k, v or "-") for k, v in (s.get("prb_src") or {}).items()) or "-"))
    for x in s.get("notes") or []: L.append("Note: " + x)
    return "\n".join(L) + "\n"


def flatten(d, pre="", out=None):
    out = {} if out is None else out
    for k, v in d.items():
        if isinstance(v, dict): flatten(v, pre + k + ".", out)
        elif isinstance(v, list): out[pre + k] = "/".join("" if x is None else str(x) for x in v)
        else: out[pre + k] = v
    return out


def iter_ticks(replay_path):
    """the per-second ticks of a replay file (a .replay.jsonl of a finished or a running measurement)"""
    with open(replay_path, "r") as fh:
        for ln in fh:
            if not ln.startswith('{"k":"tick"'): continue
            try: yield json.loads(ln)
            except ValueError: return                # a torn last line of a file that is still being written


def _ts_row(t):
    x = dict(t); x.pop("k", None); x.pop("sys", None); r = flatten(x)
    for k in list(r):
        if ".models." in k or ".combo" in k or ".est_" in k: r.pop(k)
    return r


def write_timeseries(path, replay_path):
    """one CSV row per second with every numeric field of the ticks (two passes over the replay file: columns, then rows)"""
    keys = []; seen = set()
    for t in iter_ticks(replay_path):
        for k in _ts_row(t):
            if k not in seen: seen.add(k); keys.append(k)
    with open(path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=keys); w.writeheader()
        for t in iter_ticks(replay_path): w.writerow(_ts_row(t))


def thin(ticks, max_n):
    """keep at most ~max_n ticks: the newest max_n/2 as they are, the older ones evenly decimated"""
    if len(ticks) <= max_n: return ticks
    keep = max_n // 2; old = ticks[:-keep]; step = max(2, -(-len(old) // (max_n - keep)))
    return old[::step] + ticks[-keep:]
