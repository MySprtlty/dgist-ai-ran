#!/usr/bin/env python3
"""sources.py - where the dashboard's numbers come from (2026-11-12). Every collector only READS: logs of containers that already write
them, the two services' loopback metric feeds, the dApp agent as one more subscriber (role "dash"), files under runs/. Nothing is sent to a UE.

  parsers (pure, tested against saved logs):  GnbParser, MoniParser, parse_slot_rows, parse_l1_line, parse_core
  collectors (threads):                        ProcFollower, TailFollower, AgentClient, FeedPoller, SysPoller, CorePoller
"""
import glob, json, os, re, select, socket, ssl, subprocess, threading, time, urllib.request, urllib.error
from pathlib import Path


# ---------------------------------------------------------------------------------------------------------------- .env / config
def read_env(path):
    env = {}
    try:
        for line in Path(path).read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line: continue
            k, v = line.split("=", 1); v = v.split(" #", 1)[0].strip().strip('"').strip("'"); env[k.strip()] = v
    except OSError: pass
    return env


def _f(env, k, d, cast=float):
    try: return cast(env.get(k, d))
    except (TypeError, ValueError): return d


def _ints(s):
    return [int(x) for x in re.split(r"[,\s]+", s or "") if x.strip().isdigit()]


def load_cfg(root, env=None):
    """the dashboard's static configuration from /home/nvidia/_dgist/.env (process environment wins, like in va.sh)"""
    e = read_env(Path(root) / ".env"); e.update({k: v for k, v in (env or os.environ).items() if k.split("_")[0] in ("DASH", "VA", "NONVA", "DAPP", "VCAM", "DEMO", "TRAFFIC", "TG")})
    inp = e.get("DAPP_INPUT", "4k")
    mp = {}
    for kv in re.split(r"[,\s]+", e.get("DASH_MAP", "")):
        if "=" in kv:
            k, v = kv.split("=", 1)
            try: mp[k] = float(v)
            except ValueError: pass
    host, _, port = e.get("VA_AGENT", "127.0.0.1:9810").partition(":")
    return {
        "root": str(root), "mode": e.get("DEMO_MODE", "ours"), "input": inp, "prb_total": 273, "tdd": "DDDDDDSUUU",
        "deadline_ms": _f(e, "VA_DEADLINE_MS", 200.0), "dl_fixed_ms": _f(e, "VA_VIEWER_DL_MS", 20.0),
        "classes": _ints(e.get("DAPP_TENANT_CLASSES_FHD" if inp == "fhd" else "DAPP_TENANT_CLASSES", "")) or _ints(e.get("DAPP_TENANT_CLASSES", "24,40,48,56,64,72,80")),
        "pusch_profiles": _ints(e.get("DAPP_PUSCH_PROFILES", "")), "pdsch_profiles": _ints(e.get("DAPP_PDSCH_PROFILES", "")),
        "va_imsis": [x for x in e.get("VA_IMSIS", "").split(",") if x], "nonva_imsis": [x for x in e.get("NONVA_IMSIS", "").split(",") if x],
        "baseline_ran_sm": _f(e, "DASH_BASELINE_RAN_SM", 64, int), "baseline_yolo_sm": _f(e, "DASH_BASELINE_YOLO_SM", 60, int),
        "map": mp, "map_src": e.get("DASH_MAP_SRC", "Ultralytics YOLO26 공개값 (COCO val2017, 640 px, mAP50-95)"), "base_model": e.get("DASH_BASE_MODEL", "26x"),
        "va": {"model": e.get("VA_MODEL", "26x"), "backend": e.get("VA_BACKEND", "dapp"), "target_fps": _f(e, "VA_TARGET_FPS", 15), "jpeg_quality": _f(e, "VA_JPEG_QUALITY", 0.91),
               "camera": [_f(e, "VA_CAMERA_WIDTH", 1920, int), _f(e, "VA_CAMERA_HEIGHT", 1080, int)], "file_frame": [_f(e, "VA_FILE_WIDTH", 1920, int), _f(e, "VA_FILE_HEIGHT", 1080, int)],
               "max_inflight": _f(e, "VA_MAX_INFLIGHT", 4, int), "deadline_ms": _f(e, "VA_DEADLINE_MS", 200.0), "track": e.get("VA_TRACK", "1"), "drop_missed": e.get("VA_QOS_DROP_MISSED", "1"),
               "port": _f(e, "VA_PORT", 8000, int)},
        "nonva": {"width": _f(e, "NONVA_WIDTH", 1280, int), "height": _f(e, "NONVA_HEIGHT", 720, int), "fps": _f(e, "NONVA_FPS", 15), "jpeg_quality": _f(e, "NONVA_JPEG_QUALITY", 0.85),
                  "port": _f(e, "NONVA_PORT", 8001, int), "deadline_ms": _f(e, "NONVA_DEADLINE_MS", 250.0)},
        "viewer": {"on": e.get("VA_VIEWER", "1") != "0", "nonva": e.get("VA_VIEWER_NONVA", "1") != "0", "display_conf": _f(e, "VA_DISPLAY_CONF", 0.40), "dl_ms": _f(e, "VA_VIEWER_DL_MS", 20.0),
                   "freeze": e.get("VA_VIEWER_QOS_FREEZE", "1") != "0", "badge": e.get("VA_VIEWER_BADGE", "1") != "0", "rec_badge": e.get("VA_VIEWER_REC_BADGE", "1") != "0",
                   "model_text": "mix" if e.get("VA_MODEL_SELECT", "0") not in ("", "0") else e.get("VA_MODEL", "26x"),
                   "relay": e.get("DASH_VIEWER", "1") != "0", "va_addr": e.get("DASH_VIEWER_VA", ""), "nv_addr": e.get("DASH_VIEWER_NV", "")},
        "cap": {"fps": max(1, min(60, _f(e, "DASH_CAP_FPS", _f(e, "VA_VIEWER_REC_FPS", 30, int), int))), "mbps": max(0.5, min(50.0, _f(e, "DASH_CAP_MBPS", _f(e, "VA_VIEWER_REC_MBPS", 6.0)))),
                "max_min": max(0.0, _f(e, "DASH_CAP_MAX_MIN", _f(e, "VA_VIEWER_REC_MAX_MIN", 20.0))), "scale": max(0.0, min(3.0, _f(e, "DASH_CAP_SCALE", 0.0))),
                "prefix": "".join(ch for ch in e.get("DASH_CAP_PREFIX", e.get("VA_VIEWER_REC_PREFIX", e.get("DEMO_MODE", "ours"))) if ch.isascii() and (ch.isalnum() or ch in "-_"))[:32]},
        "ctl": e.get("DASH_CTL", "1") != "0", "ctl_dry": e.get("DASH_CTL_DRYRUN", "0") != "0",
        "vcam_env": {"fps": _f(e, "VCAM_FPS", 10), "inflight": _f(e, "VCAM_INFLIGHT", 4, int), "jpeg_quality": _f(e, "VCAM_JPEG_QUALITY", 0.70), "frames": e.get("VCAM_FRAMES", "")},
        "agent": (host or "127.0.0.1", int(port or 9810)), "ppo_bundle": e.get("PPO_BUNDLE_NAME", ""),
        "tgen_file": e.get("TRAFFIC_LOAD_FILE") or "/dev/shm/dgist_tgen_load", "tgen_levels": e.get("TRAFFIC_LEVELS") or "low=1,mid=2,high=3",
        "l1_container": e.get("DASH_L1_CONTAINER", "dgist-l1"), "gnb_container": e.get("DASH_GNB_CONTAINER", "dgist-gnb"), "yolo_container": "dgist-yolo",
        "core_since": e.get("DASH_CORE_SINCE", "10 days ago"), "amf_info": e.get("DASH_AMF_INFO", "127.0.0.5:9090"), "slot_csv": e.get("DASH_SLOT_CSV", "/tmp/dapp_tenant_slots.csv"), "l1_follow": e.get("DASH_L1_FOLLOW", "1") != "0", "rec_slots": e.get("DASH_REC_SLOTS", "1") != "0",
    }


