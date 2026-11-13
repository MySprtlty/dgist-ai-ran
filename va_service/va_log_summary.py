#!/usr/bin/env python3
# va_log_summary.py [logdir] [--group PREFIX] (2026-11-10): one line per VA-service latency CSV (runs/va_service_log/va_ul_dl_latency_ue<N>_<ts>.log,
# written by the phone page and by ue_sim.py): n, frame size, achieved fps, model/SM, E2E mean/p50/p90/p99, UL / AI / DL mean/p50/p90, the dApp
# split of AI (wait, decode, infer) and the QoS-met share. Files of one minute form a group (ue1 + ue2 of the same run). Read-only.
import csv, glob, os, re, statistics as st, sys
from collections import defaultdict
D = sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].startswith("--") else "/home/nvidia/_dgist/runs/va_service_log"
pref = sys.argv[sys.argv.index("--group") + 1] if "--group" in sys.argv else ""
groups = defaultdict(list)
for f in sorted(glob.glob(D + "/va_ul_dl_latency_ue*_*.log")):
    m = re.search(r"_ue(\d)_(\d{8}_\d{4})", os.path.basename(f))
    if m and m.group(2).startswith(pref): groups[m.group(2)].append((m.group(1), f))
def pct(v, q): v = sorted(v); return v[min(len(v) - 1, int(q * (len(v) - 1)))]
def summ(f):
    rows = [r for r in csv.DictReader(open(f)) if r.get("dapp_status", "") in ("success", "")]
    if len(rows) < 20: return None
    g = lambda c: [float(r[c]) for r in rows if r.get(c) not in (None, "", "None")]
    o = dict(n=len(rows), span="%s-%s UTC" % (rows[0]["timestamp"][11:19], rows[-1]["timestamp"][11:19]), img="%sx%s" % (rows[0]["image_width"], rows[0]["image_height"]),
             kb=st.mean(g("frame_size_kb")), fps=st.mean(g("actual_fps")), model=rows[0].get("dapp_model", "?") if len({r.get("dapp_model") for r in rows}) == 1 else "mix", sm=st.mean(g("dapp_sm")) if g("dapp_sm") else 0,
             qos=100.0 * sum(float(r["qos_met"]) for r in rows) / len(rows))
    for c in ["e2e_latency_ms", "ul_latency_ms", "ai_latency_ms", "dl_latency_ms", "dapp_wait_ms", "dapp_decode_ms", "dapp_infer_ms"]:
        v = g(c); o[c] = (st.mean(v), pct(v, 0.5), pct(v, 0.9), pct(v, 0.99)) if v else (-1, -1, -1, -1)
    return o
print("%-14s %-3s %-5s %-9s %-5s %-5s %-4s %-3s | %-24s | %-17s %-17s %-17s | %-5s %-5s %-5s | %s" % ("group", "ue", "n", "frame", "KB", "fps", "mdl", "sm", "E2E mean/p50/p90/p99", "UL mean/p50/p90", "AI mean/p50/p90", "DL mean/p50/p90", "wait", "dec", "inf", "QoS% span"))
for k in sorted(groups):
    for ue, f in sorted(groups[k]):
        s = summ(f)
        if not s: continue
        e, ul, ai, dl = s["e2e_latency_ms"], s["ul_latency_ms"], s["ai_latency_ms"], s["dl_latency_ms"]
        print("%-14s ue%s %-5d %-9s %-5.0f %-5.1f %-4s %-3.0f | %5.1f/%5.1f/%5.1f/%5.1f | %5.1f/%5.1f/%5.1f %5.1f/%5.1f/%5.1f %5.1f/%5.1f/%5.1f | %-5.1f %-5.1f %-5.1f | %3.0f %s" % (
            k, ue, s["n"], s["img"], s["kb"], s["fps"], s["model"], s["sm"], e[0], e[1], e[2], e[3], ul[0], ul[1], ul[2], ai[0], ai[1], ai[2], dl[0], dl[1], dl[2],
            s["dapp_wait_ms"][0], s["dapp_decode_ms"][0], s["dapp_infer_ms"][0], s["qos"], s["span"]))
