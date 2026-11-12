#!/usr/bin/env python3
"""vcam.py - virtual cameras for the demo's load step (2026-11-12): N streams of FHD JPEG frames sent straight to the dApp agent
(the same RUN path the phones' frames take inside the VA service), no uplink involved, so only the YOLO request rate rises.

Each camera is an OPEN loop: it offers --fps frames per second whatever the agent's latency is, with at most --inflight frames
waiting for a result (like the phone page); when all are busy the frame is skipped and counted. Frames come from --frames
(JPEGs, cycled, each camera starting at a different offset) so the pictures move. One TCP connection to the agent, role "vcam",
so the VA service's own connection (role "va") is untouched.

Status: one line every --report seconds on stdout and a JSON snapshot every second in --status (per camera and total: offered,
sent, skipped, success, fail by reason, achieved req/s, RUN round trip p50/p90/p99 over the last 60 s, 'met' = success within
--deadline). --dry-run exercises the pacing without an agent (simulated 17 ms latency).

usage: vcam.py --cams N [--fps 10] [--model 26x] [--deadline 200] [--inflight 4] [--frames DIR] [--agent 127.0.0.1:9810]
               [--status runs/vcam/status.json] [--duration SEC] [--report 5] [--dry-run]        (vcam.sh start|stop|status)
"""
import argparse, collections, json, os, queue, random, signal, sys, threading, time
from pathlib import Path

HERE = Path(__file__).resolve().parent
DGIST = HERE.parent


def load_frames(d):
    p = Path(d)
    files = sorted(x for x in p.glob("*.jp*g")) if p.is_dir() else []
    if not files:
        fb = HERE / "test_frames" / "frame_1920x1080_q90.jpg"
        files = [fb] if fb.exists() else []
    frames = [f.read_bytes() for f in files]
    if not frames:
        sys.exit(f"!! no frames: put FHD JPEGs into {d} (vcam.sh make-frames) or keep test_frames/frame_1920x1080_q90.jpg")
    return frames, [f.name for f in files]