# ---------------------------------------------------------------------------------------------------------------- gNB log
RE_HDR = re.compile(r"^\[(\d\d):(\d\d):(\d\d\.\d+)\] \[NR_MAC\]\s+I\s+Frame\.Slot (\d+)\.(\d+)")
RE_UE1 = re.compile(r"^UE RNTI ([0-9a-f]{4}) CU-UE-ID (\d+) (in-sync|out-of-sync) PH (-?\d+) dB PCMAX (-?\d+) dBm(?:, average RSRP (-?\d+))?")
RE_DL = re.compile(r"^UE ([0-9a-f]{4}): dlsch_rounds (\d+)/(\d+)/(\d+)/(\d+), dlsch_errors (\d+), pucch0_DTX (\d+), BLER ([0-9.]+) MCS \(\d+\) (\d+)")
RE_UL = re.compile(r"^UE ([0-9a-f]{4}): ulsch_rounds (\d+)/(\d+)/(\d+)/(\d+), ulsch_errors (\d+), ulsch_DTX (\d+), BLER ([0-9.]+) MCS \(\d+\) (\d+)(?: \(Qm (\d+).*?\))?(?: NPRB (\d+))?.*?SNR (-?[0-9.]+) dB")
RE_MAC = re.compile(r"^UE ([0-9a-f]{4}): MAC:\s+TX\s+(\d+) RX\s+(\d+) bytes")
RE_GRANT = re.compile(r"\[UL_SERVICE_PRB\] pusch=(\d+)\.(\d+) revision=(\d+) rnti=([0-9a-f]{4}) service=(\d+) quota=(\d+) service_used=(\d+) ue_used=(\d+) grant=(\d+) retx=(\d+) total=(\d+) cap=(\d+)")
RE_CAP = re.compile(r"\[UL_TOTAL_PRB_CAP\] pusch=(\d+)\.(\d+) cap=(\d+) prior=(\d+) new=(\d+) retx=(\d+) total=(\d+) remaining=(\d+)")
RE_DONE = re.compile(r"\[E2_SLICE_TRANSITION_DONE\] revision=(\d+) s1_prbs=(\d+) s2_prbs=(\d+)")
RE_RRC = re.compile(r"UE ID (\d+) RNTI ([0-9a-f]{4})\) Received RRCSetupComplete")


