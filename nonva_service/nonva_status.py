#!/usr/bin/env python3
"""nonva_status.py - print GET /nonva/status of the running non-VA service (used by nonva.sh status)"""
import argparse
import json
import ssl
import sys
from urllib.request import urlopen

ap = argparse.ArgumentParser()
ap.add_argument("--port", type=int, default=8001)
a = ap.parse_args()
try:
    with urlopen(f"https://127.0.0.1:{a.port}/nonva/status", context=ssl._create_unverified_context(), timeout=5) as r:
        s = json.load(r)
except Exception as exc:
    sys.exit(f"   /nonva/status unreachable: {exc}")
c = s["config"]
print(f"   config: {c['width']}x{c['height']} @ {c['fps']:g} fps JPEG q{c['jpeg_quality']:g}, viewer {c['viewer_fps']:g} fps | telemetry {c['telemetry_dir']}")
for sid, x in (s.get("sessions") or {}).items():
    print(f"   session {sid}: {'ACTIVE' if x['active'] else 'stopped'} frames {x['frames']} latest #{x['latest_frame_id']} {x['latest_frame_kb']} KB ul {x['latest_ul_latency_ms']:.1f} ms")
for u, r in s["ue"].items():
    print(f"   {u}: {'ACTIVE' if r['active'] else 'idle'} {r['status']} latency {r['latency_ms']} frame {r['frame_id']} failures {r['failures']}")
print(f"   CSV dir: {c['log_dir']}")
