#!/usr/bin/env python3
"""nonva_sim.py - a phone stand-in for the non-VA streaming service (2026-11-05), standard library only:

   GET /clock_sync x10 -> POST /start_session (form) -> POST /startup_metrics -> N x POST /upload_frame (multipart, at fps)
   -> POST /stop_session?session_id&session_token  (writes the same CSV the phone produces)

  python3 nonva_sim.py --ue ue1 --count 100 --fps 20 --image test_frames/hd_1280x720_q80.jpg
"""
import argparse
import json
import ssl
import statistics
import sys
import time
import uuid
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

CTX = ssl._create_unverified_context()


def do(url, data=None, headers=None, method=None, timeout=10.0):
    req = Request(url, data=data, method=method or ("POST" if data is not None else "GET"), headers=headers or {})
    with urlopen(req, context=CTX, timeout=timeout) as r:
        return json.loads(r.read())


def form(url, fields):
    return do(url, urlencode(fields).encode(), {"Content-Type": "application/x-www-form-urlencoded"})


def multipart(url, fields, filename, data):
    b = "----nonva_sim" + uuid.uuid4().hex
    body = b"".join(f'--{b}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode() for k, v in fields.items())
    body += (f'--{b}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\nContent-Type: image/jpeg\r\n\r\n'.encode()
             + data + b"\r\n" + f"--{b}--\r\n".encode())
    return do(url, body, {"Content-Type": f"multipart/form-data; boundary={b}"})


def main():
    ap = argparse.ArgumentParser(description="phone stand-in for the non-VA streaming service")
    ap.add_argument("--url", default="https://127.0.0.1:8001")
    ap.add_argument("--ue", default="ue1", choices=["ue1", "ue2"])
    ap.add_argument("--image", default=str(Path(__file__).resolve().parent / "test_frames" / "hd_1280x720_q80.jpg"))
    ap.add_argument("--fps", type=float, default=20.0)
    ap.add_argument("--count", type=int, default=100)
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args()
    img = Path(a.image)
    if not img.is_file():
        sys.exit(f"image not found: {img}")
    data = img.read_bytes()
    u = a.url.rstrip("/")
    # clock sync (NTP style, best 5 of 10)
    t_sync0 = time.perf_counter()
    samples = []
    for _ in range(10):
        t0 = time.time() * 1000.0
        d = do(f"{u}/clock_sync?x={uuid.uuid4().hex}")
        t3 = time.time() * 1000.0
        t1, t2 = d["server_receive_ms"], d["server_send_ms"]
        samples.append(((t3 - t0) - (t2 - t1), ((t1 - t0) + (t2 - t3)) / 2.0))
        time.sleep(0.02)
    samples.sort()
    offset = statistics.mean(o for _, o in samples[:5])
    rtt = samples[0][0]
    sync_ms = (time.perf_counter() - t_sync0) * 1000.0
    try:
        s = form(f"{u}/start_session", {"clock_offset_ms": offset, "sync_rtt_ms": rtt, "ue_id": a.ue, "clock_sync_time_ms": sync_ms})
    except HTTPError as e:
        sys.exit(f"start_session failed ({e.code}): {e.read().decode()[:200]}  (is {a.ue} still streaming? stop it or use the other UE)")
    except URLError as e:
        sys.exit(f"cannot reach {u}: {e}")
    sid, tok = s["session_id"], s["session_token"]
    form(f"{u}/startup_metrics", {"session_id": sid, "session_token": tok, "session_start_time_ms": 5.0, "total_startup_time_ms": sync_ms + 5.0})
    print(f"[{a.ue}] session {tok[:8]} | clock offset {offset:+.2f} ms (rtt {rtt:.2f}) | {img.name} {len(data)/1024:.0f} KB | {a.fps:g} fps, {a.count} frames | viewer {u}{s.get('viewer_url')}")
    ul, errors = [], 0
    period = 1.0 / a.fps
    t_next = time.perf_counter()
    t_start = t_next
    for i in range(1, a.count + 1):
        now = time.perf_counter()
        if t_next > now:
            time.sleep(t_next - now)
        t_next += period
        try:
            r = multipart(f"{u}/upload_frame", {"frame_id": i, "upload_start_ms": time.time() * 1000.0, "session_id": sid, "session_token": tok},
                          "frame.jpg", data)
            ul.append(float(r["ul_latency_ms"]))
            if not a.quiet and (i % 20 == 0 or i == 1):
                print(f"[{a.ue}] #{i:4d} ul {r['ul_latency_ms']:.1f} ms")
        except (HTTPError, URLError, OSError) as e:
            errors += 1
            if errors <= 3:
                print(f"[{a.ue}] #{i} error: {e}")
    elapsed = time.perf_counter() - t_start
    saved = do(f"{u}/stop_session?" + urlencode({"session_id": sid, "session_token": tok}), data=b"", method="POST")
    if ul:
        ul.sort()
        p = lambda q: ul[min(len(ul) - 1, int(q * len(ul)))]
        print(f"[{a.ue}] == {len(ul)} frames ok, {errors} errors | {len(ul) / elapsed:.1f} fps | UL {len(ul) * len(data) * 8 / elapsed / 1e6:.1f} Mbps | "
              f"UL latency mean {statistics.mean(ul):.1f} p50 {p(.5):.1f} p90 {p(.9):.1f} max {ul[-1]:.1f} ms")
    print(f"[{a.ue}]    log: {saved.get('path')} ({saved.get('records')} records)")
    return 0 if ul and not errors else 1


if __name__ == "__main__":
    sys.exit(main())