class GnbParser:
    """one line of `docker logs dgist-gnb` at a time. The MAC prints a stats block every 128 frames (a time-stamped 'Frame.Slot' header, then
    un-stamped lines per UE) and, for about one frame a second, the UL grants of every scheduled UE ([UL_SERVICE_PRB], the hsw MAC patch)."""

    def __init__(self, hub, on_attach=None):
        self.hub = hub; self.t_blk = None; self.on_attach = on_attach; self.seen = set(); self.n = dict(hdr=0, ue=0, grant=0, cap=0, quota=0, rrc=0)

    def feed(self, line, now=None):
        line = line.strip()
        if not line: return
        if line[0] == "[":
            if "UL_SERVICE_PRB" in line:
                m = RE_GRANT.search(line)
                if m:
                    self.hub.on_gnb_grant(m.group(4), (int(m.group(1)), int(m.group(2))), int(m.group(9)), int(m.group(5)), int(m.group(6)), now); self.n["grant"] += 1
            elif "UL_TOTAL_PRB_CAP" in line:
                m = RE_CAP.search(line)
                if m: self.hub.on_gnb_cap(int(m.group(7)), int(m.group(3))); self.n["cap"] += 1
            elif "Frame.Slot" in line:
                m = RE_HDR.match(line)
                if m: self.t_blk = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3)); self.n["hdr"] += 1
            elif "E2_SLICE_TRANSITION_DONE" in line:
                m = RE_DONE.search(line)
                if m: self.hub.on_quota(int(m.group(2)), int(m.group(3)), "gnb", now); self.n["quota"] += 1
            elif "RRCSetupComplete" in line:
                self.n["rrc"] += 1
                if self.on_attach: self.on_attach()
            return
        if not line.startswith("UE "): return
        m = RE_UE1.match(line)
        if m:
            rnti = m.group(1)
            if rnti not in self.seen:
                self.seen.add(rnti)
                if self.on_attach: self.on_attach()
            self.hub.on_gnb_ue(rnti, {"cu": int(m.group(2)), "sync": m.group(3) == "in-sync", "ph": int(m.group(4)), "pcmax": int(m.group(5)),
                                      "rsrp": int(m.group(6)) if m.group(6) else None}, self.t_blk, now); self.n["ue"] += 1; return
        m = RE_UL.match(line)
        if m:
            self.hub.on_gnb_ue(m.group(1), {"ul0": int(m.group(2)), "ul1": int(m.group(3)), "ulerr": int(m.group(6)), "bler": float(m.group(8)), "mcs": int(m.group(9)),
                                            "qm": int(m.group(10)) if m.group(10) else None, "snr": float(m.group(12))}, None, now); return
        m = RE_DL.match(line)
        if m:
            self.hub.on_gnb_ue(m.group(1), {"dl0": int(m.group(2)), "dl1": int(m.group(3)), "dlbler": float(m.group(8)), "dlmcs": int(m.group(9))}, None, now); return
        m = RE_MAC.match(line)
        if m: self.hub.on_gnb_ue(m.group(1), {"tx": int(m.group(2)), "rx": int(m.group(3))}, self.t_blk, now)


# ---------------------------------------------------------------------------------------------------------------- lab monitor xApp log
RE_MONI_UE = re.compile(r"^\s+UE=([0-9a-f]{4}) service=(\d+)\((\S+?)\) service_budget=(\S+) PRB_used_avg=(\S+) UL_use273=\S+ buffer=(\S+?)B SNR=(\S+?)dB UL_MAC=(\S+?)Mbps")


def _num(s):
    try: return float(re.sub(r"[^0-9.+-]", "", s))
    except (TypeError, ValueError): return None


class MoniParser:
    """log/moni_log/ul_input_*.log of the researchers' monitor xApp (./xapp.sh moni start): one block per ~110 ms window"""

    def __init__(self, hub):
        self.hub = hub; self.blk = None; self.n = 0

    def feed(self, line, now=None):
        if line.startswith("[UL]"): self.blk = {}; return
        if self.blk is None: return
        m = RE_MONI_UE.match(line)
        if m:
            self.blk[m.group(1)] = {"service": int(m.group(2)), "budget": _num(m.group(4)), "prb": _num(m.group(5)), "buf": _num(m.group(6)), "snr": _num(m.group(7)), "mac": _num(m.group(8))}
        elif line.lstrip().startswith("TOTAL"):
            if self.blk: self.hub.on_moni(self.blk, now); self.n += 1
            self.blk = None


# ---------------------------------------------------------------------------------------------------------------- tenant slot CSV / L1 line
def parse_slot_rows(lines):
    """abs_slot,sfn,slot,pos,P,N,part,class,set,segments,paused_us,known,plan_ul_sm,plan_dl_sm,t_ns,gap_P,gap_N,run_cls,queue,model -> hub rows"""
    rows = []
    for ln in lines:
        p = ln.split(",")
        if len(p) < 20 or not p[0].isdigit(): continue
        try: rows.append((int(p[0]), int(p[3]), int(p[15]), int(p[16]), int(p[17]), int(p[18]), p[19].strip(), int(p[14])))
        except ValueError: continue
    return rows


RE_L1 = re.compile(r"Cell +(\d+) \| DL +([0-9.]+) Mbps +(\d+) Slots \| UL +([0-9.]+) Mbps +(\d+) Slots CRC +(\d+) \( *(\d+)\)")


def parse_l1_line(line):
    m = RE_L1.search(line)
    if not m: return None
    return {"cell": int(m.group(1)), "dl": float(m.group(2)), "dl_slots": int(m.group(3)), "ul": float(m.group(4)), "ul_slots": int(m.group(5)), "crc": int(m.group(6)), "crc_tot": int(m.group(7))}


def tgen_state(cfg, env, procs):
    """the 5-cell traffic generator (RU emulator + testMAC inside the L1 container, ./tgen.sh): the .env switch, the two tool processes and the load level
    of the 8-byte control word both tools map (bits 63..56 = block selection from extended frame F on, 55..48 = before F, 47..0 = F; 0 = auto; scripts/tgen_load.py)"""
    run = "/ru_emulator " in procs and "/test_mac " in procs
    d = {"on": (env.get("TRAFFIC_GEN") or "0").strip() == "1", "run": run, "load": None, "sel": None}
    if not run: return d
    try:
        with open(cfg.get("tgen_file") or "/dev/shm/dgist_tgen_load", "rb") as fh: b = fh.read(8)
    except OSError: return d
    if len(b) < 8: return d
    names = {}
    for kv in re.split(r"[,\s]+", env.get("TRAFFIC_LEVELS") or cfg.get("tgen_levels") or ""):
        k, _, x = kv.partition("=")
        if x.isdigit(): names[int(x)] = k
    names[0] = "auto"; v = int.from_bytes(b, "little")
    sel = (v >> 56) if (time.time_ns() - 3640000000) // 10000000 >= (v & ((1 << 48) - 1)) else (v >> 48) & 255
    d["sel"] = sel; d["load"] = names.get(sel, "block%d" % sel)
    return d


