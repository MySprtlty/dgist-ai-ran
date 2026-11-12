#!/usr/bin/env python3
"""ue_power.py - per-UE uplink power picture of the running gNB (docker logs dgist-gnb), read-only. 2026-11-12.
Answers "is this UE power-limited, or is the scheduler giving it grants it cannot fill?" next to the SNR of xapp.sh moni logs.

Per UE, every refresh:
  RSRP     DL power the UE receives from the gNB (its measurement report, dBm)   -> PL = ssPBCH_BlockPower - RSRP (dB, the UE's own estimate)
  PCMAX    the UE's maximum transmit power (dBm, from its power-headroom report)
  PH       the gNB's stored headroom (dB) = the UE's reported headroom + the power factor of the PUSCH it was reported on, i.e. the
           headroom the UE would have on a 1-PRB transmission at that MCS (OAI main.c prints sched_ctrl->ph; gNB_scheduler_ulsch.c
           sets it as PH_dB + compute_ph_factor(tbs, rbSize, ...)). The same value limits the scheduler (nr_ue_max_mcs_min_rb).
  Ptx/PRB  UE transmit power per PRB = PCMAX - PH (dBm)
  afford   PRBs the UE can fill at that per-PRB power before hitting PCMAX = 10^(PH/10), capped at 273
  Ptx@max  what the UE transmits on the widest grant of the window = Ptx/PRB + 10 log10(grant max) (dBm, vs PCMAX)
  grant    what the gNB gave in the window ([UL_SERVICE_PRB] lines): mean / max PRB and the share of grants above 'afford'
  MCS BLER SNR  the gNB's UL figures (SNR = per-PRB SNR at the gNB; the closed loop steers it to pusch_TargetSNRx10/10)
  rtx%     round-2 / round-1 PUSCH transmissions in the window (fresh retransmission rate; the BLER column is a long-term average)
  verdict  '전력한계' when the gNB hands out grants wider than the UE can fill at its per-PRB power (per-PRB SNR must then drop -> MCS
           falls, BLER rises); '전력여유' when the grants fit; '여유인데 BLER↑' when BLER / rtx exceed 15 % although the grants fit
           (receive side or scheduler / link adaptation; OAI's link adaptation itself aims at about 10 % first-round BLER).
Notes: the gNB sends no per-UE downlink power (NR has no per-UE DL power control): what each UE gets from the gNB is visible as its RSRP,
and what the gNB makes each UE transmit is the UL power control result (P0 + alpha x PL + TPC, visible through PH / Ptx / SNR).
usage: ue_power.py [--watch SEC] [--window SEC] [--log] [--rntis VA1,VA2 NV1,NV2]      (xapp.sh moni power)
"""
import argparse, os, re, subprocess, sys, time, math, datetime
from pathlib import Path
D = Path(__file__).resolve().parent

PAT_UE = re.compile(r"UE RNTI ([0-9a-f]{4}) CU-UE-ID (\d+) (in-sync|out-of-sync) PH (\d+) dB PCMAX (\d+) dBm(?:, average RSRP (-?\d+))?")
PAT_UL = re.compile(r"UE ([0-9a-f]{4}): ulsch_rounds (\d+)/(\d+)/(\d+)/(\d+), ulsch_errors (\d+), ulsch_DTX (\d+), BLER ([0-9.]+) MCS \(\d\) (\d+).*?NPRB (\d+)\s+SNR (-?[0-9.]+)")
PAT_GR = re.compile(r"\[UL_SERVICE_PRB\].*?rnti=([0-9a-f]{4}) service=(\d+) quota=(\d+).*?grant=(\d+) retx=(\d+)")


def sh(cmd, timeout=20):
    try:
        return subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout).stdout
    except Exception:
        return ""


def conf():
    c = {}
    try:
        t = (D / "config" / "gnb.conf").read_text()
        for k in ("ssPBCH_BlockPower", "p0_nominal", "p0_NominalWithGrant", "pusch_TargetSNRx10", "ul_max_mcs"):
            m = re.search(rf"^\s*{k}\s*=\s*(-?\d+)", t, re.M)
            if m:
                c[k] = int(m.group(1))
    except Exception:
        pass
    return c


