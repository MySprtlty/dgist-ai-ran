"""dapp_backend.py - inference backends of the VA service (DGIST edition, 2026-11-05)

dapp  : every camera frame becomes one RUN request to the DGIST dApp agent that runs inside the cuphycontroller (L1) process:
        TCP 9810, one JSON line + the raw JPEG (protocol: aerial-cuda-accelerated-ran/cuPHY-CP/dapp_hook/docs/XAPP_INTERFACE.md,
        client: .../dapp_hook/python/xapp_client.py). The agent decodes the JPEG on the GPU (nvjpeg), runs the requested model on the
        SMs the L1 does not need in the coming slots (CUDA green contexts) and answers with the result and, because we ask for
        want_dets, the detections in the original image coordinates. One TCP connection serves every UE; requests are matched by
        req_id, so two UEs' frames can be in flight at the same time.
local : the original hsw path (ultralytics YOLO in this process, PyTorch on the same GPU as a separate process) for A/B comparisons.

Both return  {"detections": [{"class", "confidence", "x1", "y1", "x2", "y2"}, ...],   # the format the phone page draws
              "dapp": {"backend", "status", "reason", "model", "sm", "wait_ms", "decode_ms", "infer_ms", "latency_ms", ...}}
"""
import os
import sys
import threading
import time
from pathlib import Path

# ultralytics COCO class names (yolo26*.pt); the agent reports the class index only
COCO80 = ["person", "bicycle", "car", "motorcycle", "airplane", "bus", "train", "truck", "boat", "traffic light", "fire hydrant",
          "stop sign", "parking meter", "bench", "bird", "cat", "dog", "horse", "sheep", "cow", "elephant", "bear", "zebra", "giraffe",
          "backpack", "umbrella", "handbag", "tie", "suitcase", "frisbee", "skis", "snowboard", "sports ball", "kite", "baseball bat",
          "baseball glove", "skateboard", "surfboard", "tennis racket", "bottle", "wine glass", "cup", "fork", "knife", "spoon", "bowl",
          "banana", "apple", "sandwich", "orange", "broccoli", "carrot", "hot dog", "pizza", "donut", "cake", "chair", "couch",
          "potted plant", "bed", "dining table", "toilet", "tv", "laptop", "mouse", "remote", "keyboard", "cell phone", "microwave",
          "oven", "toaster", "sink", "refrigerator", "book", "clock", "vase", "scissors", "teddy bear", "hair drier", "toothbrush"]
assert len(COCO80) == 80

DAPP_MODELS = ("26n", "26s", "26m", "26l", "26x")
RESULT_KEYS = ("req_id", "sm", "sm_mean", "wait_ms", "decode_ms", "infer_ms", "latency_ms", "tiles")


def _client_dir():
    """where xapp_client.py lives: $DAPP_CLIENT_DIR, else the Aerial tree of this package"""
    here = Path(__file__).resolve().parent
    for c in (os.environ.get("DAPP_CLIENT_DIR"), here.parent / "aerial-cuda-accelerated-ran/cuPHY-CP/dapp_hook/python", here / "dapp_python"):
        if c and Path(c, "xapp_client.py").is_file():
            return str(c)
    raise FileNotFoundError("xapp_client.py not found: set DAPP_CLIENT_DIR or keep ../aerial-cuda-accelerated-ran/cuPHY-CP/dapp_hook/python")