# ---------------------------------------------------------------------------------------------------------------- core: RNTI <-> IMSI <-> IP
RE_ANSI = re.compile(r"\x1b\[[0-9;]*m")
RE_AMF_TS = re.compile(r"(\d\d)/(\d\d) (\d\d):(\d\d):(\d\d)\.(\d{3}):")
RE_AMF_ID = re.compile(r"RAN_UE_NGAP_ID\[(\d+)\] AMF_UE_NGAP_ID\[(\d+)\]( TAC\[)?")
RE_AMF_WHO = re.compile(r"\[(suci-[0-9A-Za-z-]+|imsi-\d+|Unknown ID)\]")
RE_AMF_HDR = re.compile(r"\[gmm\] \w+: (?:\[imsi-(\d+)\] )?(Registration request|Service request|Identity response|Deregistration request) \(")
RE_AMF_SUCI = re.compile(r"\[(suci-[0-9A-Za-z-]+)\]\s+SUCI \(")
RE_AMF_REG = re.compile(r"\[imsi-(\d+)\] Registration complete")
RE_SMF_IP = re.compile(r"UE SUPI\[imsi-(\d+)\] DNN\[[^\]]*\] IPv4\[([0-9.]+)\]")
CORE_NEAR_S = 0.05       # lines the AMF prints while it handles one message carry the same millisecond
CORE_PEND_S = 10.0       # InitialUEMessage -> Registration complete took at most 3 s in three days of this lab's journal


def parse_core(amf_lines, smf_lines):
    """which phone (IMSI) sits on which gNB UE id, from the AMF journal; its IP from the SMF journal.

    CU-UE-ID of the gNB stats == RAN_UE_NGAP_ID of the AMF. The ids restart with every gNB connection AND are reused inside one, and a
    phone that comes back with a Service request gets a new id without a "Registration complete" line, so "last id before the line
    Registration complete" (ue_table.py) can name the wrong phone. This follows every id instead:
      InitialUEMessage ... RAN_UE_NGAP_ID[n] AMF_UE_NGAP_ID[a] TAC[   a NEW connection on id n (whatever n named before is void)
      [suci-S] 5G-S_TMSI / Known UE by / 5G-S_GUTI[AMF_ID / SUCI       the AMF says which UE context this message belongs to (S = that
                                                                      UE's concealed id, one per fresh registration)
      [imsi-X] Registration complete                                  X = the one connection still waiting for its identity
      RAN_UE_NGAP_ID[n] AMF_UE_NGAP_ID[a] without TAC                 the AMF drops that connection
      gNB-N2 accepted / Number of gNBs                                all ids void
    S -> IMSI is learnt from the same journal (give it days, not only the current gNB run). Whenever two readings are possible the id
    stays WITHOUT an IMSI: the dashboard then shows "RNTI unknown" for that phone, never another phone's radio numbers.
    returns ({id: imsi}, {imsi: ip}, info)"""
    ids = {}; suci_imsi = {}; blk = None; wait = []; ctx = None; loose = None; t = 0.0

    def near(t0): return t0 is not None and 0.0 <= t - t0 < CORE_NEAR_S

    def pend(e): return 0.0 <= t - e["t"] <= CORE_PEND_S

    def ident(e, who):
        if who.startswith("imsi-"): e["imsi"] = who[5:]
        elif who.startswith("suci-"):
            e["suci"] = who
            if who in suci_imsi: e["imsi"] = suci_imsi[who]
        if e["imsi"]: only(e)

    def only(e):                               # one connection per phone: an older id that still names this IMSI is stale
        for k in [k for k, o in ids.items() if o is not e and o["imsi"] == e["imsi"]]: del ids[k]

    for raw in amf_lines:
        ln = RE_ANSI.sub("", raw)
        m = RE_AMF_TS.search(ln)
        if m:
            mo, d, h, mi, s, ms = (int(x) for x in m.groups()); t = ((mo * 31 + d) * 24 + h) * 3600.0 + mi * 60 + s + ms / 1000.0
        if "gNB-N2 accepted" in ln or "Number of gNBs is now" in ln:
            ids.clear(); blk = None; wait = []; ctx = None; loose = None; continue
        if "InitialUEMessage (" in ln:
            blk = {"t": t, "who": None, "n": None}; continue
        m = RE_AMF_ID.search(ln)
        if m:
            n, a = int(m.group(1)), int(m.group(2))
            if m.group(3):                     # a new connection on id n
                e = ids[n] = {"amf": a, "t": t, "imsi": None, "suci": None, "st": "new"}
                if blk and blk["n"] is None and near(blk["t"]):
                    blk["n"] = n; wait.append(n)
                    if blk["who"]: ident(e, blk["who"])
            else:                              # the connection (n, a) is dropped; the line may name the UE it belonged to
                e = ids.get(n)
                if e and e["amf"] == a:
                    w = RE_AMF_WHO.search(ln[:m.start()])
                    if w and w.group(1).startswith("suci-") and e["imsi"] and e["st"] == "ok": suci_imsi.setdefault(w.group(1), e["imsi"])
                    del ids[n]
            continue
        if "5G-S_TMSI[" in ln:
            w = RE_AMF_WHO.search(ln)
            if w and "Unknown" not in ln:
                if "Known UE by" in ln:        # printed while the NAS message of the block is handled
                    wait = [n for n in wait if n in ids and near(ids[n]["t"])]
                    if len(wait) == 1: ident(ids[wait[0]], w.group(1))
                elif blk and near(blk["t"]):
                    blk["who"] = w.group(1)
                    if blk["n"] in ids: ident(ids[blk["n"]], blk["who"])
            continue
        m = RE_AMF_HDR.search(ln)
        if m:
            kind = m.group(2); cid = None
            if kind != "Identity response":
                wait = [n for n in wait if n in ids and near(ids[n]["t"])]
                if len(wait) == 1: cid = wait[0]
                for n in (wait if len(wait) > 1 else ()): ids[n]["st"] = "amb"      # two messages printed before their NAS parts
                wait = []
            if cid is not None:
                if kind == "Service request": ids[cid]["st"] = "svc"                 # no Registration complete will follow
                if m.group(1): ident(ids[cid], "imsi-" + m.group(1))
            elif kind == "Registration request": loose = t                           # on a connection that already exists
            ctx = {"kind": kind, "t": t, "id": cid, "imsi": m.group(1)}
            continue
        if "5G-S_GUTI[AMF_ID" in ln:
            w = RE_AMF_WHO.search(ln)
            if w and w.group(1) != "Unknown ID" and ctx and near(ctx["t"]) and ctx["id"] in ids: ident(ids[ctx["id"]], w.group(1))
            continue
        m = RE_AMF_SUCI.search(ln)
        if m:
            if ctx and near(ctx["t"]):
                if ctx["kind"] == "Deregistration request" and ctx["imsi"]: suci_imsi.setdefault(m.group(1), ctx["imsi"])
                elif ctx["kind"] == "Registration request" and ctx["id"] in ids: ident(ids[ctx["id"]], m.group(1))
                elif ctx["kind"] == "Identity response":                             # answer of a UE the AMF did not know
                    fresh = [e for e in ids.values() if e["st"] == "new" and not e["suci"] and not e["imsi"] and pend(e)]
                    if len(fresh) == 1: ident(fresh[0], m.group(1))
                    for e in (fresh if len(fresh) > 1 else ()): e["st"] = "amb"
            continue
        m = RE_AMF_REG.search(ln)
        if m:
            x = m.group(1); mine = [e for e in ids.values() if e["imsi"] == x and e["st"] == "new" and pend(e)]
            cand = [e for e in ids.values() if e["imsi"] is None and e["st"] == "new" and pend(e)]
            if mine: max(mine, key=lambda e: e["t"])["st"] = "ok"
            elif loose is not None and 0.0 <= t - loose <= CORE_PEND_S:               # may be the update of a connected phone
                for e in cand: e["st"] = "amb"
            elif len(cand) == 1:
                e = cand[0]; e["imsi"] = x; e["st"] = "ok"; only(e)
                if e["suci"]: suci_imsi[e["suci"]] = x
            else:
                for e in cand: e["st"] = "amb"
            loose = None
    cu_imsi = {n: e["imsi"] for n, e in ids.items() if e["imsi"]}
    info = {"ids": len(ids), "open": sorted(n for n, e in ids.items() if not e["imsi"]), "amb": sorted(n for n, e in ids.items() if e["st"] == "amb"), "learned": len(suci_imsi)}
    imsi_ip = {}
    for ln in smf_lines:
        m = RE_SMF_IP.search(ln)
        if m: imsi_ip[m.group(1)] = m.group(2)
    return cu_imsi, imsi_ip, info


