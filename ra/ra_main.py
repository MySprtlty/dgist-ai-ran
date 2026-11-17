#!/usr/bin/env python3
"""ra_main.py (ICNL, 2026-11-11): the Resource Allocator of the IITP 6G demo (ra/README.md, started by ../ra.sh). Runs next to its E2 side,
the FlexRIC xApp xapp_ccma_ra (xapp/), and talks over local UDP (one JSON object per datagram) to:

  xapp_ccma_ra      events mac / slice / ack / hello in (RA_SHELL_EVT 127.0.0.1:9823), commands quota / assoc / floor out (9824)
  Slicing xApp      budget / bye in (127.0.0.1:9820), ra_state every 10 ms and ra_qos every 100 ms out (127.0.0.1:9821)
                    (ra_slicing_link/INTERFACE.md)
  VA service hook   frame_start / progress / decide / done in (127.0.0.1:9822), decision back to the sender (va_hook.py)
  DGIST dApp agent  TCP 9810, role "ra": per-slot estimate of each YOLO model's server time (optional; static table without it)

Every 10 ms: apply the newest budget to the MAC in a safe order (Slice SM quotas, then UE association), plan the UL PRB floor
of every VA UE that is uploading a frame together with a tentative YOLO model (ra_policy.plan), send the floors that changed
(RC Style 2, only with RA_RC_FLOORS=1 in the E2 side), report ra_state. When the VA service has a whole frame it asks for
the model (ra_policy.decide) and later reports the result, which corrects the server-time estimate and feeds ra_qos.

  ra_main.py [--deadline-ms 100] [--agent 127.0.0.1:9810] [--ue-ip 10.45.0.2=4601,...] [--ue-label ue1=4601,...]
             [--static-budget 164:109 --va-rntis 4601,7a3c --nonva-rntis b210,c0de] [--objective acc|cost] [--log-dir D]
"""
import argparse
import csv
import json
import os
import select
import signal
import socket
import statistics
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "ra_slicing_link"))
from ra_policy import MAP, MODELS, STATIC_MS, Config, Frame, UeRadio, decide, plan  # noqa: E402
from ra_slicing_link import RaSide, check_budget, hostport, wall_ms  # noqa: E402
DAPP_CLIENT_DEFAULT = os.path.join(HERE, "..", "aerial-cuda-accelerated-ran", "cuPHY-CP", "dapp_hook", "python")   # the package's xapp_client.py

QUOTA_LABEL = "NR_UL_PRB_COUNT_V1"


def udp(bind):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 << 20)   # MAC + Slice reports every 10 ms must not overflow a busy moment
    s.bind(bind)
    s.setblocking(False)
    return s


def kv_map(s):
    """'a=b,c=d' -> {a: b, c: d} (keys and values lower case)"""
    out = {}
    for item in (s or "").split(","):
        if "=" in item:
            k, v = item.split("=", 1)
            out[k.strip().lower()] = v.strip().lower()
    return out


def p50(v):
    v = [x for x in v if x is not None]
    return round(statistics.median(v), 1) if v else None


class Csv:
    def __init__(self, path, cols):
        self.f = open(path, "w", newline="")
        self.w = csv.DictWriter(self.f, fieldnames=cols, extrasaction="ignore")
        self.w.writeheader()

    def row(self, **kw):
        self.w.writerow(kw)
        self.f.flush()


class Budget:
    """the budget being applied (INTERFACE.md 4.2)"""

    def __init__(self):
        self.run = self.seq = 0
        self.status, self.reason = "none", ""
        self.target = None            # (va, nonva)
        self.assoc = {}               # rnti -> service
        self.t_rx = self.quota_sent = self.assoc_sent = 0.0
        self.last_seq = {}            # run -> last processed seq
        self.src = ""


