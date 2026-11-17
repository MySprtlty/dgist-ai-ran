#!/usr/bin/env python3
"""ra_policy.py (ICNL, 2026-11-11): the decisions of the Resource Allocator (IITP 6G demo), no I/O.

Every 10 ms (plan):     for the VA frames still uploading, joint choice of a YOLO model per UE and the UL PRB floor per slot
                        that lets the upload finish in time for that model, inside the VA quota of the Slicing xApp.
When a frame is complete (decide):  the model for its RUN and the server-side deadline left for it.
ra/README.md.
"""
import itertools
import math
from dataclasses import dataclass, field

MODELS = ["26n", "26s", "26m", "26l", "26x"]                                  # smallest .. largest (DGIST dApp agent names)
MAP = {"26n": 40.9, "26s": 48.6, "26m": 53.1, "26l": 55.0, "26x": 57.5}       # COCO mAP50-95 (DGIST dapp_hook/python/ra_stub.py)
# server time (queue + JPEG decode + inference, ms) without a dApp estimate: DGIST real L1, 80 SM, 4K batch 8
# (dapp_hook/cots/MANUAL.md:107). 26s / 26l were not measured there: no static value, the models need the live estimate.
STATIC_MS = {"26n": 11.4, "26m": 21.0, "26x": 38.2}
# TS 38.214 table 5.1.3.1-2 (256QAM): MCS -> (Qm, R x 1024)
MCS_T2 = [(2, 120), (2, 193), (2, 308), (2, 449), (2, 602), (4, 378), (4, 434), (4, 490), (4, 553), (4, 616), (4, 658),
          (6, 466), (6, 517), (6, 567), (6, 616), (6, 666), (6, 719), (6, 772), (6, 822), (6, 873), (8, 682.5), (8, 711),
          (8, 754), (8, 797), (8, 841), (8, 885), (8, 916.5), (8, 948)]
RE_PER_PRB = 138      # OAI default PUSCH: 13 symbols, DMRS 3 symbols with CDM group 1 


def bits_per_prb(mcs, layers=1):
    qm, r = MCS_T2[min(max(int(mcs), 0), len(MCS_T2) - 1)]
    return RE_PER_PRB * qm * r / 1024.0 * layers


@dataclass
class Config:
    deadline_ms: float = 100.0          # E2E: UE send -> result back at the UE
    t_dl_ms: float = 5.0                # result back over the air
    margin_ms: float = 5.0              # kept free in the upload budget
    decide_margin_ms: float = 3.0       # at decide: the model's server time must leave this much of the deadline
    ul_slots_per_s: float = 600.0       # TDD DDDDDDSUUU at 30 kHz (dgist-ai-ran config/gnb.conf:71-75)
    layers: int = 1
    overhead: float = 1.05              # TCP/TLS/IP bytes on top of the JPEG
    models: tuple = tuple(MODELS)
    objective: str = "acc"              # acc = largest accuracy in time | cost = lowest cost with mean mAP >= acc_floor
    acc_floor: float = 0.0              # mean mAP floor for objective cost (e.g. 51.75 = 0.9 x 26x)
    cost: dict = field(default_factory=lambda: dict(STATIC_MS))   # model cost for objective cost (replace by GFLOPs when measured)
    static_ms: dict = field(default_factory=lambda: dict(STATIC_MS))
    max_floor: int = 273
    total_prb: int = 273


class UeRadio:
    """UL radio state of one UE from the MAC SM reports (every 10 ms)."""

    def __init__(self, rnti):
        self.rnti = rnti
        self.mcs = 0
        self.bler = 0.0
        self.bsr = 0
        self.snr = 0.0
        self.last = None            # (tbs bytes, prb, rprb) cumulative
        self.k_bpp = 1.0            # measured bits per PRB / table value at the reported MCS (EWMA)
        self.n_bpp = 0
        self.d_tbs = 0              # bytes delivered in the last report interval

    def update(self, ue):
        self.mcs, self.bler, self.bsr, self.snr = int(ue.get("mcs", 0)), float(ue.get("bler", 0.0)), int(ue.get("bsr", 0)), float(ue.get("snr", 0.0))
        cur = (int(ue.get("tbs", 0)), int(ue.get("prb", 0)), int(ue.get("rprb", 0)))
        self.d_tbs = 0
        if self.last is not None and cur[0] >= self.last[0] and cur[1] >= self.last[1]:
            self.d_tbs = cur[0] - self.last[0]
            d_prb = cur[1] - self.last[1]
            if d_prb >= 20 and self.mcs > 0:
                k = 8.0 * self.d_tbs / d_prb / bits_per_prb(self.mcs)
                if 0.2 < k < 2.0:
                    a = 0.3 if self.n_bpp else 1.0
                    self.k_bpp = (1 - a) * self.k_bpp + a * k
                    self.n_bpp += 1
        self.last = cur

    def bpp(self, layers=1):
        """bits a PRB carries per UL slot now: table value at the current MCS x learnt correction x (1 - BLER)"""
        return max(1.0, bits_per_prb(self.mcs, layers) * self.k_bpp * (1.0 - min(self.bler, 0.9)))


