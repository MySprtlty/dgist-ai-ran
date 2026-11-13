#!/usr/bin/env python3
"""quota_exp.py <VA_PRBS> <seconds> [--label NAME] [--every SEC]   (run as nvidia from /home/nvidia/_dgist; PPO xApp must be stopped)
Fixed-quota experiment (2026-11-12): holds the VA slice at VA_PRBS PRBs (non-VA gets 273 - VA_PRBS) with the lab's
xapp_slice_quota_ctrl (SLICE_MODE=fixed), samples every --every seconds the VA service's rolling 60-s latency figures
(/viewer_status last60: UL / sum p50 p90 p99, QoS, req/s, KB, Mbps per VA UE), the non-VA receiver stats, the gNB per-UE radio
(ue_power.py: SNR, MCS, grant mean/max, Ptx@max) and the L1 cell UL, writes runs/quota_exp/<label>_<ts>.csv and prints a summary;
on exit (or Ctrl-C) the controller is stopped, which removes the quotas. Compare phases: quota_exp.py 100 90; quota_exp.py 200 90."""
import argparse, csv, datetime, json, os, re, signal, ssl, subprocess, sys, time, urllib.request
from pathlib import Path
D = Path(__file__).resolve().parent.parent if Path(__file__).resolve().parent.name == "scripts" else Path(__file__).resolve().parent
sys.path.insert(0, str(D))
import ue_power   # noqa: E402

CTX = ssl._create_unverified_context()


def env_file():
    e = {}
    for line in (D / ".env").read_text().splitlines():
        m = re.match(r"^([A-Z0-9_]+)=(.*)$", line.strip())
        if m:
            e[m.group(1)] = m.group(2).split(" #")[0].strip()
    return e


