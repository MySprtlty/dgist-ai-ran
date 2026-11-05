#!/usr/bin/env python3
"""ue_table.py - who is who on the cell right now: RNTI, gNB UE id, IMSI, UE IP, sync state, radio, and which service the UE talks to.

Sources (all read-only): gNB log (RRC "UE ID n RNTI xxxx", UE stats lines), AMF journal (RAN_UE_NGAP_ID[n] <-> IMSI at registration),
SMF journal (IMSI -> UE IPv4), VA/non-VA uvicorn logs (which IPs post frames). The gNB UE id equals the NGAP RAN_UE_NGAP_ID.

  python3 ue_table.py            table
  python3 ue_table.py --rntis    VA_RNTIS=a,b NONVA_RNTIS=c,d  (exit 1 when the mapping is not exactly 2 + 2)
Reads the journal directly (the nvidia account may), docker logs via the docker group.
"""
import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

D = Path(__file__).resolve().parent


def sh(cmd, timeout=20):
    try:
        return subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout).stdout
    except Exception:
        return ""


def journal(unit, since):
    """plain journalctl first (the nvidia account can read the journal), sudo -n as a fallback"""
    out = sh(f"journalctl -q -u {unit} --since '{since}' --no-pager 2>/dev/null")
    if unit not in out:   # a user outside the systemd-journal group gets only "No entries": try sudo -n (works for troy, not for nvidia)
        out = sh(f"sudo -n journalctl -q -u {unit} --since '{since}' --no-pager 2>/dev/null")
    if unit not in out:
        print(f"# note: cannot read the {unit} journal (add this account to the systemd-journal group: sudo usermod -aG systemd-journal $USER) -> IMSI/IP unknown", file=sys.stderr)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rntis", action="store_true", help="print VA_RNTIS=.. NONVA_RNTIS=.. for xapp.sh")
    ap.add_argument("--va-imsis", default=os.environ.get("VA_IMSIS", ""), help="comma list: the two VA phones' IMSIs (stable across re-attach)")
    ap.add_argument("--nonva-imsis", default=os.environ.get("NONVA_IMSIS", ""), help="comma list: the two non-VA phones' IMSIs")
    ap.add_argument("--since", default="12 hours ago", help="journal window for the IMSI mapping")
    ap.add_argument("--window", type=int, default=3000, help="lines of each service log to classify UE IPs")
    a = ap.parse_args()

    gnb = sh("docker logs --tail 20000 dgist-gnb 2>&1") or sh("sudo -n docker logs --tail 20000 dgist-gnb 2>&1")
    # gNB UE id -> RNTI (latest RRCSetupComplete per id), and the live stats line per RNTI
    ueid_rnti = {}
    for m in re.finditer(r"UE ID (\d+) RNTI ([0-9a-f]{4})\) Received RRCSetupComplete", gnb):
        ueid_rnti[int(m.group(1))] = m.group(2)
    live = {}
    for m in re.finditer(r"UE RNTI ([0-9a-f]{4}) CU-UE-ID (\d+) (in-sync|out-of-sync) PH (\d+) dB PCMAX (\d+) dBm(?:, average RSRP (-?\d+))?", gnb):
        live[m.group(1)] = dict(ueid=int(m.group(2)), sync=m.group(3), pcmax=m.group(5), rsrp=m.group(6))
    radio = {}
    for m in re.finditer(r"UE ([0-9a-f]{4}): ulsch_rounds (\d+)/(\d+)/\d+/\d+, ulsch_errors \d+, ulsch_DTX \d+, BLER ([0-9.]+) MCS \(1\) (\d+).*?SNR (-?[0-9.]+) dB", gnb):
        radio[m.group(1)] = dict(bler=float(m.group(4)), mcs=int(m.group(5)), snr=float(m.group(6)))
    # AMF: RAN_UE_NGAP_ID -> IMSI (the registration that completed)
    amf = journal("open5gs-amfd", a.since)
    ran_imsi = {}
    cur = None
    for line in amf.splitlines():
        m = re.search(r"RAN_UE_NGAP_ID\[(\d+)\] AMF_UE_NGAP_ID\[(\d+)\] TAC\[", line)   # the InitialUEMessage line only (release lines have no TAC)
        if m:
            cur = int(m.group(1))
        m = re.search(r"\[imsi-(\d+)\] Registration complete", line)
        if m and cur is not None:
            ran_imsi[cur] = m.group(1)
    # SMF: IMSI -> UE IP (latest)
    smf = journal("open5gs-smfd", a.since)
    imsi_ip = {}
    for m in re.finditer(r"UE SUPI\[imsi-(\d+)\] DNN\[[^\]]*\] IPv4\[([0-9.]+)\]", smf):   # "UE SUPI[imsi-..] DNN[internet] IPv4[10.45.0.6]"
        imsi_ip[m.group(1)] = m.group(2)
    # services: which IPs post frames (recent lines)
    def ips(path, route):
        try:
            lines = Path(path).read_text(errors="replace").splitlines()[-a.window:]
        except OSError:
            return {}
        out = {}
        for ln in lines:
            m = re.search(r"(10\.45\.\d+\.\d+):\d+ - \"POST " + route, ln)
            if m:
                out[m.group(1)] = out.get(m.group(1), 0) + 1
        return out
    va_ips = ips(D / "runs/va_service/uvicorn.log", "/detect_frame")
    nonva_ips = ips(D / "runs/nonva_service/uvicorn.log", "/upload_frame")

    rows = []
    for rnti, lv in sorted(live.items(), key=lambda kv: kv[1]["ueid"]):
        imsi = ran_imsi.get(lv["ueid"])
        ip = imsi_ip.get(imsi) if imsi else None
        svc = "VA" if ip in va_ips else "non-VA" if ip in nonva_ips else "-"
        r = radio.get(rnti, {})
        rows.append(dict(rnti=rnti, ueid=lv["ueid"], imsi=imsi or "?", ip=ip or "?", sync=lv["sync"], rsrp=lv["rsrp"], pcmax=lv["pcmax"],
                         mcs=r.get("mcs"), bler=r.get("bler"), snr=r.get("snr"), svc=svc,
                         frames=va_ips.get(ip, 0) if svc == "VA" else nonva_ips.get(ip, 0) if svc == "non-VA" else 0))
    if a.rntis:
        if a.va_imsis and a.nonva_imsis:   # fixed SIM -> service assignment (.env VA_IMSIS / NONVA_IMSIS): robust to RNTI changes
            by_imsi = {r["imsi"]: r for r in rows if r["sync"] == "in-sync"}
            va = [by_imsi[i]["rnti"] for i in a.va_imsis.split(",") if i in by_imsi]
            nv = [by_imsi[i]["rnti"] for i in a.nonva_imsis.split(",") if i in by_imsi]
            if len(va) == 2 and len(nv) == 2:
                print(f"VA_RNTIS={va[0]},{va[1]} NONVA_RNTIS={nv[0]},{nv[1]}")
                return 0
            print(f"# not all configured phones attached: VA {a.va_imsis} -> {va}, non-VA {a.nonva_imsis} -> {nv} (in-sync: {[(r['rnti'], r['imsi']) for r in rows if r['sync'] == 'in-sync']})", file=sys.stderr)
            return 1
        va = [r["rnti"] for r in rows if r["svc"] == "VA" and r["sync"] == "in-sync"]
        nv = [r["rnti"] for r in rows if r["svc"] == "non-VA" and r["sync"] == "in-sync"]
        if len(va) == 2 and len(nv) == 2:
            print(f"VA_RNTIS={va[0]},{va[1]} NONVA_RNTIS={nv[0]},{nv[1]}")
            return 0
        print(f"# cannot map 2+2 automatically: VA {va} non-VA {nv} (UEs in-sync: {[r['rnti'] for r in rows if r['sync']=='in-sync']})", file=sys.stderr)
        return 1
    print(f"{'RNTI':5} {'id':>2} {'IMSI':16} {'UE IP':10} {'sync':11} {'RSRP':>5} {'PCMAX':>5} {'MCS':>3} {'BLER':>6} {'SNR':>5}  service (frames in log tail)")
    for r in rows:
        print(f"{r['rnti']:5} {r['ueid']:>2} {r['imsi']:16} {r['ip']:10} {r['sync']:11} {str(r['rsrp'] or '-'):>5} {r['pcmax']:>5} "
              f"{str(r['mcs'] if r['mcs'] is not None else '-'):>3} {('%.3f' % r['bler']) if r['bler'] is not None else '-':>6} "
              f"{str(r['snr'] if r['snr'] is not None else '-'):>5}  {r['svc']} ({r['frames']})")
    if not rows:
        print("no UEs (is the gNB running?)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
