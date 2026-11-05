# =====================================================
# BACKGROUND VIRTUAL VA UE (same model and model_lock)
# No extra CUDA model/context or UE UL traffic.
# =====================================================
import os
import csv
import time
import threading
from pathlib import Path
from datetime import datetime
from PIL import Image
from fastapi import APIRouter, Request, HTTPException
from pydantic import BaseModel, Field

router = APIRouter()
_model = None
_model_lock = None
_read_gpu_metrics = None
_log_dir = None
_device = None

def configure_background(model, model_lock, read_gpu_metrics, log_dir, device):
    global _model, _model_lock, _read_gpu_metrics, _log_dir, _device
    _model = model
    _model_lock = model_lock
    _read_gpu_metrics = read_gpu_metrics
    _log_dir = log_dir
    _device = device

BG_IMAGE_PATH = Path(os.environ.get("BG_IMAGE_PATH", "bg_test_image.jpg"))
BG_MAX_FPS = 15.0
_bg_guard = threading.RLock()
_bg_stop = threading.Event()
_bg_thread = None
_bg_running = False
_bg_image = None
_bg_error = None
_bg_target_fps = 10.0
_bg_completed = 0
_bg_started_utc = None
_bg_log_path = None

class BackgroundStart(BaseModel):
    fps: float = Field(default=10.0, ge=0.1, le=BG_MAX_FPS)


def _bg_local_only(request: Request):
    host = request.client.host if request.client else ""
    if host not in ("127.0.0.1", "::1"):
        raise HTTPException(status_code=403, detail="Background control is localhost-only")


def _bg_worker():
    global _bg_error, _bg_running, _bg_completed, _bg_log_path
    try:
        with _bg_guard:
            image = _bg_image
        if image is None:
            raise RuntimeError("Background image not prepared")
        filename = "bg_virtual_ue_" + datetime.now().strftime("%Y%m%d_%H%M%S_%f") + ".log"
        log_path = _log_dir / filename
        with _bg_guard:
            _bg_log_path = str(log_path)
        with log_path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["timestamp", "target_fps", "lock_wait_ms", "predict_ms", "ai_total_ms", "gpu_util_percent", "gpu_memory_used_gb", "completed"])
            while not _bg_stop.is_set():
                cycle_start = time.monotonic()
                with _bg_guard:
                    fps = _bg_target_fps
                # Match the actual VA request's shared model_lock and predict call.
                with _model_lock:
                    acquired = time.monotonic()
                    if _bg_stop.is_set():
                        break
                    results = _model.predict(source=image, device=_device, verbose=False)
                    finished = time.monotonic()
                del results
                util, memory = _read_gpu_metrics()
                with _bg_guard:
                    _bg_completed += 1
                    completed = _bg_completed
                writer.writerow([
                    datetime.now().astimezone().isoformat(), fps,
                    round((acquired-cycle_start)*1000, 3),
                    round((finished-acquired)*1000, 3),
                    round((finished-cycle_start)*1000, 3),
                    util, memory, completed
                ])
                f.flush()
                _bg_stop.wait(max(0.0, 1.0/fps - (time.monotonic()-cycle_start)))
    except Exception as exc:
        with _bg_guard:
            _bg_error = repr(exc)
        print(f"Background virtual UE stopped with error: {exc!r}", flush=True)
    finally:
        with _bg_guard:
            _bg_running = False


@router.post("/background/prepare")
async def background_prepare(request: Request):
    """Load local image only; reuse the existing VA model and CUDA context."""
    global _bg_image, _bg_error
    _bg_local_only(request)
    if not BG_IMAGE_PATH.is_file():
        raise HTTPException(status_code=400, detail=f"Missing background image: {BG_IMAGE_PATH}")
    with _bg_guard:
        if _bg_running or (_bg_thread is not None and _bg_thread.is_alive()):
            raise HTTPException(status_code=409, detail="Stop background before preparing")
    try:
        with Image.open(BG_IMAGE_PATH) as source:
            image = source.convert("RGB")
        with _bg_guard:
            _bg_image = image
            _bg_error = None
        return {"prepared": True, "image_width": image.width, "image_height": image.height,
                "model_shared": True}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/background/start")
async def background_start(config: BackgroundStart, request: Request):
    global _bg_thread, _bg_running, _bg_error, _bg_target_fps, _bg_completed, _bg_started_utc, _bg_log_path
    _bg_local_only(request)
    with _bg_guard:
        if _bg_running or (_bg_thread is not None and _bg_thread.is_alive()):
            raise HTTPException(status_code=409, detail="Background already running or stopping")
        if _bg_image is None:
            raise HTTPException(status_code=409, detail="Call POST /background/prepare first")
        _bg_stop.clear()
        _bg_error = None
        _bg_target_fps = config.fps
        _bg_completed = 0
        _bg_log_path = None
        _bg_started_utc = datetime.now().astimezone().isoformat()
        _bg_running = True
        _bg_thread = threading.Thread(target=_bg_worker, daemon=True, name="bg-virtual-ue")
        _bg_thread.start()
    return {"status": "starting", "target_fps": config.fps, "model_shared": True}


@router.post("/background/stop")
async def background_stop(request: Request):
    _bg_local_only(request)
    _bg_stop.set()
    with _bg_guard:
        return {"status": "stopping" if _bg_running else "stopped", "completed": _bg_completed}


@router.get("/background/status")
async def background_status(request: Request):
    _bg_local_only(request)
    with _bg_guard:
        return {"running": _bg_running, "stopping": _bg_running and _bg_stop.is_set(),
                "prepared": _bg_image is not None, "model_shared": True,
                "target_fps": _bg_target_fps, "completed": _bg_completed,
                "started_utc": _bg_started_utc, "log_path": _bg_log_path, "error": _bg_error}


@router.on_event("shutdown")
def background_shutdown():
    _bg_stop.set()
    thread = _bg_thread
    if thread is not None and thread.is_alive():
        thread.join(timeout=5)


# =====================================================
# Command-line controller (does NOT run YOLO in this process)
# =====================================================
#!/usr/bin/env python3
import argparse, json, ssl, sys
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
URL = "https://localhost:8000"
CTX = ssl._create_unverified_context()  # localhost self-signed certificate only

def call(method, path, payload=None):
    data = json.dumps(payload).encode() if payload is not None else (b"" if method == "POST" else None)
    req = Request(URL + path, data=data, method=method)
    if payload is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urlopen(req, context=CTX, timeout=15) as response:
            return json.load(response)
    except HTTPError as exc:
        print(f"HTTP {exc.code}: {exc.read().decode()}", file=sys.stderr)
        sys.exit(1)
    except URLError as exc:
        print(f"Cannot connect to VA server: {exc}", file=sys.stderr)
        sys.exit(1)

def main():
    parser = argparse.ArgumentParser(description="Control BG YOLO inside the VA server")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("prepare")
    start = sub.add_parser("start")
    start.add_argument("--fps", type=float, default=10)
    sub.add_parser("stop")
    sub.add_parser("status")
    args = parser.parse_args()
    if args.command == "start":
        state = call("GET", "/background/status")
        if not state["prepared"]:
            call("POST", "/background/prepare")
        result = call("POST", "/background/start", {"fps": args.fps})
    elif args.command == "prepare":
        result = call("POST", "/background/prepare")
    elif args.command == "stop":
        result = call("POST", "/background/stop")
    else:
        result = call("GET", "/background/status")
    print(json.dumps(result, ensure_ascii=False, indent=2))



if __name__ == "__main__":
    main()