def parse_ue_info(text):
    """the AMF's own UE table (GET /ue-info on its metrics port, Open5GS 2.8): {RAN_UE_NGAP_ID: imsi} of the phones it holds connected now"""
    try: items = json.loads(text).get("items") or []
    except (ValueError, AttributeError): return {}
    live = {}
    for it in items:
        if not isinstance(it, dict) or it.get("cm_state") != "connected": continue
        supi = str(it.get("supi") or ""); g = it.get("gnb"); n = g.get("ran_ue_ngap_id") if isinstance(g, dict) else None
        if supi.startswith("imsi-") and isinstance(n, (int, float)) and not isinstance(n, bool): live[int(n)] = supi[5:]
    return live


def merge_core(cu_imsi, live):
    """the AMF's table wins over the reading of its journal: an id it names, and a phone it places on another id, are replaced"""
    if not live: return dict(cu_imsi)
    out = {n: x for n, x in cu_imsi.items() if n not in live and x not in live.values()}
    out.update(live); return out


# ---------------------------------------------------------------------------------------------------------------- collectors
class Collector(threading.Thread):
    def __init__(self, name):
        super().__init__(name=name, daemon=True); self.stop_ev = threading.Event(); self.state = "init"; self.err = None

    def stop(self): self.stop_ev.set()


class ProcFollower(Collector):
    """a child process whose stdout we consume in chunks (docker logs -f / docker exec tail -F); restarted when it ends"""

    def __init__(self, name, cmd, on_lines, on_start=None, retry=3.0):
        super().__init__(name); self.cmd = cmd; self.on_lines = on_lines; self.on_start = on_start; self.retry = retry; self.proc = None; self.lines = 0

    def run(self):
        while not self.stop_ev.is_set():
            try:
                self.proc = subprocess.Popen(self.cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, bufsize=0)
            except OSError as e:
                self.err = str(e); self.state = "down"; self.stop_ev.wait(self.retry); continue
            if self.on_start:
                try: self.on_start()
                except Exception as e: self.err = repr(e)
            fd = self.proc.stdout.fileno(); buf = b""; t0 = time.time()
            while not self.stop_ev.is_set():
                r, _, _ = select.select([fd], [], [], 0.5)
                if not r:
                    if self.proc.poll() is not None: break
                    if time.time() - t0 > 1.0: self.state = "up"
                    continue
                chunk = os.read(fd, 1 << 16)
                if not chunk: break
                buf += chunk; i = buf.rfind(b"\n")
                if i < 0: continue
                lines = buf[:i].decode("utf-8", "replace").split("\n"); buf = buf[i + 1:]
                self.lines += len(lines)
                if time.time() - t0 > 1.0: self.state = "up"   # a child that only prints an error and exits never counts as up
                try: self.on_lines(lines)
                except Exception as e: self.err = repr(e)
            try: self.proc.kill(); self.proc.wait(timeout=2)
            except Exception: pass
            self.state = "down"
            self.stop_ev.wait(self.retry if time.time() - t0 > 5 else self.retry * 2)

    def stop(self):
        super().stop()
        try:
            if self.proc: self.proc.kill()
        except Exception: pass


