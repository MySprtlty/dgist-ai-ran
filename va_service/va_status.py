#!/usr/bin/env python3
"""va_status.py - print GET /va/status of the running VA service in a readable form (used by va.sh status / start)"""
import argparse
import json
import ssl
import sys
from urllib.request import urlopen

ap = argparse.ArgumentParser()
ap.add_argument("--port", type=int, default=8000)
ap.add_argument("--short", action="store_true")
a = ap.parse_args()
try:
    with urlopen(f"https://127.0.0.1:{a.port}/va/status", context=ssl._create_unverified_context(), timeout=5) as r:
        s = json.load(r)
except Exception as exc:
    sys.exit(f"   /va/status unreachable: {exc}")
c, b = s["config"], s["backend"]
w = b.get("welcome") or {}
print(f"   backend: {b['backend']} | agent {b.get('agent', '-')} {'connected' if b.get('connected') else 'NOT connected'}"
      f"{' (models ' + ' '.join(w.get('models', [])) + ', gpu_sm ' + str(w.get('gpu_sm')) + ', tdd ' + str(w.get('tdd')) + ')' if w else ''}"
      f" | model {'rule (VA_MODEL_SELECT=1)' if c.get('model_select') else c['model']} QoS {c['deadline_ms']} ms | last_error: {b.get('last_error')}")
sel = b.get("select")
if sel:    # rule-based model selection: requests per block and what has been sent so far
    print(f"   model selection: per {sel['block']} requests " + " ".join(f"{m} {n}" for m, n in sel["weights"].items())
          + f" (random order) | sent so far ({sel['requests']}): " + " ".join(f"{m} {n}" for m, n in sel["sent"].items())
          + (f" | NOT loaded by the agent: {' '.join(sel['missing'])} -> {sel['replaced']} request(s) sent as {sel['fallback']}" if sel["missing"] else ""))
if a.short:
    sys.exit(0)
print(f"   config: {c['target_fps']:g} req/s per UE, inflight<={c['max_inflight']}, camera {c['camera'][0]}x{c['camera'][1]} JPEG q{c['jpeg_quality']:g}, "
      f"telemetry {c['telemetry_dir']}")
print(f"   counters: {b.get('counters')}")
e = b.get("estimate") or {}
if e:
    print(f"   agent estimate: yolo_sm_now {e.get('yolo_sm_now')} min {e.get('yolo_sm_min')} queue {e.get('queue')} inflight {e.get('inflight')} "
          f"est_ms {e.get('est_ms')} (sfn {e.get('sfn')} slot {e.get('slot')})")
for u, r in s["ue"].items():
    print(f"   {u}: {'ACTIVE' if r['active'] else 'idle'} {r['status']} latency {r['latency_ms']} frame {r['frame_id']} failures {r['failures']}")
print(f"   CSV dir: {c['log_dir']}")