@dataclass
class Frame:
    key: tuple
    rnti: str
    frame_id: int
    nbytes: int
    t0_ms: float                 # UE send time (server clock)
    t_hdr_ms: float              # request headers at the VA service
    received: int = -1           # bytes the VA service has (-1 = unknown: use MAC deliveries)
    delivered: float = 0.0       # MAC UL bytes of this UE since the start (FIFO share)
    t1_ms: float = 0.0           # upload complete
    model_tent: str = ""
    floor: int = 0
    model: str = ""
    deadline_ms: float = 0.0
    pred_ms: float = 0.0
    t_done_ms: float = 0.0

    def remaining(self, overhead):
        if self.received >= 0:
            return max(0, self.nbytes - self.received) * overhead
        return max(0.0, self.nbytes * overhead - self.delivered)


def server_ms(model, est, kappa, cfg):
    """predicted server time (queue + decode + inference) of a RUN of this model sent now"""
    v = est.get(model) if est else None
    if v is None or v < 0:
        v = cfg.static_ms.get(model)
    if v is None:
        return None
    return v * kappa.get(model, 1.0)


def _score(models, cfg):
    acc = sum(MAP[m] for m in models)
    if cfg.objective == "cost":
        if cfg.acc_floor and acc / len(models) < cfg.acc_floor:
            return None
        return -sum(cfg.cost.get(m, 1e3) for m in models)
    return acc


def _schedule(ents, models, order, now_ms, backlog_end_ms, svc, cfg):
    """One candidate: the UEs' frames run on the GPU one after another in `order`. Backward pass: the latest time each frame's
    inference may start so that every frame (and every later one) meets its deadline; the upload must be complete by then,
    which sets the UE's PRB floor. Returns (floors list in ents order, total) or None if the queue cannot meet the deadlines."""
    k = len(ents)
    start = [0.0] * k
    nxt = math.inf
    for i in reversed(order):
        rnti, t0, rem, b = ents[i]
        dl = t0 + cfg.deadline_ms - cfg.t_dl_ms - cfg.margin_ms
        start[i] = min(dl, nxt) - svc[models[i]]
        nxt = start[i]
    if start[order[0]] < max(now_ms, backlog_end_ms):
        return None                                   # the GPU is busy until after the first frame would have to start
    floors = []
    for i, (rnti, t0, rem, b) in enumerate(ents):
        left = start[i] - now_ms
        if left <= 0:
            if rem > 0:
                return None
            floors.append(0)
            continue
        floors.append(math.ceil(8.0 * rem / (left / 1000.0 * cfg.ul_slots_per_s * b)))
    return floors, sum(floors)