class Ra:
    def __init__(self, a):
        self.a = a
        self.cfg = Config(deadline_ms=a.deadline_ms, t_dl_ms=a.t_dl_ms, margin_ms=a.margin_ms, ul_slots_per_s=a.ul_slots,
                          layers=a.layers, objective=a.objective, acc_floor=a.acc_floor, total_prb=a.total_prb,
                          max_floor=a.total_prb, models=tuple(m for m in a.models.split(",") if m in MODELS))
        if a.static_ms:
            self.cfg.static_ms = {k: float(v) for k, v in (x.split(":") for x in a.static_ms.split(","))}
        self.slicing = RaSide(a.listen, a.slicing)
        self.slicing.sock.setblocking(False)   # the main loop drains every socket until it would block
        self.shell = udp(hostport(a.shell_evt, ("127.0.0.1", 9823)))
        self.shell_cmd = hostport(a.shell_cmd, ("127.0.0.1", 9824))
        self.hook = udp(hostport(a.hook, ("127.0.0.1", 9822)))
        self.cmd_id = 0
        self.hello = None
        self.radios = {}                       # rnti -> UeRadio
        self.mac_rx = 0.0
        self.slice_quota = (0, 0)              # quotas in force in the MAC (Slice SM report)
        self.slice_assoc = {}                  # rnti -> service, every connected UE (Slice SM report)
        self.slice_rx = 0.0
        self.budget = Budget()
        self.frames = {}                       # (rnti, frame_id) -> Frame
        self.floors_sent = {}                  # rnti -> (prb, wall ms)
        self.prev_plan = None                  # (GPU order, models) of the latest plan
        self.kappa = {}                        # model -> measured / predicted server time (EWMA)
        self.done = []                         # (t_ms, dict) of the last second, for ra_qos
        self.last_model = {}                   # rnti -> (model, sm)
        self.ue_ip, self.ue_label = kv_map(a.ue_ip), kv_map(a.ue_label)
        self.agent = None
        self.agent_try = 0.0
        self.est, self.est_rx = {}, 0.0        # server time per model with an idle GPU (estimates with queue 0, inflight 0)
        self.est_any_rx = 0.0
        self.gpu = []                          # RUNs sent and not reported back: (key, t_decide_ms, predicted service ms)
        self.stop = False
        self.n = {"budget": 0, "floor_cmd": 0, "floor_fail": 0, "decide": 0, "done": 0, "lost": 0, "mac": 0, "slice": 0}
        d = a.log_dir or os.path.join(os.getcwd(), "ra_logs", time.strftime("%y%m%d_%H%M%S"))
        os.makedirs(d, exist_ok=True)
        self.log_dir = d
        self.l_tick = Csv(os.path.join(d, "ticks.csv"), ["t_ms", "q_va", "feasible", "n_active", "floors", "models", "sum_floor"])
        self.l_frame = Csv(os.path.join(d, "frames.csv"),
                           ["rnti", "frame_id", "bytes", "t0_ms", "t_hdr_ms", "t1_ms", "net_ms", "model_tent", "floor_last", "model",
                            "deadline_ms", "pred_ms", "status", "reason", "sm", "wait_ms", "decode_ms", "infer_ms", "server_ms",
                            "t_done_ms", "e2e_est_ms", "on_time"])
        self.l_budget = Csv(os.path.join(d, "budgets.csv"), ["t_ms", "src", "run", "seq", "va", "nonva", "status", "reason"])
        self.l_cmd = Csv(os.path.join(d, "cmds.csv"), ["t_ms", "dir", "id", "cmd", "detail", "ok", "rtt_us", "reason"])
        if a.static_budget:
            va, nv = (int(x) for x in a.static_budget.split(":"))
            self.on_budget({"type": "budget", "run": 0, "seq": 1, "va_prb": va, "nonva_prb": nv, "total": a.total_prb, "min_prb": 1,
                            "va_rntis": [r for r in a.va_rntis.split(",") if r], "nonva_rntis": [r for r in a.nonva_rntis.split(",") if r],
                            "src": "static"})

    # ------------------------------------------------------------------------------------------------ outputs
    def cmd(self, obj, detail=""):
        self.cmd_id += 1
        obj = {**obj, "id": self.cmd_id, "v": 1}
        self.shell.sendto(json.dumps(obj, separators=(",", ":")).encode(), self.shell_cmd)
        self.l_cmd.row(t_ms=wall_ms(), dir="out", id=self.cmd_id, cmd=obj["type"], detail=detail)
        return self.cmd_id

    def send_state(self):
        ues = sorted(self.slice_assoc.items())
        self.slicing.send_state(self.budget.run, self.budget.seq, self.budget.status, self.slice_quota[0], self.slice_quota[1], ues,
                                self.budget.reason)

    def send_qos(self, now):
        win = [d for t, d in self.done if now - t <= 100]
        va = [d for d in win]
        self.slicing.send_qos(100, va={"requests": len(va), "on_time": sum(1 for d in va if d["on_time"]), "deadline_ms": self.cfg.deadline_ms,
                                       "e2e_p50_ms": p50(d["e2e_est_ms"] for d in va), "net_p50_ms": p50(d["net_ms"] for d in va),
                                       "proc_p50_ms": p50(d["server_ms"] for d in va)},
                              nonva={"requests": 0, "on_time": 0, "deadline_ms": 0, "e2e_p50_ms": None, "net_p50_ms": None, "proc_p50_ms": None},
                              ues=[{"rnti": r, "svc": s, "model": self.last_model.get(r, (None, None))[0], "floor_prb": self.floors_sent.get(r, (0, 0))[0],
                                    "sm": self.last_model.get(r, (None, None))[1]} for r, s in sorted(self.slice_assoc.items())])

    # ------------------------------------------------------------------------------------------------- inputs
    def on_shell(self, m):
        t = m.get("type")
        if t == "mac":
            self.n["mac"] += 1
            self.mac_rx = wall_ms()
            for ue in m.get("ues", []):
                r = ue["rnti"].lower()
                rad = self.radios.setdefault(r, UeRadio(r))
                rad.update(ue)
                d = rad.d_tbs               # MAC deliveries go to this UE's frames in order (one UL buffer)
                for f in sorted((f for f in self.frames.values() if f.rnti == r and not f.t1_ms), key=lambda f: f.t0_ms):
                    take = min(d, max(0.0, f.nbytes * self.cfg.overhead - f.delivered))
                    f.delivered += take
                    d -= take
                    if d <= 0:
                        break
        elif t == "slice":
            self.n["slice"] += 1
            q = {x["id"]: x["prb"] for x in m.get("quota", []) if x.get("label") == QUOTA_LABEL and x.get("low", 0) == 0}
            self.slice_quota = (q.get(1, 0), q.get(2, 0))
            self.slice_assoc = {u["rnti"].lower(): int(u.get("ul", 0)) for u in m.get("ues", [])}
            self.slice_rx = wall_ms()
        elif t == "ack":
            self.l_cmd.row(t_ms=wall_ms(), dir="ack", id=m.get("id"), cmd=m.get("cmd"), ok=m.get("ok"), rtt_us=m.get("rtt_us"), reason=m.get("reason"))
            if not m.get("ok"):
                if m.get("cmd") == "floor":
                    self.n["floor_fail"] += 1
                    if self.n["floor_fail"] in (1, 100):
                        print(f"[RA] floor not applied: {m.get('reason')}", flush=True)
                elif m.get("cmd") in ("quota", "assoc") and self.budget.status == "pending":
                    self.set_status("rejected", f"E2 {m.get('cmd')} control failed: {m.get('reason')}")
        elif t == "hello":
            self.hello = m
            print(f"[RA] E2 side: node {m.get('node')} RAN functions {m.get('rf')} RC floors {'on' if m.get('rc_floors') else 'off'}", flush=True)

    def set_status(self, status, reason=""):
        b = self.budget
        b.status, b.reason = status, reason
        self.l_budget.row(t_ms=wall_ms(), src=b.src, run=b.run, seq=b.seq, va=b.target[0] if b.target else "",
                          nonva=b.target[1] if b.target else "", status=status, reason=reason)

    def on_budget(self, m):
        b = self.budget
        run, seq = int(m.get("run", 0)), int(m.get("seq", 0))
        if seq <= b.last_seq.get(run, 0):
            self.send_state()
            return
        b.last_seq[run] = seq
        b.run, b.seq, b.src = run, seq, m.get("src", "")
        self.n["budget"] += 1
        ok, why = check_budget(m, total=self.cfg.total_prb)
        if not ok:
            self.set_status("rejected", why)
        else:
            b.target = (int(m["va_prb"]), int(m["nonva_prb"]))
            b.assoc = {r.lower(): 1 for r in m.get("va_rntis", [])}
            b.assoc.update({r.lower(): 2 for r in m.get("nonva_rntis", [])})
            b.t_rx, b.quota_sent, b.assoc_sent = wall_ms(), 0.0, 0.0
            self.set_status("pending")
            if not self.slice_quota[0] or b.target[0] >= self.slice_quota[0]:
                self.step_budget()       # growing VA quota: at once; shrinking: the next tick lowers the floors first
        self.send_state()

    def ue_rnti(self, m):
        ip, lab = str(m.get("ue_ip", "")).lower(), str(m.get("ue_id", "")).lower()
        r = self.ue_ip.get(ip) or self.ue_label.get(lab) or str(m.get("rnti", "")).lower()
        return r or None

    def on_hook(self, m, src):
        t = m.get("type")
        now = wall_ms()
        r = self.ue_rnti(m)
        if r is None:
            return
        key = (r, int(m.get("frame_id", 0)))
        if t == "frame_start":
            self.frames[key] = Frame(key, r, key[1], int(m.get("bytes", 0)), float(m.get("t0_ms", now)), float(m.get("t_hdr_ms", now)))
        elif t == "progress":
            f = self.frames.get(key)
            if f:
                f.received = int(m.get("received", -1))
        elif t == "decide":
            f = self.frames.get(key)
            if f is None:
                f = self.frames[key] = Frame(key, r, key[1], int(m.get("bytes", 0)), float(m.get("t0_ms", now)), float(m.get("t0_ms", now)))
            f.t1_ms = float(m.get("t1_ms", now))
            est = self.estimate()
            be = self.backlog_end(now)
            others = any(g.rnti != r and not g.t1_ms for g in self.frames.values())
            # 11/12: the cap is this frame's own tentative model (the last plan it took part in), not the UE's latest one from any
            # earlier plan: a frame that came in between two plans was capped by a stale model (VA service link test)
            cap = (f.model_tent or None) if others else None
            f.model, f.deadline_ms, f.pred_ms, feas = decide(f, est, self.kappa, now, self.cfg, be, cap)
            svc = f.pred_ms - max(0.0, be - now)               # the model's own service time, without the wait
            f.pred_raw = svc / self.kappa.get(f.model, 1.0)
            self.gpu.append((key, now, svc))
            self.n["decide"] += 1
            reply = {"type": "decision", "v": 1, "req": m.get("req"), "frame_id": key[1], "model": f.model,
                     "deadline_ms": round(f.deadline_ms, 1), "pred_ms": round(f.pred_ms, 1), "feasible": feas}
            self.hook.sendto(json.dumps(reply, separators=(",", ":")).encode(), src)
        elif t == "done":
            f = self.frames.pop(key, None)
            if f is None:
                return
            self.n["done"] += 1
            self.gpu = [g for g in self.gpu if g[0] != key]
            f.t_done_ms = float(m.get("t_done_ms", now))
            srv = m.get("latency_ms")
            ok = m.get("status") == "success"
            if m.get("decode_ms") is not None and m.get("infer_ms") is not None:
                service = float(m["decode_ms"]) + float(m["infer_ms"])          # without the agent's queue wait
            else:
                service = float(srv or 0) - float(m.get("wait_ms") or 0)
            if ok and service > 0 and getattr(f, "pred_raw", 0) > 0:
                k = self.kappa.get(f.model, 1.0)
                self.kappa[f.model] = min(3.0, max(0.5, 0.8 * k + 0.2 * service / f.pred_raw))
            e2e = f.t_done_ms - f.t0_ms + self.cfg.t_dl_ms
            rec = dict(rnti=r, frame_id=key[1], bytes=f.nbytes, t0_ms=f.t0_ms, t_hdr_ms=f.t_hdr_ms, t1_ms=f.t1_ms, net_ms=round(f.t1_ms - f.t0_ms, 1),
                       model_tent=f.model_tent, floor_last=f.floor, model=f.model, deadline_ms=round(f.deadline_ms, 1), pred_ms=round(f.pred_ms, 1),
                       status=m.get("status"), reason=m.get("reason", ""), sm=m.get("sm"), wait_ms=m.get("wait_ms"), decode_ms=m.get("decode_ms"),
                       infer_ms=m.get("infer_ms"), server_ms=srv, t_done_ms=f.t_done_ms, e2e_est_ms=round(e2e, 1),
                       on_time=ok and e2e <= self.cfg.deadline_ms)
            self.l_frame.row(**rec)
            self.done.append((now, rec))
            self.last_model[r] = (f.model, m.get("sm"))

    # --------------------------------------------------------------------------------------------- dApp agent
    def estimate(self):
        """server time per model with an idle GPU: the latest dApp estimate taken while its queue was empty (the RA models the
        queue of its own RUNs itself, backlog_end); {} = static table"""
        if self.est and wall_ms() - self.est_any_rx <= 1000:
            return self.est
        return {}

    def backlog_end(self, now):
        """when the RUNs the RA has sent (not reported back yet) should be done, if the GPU serves them one after another"""
        end = 0.0
        for _, t, svc in sorted(self.gpu, key=lambda g: g[1]):
            end = max(end, t) + svc
        return max(end, now)

    def agent_connect(self):
        if not self.a.agent or self.agent is not None or time.time() - self.agent_try < 2.0:
            return
        self.agent_try = time.time()
        try:
            sys.path.insert(0, self.a.dapp_client_dir)
            from xapp_client import DappAgent
            h, p = hostport(self.a.agent, ("127.0.0.1", 9810))
            ag = DappAgent(h, p, timeout=5.0)

            def on_msg(msg):
                if msg.get("type") == "estimate":
                    self.est_any_rx = wall_ms()
                    if not msg.get("queue") and not msg.get("inflight"):
                        self.est, self.est_rx = {k: float(v) for k, v in msg.get("est_ms", {}).items()}, wall_ms()
            w = ag.hello("ra", "ccma-ra", ["estimate"], every=self.a.agent_every, on_message=on_msg)
            if w is None:
                ag.close()
                return
            self.agent = ag
            print(f"[RA] dApp agent {self.a.agent}: models {w.get('models')} input {w.get('input')} gpu_sm {w.get('gpu_sm')}", flush=True)
        except (OSError, ImportError) as e:
            if self.agent_try and int(self.agent_try) % 30 < 2:
                print(f"[RA] dApp agent {self.a.agent} not reachable ({e}); static server times {self.cfg.static_ms}", flush=True)

    # --------------------------------------------------------------------------------------------- every tick
    def step_budget(self):
        b, now = self.budget, wall_ms()
        if b.status != "pending" or b.target is None:
            return
        if self.slice_quota != b.target:
            if not b.quota_sent or now - b.quota_sent > 300:
                self.cmd({"type": "quota", "va": b.target[0], "nonva": b.target[1]}, f"{b.target[0]}:{b.target[1]} seq {b.seq}")
                b.quota_sent = now
            elif now - b.t_rx > 3000:
                self.set_status("rejected", "MAC did not report the quotas within 3 s")
            return
        want = {r: s for r, s in b.assoc.items() if self.slice_assoc.get(r) != s}
        if want:
            if not b.assoc_sent or now - b.assoc_sent > 300:
                self.cmd({"type": "assoc", "ues": [{"rnti": r, "ul": s} for r, s in sorted(b.assoc.items())]}, f"{len(b.assoc)} UEs")
                b.assoc_sent = now
            elif now - b.t_rx > 3000:
                self.set_status("rejected", "MAC did not report the UE association within 3 s")
            return
        self.set_status("applied")

    def va_ues(self):
        va = [r for r, s in self.slice_assoc.items() if s == 1]
        return va or [r for r, s in self.budget.assoc.items() if s == 1]

    def tick(self):
        now = wall_ms()
        self.agent_connect()
        # 1. plan floors and tentative models for the VA frames that are uploading
        q_va = self.slice_quota[0] or self.cfg.total_prb
        if self.budget.status == "pending" and self.budget.target:
            q_va = min(q_va, self.budget.target[0])        # shrinking: floors first fit the new quota
        active = {}
        for f in self.frames.values():
            if not f.t1_ms:
                active.setdefault(f.rnti, []).append(f)
        for v in active.values():
            v.sort(key=lambda f: f.t0_ms)
        floors, models, info = plan(active, self.radios, q_va, self.estimate(), self.kappa, now, self.cfg, self.backlog_end(now),
                                    self.prev_plan)
        if active:
            self.prev_plan = (info.get("order"), models)
        for r, fr in active.items():
            for f in fr:
                f.floor, f.model_tent = floors.get(r, 0), models.get(r, "")
        changed = False
        for r in set(self.va_ues()) | set(floors) | set(self.floors_sent):
            want = floors.get(r, 0)
            prb, t = self.floors_sent.get(r, (0, 0.0))
            if want != prb or (want > 0 and now - t >= self.a.floor_refresh_ms):
                if want == 0 and prb == 0:
                    continue
                self.cmd({"type": "floor", "rnti": r, "prb": want, "validity_ms": self.a.floor_validity_ms}, f"{r} {want}")
                self.floors_sent[r] = (want, now)
                self.n["floor_cmd"] += 1
                changed |= want != prb
        if active or changed:
            self.l_tick.row(t_ms=now, q_va=q_va, feasible=info.get("feasible"), n_active=info.get("n"), floors=json.dumps(floors),
                            models=json.dumps(models), sum_floor=info.get("sum", 0))
        # 2. budget, after the floors (a shrinking quota goes out only once the floors fit)
        self.step_budget()
        # 3. drop frames that never completed / never reported
        for k, f in list(self.frames.items()):
            if (not f.t1_ms and now - f.t0_ms > 3 * self.cfg.deadline_ms + 2000) or (f.t1_ms and now - f.t1_ms > 5000):
                self.frames.pop(k)
                self.gpu = [g for g in self.gpu if g[0] != k]
                self.n["lost"] += 1
                self.l_frame.row(rnti=f.rnti, frame_id=f.frame_id, bytes=f.nbytes, t0_ms=f.t0_ms, t1_ms=f.t1_ms, status="lost")
        self.done = [(t, d) for t, d in self.done if now - t <= 1000]
        self.send_state()

    def run(self):
        signal.signal(signal.SIGINT, lambda *_: setattr(self, "stop", True))
        signal.signal(signal.SIGTERM, lambda *_: setattr(self, "stop", True))
        print(f"[RA] up: deadline {self.cfg.deadline_ms} ms, models {self.cfg.models}, objective {self.cfg.objective}, logs {self.log_dir}", flush=True)
        socks = {self.slicing.sock: "slicing", self.shell: "shell", self.hook: "hook"}
        next_tick = next_qos = next_print = time.time()
        while not self.stop:
            r, _, _ = select.select(list(socks), [], [], max(0.0, next_tick - time.time()))
            for s in r:
                while True:
                    try:
                        data, src = s.recvfrom(4096)
                    except (BlockingIOError, OSError):
                        break
                    try:
                        m = json.loads(data)
                    except ValueError:
                        continue
                    if not isinstance(m, dict):
                        continue
                    kind = socks[s]
                    if kind == "shell":
                        self.on_shell(m)
                    elif kind == "slicing":
                        if m.get("type") == "budget":
                            self.on_budget(m)
                        elif m.get("type") == "bye":
                            print(f"[RA] bye from the Slicing xApp (run {m.get('run')}): keeping {self.slice_quota}", flush=True)
                    else:
                        self.on_hook(m, src)
            t = time.time()
            if t >= next_tick:
                self.tick()
                next_tick += 0.010
                if next_tick < t:
                    next_tick = t + 0.010
            if t >= next_qos:
                self.send_qos(wall_ms())
                next_qos = t + 0.100
            if t >= next_print:
                next_print = t + 1.0
                print(f"[RA] {time.strftime('%H:%M:%S')} quota {self.slice_quota[0]}:{self.slice_quota[1]} budget seq {self.budget.seq} {self.budget.status}"
                      f" | frames {len(self.frames)} floors {{{', '.join(f'{r}:{p}' for r, (p, _) in sorted(self.floors_sent.items()) if p)}}}"
                      f" | est {'live' if self.estimate() else 'static'} kappa {{{', '.join(f'{k}:{v:.2f}' for k, v in self.kappa.items())}}} | {self.n}", flush=True)
        if self.a.cleanup:
            for r, (p, _) in self.floors_sent.items():
                if p:
                    self.cmd({"type": "floor", "rnti": r, "prb": 0, "validity_ms": 1})
        # the service quotas and UE association this RA put into the MAC (static budget, or a Slicing xApp that said bye) are removed
        # again (Slice SM delete of services 1 and 2 = no quotas, the default scheduler), unless --keep-quotas
        if not self.a.keep_quotas and (self.budget.target or any(self.slice_quota)):
            self.shell.setblocking(False)
            want = self.cmd({"type": "del"}, "services 1, 2 on exit")
            res, t_end = None, time.time() + 2.0
            while res is None and time.time() < t_end:
                r, _, _ = select.select([self.shell], [], [], 0.1)
                while r:
                    try:
                        m = json.loads(self.shell.recvfrom(4096)[0])
                    except (BlockingIOError, OSError):
                        break
                    except ValueError:
                        continue
                    if isinstance(m, dict) and m.get("type") == "ack" and m.get("id") == want:
                        res = m
                        self.on_shell(m)
            print(f"[RA] quotas removed on exit (Slice SM delete): {'ok' if res and res.get('ok') else 'NO ACK (E2 side down?) - quotas may remain: ' + str(res.get('reason') if res else 'timeout')}", flush=True)
        print(f"[RA] stopped: {self.n}; logs {self.log_dir}", flush=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--deadline-ms", type=float, default=100.0); ap.add_argument("--t-dl-ms", type=float, default=5.0)
    ap.add_argument("--margin-ms", type=float, default=5.0); ap.add_argument("--ul-slots", type=float, default=600.0)
    ap.add_argument("--layers", type=int, default=1); ap.add_argument("--total-prb", type=int, default=273)
    ap.add_argument("--models", default=",".join(MODELS)); ap.add_argument("--objective", default="acc", choices=["acc", "cost"])
    ap.add_argument("--acc-floor", type=float, default=0.0)
    ap.add_argument("--static-ms", default="", help="model:ms,... server times without the dApp estimate (default DGIST 80 SM 4K)")
    ap.add_argument("--agent", default="", help="DGIST dApp agent host:port (e.g. 127.0.0.1:9810); empty = static times")
    ap.add_argument("--agent-every", type=int, default=20, help="estimate every N slots (20 = 10 ms)")
    ap.add_argument("--dapp-client-dir", default=os.environ.get("DAPP_CLIENT_DIR") or DAPP_CLIENT_DEFAULT, help="folder of the DGIST xapp_client.py")
    ap.add_argument("--listen", default=os.environ.get("RA_SLICING_LISTEN", "")); ap.add_argument("--slicing", default=os.environ.get("RA_PPO_ADDR", ""))
    ap.add_argument("--shell-evt", default=os.environ.get("RA_SHELL_EVT", "")); ap.add_argument("--shell-cmd", default=os.environ.get("RA_SHELL_CMD", ""))
    ap.add_argument("--hook", default=os.environ.get("RA_HOOK", ""))
    ap.add_argument("--ue-ip", default="", help="UE IP = RNTI pairs, e.g. 10.45.0.2=4601,10.45.0.3=7a3c")
    ap.add_argument("--ue-label", default="", help="VA service ue_id = RNTI pairs, e.g. ue1=4601,ue2=7a3c")
    ap.add_argument("--static-budget", default="", help="VA:NONVA applied at start (without a Slicing xApp)")
    ap.add_argument("--va-rntis", default=""); ap.add_argument("--nonva-rntis", default="")
    ap.add_argument("--floor-validity-ms", type=int, default=60); ap.add_argument("--floor-refresh-ms", type=int, default=30)
    ap.add_argument("--log-dir", default=""); ap.add_argument("--cleanup", action="store_true", help="floors back to 0 on exit")
    ap.add_argument("--keep-quotas", action="store_true", help="leave the service quotas / association in the MAC on exit (default: removed)")
    Ra(ap.parse_args()).run()


if __name__ == "__main__":
    main()
