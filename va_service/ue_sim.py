#!/usr/bin/env python3
"""ue_sim.py - a COTS-UE stand-in for the VA service (2026-11-05). It does what the phone page does, without a camera or a radio:

   POST /telemetry/start -> GET /clock_sync x10 -> N x POST /detect_frame (multipart: file, enable_yolo, ue_id, session_token, client_send_ms, clock_offset_ms)
   at R req/s with at most K frames in flight -> POST /save_latency_log (the same CSV the phone produces) -> POST /telemetry/stop

Standard library only (runs with the system python3 or the venv). Self-signed certificate accepted.

  python3 ue_sim.py --ue ue1 --count 25 --rate 5 --image test_frames/uhd_3840x2160_q90.jpg
  python3 ue_sim.py --ue ue1 --count 50 & python3 ue_sim.py --ue ue2 --count 50 & wait      # two UEs at once
"""
import argparse
import json
import ssl
import statistics
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

CTX = ssl._create_unverified_context()


class Http:
    def __init__(self, url, timeout=10.0):
        self.url, self.timeout = url.rstrip("/"), timeout

    def _do(self, req):
        with urlopen(req, context=CTX, timeout=self.timeout) as r:
            return r.read()

    def get_json(self, path):
        return json.loads(self._do(Request(self.url + path)))

    def post_json(self, path, obj):
        req = Request(self.url + path, data=json.dumps(obj).encode(), method="POST", headers={"Content-Type": "application/json"})
        return json.loads(self._do(req))

    def post_multipart(self, path, fields, filename, data):
        b = "----ue_sim" + uuid.uuid4().hex
        body = b"".join(f'--{b}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode() for k, v in fields.items())
        body += (f'--{b}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\nContent-Type: image/jpeg\r\n\r\n'.encode()
                 + data + b"\r\n" + f"--{b}--\r\n".encode())
        req = Request(self.url + path, data=body, method="POST", headers={"Content-Type": f"multipart/form-data; boundary={b}"})
        return self._do(req)


def clock_sync(h, n=10):
    samples = []
    for _ in range(n):
        t0 = time.time() * 1000.0
        d = h.get_json("/clock_sync?x=" + uuid.uuid4().hex)
        t3 = time.time() * 1000.0
        t1, t2 = d["server_receive_ms"], d["server_send_ms"]
        samples.append(((t3 - t0) - (t2 - t1), ((t1 - t0) + (t2 - t3)) / 2.0))
        time.sleep(0.02)
    samples.sort()
    best = samples[:5]
    return statistics.mean(o for _, o in best), best[0][0]


def pct(xs, p):
    if not xs:
        return float("nan")
    xs = sorted(xs)
    k = min(len(xs) - 1, max(0, int(round((p / 100.0) * (len(xs) - 1)))))
    return xs[k]


def main():
    ap = argparse.ArgumentParser(description="COTS-UE stand-in for the VA service")
    ap.add_argument("--url", default="https://127.0.0.1:8000")
    ap.add_argument("--ue", default="ue1", choices=["ue1", "ue2"])
    ap.add_argument("--image", default=str(Path(__file__).resolve().parent / "test_frames" / "uhd_3840x2160_q90.jpg"))
    ap.add_argument("--rate", type=float, default=5.0, help="requests per second (the page's TARGET_FPS)")
    ap.add_argument("--count", type=int, default=25)
    ap.add_argument("--inflight", type=int, default=4, help="max unanswered frames (the page's MAX_INFLIGHT)")
    ap.add_argument("--deadline-ms", type=float, default=None, help="QoS target for the summary (default: the server's value)")
    ap.add_argument("--no-yolo", action="store_true", help="upload only (enable_yolo=false)")
    ap.add_argument("--csv", default=None, help="also write the records locally")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args()

    img = Path(a.image)
    if not img.is_file():
        sys.exit(f"image not found: {img}")
    data = img.read_bytes()
    h = Http(a.url)
    ue = a.ue
    try:
        token = h.post_json("/telemetry/start", {"ue_id": ue, "session_token": None})["session_token"]
    except HTTPError as e:
        sys.exit(f"telemetry start failed ({e.code}): {e.read().decode()}  (is {ue} still active? restart the service or pick the other UE)")
    except URLError as e:
        sys.exit(f"cannot reach {a.url}: {e}")
    stop_hb = threading.Event()

    def heartbeat():
        while not stop_hb.wait(0.5):
            try:
                h.post_json("/telemetry/heartbeat", {"ue_id": ue, "session_token": token})
            except Exception:
                pass
    threading.Thread(target=heartbeat, daemon=True).start()

    offset_ms, rtt_ms = clock_sync(h)
    print(f"[{ue}] session {token[:8]} | clock offset {offset_ms:+.2f} ms (rtt {rtt_ms:.2f}) | {img.name} {len(data)/1024:.0f} KB | "
          f"{a.rate:g} req/s, {a.count} frames, inflight<={a.inflight}, yolo {'off' if a.no_yolo else 'on'}")

    lock = threading.Lock()
    records, fails, dropped = [], [], 0
    inflight = [0]
    t_session = time.perf_counter()
    bytes_ul, bytes_dl = [0], [0]
    deadline = [a.deadline_ms]

    def one(frame_id):
        client_send_ms = time.time() * 1000.0
        e2e0 = time.perf_counter()
        try:
            raw = h.post_multipart("/detect_frame", {"enable_yolo": "false" if a.no_yolo else "true", "ue_id": ue, "session_token": token, "frame_id": str(frame_id),
                                             "client_send_ms": f"{client_send_ms:.3f}", "clock_offset_ms": f"{offset_ms:.3f}"},   # viewer: UL on the server
                                   "frame.jpg", data)
        except HTTPError as e:
            msg = e.read().decode(errors="replace")[:200]
            with lock:
                fails.append(f"HTTP {e.code} {msg}")
            try:
                h.post_json("/telemetry/result", {"ue_id": ue, "session_token": token, "frame_id": frame_id, "success": False})
            except Exception:
                pass
            return
        except (URLError, OSError) as e:
            with lock:
                fails.append(f"{type(e).__name__}: {e}")
            return
        finally:
            with lock:
                inflight[0] -= 1
        client_recv_ms = time.time() * 1000.0
        e2e = (time.perf_counter() - e2e0) * 1000.0
        d = json.loads(raw)
        srv_recv, ai0, ai1, srv_send = d["server_receive_ms"], d["ai_start_ms"], d["ai_end_ms"], d["server_send_ms"]
        ul = max(0.0, (srv_recv - offset_ms) - client_send_ms)
        ai = ai1 - ai0
        other = max(0.0, ai0 - srv_recv) + max(0.0, srv_send - ai1)
        dl = max(0.0, client_recv_ms - (srv_send - offset_ms))
        dapp = d.get("dapp") or {}
        if deadline[0] is None:
            deadline[0] = float(d.get("qos_deadline_ms", 200))
        qos_met = (dapp.get("status") == "success" and e2e <= deadline[0]) if d.get("yolo_enabled") else (e2e <= deadline[0])
        with lock:
            bytes_ul[0] += len(data)
            bytes_dl[0] += len(raw)
            elapsed = max(1e-6, time.perf_counter() - t_session)
            n = len(records) + 1
            rec = {"frame_id": frame_id, "timestamp": datetime.fromtimestamp(srv_recv / 1000.0, tz=timezone.utc).isoformat(),
                   "yolo_enabled": bool(d.get("yolo_enabled")), "image_width": d.get("image_width", 0), "image_height": d.get("image_height", 0),
                   "frame_size_kb": round(len(data) / 1024.0, 3), "actual_fps": round(n / elapsed, 3),
                   "ul_payload_mbps": round(bytes_ul[0] * 8 / elapsed / 1e6, 3), "dl_payload_mbps": round(bytes_dl[0] * 8 / elapsed / 1e6, 3),
                   "dl_response_kb": round(len(raw) / 1024.0, 3), "e2e_latency_ms": round(e2e, 3), "ul_latency_ms": round(ul, 3),
                   "ai_latency_ms": round(ai, 3), "server_other_ms": round(other, 3), "dl_latency_ms": round(dl, 3),
                   "network_latency_ms": round(max(0.0, e2e - ai), 3), "breakdown_residual_ms": round(e2e - (ul + ai + other + dl), 3),
                   "clock_offset_ms": round(offset_ms, 3), "sync_rtt_ms": round(rtt_ms, 3),
                   "gpu_util_percent": d.get("gpu_util_percent"), "gpu_memory_used_gb": d.get("gpu_memory_used_gb"),
                   "qos_deadline_ms": deadline[0], "qos_met": qos_met, "dapp_status": dapp.get("status"), "dapp_reason": dapp.get("reason"),
                   "dapp_model": dapp.get("model"), "dapp_sm": dapp.get("sm"), "dapp_wait_ms": dapp.get("wait_ms"),
                   "dapp_decode_ms": dapp.get("decode_ms"), "dapp_infer_ms": dapp.get("infer_ms"), "dapp_latency_ms": dapp.get("latency_ms"),
                   "detections": len(d.get("detections") or [])}
            records.append(rec)
        try:
            h.post_json("/telemetry/result", {"ue_id": ue, "session_token": token, "frame_id": frame_id, "latency_ms": e2e,
                                              "payload_bytes": len(data), "sample_ms": client_recv_ms + offset_ms, "success": True})
        except Exception:
            pass
        if not a.quiet:
            print(f"[{ue}] #{frame_id:3d} {dapp.get('status', '-'):8s}{('/' + dapp['reason']) if dapp.get('reason') else '':10s} "
                  f"e2e {e2e:7.1f} ms = ul {ul:5.1f} + ai {ai:6.1f} + other {other:4.1f} + dl {dl:4.1f} | "
                  f"sm {str(dapp.get('sm', '-')):>3s} wait {float(dapp.get('wait_ms') or 0):5.1f} dec {float(dapp.get('decode_ms') or 0):4.1f} "
                  f"inf {float(dapp.get('infer_ms') or 0):5.1f} | det {rec['detections']:3d} | QoS {'met' if qos_met else 'MISS'}", flush=True)

    pool = ThreadPoolExecutor(max_workers=max(1, a.inflight))
    futs = []
    period = 1.0 / a.rate
    t_next = time.perf_counter()
    for i in range(1, a.count + 1):
        now = time.perf_counter()
        if t_next > now:
            time.sleep(t_next - now)
        t_next += period
        with lock:
            if inflight[0] >= a.inflight:
                dropped += 1
                continue
            inflight[0] += 1
        futs.append(pool.submit(one, i))
    wait(futs, timeout=60)
    stop_hb.set()

    records.sort(key=lambda r: r["frame_id"])
    saved = {}
    if records:
        try:
            saved = h.post_json("/save_latency_log", {"ue_id": ue, "session_token": token, "records": records})
        except HTTPError as e:
            print(f"[{ue}] save_latency_log failed ({e.code}): {e.read().decode()[:300]}")
    try:
        h.post_json("/telemetry/stop", {"ue_id": ue, "session_token": token})
    except Exception as e:
        print(f"[{ue}] telemetry stop failed: {e}")
    if a.csv and records:
        import csv
        with open(a.csv, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(records[0].keys()))
            w.writeheader()
            w.writerows(records)

    ok = [r for r in records if r["dapp_status"] == "success"] if not a.no_yolo else records
    e2e = [r["e2e_latency_ms"] for r in records]
    met = sum(1 for r in records if r["qos_met"])
    elapsed = max(1e-6, time.perf_counter() - t_session)
    print(f"[{ue}] == {len(records)} answered ({len(ok)} success, {len(records) - len(ok)} fail), {len(fails)} errors, {dropped} skipped "
          f"(inflight cap) | {len(records) / elapsed:.2f} req/s | UL {bytes_ul[0] * 8 / elapsed / 1e6:.1f} Mbps")
    if records:
        def m(k, rows):
            xs = [float(r[k]) for r in rows if r.get(k) is not None]
            return statistics.mean(xs) if xs else float("nan")
        print(f"[{ue}]    QoS {deadline[0]:.0f} ms: met {met}/{len(records)} ({100.0 * met / len(records):.1f} %) | "
              f"E2E mean {statistics.mean(e2e):.1f} p50 {pct(e2e, 50):.1f} p95 {pct(e2e, 95):.1f} max {max(e2e):.1f} ms")
        print(f"[{ue}]    mean: ul {m('ul_latency_ms', records):.1f} | ai {m('ai_latency_ms', records):.1f} | other {m('server_other_ms', records):.1f} | "
              f"dl {m('dl_latency_ms', records):.1f} ms || dApp: sm {m('dapp_sm', ok):.1f} wait {m('dapp_wait_ms', ok):.1f} dec {m('dapp_decode_ms', ok):.1f} "
              f"inf {m('dapp_infer_ms', ok):.1f} srv {m('dapp_latency_ms', ok):.1f} ms | det {m('detections', ok):.1f}")
        per_model = {}
        for r in records:
            if r.get("dapp_model"):
                per_model[r["dapp_model"]] = per_model.get(r["dapp_model"], 0) + 1
        if per_model:
            print(f"[{ue}]    models: " + ", ".join(f"{k} {v}" for k, v in sorted(per_model.items(), key=lambda kv: "nsmlx".find(kv[0][-1]))))
        reasons = {}
        for r in records:
            if r["dapp_status"] not in (None, "success"):
                reasons[r["dapp_reason"]] = reasons.get(r["dapp_reason"], 0) + 1
        if reasons or fails:
            print(f"[{ue}]    failures: {reasons} {fails[:3]}")
    if saved.get("saved"):
        print(f"[{ue}]    log: {saved['path']} ({saved['records']} records)")
    return 0 if records and not fails else 1


if __name__ == "__main__":
    sys.exit(main())