class DappBackend:
    name = "dapp"

    def __init__(self, agent="127.0.0.1:9810", model="26x", deadline_ms=200, priority=0, role="va", client_name="va-service",
                 result_timeout=3.0):
        host, _, port = str(agent).partition(":")
        self.host, self.port = host or "127.0.0.1", int(port or 9810)
        if model not in DAPP_MODELS:
            raise ValueError(f"unknown dApp model {model!r} (one of {DAPP_MODELS})")
        self.model, self.deadline_ms, self.priority = model, int(deadline_ms), int(priority)
        self.role, self.client_name, self.result_timeout = role, client_name, float(result_timeout)
        cdir = _client_dir()
        if cdir not in sys.path:
            sys.path.insert(0, cdir)
        from xapp_client import DappAgent   # noqa: E402  (the dApp client library of the Aerial tree)
        self._DappAgent = DappAgent
        self._agent = None
        self._lock = threading.Lock()
        self._next_try = 0.0
        self.counters = {"requests": 0, "success": 0, "fail": 0, "timeout": 0, "unreachable": 0, "connects": 0}
        self.last_error = None
        self.connected_since = None
        try:
            self._connect()
        except ConnectionError as exc:
            print(f"[VA] dApp agent {self.host}:{self.port} not reachable yet ({exc}); will retry on the first frame", flush=True)

    # ---- connection -----------------------------------------------------------------------------------------------
    def _alive(self):
        a = self._agent
        return a is not None and a._rx.is_alive()        # the receive thread ends when the agent closes the socket

    def _connect(self):
        with self._lock:
            if self._alive():
                return self._agent
            now = time.monotonic()
            if now < self._next_try:
                raise ConnectionError(self.last_error or "agent down (retry backoff)")
            self._next_try = now + 1.0
            if self._agent is not None:
                try:
                    self._agent.close()
                except Exception:
                    pass
                self._agent = None
                self.connected_since = None
            try:
                a = self._DappAgent(self.host, self.port, timeout=self.result_timeout)
                # role "va": the Resource-Allocator ("ra") and Slicing xApps keep their own connections; estimates every 200 slots
                w = a.hello(role=self.role, name=self.client_name, subscribe=["estimate"], every=200, wait=3.0)
            except OSError as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"
                self.counters["unreachable"] += 1
                raise ConnectionError(self.last_error) from exc
            if w is None:
                a.close()
                self.last_error = "connected but no welcome from the agent"
                raise ConnectionError(self.last_error)
            if self.model not in (w.get("models") or DAPP_MODELS):
                print(f"[VA] warning: agent models {w.get('models')} do not list {self.model}", flush=True)
            self._agent = a
            self.connected_since = time.time()
            self.counters["connects"] += 1
            self.last_error = None
            print(f"[VA] dApp agent connected {self.host}:{self.port}: models {w.get('models')} gpu_sm {w.get('gpu_sm')} "
                  f"tdd {w.get('tdd')} cells {w.get('cells')}", flush=True)
            return a

    # ---- one frame --------------------------------------------------------------------------------------------------
    def infer(self, jpeg_bytes, ue_id=None):
        t0 = time.monotonic()
        self.counters["requests"] += 1
        try:
            a = self._agent if self._alive() else self._connect()
        except ConnectionError as exc:
            return {"detections": [], "dapp": self._fields(None, t0, reason="agent_unreachable", error=str(exc))}
        try:
            r = a.run(jpeg_bytes, model=self.model, qos={"deadline_ms": self.deadline_ms, "priority": self.priority},
                      want_dets=True, timeout=self.result_timeout)
        except OSError as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            return {"detections": [], "dapp": self._fields(None, t0, reason="agent_lost", error=self.last_error)}
        status = r.get("status")
        if status == "timeout":
            self.counters["timeout"] += 1
            if not self._alive():
                self.last_error = "connection closed by the agent"
            return {"detections": [], "dapp": self._fields(r, t0, reason="timeout", error=f"no result within {self.result_timeout:.0f} s")}
        self.counters["success" if status == "success" else "fail"] += 1
        dets = []
        for d in r.get("dets") or []:
            cls = int(d.get("cls", -1))
            xy = d.get("xyxy") or [0, 0, 0, 0]
            dets.append({"class": COCO80[cls] if 0 <= cls < len(COCO80) else str(cls),
                         "confidence": round(float(d.get("conf", 0.0)), 3),
                         "x1": round(float(xy[0]), 2), "y1": round(float(xy[1]), 2),
                         "x2": round(float(xy[2]), 2), "y2": round(float(xy[3]), 2)})
        return {"detections": dets, "dapp": self._fields(r, t0)}

    def _fields(self, r, t0, reason=None, error=None):
        f = {"backend": "dapp", "status": (r or {}).get("status", "fail"), "reason": (r or {}).get("reason", reason),
             "model": (r or {}).get("model", self.model), "deadline_ms": self.deadline_ms,
             "client_ms": round((time.monotonic() - t0) * 1000.0, 3)}
        if r:
            for k in RESULT_KEYS:
                if k in r:
                    f[k] = r[k]
            if "detections" in r:
                f["n_detections"] = r["detections"]
            if "e2e_ms" in r:
                f["rtt_ms"] = round(r["e2e_ms"], 3)        # send -> result on this host (agent latency + socket)
        if error:
            f["error"] = error
        return f

    def status(self):
        a = self._agent
        alive = self._alive()
        return {"backend": "dapp", "agent": f"{self.host}:{self.port}", "role": self.role, "connected": alive,
                "connected_since": self.connected_since, "model": self.model, "deadline_ms": self.deadline_ms,
                "welcome": a.welcome if a else None, "estimate": a.latest_estimate if (a and alive) else None,
                "counters": dict(self.counters), "last_error": self.last_error}


