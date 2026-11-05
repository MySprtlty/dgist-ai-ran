"""dapp_reader.py - the DGIST dApp agent as a telemetry source for the PPO xApp bridge (2026-11-05)

Connects to the dApp agent inside the cuphycontroller (TCP 9810, docs: aerial-cuda-accelerated-ran/cuPHY-CP/dapp_hook/docs/XAPP_INTERFACE.md)
with role "slicing", subscribes to `sm_report` (every PPO_DAPP_PERIOD_MS, default 100 ms) and `estimate` (every 200 slots), and keeps
the latest of each. `snapshot()` never raises; when nothing fresh is known it returns {"available": False, ...}.

What the slicing policy gets from it (the fields the GPU-aware xApp asked for):
  last_model / last_sm        the YOLO model of the most recently finished request and the number of SMs it ran on (sm_report.last)
  last_infer_ms, last_age_ms  its inference time and how long ago it finished
  yolo_sm_time_mean / ran_sm_time_mean   time-weighted SM shares of YOLO and of the RAN in the last report window (of gpu_sm, 132)
  yolo_sm_now, est_ms         the agent's current free-SM estimate and the per-model latency estimates (estimate)
  requests_window             requests finished in the last window
The reconnect loop runs in a daemon thread; the policy's 100 ms tick is never blocked by it.
"""
import os
import sys
import threading
import time
from pathlib import Path


def _client_dir():
    here = Path(__file__).resolve().parent
    for c in (os.environ.get("DAPP_CLIENT_DIR"), here.parent.parent / "aerial-cuda-accelerated-ran/cuPHY-CP/dapp_hook/python",
              here.parent / "dapp_python"):
        if c and Path(c, "xapp_client.py").is_file():
            return str(c)
    raise FileNotFoundError("xapp_client.py not found: set DAPP_CLIENT_DIR or keep ../../aerial-cuda-accelerated-ran/cuPHY-CP/dapp_hook/python")