class Cam:
    def __init__(self, idx, frames, fps, inflight, deadline, model, agent, dry, lock):
        self.idx, self.frames, self.fps, self.cap, self.deadline, self.model, self.agent, self.dry = idx, frames, fps, inflight, deadline, model, agent, dry
        self.q = queue.Queue(maxsize=max(1, inflight))
        self.pos = (idx * 7) % len(frames)
        self.offered = self.sent = self.skipped = self.success = self.met = 0
        self.fail = collections.Counter()
        self.lat = collections.deque()      # (t, round trip ms, success, met)
        self.lock = lock
        self.stop = threading.Event()

    def pace(self):
        period = 1.0 / self.fps; nxt = time.monotonic() + random.random() * period
        while not self.stop.is_set():
            now = time.monotonic()
            if now < nxt:
                time.sleep(min(nxt - now, 0.05)); continue
            nxt += period
            if nxt < now - period:      # fell far behind (sleep overrun): resync instead of bursting
                nxt = now + period
            self.offered += 1
            fr = self.frames[self.pos]; self.pos = (self.pos + 1) % len(self.frames)
            try:
                self.q.put_nowait(fr)
            except queue.Full:
                self.skipped += 1

    def work(self):
        while not self.stop.is_set():
            try:
                fr = self.q.get(timeout=0.2)
            except queue.Empty:
                continue
            t0 = time.monotonic()
            if self.dry:
                time.sleep(0.017 + random.random() * 0.004); r = {"status": "success", "sm": 80, "infer_ms": 14.5}
            else:
                try:
                    r = self.agent.run(fr, model=self.model, qos={"deadline_ms": self.deadline, "priority": 0}, want_dets=False, timeout=5.0)
                except OSError as exc:
                    r = {"status": "fail", "reason": f"io:{type(exc).__name__}"}
            ms = (time.monotonic() - t0) * 1000.0
            ok = r.get("status") == "success"
            met = ok and ms <= self.deadline
            with self.lock:
                self.sent += 1
                if ok: self.success += 1
                else: self.fail[str(r.get("reason") or r.get("status"))] += 1
                if met: self.met += 1
                self.lat.append((time.monotonic(), ms, ok, met))
                while self.lat and self.lat[0][0] < time.monotonic() - 60.0:
                    self.lat.popleft()

    def snapshot(self):
        with self.lock:
            L = list(self.lat)
        ms = sorted(x[1] for x in L)
        pct = lambda p: round(ms[int(p * (len(ms) - 1))], 1) if ms else None
        span = (L[-1][0] - L[0][0]) if len(L) > 1 else 0.0
        return {"cam": self.idx, "offered": self.offered, "sent": self.sent, "skipped": self.skipped, "success": self.success,
                "fail": dict(self.fail), "met": self.met, "req_s_60": round(len(L) / span, 1) if span > 5 else None,
                "rt_p50": pct(.5), "rt_p90": pct(.9), "rt_p99": pct(.99), "met_pct_60": round(100.0 * sum(1 for x in L if x[3]) / len(L), 1) if L else None,
                "inflight": self.q.qsize()}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cams", type=int, required=True); ap.add_argument("--fps", type=float, default=10.0)
    ap.add_argument("--model", default=os.environ.get("VA_MODEL", "26x")); ap.add_argument("--deadline", type=int, default=int(os.environ.get("VA_DEADLINE_MS", 200)))
    ap.add_argument("--inflight", type=int, default=4); ap.add_argument("--frames", default=str(HERE / "vcam_frames"))
    ap.add_argument("--agent", default=os.environ.get("VA_AGENT", "127.0.0.1:9810")); ap.add_argument("--status", default=str(DGIST / "runs" / "vcam" / "status.json"))
    ap.add_argument("--duration", type=float, default=0); ap.add_argument("--report", type=float, default=5); ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    frames, names = load_frames(a.frames)
    agent = None
    if not a.dry_run:
        sys.path.insert(0, os.environ.get("DAPP_CLIENT_DIR", str(DGIST / "aerial-cuda-accelerated-ran" / "cuPHY-CP" / "dapp_hook" / "python")))
        from xapp_client import DappAgent
        host, port = a.agent.split(":")
        agent = DappAgent(host, int(port), timeout=10.0)
        w = agent.hello(role="vcam", name=f"vcam-{a.cams}x{a.fps:g}", wait=3.0)
        if w is None:
            sys.exit("!! no welcome from the dApp agent")
        print(f"[vcam] agent {a.agent}: models {w.get('models')} gpu_sm {w.get('gpu_sm')}", flush=True)
    lock = threading.Lock()
    cams = [Cam(i + 1, frames, a.fps, a.inflight, a.deadline, a.model, agent, a.dry_run, lock) for i in range(a.cams)]
    threads = []
    for c in cams:
        threads.append(threading.Thread(target=c.pace, daemon=True, name=f"pace{c.idx}"))
        for k in range(max(1, a.inflight)):
            threads.append(threading.Thread(target=c.work, daemon=True, name=f"work{c.idx}.{k}"))
    print(f"[vcam] {a.cams} camera(s) x {a.fps:g} fps = {a.cams * a.fps:g} req/s offered | model {a.model} deadline {a.deadline} ms inflight {a.inflight} | "
          f"{len(frames)} frames from {a.frames} ({sum(len(f) for f in frames) / len(frames) / 1024:.0f} KB mean){' | DRY RUN' if a.dry_run else ''}", flush=True)
    for t in threads: t.start()
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set()); signal.signal(signal.SIGINT, lambda *_: stop.set())
    Path(a.status).parent.mkdir(parents=True, exist_ok=True)
    t0 = time.monotonic(); last_report = t0
    try:
        while not stop.is_set() and (a.duration <= 0 or time.monotonic() - t0 < a.duration):
            time.sleep(1.0)
            snaps = [c.snapshot() for c in cams]
            tot = {"cams": a.cams, "fps": a.fps, "model": a.model, "deadline_ms": a.deadline, "inflight": a.inflight, "dry_run": a.dry_run,
                   "offered": sum(s["offered"] for s in snaps), "sent": sum(s["sent"] for s in snaps), "skipped": sum(s["skipped"] for s in snaps),
                   "success": sum(s["success"] for s in snaps), "met": sum(s["met"] for s in snaps),
                   "fail": dict(sum((collections.Counter(s["fail"]) for s in snaps), collections.Counter())),
                   "req_s_60": round(sum(s["req_s_60"] or 0 for s in snaps), 1), "uptime_s": round(time.monotonic() - t0), "ts": time.time()}
            allms = sorted(x[1] for c in cams for x in list(c.lat))
            if allms:
                tot.update(rt_p50=round(allms[len(allms) // 2], 1), rt_p90=round(allms[int(.9 * (len(allms) - 1))], 1), rt_p99=round(allms[int(.99 * (len(allms) - 1))], 1),
                           met_pct_60=round(100.0 * sum(1 for c in cams for x in list(c.lat) if x[3]) / len(allms), 1))
            tmp = a.status + ".tmp"
            with open(tmp, "w") as f: json.dump({"total": tot, "cams": snaps}, f)
            os.replace(tmp, a.status)
            if time.monotonic() - last_report >= a.report:
                last_report = time.monotonic()
                print(f"[vcam] {time.strftime('%H:%M:%S')} {tot['req_s_60']} req/s | sent {tot['sent']} skipped {tot['skipped']} fail {tot['fail'] or 0} | "
                      f"RT p50/p90/p99 {tot.get('rt_p50')}/{tot.get('rt_p90')}/{tot.get('rt_p99')} ms | met {tot.get('met_pct_60')} %", flush=True)
    finally:
        for c in cams: c.stop.set()
        if agent:
            try: agent.close()
            except Exception: pass
        snaps = [c.snapshot() for c in cams]
        print(f"[vcam] stopped after {time.monotonic() - t0:.0f} s: offered {sum(s['offered'] for s in snaps)} sent {sum(s['sent'] for s in snaps)} "
              f"skipped {sum(s['skipped'] for s in snaps)} success {sum(s['success'] for s in snaps)} met {sum(s['met'] for s in snaps)}", flush=True)


if __name__ == "__main__":
    main()
