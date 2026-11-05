#!/usr/bin/env python3
"""dapp_compute_writer.py - the DGIST producer for the 6G bundle's compute-telemetry file (2026-11-05)

The hsw 6G policy (sm_runtime.py, 20 inputs) takes, besides the 18 radio inputs, the YOLO model and the number of SMs allocated to it,
read by sm_reader.py from a JSON file that a producer must atomically replace with a fresh observation (README_6G.txt):
    {"model_id": "26x", "allocated_sm": 80, "timestamp_ms": <Unix ms of the observation>}
    allocated_sm integer 24..80; the policy refuses (ERR/HOLD) a compute input older than 2000 ms.

This producer is the DGIST dApp agent inside the cuphycontroller (TCP 9810, role "slicing"): every sm_report (PPO_DAPP_PERIOD_MS,
default 100 ms) is one observation of the YOLO allocation and is written as
    model_id     = model of the most recently finished request (sm_report.last; window fallback for an older L1), else PPO_COMPUTE_MODEL
    allocated_sm = SMs that request ran on when it finished within PPO_COMPUTE_LAST_MAX_MS (default 2000 ms), else the agent's current
                   estimate est_sm[model_id] (the SMs a request of that model would receive right now); clamped to the policy's 24..80
    timestamp_ms = the report's t_ns (CLOCK_REALTIME of the agent, same host) - a real observation time, never a re-stamp of old state
Nothing is written while the agent is unreachable or its report is stale, so the policy holds its confirmed budget (their contract).

  dapp_compute_writer.py --output runs/xapp/compute_state.json [--agent 127.0.0.1:9810] [--model 26x] [--verbose]
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from dapp_reader import DappReader   # noqa: E402

MODELS = ("26n", "26s", "26m", "26l", "26x")


def write_atomic(dest: Path, obj: dict):
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(obj), encoding="utf-8")
    os.replace(tmp, dest)


def main():
    ap = argparse.ArgumentParser(description="dApp agent -> 6G compute-telemetry file")
    ap.add_argument("--output", default=os.environ.get("PPO_SM_INPUT_FILE"), required=os.environ.get("PPO_SM_INPUT_FILE") is None)
    ap.add_argument("--agent", default=os.environ.get("PPO_DAPP_AGENT", "127.0.0.1:9810"))
    ap.add_argument("--model", default=os.environ.get("PPO_COMPUTE_MODEL", os.environ.get("VA_MODEL", "26x")), help="model when the agent has not finished any request yet")
    ap.add_argument("--last-max-ms", type=float, default=float(os.environ.get("PPO_COMPUTE_LAST_MAX_MS", "2000")))
    ap.add_argument("--max-report-age-ms", type=float, default=1000.0)
    ap.add_argument("--verbose", action="store_true")
    a = ap.parse_args()
    dest = Path(a.output)
    reader = DappReader(agent=a.agent, role="slicing", name="ppo-6g-compute-writer")
    print(f"[COMPUTE] dApp agent {a.agent} -> {dest} (model fallback {a.model}, last request accepted within {a.last_max_ms:.0f} ms)", flush=True)
    last_key, last_print, writes, holds = None, 0.0, 0, 0
    try:
        while True:
            time.sleep(0.05)
            s = reader.snapshot()
            rep_age = s.get("report_age_ms")
            if not s.get("available") or rep_age is None or rep_age > a.max_report_age_ms:
                holds += 1
                if time.monotonic() - last_print > 5:
                    print(f"[COMPUTE] no fresh report ({s.get('error') or 'age ' + str(s.get('report_age_ms')) + ' ms'}); nothing written -> policy holds", flush=True)
                    last_print = time.monotonic()
                continue
            # which model / how many SMs
            model = s.get("last_model") if s.get("last_model") in MODELS else a.model
            src = "last"
            sm = s.get("last_sm")
            age = s.get("last_age_ms")
            if sm is None or age is None or age > a.last_max_ms:
                est = (s.get("est_sm") or {}) if isinstance(s.get("est_sm"), dict) else {}
                sm = est.get(model)
                src = "estimate"
            if sm is None:
                holds += 1
                if time.monotonic() - last_print > 5:
                    print(f"[COMPUTE] no SM figure for {model}: last_sm {s.get('last_sm')} (age {s.get('last_age_ms')}), est_sm {s.get('est_sm')}; nothing written -> policy holds", flush=True)
                    last_print = time.monotonic()
                continue
            sm = int(round(float(sm)))
            sm_clamped = max(24, min(80, sm))
            # observation time = the agent's report time (its CLOCK_REALTIME); report_age_ms tells how long ago that was
            ts_ms = time.time() * 1000.0 - float(s.get("report_age_ms") or 0.0)
            obj = {"model_id": model, "allocated_sm": sm_clamped, "timestamp_ms": ts_ms}
            write_atomic(dest, obj)
            writes += 1
            key = (model, sm_clamped, src)
            if key != last_key or (a.verbose and time.monotonic() - last_print > 5) or time.monotonic() - last_print > 30:
                print(f"[COMPUTE] {model} sm {sm_clamped}{' (clamped from %d)' % sm if sm != sm_clamped else ''} from {src}"
                      f"{' (last request %.0f ms ago, inf %s ms)' % (age or 0, s.get('last_infer_ms')) if src == 'last' else ''} | "
                      f"YOLO {s.get('yolo_sm_time_mean')} / RAN {s.get('ran_sm_time_mean')} SM time-mean | yolo_sm_now {s.get('yolo_sm_now')} | writes {writes} holds {holds}", flush=True)
                last_key, last_print = key, time.monotonic()
    except KeyboardInterrupt:
        pass
    finally:
        reader.close()
        print(f"[COMPUTE] stopped: {writes} writes, {holds} holds", flush=True)


if __name__ == "__main__":
    main()