class DappReader:
    def __init__(self, agent=None, period_ms=None, role="slicing", name="ppo-xapp", max_age_ms=1500.0):
        agent = agent or os.environ.get("PPO_DAPP_AGENT", "127.0.0.1:9810")
        host, _, port = agent.partition(":")
        self.host, self.port = host or "127.0.0.1", int(port or 9810)
        self.period_ms = int(period_ms or os.environ.get("PPO_DAPP_PERIOD_MS", "100"))
        self.role, self.name, self.max_age_ms = role, name, float(max_age_ms)
        cdir = _client_dir()
        if cdir not in sys.path:
            sys.path.insert(0, cdir)
        from xapp_client import DappAgent   # noqa: E402
        self._DappAgent = DappAgent
        self.lock = threading.Lock()
        self.report = None
        self.report_t = 0.0
        self.estimate = None
        self.estimate_t = 0.0
        self.welcome = None
        self.error = None
        self.connects = 0
        self._agent = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True, name="dapp-reader")
        self._thread.start()

    # ---- connection thread ----------------------------------------------------------------------------------------
    def _on_message(self, msg):
        t = msg.get("type")
        with self.lock:
            if t == "sm_report":
                self.report, self.report_t = msg, time.monotonic()
            elif t == "estimate":
                self.estimate, self.estimate_t = msg, time.monotonic()
            elif t == "welcome":
                self.welcome = msg

    def _run(self):
        while not self._stop.is_set():
            try:
                a = self._DappAgent(self.host, self.port, timeout=5.0)
                w = a.hello(role=self.role, name=self.name, subscribe=["sm_report", "estimate"], every=200,
                            period_ms=self.period_ms, on_message=self._on_message, wait=3.0)
                if w is None:
                    a.close()
                    raise ConnectionError("no welcome")
                with self.lock:
                    self._agent, self.error, self.welcome = a, None, w
                    self.connects += 1
                print(f"[DAPP] connected {self.host}:{self.port}: models {w.get('models')} gpu_sm {w.get('gpu_sm')} "
                      f"sm_report every {self.period_ms} ms", flush=True)
                while not self._stop.is_set() and a._rx.is_alive():
                    self._stop.wait(0.5)
                with self.lock:
                    self._agent = None
                    if not self._stop.is_set():
                        self.error = "connection closed by the agent"
                a.close()
            except OSError as exc:
                with self.lock:
                    self.error = f"{type(exc).__name__}: {exc}"
            if not self._stop.is_set():
                self._stop.wait(2.0)

    def close(self):
        self._stop.set()
        a = self._agent
        if a is not None:
            try:
                a.close()
            except Exception:
                pass

    # ---- what the bridge reads every tick ---------------------------------------------------------------------------
    def snapshot(self):
        now = time.monotonic()
        with self.lock:
            r, rt, e, et, w, err = self.report, self.report_t, self.estimate, self.estimate_t, self.welcome, self.error
            connected = self._agent is not None
        out = {"available": False, "connected": connected, "agent": f"{self.host}:{self.port}", "error": err,
               "gpu_sm": (w or {}).get("gpu_sm")}
        if r is not None:
            age = (now - rt) * 1000.0
            out.update(report_age_ms=round(age, 1), requests_window=r.get("requests"), window_ms=r.get("window_ms"),
                       sm_mean_window=r.get("sm_mean"), yolo_sm_time_mean=r.get("yolo_sm_time_mean"),
                       ran_sm_time_mean=r.get("ran_sm_time_mean"), sm_by_model=r.get("sm_by_model"))
            last = r.get("last")
            if last:
                out.update(last_model=last.get("model"), last_sm=last.get("sm"), last_sm_mean=last.get("sm_mean"),
                           last_status=last.get("status"), last_infer_ms=last.get("infer_ms"), last_latency_ms=last.get("latency_ms"),
                           last_age_ms=(last.get("age_ms") or 0) + age)
            else:
                # agent without the "last" field (older L1 build): fall back to the window's last request
                per = r.get("sm_mean_per_request") or []
                by = r.get("sm_by_model") or {}
                out.update(last_model=(next(iter(by)) if len(by) == 1 else None), last_sm=(per[-1] if per else None),
                           last_status=None, last_infer_ms=None, last_age_ms=None, last_from="window")
            out["available"] = age <= self.max_age_ms
        if e is not None:
            out.update(estimate_age_ms=round((now - et) * 1000.0, 1), yolo_sm_now=e.get("yolo_sm_now"), yolo_sm_min=e.get("yolo_sm_min"),
                       ul_sm=e.get("ul_sm"), dl_sm=e.get("dl_sm"), queue=e.get("queue"), inflight=e.get("inflight"), est_ms=e.get("est_ms"),
                       est_sm=e.get("est_sm"))   # est_sm[model] = SMs a request of that model would get now (its class)
        if out["available"] and out.get("gpu_sm") and out.get("yolo_sm_time_mean") is not None and out.get("ran_sm_time_mean") is not None:
            # GPU busy share seen by the L1 process: SMs owned by YOLO + RAN over the window, in percent of the GPU
            out["gpu_busy_share_percent"] = round(100.0 * (out["yolo_sm_time_mean"] + out["ran_sm_time_mean"]) / out["gpu_sm"], 2)
        return out

    def one_line(self):
        s = self.snapshot()
        if not s["available"]:
            return f"[DAPP] unavailable ({s.get('error') or 'no report yet'}; connected={s['connected']})"
        est = (s.get("est_ms") or {}).get(s.get("last_model") or "26x")
        return (f"[DAPP] last {s.get('last_model')} sm {s.get('last_sm')} ({s.get('last_status')}, inf {s.get('last_infer_ms')} ms, "
                f"{(s.get('last_age_ms') or 0):.0f} ms ago) | window {s.get('requests_window')} req, YOLO {s.get('yolo_sm_time_mean')} / "
                f"RAN {s.get('ran_sm_time_mean')} SM time-mean of {s.get('gpu_sm')} (busy {s.get('gpu_busy_share_percent')} %) | "
                f"yolo_sm_now {s.get('yolo_sm_now')} queue {s.get('queue')} est {est} ms")


if __name__ == "__main__":
    r = DappReader()
    try:
        for _ in range(int(os.environ.get("N", "5"))):
            time.sleep(1.0)
            print(r.one_line(), flush=True)
    finally:
        r.close()