class LocalBackend:
    """the hsw original: ultralytics YOLO in this process (PyTorch); decode + predict serialised by one lock"""
    name = "local"

    def __init__(self, model_path, device=0):
        from ultralytics import YOLO
        self.model_path, self.device = str(model_path), device
        print(f"[VA] loading {self.model_path} (ultralytics, device {device}) ...", flush=True)
        self.model = YOLO(self.model_path)
        self.lock = threading.Lock()
        self.counters = {"requests": 0, "success": 0, "fail": 0}
        print("[VA] local model loaded", flush=True)

    def infer(self, jpeg_bytes, ue_id=None):
        import io
        from PIL import Image
        t0 = time.monotonic()
        self.counters["requests"] += 1
        try:
            image = Image.open(io.BytesIO(jpeg_bytes)).convert("RGB")
            t1 = time.monotonic()
            with self.lock:
                t2 = time.monotonic()
                results = self.model.predict(source=image, device=self.device, verbose=False)
                t3 = time.monotonic()
        except Exception as exc:
            self.counters["fail"] += 1
            return {"detections": [], "dapp": {"backend": "local", "status": "fail", "reason": "internal", "error": repr(exc),
                                               "model": Path(self.model_path).name, "client_ms": round((time.monotonic() - t0) * 1e3, 3)}}
        res = results[0]
        dets = []
        for box in res.boxes:
            x1, y1, x2, y2 = box.xyxy[0].tolist()
            dets.append({"class": res.names[int(box.cls[0])], "confidence": round(float(box.conf[0]), 3),
                         "x1": round(float(x1), 2), "y1": round(float(y1), 2), "x2": round(float(x2), 2), "y2": round(float(y2), 2)})
        self.counters["success"] += 1
        return {"detections": dets, "dapp": {"backend": "local", "status": "success", "reason": None, "model": Path(self.model_path).name,
                                             "sm": None, "decode_ms": round((t1 - t0) * 1e3, 3), "wait_ms": round((t2 - t1) * 1e3, 3),
                                             "infer_ms": round((t3 - t2) * 1e3, 3), "latency_ms": round((t3 - t0) * 1e3, 3),
                                             "client_ms": round((time.monotonic() - t0) * 1e3, 3), "n_detections": len(dets)}}

    def status(self):
        return {"backend": "local", "model": self.model_path, "device": self.device, "connected": True, "counters": dict(self.counters)}


def make_backend(kind, agent="127.0.0.1:9810", model="26x", deadline_ms=200, priority=0, local_model_path="yolo26x.pt", device=0):
    if kind == "dapp":
        return DappBackend(agent=agent, model=model, deadline_ms=deadline_ms, priority=priority)
    if kind == "local":
        return LocalBackend(local_model_path, device)
    raise ValueError(f"VA_BACKEND must be dapp or local, not {kind!r}")
