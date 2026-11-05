#!/usr/bin/env python3
"""trace_tail.py - print the last N PPO decisions of the newest input trace in one line each (used by `xapp.sh trace [N]`).
Handles both bundles: the 19-input GPU-util bundle (trace field "dapp" from our patched bridge, gpu_util_model_input) and the
6G 20-input bundle (fields compute_model / allocated_sm = the dApp's YOLO model and SMs fed through the compute file)."""
import glob
import json
import os
import sys

n = int(sys.argv[1]) if len(sys.argv) > 1 else 5
dirs = sys.argv[2:] or [os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "runs", "xapp", "ppo_input_log")]
files = []
for d in dirs:
    files += glob.glob(os.path.join(d, "*.log"))
files = sorted(files, key=os.path.getmtime)
if not files:
    sys.exit(f"no trace in {dirs}")
f = files[-1]
lines = open(f, encoding="utf-8").read().splitlines()
print(f"== {f} (last {n} of {len(lines)})")
for ln in lines[-n:]:
    try:
        r = json.loads(ln)
    except ValueError:
        continue
    if "HOLD" in r:
        print(f"seq {r.get('seq')}: HOLD {r['HOLD']}")
        continue
    q = r["proposed_prbs"]
    u = r["input"]["ues"]
    tp = lambda k: u[k]["ul_tp_mbps"]
    buf = lambda k: u[k]["ul_buffer_bytes"]
    if "compute_model" in r:   # 6G bundle
        comp = r["input"].get("compute") or {}
        age = r["wall_ms"] - comp.get("timestamp_ms", r["wall_ms"])
        gpu = f"compute {r['compute_model']} sm {r['allocated_sm']} (age {age:.0f} ms)"
    else:
        dp = r.get("dapp") or {}
        gpu = (f"gpu in {r['gpu_util_model_input']:.1f} ({r['gpu_input_mode']}) | dApp last {dp.get('last_model')} sm {dp.get('last_sm')} "
               f"YOLO {dp.get('yolo_sm_time_mean')}/RAN {dp.get('ran_sm_time_mean')} SM")
    print(f"seq {r['seq']:6d} | yolo {q['yolo']:3d} live {q['live']:3d} PRB | {gpu} | tp Mbps yolo {tp('yolo_1'):.1f}/{tp('yolo_2'):.1f} "
          f"live {tp('live_1'):.1f}/{tp('live_2'):.1f} | buf KB yolo {buf('yolo_1')/1e3:.0f}/{buf('yolo_2')/1e3:.0f} live {buf('live_1')/1e3:.0f}/{buf('live_2')/1e3:.0f} | {r['bridge_us']} us")