class TailFollower(Collector):
    """follows the NEWEST file matching a glob from its end (the xApps start a new log file per run)"""

    def __init__(self, name, pattern, on_lines, poll=0.1):
        super().__init__(name); self.pattern = pattern; self.on_lines = on_lines; self.poll = poll; self.path = None; self.lines = 0

    def _newest(self):
        try:
            fs = glob.glob(self.pattern)
            return max(fs, key=os.path.getmtime) if fs else None
        except OSError: return None

    def run(self):
        fh = None; buf = ""; t_chk = 0.0
        while not self.stop_ev.is_set():
            now = time.time()
            if now - t_chk > 2.0:
                t_chk = now; p = self._newest()
                if p != self.path:
                    if fh: fh.close()
                    fh = None; self.path = p; buf = ""
                    if p:
                        try:
                            fh = open(p, "r", errors="replace"); fh.seek(0, os.SEEK_END)
                        except OSError as e: self.err = str(e); fh = None; self.path = None
            if fh is None: self.state = "down"; self.stop_ev.wait(1.0); continue
            data = fh.read(1 << 18)
            if not data: self.stop_ev.wait(self.poll); continue
            buf += data; i = buf.rfind("\n")
            if i < 0: continue
            lines = buf[:i].split("\n"); buf = buf[i + 1:]; self.lines += len(lines); self.state = "up"
            try: self.on_lines(lines)
            except Exception as e: self.err = repr(e)


class AgentClient(Collector):
    """one more subscriber of the dApp agent (newline-delimited JSON over TCP). Role "dash": the agent replaces an older connection of the SAME
    role only, so the slicing xApp ("slicing"), the VA service ("ra") and the virtual cameras keep their connections."""

    def __init__(self, hub, host, port, every=200, period_ms=1000):
        super().__init__("agent"); self.hub = hub; self.addr = (host, port); self.every = every; self.period_ms = period_ms

    def run(self):
        while not self.stop_ev.is_set():
            s = None
            try:
                s = socket.create_connection(self.addr, timeout=3.0); s.settimeout(5.0)
                hello = {"type": "hello", "role": "dash", "name": "dgist-dashboard", "subscribe": ["estimate", "sm_report"], "every": self.every, "period_ms": self.period_ms}
                s.sendall((json.dumps(hello, separators=(",", ":")) + "\n").encode()); buf = b""; self.state = "up"; idle = 0
                while not self.stop_ev.is_set():
                    try: chunk = s.recv(1 << 16)
                    except socket.timeout:
                        idle += 1
                        if idle >= 3: break
                        continue
                    if not chunk: break
                    idle = 0; buf += chunk
                    while True:
                        i = buf.find(b"\n")
                        if i < 0: break
                        ln, buf = buf[:i], buf[i + 1:]
                        try: msg = json.loads(ln)
                        except ValueError: continue
                        if isinstance(msg, dict): self.hub.on_agent(msg)
            except OSError as e: self.err = str(e)
            finally:
                try:
                    if s: s.close()
                except OSError: pass
            self.state = "down"; self.stop_ev.wait(3.0)


class FeedPoller(Collector):
    """GET https://127.0.0.1:<port>/metrics/frames?since=<seq> of the VA / non-VA service (loopback; the per-frame events the services already
    compute), plus the service's status page every 2 s. A 404 means the service runs an app.py from before this feed (restart it once)."""

    def __init__(self, name, port, on_event, on_status=None, status_path=None, period=0.2):
        super().__init__(name); self.base = "https://127.0.0.1:%d" % port; self.on_event = on_event; self.on_status = on_status; self.status_path = status_path
        self.period = period; self.since = None; self.ctx = ssl.create_default_context(); self.ctx.check_hostname = False; self.ctx.verify_mode = ssl.CERT_NONE; self.feed = None; self.events = 0

    def _get(self, path, timeout=2.0):
        with urllib.request.urlopen(self.base + path, timeout=timeout, context=self.ctx) as r: return json.loads(r.read())

    def run(self):
        t_st = 0.0
        while not self.stop_ev.is_set():
            t0 = time.time()
            try:
                d = self._get("/metrics/frames?since=%d" % (10 ** 15 if self.since is None else self.since))
                seq = int(d.get("seq", 0))
                if self.since is not None and seq < self.since: self.since = 0; continue        # the service restarted
                first = self.since is None; self.since = seq; self.feed = True; self.state = "up"
                if not first:
                    for ev in d.get("ev") or []:
                        self.events += 1
                        try: self.on_event(ev)
                        except Exception as e: self.err = repr(e)
            except urllib.error.HTTPError as e:
                self.state = "up"; self.feed = False if e.code == 404 else self.feed; self.err = "HTTP %d" % e.code
            except Exception as e:
                self.state = "down"; self.err = str(e); self.since = None; self.stop_ev.wait(1.0)
            if self.on_status and self.status_path and t0 - t_st > 2.0 and self.state == "up":
                t_st = t0
                try: self.on_status(self._get(self.status_path))
                except Exception: pass
            dt = time.time() - t0
            if dt < self.period: self.stop_ev.wait(self.period - dt)


def pid_alive(path):
    try: pid = int(Path(path).read_text().split()[0])
    except (OSError, ValueError, IndexError): return False
    try: os.kill(pid, 0); return True
    except PermissionError: return True
    except OSError: return False


def run_out(cmd, timeout=4.0):
    try: return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, timeout=timeout).stdout.decode("utf-8", "replace")
    except (OSError, subprocess.SubprocessError): return ""