def plan(frames_by_ue, radios, q_va, est, kappa, now_ms, cfg, backlog_end_ms=0.0, prev=None):
    """frames_by_ue: {rnti: [active Frame, oldest first]}; est: server time per model with an idle GPU; backlog_end_ms: when the
    RUNs already sent are expected to be done; prev: (order, models) of the last plan. Joint choice of a model per UE and the GPU
    order (all orders up to 3 UEs, else earliest deadline first) that maximises the objective with the floors inside q_va; among
    equal scores the last plan's order and models are kept (so the floors do not flip every 10 ms), then the fewest PRBs.
    Returns (floors, tentative models, info)."""
    prev_order, prev_models = (prev or (None, None))
    ents = []
    for rnti, fr in frames_by_ue.items():
        if not fr:
            continue
        r = radios.get(rnti)
        b = r.bpp(cfg.layers) if r else bits_per_prb(10, cfg.layers)
        ents.append((rnti, fr[0].t0_ms, sum(f.remaining(cfg.overhead) for f in fr), b))
    if not ents:
        return {}, {}, {"feasible": True, "n": 0}
    svc = {m: server_ms(m, est, kappa, cfg) for m in cfg.models}
    svc = {m: v for m, v in svc.items() if v is not None}
    if not svc:
        return {}, {}, {"feasible": False, "n": len(ents), "reason": "no server time"}
    k = len(ents)
    edf = sorted(range(k), key=lambda i: (ents[i][1], ents[i][2]))
    orders = list(itertools.permutations(range(k))) if k <= 3 else [tuple(edf)]
    best = None
    for combo in itertools.product(list(svc), repeat=k):
        s = _score(combo, cfg)
        if s is None:
            continue
        if best is not None and s < best[0][0]:
            continue
        for order in orders:
            r = _schedule(ents, combo, order, now_ms, backlog_end_ms, svc, cfg)
            if r is None:
                continue
            fl, tot = r
            if tot > q_va or max(fl) > cfg.max_floor:
                continue
            same_order = prev_order is not None and [ents[i][0] for i in order] == list(prev_order)
            same_models = prev_models is not None and all(prev_models.get(ents[i][0]) == combo[i] for i in range(k))
            key = (s, same_models, same_order, -tot)
            if best is None or key > best[0]:
                best = (key, combo, fl, order)
    if best is None and cfg.objective == "cost" and cfg.acc_floor:   # nothing meets the accuracy floor: best accuracy in time
        alt = Config(**{**cfg.__dict__, "objective": "acc"})
        return plan(frames_by_ue, radios, q_va, est, kappa, now_ms, alt, backlog_end_ms, prev)
    if best is not None:
        _, combo, fl, order = best
        return ({ents[i][0]: int(fl[i]) for i in range(k)}, {ents[i][0]: combo[i] for i in range(k)},
                {"feasible": True, "n": k, "sum": int(sum(fl)), "order": [ents[i][0] for i in order]})
    # nothing fits: lightest model for all, the quota to the earliest deadlines first, each as much as it needs
    light = min(svc, key=svc.get)
    floors, left_q = {}, q_va
    for i in edf:
        rnti, t0, rem, b = ents[i]
        left = t0 + cfg.deadline_ms - cfg.t_dl_ms - cfg.margin_ms - svc[light] - now_ms
        need = cfg.max_floor if left <= 0 else math.ceil(8.0 * rem / (left / 1000.0 * cfg.ul_slots_per_s * b))
        floors[rnti] = max(0, min(need, left_q, cfg.max_floor))
        left_q -= floors[rnti]
    return floors, {e[0]: light for e in ents}, {"feasible": False, "n": k, "sum": int(sum(floors.values()))}


def decide(frame, est, kappa, now_ms, cfg, backlog_end_ms=0.0, cap_model=None):
    """model and server deadline for a complete frame; the RUNs already sent occupy the GPU until backlog_end_ms. cap_model:
    while other VA frames are still uploading, the joint plan's model for this UE is the largest allowed (a bigger one would
    take GPU time the plan gave to the frames behind it). Returns (model, deadline_ms, predicted server ms incl. the wait, feasible)."""
    left = cfg.deadline_ms - (frame.t1_ms - frame.t0_ms) - cfg.t_dl_ms
    wait = max(0.0, backlog_end_ms - now_ms)
    cap = server_ms(cap_model, est, kappa, cfg) if cap_model else None
    cand = []
    for m in cfg.models:
        p = server_ms(m, est, kappa, cfg)
        if p is not None and (cap is None or p <= cap + 1e-6):
            cand.append((m, p + wait))
    if not cand:
        return cfg.models[0], max(1.0, left), 0.0, False
    ok = [(m, p) for m, p in cand if p <= left - cfg.decide_margin_ms]
    if ok:
        if cfg.objective == "cost":
            good = [(m, p) for m, p in ok if not cfg.acc_floor or MAP[m] >= cfg.acc_floor] or ok
            m, p = min(good, key=lambda x: cfg.cost.get(x[0], 1e3))
        else:
            m, p = max(ok, key=lambda x: MAP[x[0]])
        return m, left, p, True
    m, p = min(cand, key=lambda x: x[1])
    return m, max(1.0, left), p, False