def ph_db(v):
    """the printed PH is already dB, normalized to a 1-PRB transmission (see the module docstring)"""
    return float(v)


def services(arg):
    svc = {}
    if arg:
        va, nv = (arg.split() + [""])[:2]
        for r in va.split(","):
            if r: svc[r] = "VA"
        for r in nv.split(","):
            if r: svc[r] = "nonVA"
        return svc
    for k, name in (("VA_RNTIS", "VA"), ("NONVA_RNTIS", "nonVA")):
        for r in os.environ.get(k, "").split(","):
            if r: svc[r] = name
    if not svc:
        out = sh(f"python3 {D}/ue_table.py --rntis 2>/dev/null", timeout=40)
        for k, name in (("VA_RNTIS", "VA"), ("NONVA_RNTIS", "nonVA")):
            m = re.search(rf"{k}=([0-9a-f,]+)", out)
            if m:
                for r in m.group(1).split(","):
                    svc[r] = name
    return svc


def snapshot(window):
    log = sh(f"docker logs dgist-gnb --since {window}s 2>&1", timeout=15) or sh(f"sudo -n docker logs dgist-gnb --since {window}s 2>&1", timeout=15)
    ue = {}
    for m in PAT_UE.finditer(log):
        r = m.group(1)
        ue.setdefault(r, {})
        ue[r].update(id=int(m.group(2)), sync=m.group(3), ph_raw=int(m.group(4)), pcmax=int(m.group(5)), rsrp=(int(m.group(6)) if m.group(6) else None))
    first, last = {}, {}
    for m in PAT_UL.finditer(log):
        r = m.group(1)
        rows = tuple(int(x) for x in m.groups()[1:5])
        first.setdefault(r, rows); last[r] = rows
        ue.setdefault(r, {}).update(bler=float(m.group(8)), mcs=int(m.group(9)), nprb=int(m.group(10)), snr=float(m.group(11)))
    for r in last:
        d1 = last[r][0] - first[r][0]; d2 = last[r][1] - first[r][1]
        ue[r]["rtx"] = (100.0 * d2 / d1) if d1 > 0 else None
    gr = {}
    for m in PAT_GR.finditer(log):
        r = m.group(1); g = int(m.group(4))
        d = gr.setdefault(r, {"n": 0, "sum": 0, "max": 0, "quota": int(m.group(3)), "svc": int(m.group(2)), "ge": []})
        d["n"] += 1; d["sum"] += g; d["max"] = max(d["max"], g); d["ge"].append(g)
    return ue, gr


def table(ue, gr, svc, c):
    tgt = c.get("pusch_TargetSNRx10", 200) / 10.0
    rows = []
    for r in sorted(ue, key=lambda x: ue[x].get("id", 99)):
        u = ue[r]
        ph = ph_db(u["ph_raw"]) if "ph_raw" in u else None
        pcmax = u.get("pcmax")
        ptx = (pcmax - ph) if (ph is not None and pcmax is not None) else None
        afford = min(273, int(10 ** (ph / 10.0))) if ph is not None else None
        rsrp = u.get("rsrp"); pl = (c.get("ssPBCH_BlockPower", -11) - rsrp) if rsrp is not None else None
        g = gr.get(r); gmean = (g["sum"] / g["n"]) if g and g["n"] else None; gmax = g["max"] if g else None
        ptx_max = (ptx + 10 * math.log10(gmax)) if (ptx is not None and gmax) else None
        over = (100.0 * sum(1 for x in g["ge"] if afford is not None and x > afford) / g["n"]) if g and g["n"] and afford is not None else None
        bler = u.get("bler"); rtx = u.get("rtx")
        if afford is not None and gmax is not None and gmax > afford:
            verdict = f"전력한계 grant {gmax}>{afford}"
        elif (bler or 0) > 0.15 or (rtx or 0) > 15:   # OAI link adaptation aims at ~10 % first-round BLER; above 15 % something else is wrong
            verdict = "여유인데 BLER↑ (스케줄러/LA)"
        else:
            verdict = "전력여유"
        rows.append((r, svc.get(r, "?"), u.get("sync", "?")[:3], rsrp, pl, pcmax, ph, ptx, afford, gmean, gmax, over, u.get("mcs"), bler, rtx, u.get("snr"), tgt, verdict, ptx_max))
    return rows