class SysPoller(Collector):
    """every 2 s: which components run (docker ps, pid files), the mode (.env DEMO_MODE), the virtual cameras' status file, NVML, disk"""

    def __init__(self, hub, cfg, collectors=None):
        super().__init__("sys"); self.hub = hub; self.cfg = cfg; self.root = Path(cfg["root"]); self.collectors = collectors or {}; self.nv = None; self.nvh = None

    def _nvml(self):
        try:
            import pynvml
            if self.nv is None: pynvml.nvmlInit(); self.nv = pynvml; self.nvh = pynvml.nvmlDeviceGetHandleByIndex(0)
            nv, h = self.nv, self.nvh; d = {}
            for k, fn in (("util", lambda: nv.nvmlDeviceGetUtilizationRates(h).gpu), ("power", lambda: nv.nvmlDeviceGetPowerUsage(h) / 1000.0),
                          ("temp", lambda: nv.nvmlDeviceGetTemperature(h, 0)), ("mem", lambda: nv.nvmlDeviceGetMemoryInfo(h).used / 2 ** 30)):
                try: d[k] = round(float(fn()), 1)
                except Exception: d[k] = None
            return d
        except Exception: return None

    def _col_up(self, name):
        c = self.collectors.get(name); return bool(c is not None and c.state == "up")     # the service answers on its port (started outside va.sh / nonva.sh)

    def run(self):
        n = 0
        while not self.stop_ev.is_set():
            r = self.root; env = read_env(r / ".env"); names = set(run_out(["docker", "ps", "--format", "{{.Names}}"]).split())
            ps = run_out(["ps", "-eo", "comm="]).split(); tg = tgen_state(self.cfg, env, run_out(["ps", "-eo", "args="]) if self.cfg["l1_container"] in names else "")
            st = {"mode": env.get("DEMO_MODE", "ours"),
                  "up": {"l1": self.cfg["l1_container"] in names, "gnb": self.cfg["gnb_container"] in names, "yolo": self.cfg["yolo_container"] in names,
                         "va": pid_alive(r / "runs/va_service/uvicorn.pid") or self._col_up("va"), "nonva": pid_alive(r / "runs/nonva_service/uvicorn.pid") or self._col_up("nonva"),
                         "xapp": pid_alive(r / "runs/xapp/xapp.pid"), "moni": pid_alive(r / "runs/xapp/moni.pid"), "vcam": pid_alive(r / "runs/vcam/vcam.pid"),
                         "ric": "nearRT-RIC" in ps, "core": "open5gs-amfd" in ps, "tgen": tg["run"]},
                  "tgen": tg,
                  "col": {k: {"state": c.state, "feed": getattr(c, "feed", None)} for k, c in self.collectors.items()}}
            try:
                sv = os.statvfs(str(r / "runs")); st["disk_gb"] = round(sv.f_bavail * sv.f_frsize / 2 ** 30, 1)
            except OSError: pass
            try: st["rntis"] = (r / "runs/xapp/rntis.txt").read_text().strip()[:120] if st["up"]["xapp"] else None
            except OSError: st["rntis"] = None
            self.hub.set_sys(st); self.hub.set_nvml(self._nvml())
            vc = None
            try:
                p = r / "runs/vcam/status.json"
                if time.time() - p.stat().st_mtime < 5.0:
                    d = json.loads(p.read_text()); t = d.get("total") or {}; ve = self.cfg.get("vcam_env") or {}
                    vc = {"seen": time.time(), "cams": t.get("cams"), "fps": t.get("fps"), "model": t.get("model"), "deadline_ms": t.get("deadline_ms"), "inflight": t.get("inflight"),
                          "dry": t.get("dry_run"), "req_s": t.get("req_s_60"), "met_pct": t.get("met_pct_60"), "rt": [t.get("rt_p50"), t.get("rt_p90"), t.get("rt_p99")],
                          "offered": t.get("offered"), "sent": t.get("sent"), "skipped": t.get("skipped"), "success": t.get("success"), "met": t.get("met"), "uptime_s": t.get("uptime_s"),
                          "jpeg_quality": ve.get("jpeg_quality"), "frames": ve.get("frames"),
                          "per": [{"cam": c.get("cam"), "req_s": c.get("req_s_60"), "met_pct": c.get("met_pct_60"), "rt_p50": c.get("rt_p50"), "rt_p99": c.get("rt_p99")} for c in d.get("cams") or []]}
            except (OSError, ValueError): pass
            self.hub.set_vcam(vc); n += 1
            self.stop_ev.wait(2.0)


class CorePoller(Collector):
    """IMSI and IP of every attached UE: the Open5GS journals (parse_core) + the AMF's UE table (parse_ue_info), every 15 s and right after an attach"""

    def __init__(self, hub, cfg):
        super().__init__("core"); self.hub = hub; self.cfg = cfg; self.kick_ev = threading.Event(); self.n = 0; self.info = {}

    def kick(self): self.kick_ev.set()

    def _journal(self, unit, since):
        cmd = ["journalctl", "-q", "-u", unit, "--since", since, "--no-pager"]
        out = run_out(cmd, 8.0)
        return out if out.strip() else run_out(["sudo", "-n"] + cmd, 8.0)

    def _ue_info(self):
        a = self.cfg.get("amf_info") or ""
        if not a or a in ("0", "off"): return {}
        try:
            with urllib.request.urlopen("http://%s/ue-info" % a, timeout=2.0) as r: return parse_ue_info(r.read().decode("utf-8", "replace"))
        except (OSError, ValueError): return {}

    def run(self):
        while not self.stop_ev.is_set():
            since = "12 hours ago"
            st = run_out(["docker", "inspect", "-f", "{{.State.StartedAt}}", self.cfg["gnb_container"]]).strip()
            m = re.match(r"(\d{4})-(\d\d)-(\d\d)T(\d\d):(\d\d):(\d\d)", st)
            if m:
                import calendar
                since = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(calendar.timegm(tuple(int(x) for x in m.groups()) + (0, 0, 0))))
            t_read = time.time()               # the AMF journal is read over days: a phone's concealed id is learnt at its last fresh registration
            amf = self._journal("open5gs-amfd", self.cfg.get("core_since") or "10 days ago"); smf = self._journal("open5gs-smfd", "24 hours ago" if not m else since)
            cu_imsi, imsi_ip, self.info = parse_core(amf.splitlines(), smf.splitlines())
            if not imsi_ip: imsi_ip = parse_core((), self._journal("open5gs-smfd", "24 hours ago").splitlines())[1]
            live = self._ue_info(); self.info["journal"] = len(cu_imsi); self.info["amf_info"] = len(live)
            self.hub.set_core(merge_core(cu_imsi, live), imsi_ip, t=t_read); self.n += 1; self.state = "up" if amf else "down"
            self.kick_ev.wait(15.0)
            if self.kick_ev.is_set(): self.kick_ev.clear(); self.stop_ev.wait(2.5)