def get(url, timeout=3):
    try:
        with urllib.request.urlopen(url, context=CTX, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except Exception:
        return None


def cell_line():
    """last per-second L1 line of the phones' cell. With the 5-cell traffic generator (the L1 container runs l1_entrypoint_tg.sh) the L1 has 6 cells and the
    phones' cell is L1 cell 5; its line then comes from the tail of the host-side console log - the full L1 log grows by GBs per hour and is not searched."""
    if cell_line.tg is None:
        cell_line.tg = "l1_entrypoint_tg" in subprocess.run(["docker", "inspect", "-f", "{{join .Config.Cmd \" \"}}", "dgist-l1"], capture_output=True, text=True).stdout
    if not cell_line.tg:
        return subprocess.run("docker exec dgist-l1 bash -c \"grep -E '\\] Cell +0 \\| DL' /tmp/phy.log | tail -1\" 2>/dev/null", shell=True, capture_output=True, text=True).stdout
    try:
        f = D / "runs" / (D / "runs" / "current_run").read_text().strip() / "l1_console.log"
        out = subprocess.run(["tail", "-n", "4000", str(f)], capture_output=True, text=True, errors="replace").stdout
    except OSError:
        return ""
    ls = [x for x in out.splitlines() if re.search(r"\] Cell +5 \| DL", x)]
    return ls[-1] if ls else ""


cell_line.tg = None


def rntis():
    p = D / "runs" / "xapp" / "rntis.txt"
    if p.exists():
        m = re.search(r"VA_RNTIS=([0-9a-f,]+) NONVA_RNTIS=([0-9a-f,]+)", p.read_text())
        if m:
            return m.group(1), m.group(2)
    out = subprocess.run([sys.executable, str(D / "ue_table.py"), "--rntis"], capture_output=True, text=True, timeout=60).stdout
    m = re.search(r"VA_RNTIS=([0-9a-f,]+) NONVA_RNTIS=([0-9a-f,]+)", out)
    return (m.group(1), m.group(2)) if m else (None, None)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("va_prbs", type=int); ap.add_argument("seconds", type=float)
    ap.add_argument("--label", default=None); ap.add_argument("--every", type=float, default=10)
    ap.add_argument("--no-control", action="store_true", help="do not start the quota controller (just sample the current state)")
    a = ap.parse_args()
    e = env_file()
    va, nv = rntis()
    if not va:
        print("!! RNTI mapping unknown (runs/xapp/rntis.txt missing and ue_table.py failed)"); return 1
    B = D / "openairinterface5g" / "openair2" / "E2AP" / "flexric" / e.get("FLEXRIC_BUILD", "build_dgist")
    Q = B / "examples" / "xApp" / "c" / "slice" / "xapp_slice_quota_ctrl"
    SM = Path(e.get("FLEXRIC_SM_DIR") or (D / "flexric_sm")); CONF = D / "config" / "flexric_xapp.conf"
    label = a.label or f"va{a.va_prbs}"
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    out = D / "runs" / "quota_exp"; out.mkdir(parents=True, exist_ok=True)
    csvp = out / f"{label}_{ts}.csv"
    proc = None
    if not a.no_control:
        other = subprocess.run("pgrep -af 'xapp_slice_(ppo|quota|prb|two_service)_ctrl'", shell=True, capture_output=True, text=True).stdout.strip()
        if other:
            print(f"!! another slice controller is running - stop it first (./xapp.sh stop):\n{other}"); return 1
        envp = dict(os.environ, SLICE_MODE="fixed", SLICE1_PRBS=str(a.va_prbs), VA_RNTIS=va, NONVA_RNTIS=nv)
        logp = out / f"{label}_{ts}_quota_ctrl.log"
        proc = subprocess.Popen([str(Q), "-c", str(CONF), "-p", str(SM) + "/"], env=envp, cwd=str(D / "runs" / "xapp"),
                                stdout=open(logp, "w"), stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
        print(f"== quota controller: VA {a.va_prbs} / non-VA {273 - a.va_prbs} PRB, VA_RNTIS={va} NONVA_RNTIS={nv} (pid {proc.pid}, log {logp})")
        ok = False
        for i in range(30):
            time.sleep(1)
            if proc.poll() is not None:
                print("!! controller exited early:"); print(open(logp).read()[-1500:]); return 1
            g = subprocess.run(f"docker logs dgist-gnb --since 3s 2>&1 | grep -E 'UL_SERVICE_PRB.*service=1 quota={a.va_prbs} ' | tail -1", shell=True, capture_output=True, text=True).stdout
            if g.strip():
                ok = True; break
        print("   gNB applies the quota" if ok else "   !! gNB log does not show the quota yet (continuing)")
    else:
        print("== sampling only (no quota control)")
    fields = ["time", "label", "va_quota", "ue", "svc", "rnti", "ul_p50", "ul_p90", "ul_p99", "ai_p50", "sum_p50", "sum_p90", "sum_p99", "met_pct60", "req_s", "kb", "mbps",
              "snr", "mcs", "bler", "rtx_pct", "grant_mean", "grant_max", "ptx_at_max", "pcmax", "ph", "nv_fps", "nv_mbps", "nv_kb", "nv_ul", "cell_ul_mbps"]
    w = csv.DictWriter(open(csvp, "w", newline=""), fieldnames=fields); w.writeheader()
    samples = []
    t0 = time.time()
    def stop(*_):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, stop)
    try:
        while time.time() - t0 < a.seconds:
            time.sleep(a.every)
            now = datetime.datetime.now().strftime("%H:%M:%S")
            vs = get("https://127.0.0.1:8000/viewer_status") or {}
            ue_r, gr = ue_power.snapshot(a.every)
            svc = {}
            for r in va.split(","): svc[r] = "VA"
            for r in nv.split(","): svc[r] = "nonVA"
            radio = {row[0]: row for row in ue_power.table(ue_r, gr, svc, ue_power.conf())}
            cell = cell_line()
            m = re.search(r"UL +([0-9.]+) Mbps", cell); cell_ul = float(m.group(1)) if m else None
            # VA UEs: viewer order ue1, ue2 <-> VA_RNTIS order (xapp.sh maps VA_IMSIS order = ue1, ue2)
            va_list = va.split(","); nv_list = nv.split(",")
            for i, (u, mta) in enumerate(sorted((vs.get("ue") or {}).items())):
                l = mta.get("last60") or {}
                r = va_list[i] if i < len(va_list) else ""
                rd = radio.get(r)
                row = dict(time=now, label=label, va_quota=a.va_prbs if not a.no_control else "", ue=u, svc="VA", rnti=r,
                           ul_p50=l.get("ul_p50"), ul_p90=l.get("ul_p90"), ul_p99=l.get("ul_p99"), ai_p50=l.get("ai_p50"), sum_p50=l.get("sum_p50"), sum_p90=l.get("sum_p90"), sum_p99=l.get("sum_p99"),
                           met_pct60=l.get("met_pct"), req_s=l.get("req_s"), kb=mta.get("kb"), mbps=mta.get("mbps"),
                           snr=rd[15] if rd else None, mcs=rd[12] if rd else None, bler=rd[13] if rd else None, rtx_pct=(round(rd[14], 1) if rd and rd[14] is not None else None),
                           grant_mean=(round(rd[9], 1) if rd and rd[9] is not None else None), grant_max=rd[10] if rd else None, ptx_at_max=(round(rd[18]) if rd and rd[18] is not None else None),
                           pcmax=rd[5] if rd else None, ph=rd[6] if rd else None, cell_ul_mbps=cell_ul)
                w.writerow(row); samples.append(row)
            for i, u in enumerate(("ue1", "ue2")):
                st = get(f"https://127.0.0.1:8001/receiver_stats?session_id={u}") or {}
                r = nv_list[i] if i < len(nv_list) else ""
                rd = radio.get(r)
                row = dict(time=now, label=label, va_quota=a.va_prbs if not a.no_control else "", ue=u, svc="nonVA", rnti=r,
                           nv_fps=(round(st["receive_fps"], 1) if "receive_fps" in st else None), nv_mbps=(round(st["receive_mbps"], 1) if "receive_mbps" in st else None),
                           nv_kb=(round(st["frame_size_kb"]) if "frame_size_kb" in st else None), nv_ul=(round(st["ul_latency_ms"]) if "ul_latency_ms" in st else None),
                           snr=rd[15] if rd else None, mcs=rd[12] if rd else None, bler=rd[13] if rd else None, rtx_pct=(round(rd[14], 1) if rd and rd[14] is not None else None),
                           grant_mean=(round(rd[9], 1) if rd and rd[9] is not None else None), grant_max=rd[10] if rd else None, ptx_at_max=(round(rd[18]) if rd and rd[18] is not None else None),
                           pcmax=rd[5] if rd else None, ph=rd[6] if rd else None, cell_ul_mbps=cell_ul)
                w.writerow(row); samples.append(row)
            vrows = [s for s in samples if s["svc"] == "VA" and s["time"] == now]
            print("   %s | cell UL %s Mbps | " % (now, cell_ul) + " | ".join(
                "%s UL p50/p90 %s/%s AI %s sum p90 %s QoS60 %s%% %s req/s %s KB snr %s mcs %s grant %s/%s" % (
                    s["ue"], s["ul_p50"], s["ul_p90"], s["ai_p50"], s["sum_p90"], s["met_pct60"], s["req_s"], s["kb"], s["snr"], s["mcs"], s["grant_mean"], s["grant_max"]) for s in vrows))
    except KeyboardInterrupt:
        pass
    finally:
        if proc and proc.poll() is None:
            proc.send_signal(signal.SIGTERM)
            try: proc.wait(timeout=15)
            except Exception: proc.kill()
            print("== quota controller stopped (quotas removed)")
    # summary: median over samples per UE
    def med(vals):
        v = sorted(x for x in vals if x is not None)
        return v[len(v) // 2] if v else None
    print(f"== summary {label} (VA quota {a.va_prbs}), medians over {len(set(s['time'] for s in samples))} samples -> {csvp}")
    for u in sorted(set((s["svc"], s["ue"]) for s in samples)):
        ss = [s for s in samples if (s["svc"], s["ue"]) == u]
        if u[0] == "VA":
            print("   VA %s: UL p50 %s p90 %s p99 %s | AI p50 %s | sum p90 %s | QoS60 %s %% | %s req/s %s KB %s Mbps | SNR %s MCS %s grant mean %s max %s Ptx@max %s dBm" % (
                u[1], med([s["ul_p50"] for s in ss]), med([s["ul_p90"] for s in ss]), med([s["ul_p99"] for s in ss]), med([s["ai_p50"] for s in ss]), med([s["sum_p90"] for s in ss]), med([s["met_pct60"] for s in ss]),
                med([s["req_s"] for s in ss]), med([s["kb"] for s in ss]), med([s["mbps"] for s in ss]), med([s["snr"] for s in ss]), med([s["mcs"] for s in ss]),
                med([s["grant_mean"] for s in ss]), med([s["grant_max"] for s in ss]), med([s["ptx_at_max"] for s in ss])))
        else:
            print("   nonVA %s: %s fps %s Mbps %s KB UL %s ms | SNR %s MCS %s grant mean %s max %s" % (
                u[1], med([s["nv_fps"] for s in ss]), med([s["nv_mbps"] for s in ss]), med([s["nv_kb"] for s in ss]), med([s["nv_ul"] for s in ss]),
                med([s["snr"] for s in ss]), med([s["mcs"] for s in ss]), med([s["grant_mean"] for s in ss]), med([s["grant_max"] for s in ss])))
    print("   cell UL median %s Mbps" % med([s["cell_ul_mbps"] for s in samples]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