def fmt(v, f="%d"):
    return "-" if v is None else (f % v)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--watch", type=float, default=0, help="refresh every SEC seconds (0 = once)")
    ap.add_argument("--window", type=float, default=5, help="seconds of gNB log per refresh (grants, retransmissions)")
    ap.add_argument("--log", action="store_true", help="append CSV rows to log/moni_log/ue_power_<ts>.csv")
    ap.add_argument("--rntis", default="", help="'VA1,VA2 NV1,NV2' (default: VA_RNTIS/NONVA_RNTIS env, else ue_table.py)")
    a = ap.parse_args()
    c = conf(); svc = services(a.rntis)
    print(f"# gNB cell: ssPBCH_BlockPower {c.get('ssPBCH_BlockPower')} dBm/RE (what the UE uses for its pathloss), p0_nominal {c.get('p0_nominal')} dBm, "
          f"PUSCH target SNR {c.get('pusch_TargetSNRx10', 200) / 10:.0f} dB per PRB, ul_max_mcs {c.get('ul_max_mcs')} | window {a.window:g} s")
    csvf = None
    if a.log:
        p = D / "log" / "moni_log"; p.mkdir(parents=True, exist_ok=True)
        csvf = open(p / f"ue_power_{datetime.datetime.now():%Y%m%d_%H%M%S}.csv", "a")
        csvf.write("time,rnti,service,sync,rsrp_dbm,pl_db,pcmax_dbm,ph_db,ptx_per_prb_dbm,afford_prb,grant_mean_prb,grant_max_prb,grants_over_pct,mcs,bler,rtx_pct,snr_db,ptx_at_max_dbm,verdict\n")
        print(f"# csv: {csvf.name}")
    hdr = "%-5s %-6s %-4s %5s %4s %6s %5s %7s %6s | %11s %6s %5s %7s | %3s %6s %5s %5s | %s" % ("RNTI", "svc", "sync", "RSRP", "PL", "PCMAX", "PH", "Ptx/PRB", "afford", "grant mean", "max", "over%", "Ptx@max", "MCS", "BLER", "rtx%", "SNR", "verdict (target SNR %.0f dB)" % (c.get("pusch_TargetSNRx10", 200) / 10.0))
    while True:
        ue, gr = snapshot(a.window)
        rows = table(ue, gr, svc, c)
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        print(f"== {ts}"); print(hdr)
        for r in rows:
            print("%-5s %-6s %-4s %5s %4s %6s %5s %7s %6s | %11s %6s %5s %7s | %3s %6s %5s %5s | %s" % (
                r[0], r[1], r[2], fmt(r[3]), fmt(r[4]), fmt(r[5]), fmt(r[6], "%.0f"), fmt(r[7], "%.0f"), fmt(r[8]),
                fmt(r[9], "%.1f"), fmt(r[10]), fmt(r[11], "%.0f"), fmt(r[18], "%.0f"), fmt(r[12]), fmt(r[13], "%.3f"), fmt(r[14], "%.1f"), fmt(r[15], "%.1f"), r[17]))
            if csvf:
                csvf.write(",".join(str(x if x is not None else "") for x in (ts, r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7], r[8],
                           ("%.1f" % r[9]) if r[9] is not None else "", r[10], ("%.0f" % r[11]) if r[11] is not None else "", r[12], r[13], ("%.1f" % r[14]) if r[14] is not None else "", r[15], ("%.0f" % r[18]) if r[18] is not None else "", r[17])) + "\n")
                csvf.flush()
        if not rows:
            print("   (no UE statistics in the window: gNB down or no UE attached)")
        if a.watch <= 0:
            break
        sys.stdout.flush(); time.sleep(a.watch)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