def start_collectors(hub, cfg):
    """every live source; returns {name: collector}"""
    root = Path(cfg["root"]); col = {}
    core = CorePoller(hub, cfg); col["core"] = core
    gp = GnbParser(hub, on_attach=core.kick)

    def gnb_lines(lines):
        now = time.time()
        for ln in lines: gp.feed(ln, now)

    def gnb_start():
        hub.on_quota(None, None, None)
        out = run_out(["sh", "-c", "docker logs --tail 20000 %s 2>&1 | grep E2_SLICE_TRANSITION_DONE | tail -1" % cfg["gnb_container"]], 10.0)
        m = RE_DONE.search(out)
        if m: hub.on_quota(int(m.group(2)), int(m.group(3)), "gnb")
    col["gnb"] = ProcFollower("gnb", ["docker", "logs", "-f", "--tail", "0", cfg["gnb_container"]], gnb_lines, on_start=gnb_start)
    col["slots"] = ProcFollower("slots", ["docker", "exec", cfg["l1_container"], "tail", "-n0", "-F", cfg["slot_csv"]], lambda lines: hub.on_slots(parse_slot_rows(lines)))
    if cfg.get("l1_follow"):
        def l1_lines(lines):
            for ln in lines:
                d = parse_l1_line(ln)
                if d: hub.on_l1(d)
        col["l1"] = ProcFollower("l1", ["docker", "exec", cfg["l1_container"], "sh", "-c", "tail -n0 -F /tmp/phy.log 2>/dev/null | grep --line-buffered -E '\\] Cell +[0-9]+ \\| DL'"], l1_lines)   # every cell: 1 line per second without the traffic generator, 6 with it

    def ppo_lines(lines):
        now = time.time()
        for ln in lines:
            if not ln.startswith("{"): continue
            try: rec = json.loads(ln)
            except ValueError: continue
            if now * 1000.0 - float(rec.get("wall_ms") or 0) < 3000.0: hub.on_ppo(rec, now)
    if cfg.get("ppo_bundle"): col["ppo"] = TailFollower("ppo", str(root / "xapp_ppo" / cfg["ppo_bundle"] / "logs" / "ppo_radio_sm_inputs_*.log"), ppo_lines)
    mp = MoniParser(hub)

    def moni_lines(lines):
        now = time.time()
        for ln in lines: mp.feed(ln, now)
    col["moni"] = TailFollower("moni", str(root / "log" / "moni_log" / "ul_input_*.log"), moni_lines)
    col["agent"] = AgentClient(hub, cfg["agent"][0], cfg["agent"][1])

    def va_status(d):
        c = d.get("config") or {}
        if c:
            v = cfg["va"]; v.update(model=c.get("model", v["model"]), target_fps=c.get("target_fps", v["target_fps"]), jpeg_quality=c.get("jpeg_quality", v["jpeg_quality"]),
                                    camera=c.get("camera", v["camera"]), file_frame=c.get("file_frame", v["file_frame"]), max_inflight=c.get("max_inflight", v["max_inflight"]),
                                    deadline_ms=c.get("deadline_ms", v["deadline_ms"]), backend=c.get("backend", v["backend"]))
            hub.deadline = float(v["deadline_ms"]); hub.dl_fixed = float((d.get("viewer") or {}).get("dl_ms", hub.dl_fixed))
            w = d.get("viewer") or {}; vw = cfg["viewer"]                              # what the running service really uses wins over .env
            vw.update(on=bool(w.get("on", vw["on"])), dl_ms=float(w.get("dl_ms", vw["dl_ms"])), freeze=bool(w.get("freeze", vw["freeze"])),
                      model_text="mix" if c.get("model_select") else str(c.get("model_fixed") or c.get("model") or vw["model_text"]))
        b = d.get("backend") or {}
        hub.set_sys({"va_backend": {"connected": b.get("connected"), "counters": b.get("counters"), "last_error": b.get("last_error")}})

    def nv_status(d):
        c = d.get("config") or {}
        if c: cfg["nonva"].update(width=c.get("width"), height=c.get("height"), fps=c.get("fps"), jpeg_quality=c.get("jpeg_quality"))
        if c.get("deadline_ms"): cfg["nonva"]["deadline_ms"] = float(c["deadline_ms"]); hub.nv_deadline = float(c["deadline_ms"])
    col["va"] = FeedPoller("va", cfg["va"]["port"], hub.on_va_event, va_status, "/va/status")
    col["nonva"] = FeedPoller("nonva", cfg["nonva"]["port"], hub.on_nv_event, nv_status, "/nonva/status")
    col["sys"] = SysPoller(hub, cfg, col)
    for c in col.values(): c.start()
    return col
