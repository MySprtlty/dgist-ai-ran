# =====================================================
# BACKGROUND VIRTUAL VA UE (hsw bg_yolo.py, DGIST edition 2026-11-05)
#
# A "virtual UE" that sends the same inference requests the phone sends, without radio traffic: the configured backend
# (dApp agent RUN, or the local model) receives one JPEG at the requested rate. Used to add YOLO load next to the real UEs.
# Control is localhost-only: POST /background/prepare | /background/start {"fps": N} | /background/stop, GET /background/status,
# or the command-line part at the end of this file:  python3 bg_yolo.py start --fps 5 | stop | status
# =====================================================
import os
import csv
import time
import threading
import warnings
from pathlib import Path
from datetime import datetime
warnings.filterwarnings("ignore", message=r"(?s).*on_event is deprecated")   # FastAPI 0.141 still accepts @router.on_event (hsw code); the message starts with a newline
from fastapi import APIRouter, Request, HTTPException
from pydantic import BaseModel, Field

router = APIRouter()
_backend = None
_read_gpu_metrics = None
_log_dir = None


def configure_background(backend, read_gpu_metrics, log_dir):
    global _backend, _read_gpu_metrics, _log_dir
    _backend = backend
    _read_gpu_metrics = read_gpu_metrics
    _log_dir = Path(log_dir)


def _default_bg_image():
    here = Path(__file__).resolve().parent
    for c in (os.environ.get("BG_IMAGE_PATH"),
              here / "test_frames" / "uhd_3840x2160_q90.jpg",                                   # the 4K frame va.sh test uses
              here.parent / "aerial-cuda-accelerated-ran/cuPHY-CP/dapp_hook/models/img4k/img4k_0.jpg",
              here / "bg_test_image.jpg"):
        if c and Path(c).is_file():
            return Path(c)
    return here / "bg_test_image.jpg"


BG_IMAGE_PATH = _default_bg_image()
BG_MAX_FPS = 15.0
_bg_guard = threading.RLock()
_bg_stop = threading.Event()
_bg_thread = None
_bg_running = False
_bg_jpeg = None
_bg_size = (0, 0)
_bg_error = None
_bg_target_fps = 5.0
_bg_completed = 0
_bg_failed = 0
_bg_started_utc = None
_bg_log_path = None


class BackgroundStart(BaseModel):
    fps: float = Field(default=5.0, ge=0.1, le=BG_MAX_FPS)


def _bg_local_only(request: Request):
    host = request.client.host if request.client else ""
    if host not in ("127.0.0.1", "::1"):
        raise HTTPException(status_code=403, detail="Background control is localhost-only")


def _bg_worker():
    global _bg_error, _bg_running, _bg_completed, _bg_failed, _bg_log_path
    try:
        with _bg_guard:
            jpeg = _bg_jpeg
        if jpeg is None:
            raise RuntimeError("Background image not prepared")
        filename = "bg_virtual_ue_" + datetime.now().strftime("%Y%m%d_%H%M%S_%f") + ".log"
        log_path = _log_dir / filename
        with _bg_guard:
            _bg_log_path = str(log_path)
        with log_path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["timestamp", "target_fps", "backend", "status", "reason", "model", "sm", "wait_ms", "decode_ms",
                             "infer_ms", "server_latency_ms", "ai_total_ms", "detections", "gpu_util_percent", "gpu_memory_used_gb",
                             "completed", "failed"])
            while not _bg_stop.is_set():
                cycle_start = time.monotonic()
                with _bg_guard:
                    fps = _bg_target_fps
                res = _backend.infer(jpeg, "bg")            # the same call the phone's frames take
                finished = time.monotonic()
                d = res["dapp"]
                util, memory = _read_gpu_metrics()
                with _bg_guard:
                    if d.get("status") == "success":
                        _bg_completed += 1
                    else:
                        _bg_failed += 1
                    completed, failed = _bg_completed, _bg_failed
                writer.writerow([datetime.now().astimezone().isoformat(), fps, d.get("backend"), d.get("status"), d.get("reason"),
                                 d.get("model"), d.get("sm"), d.get("wait_ms"), d.get("decode_ms"), d.get("infer_ms"),
                                 d.get("latency_ms"), round((finished - cycle_start) * 1000, 3), len(res["detections"]),
                                 util, memory, completed, failed])
                f.flush()
                _bg_stop.wait(max(0.0, 1.0 / fps - (time.monotonic() - cycle_start)))
    except Exception as exc:
        with _bg_guard:
            _bg_error = repr(exc)
        print(f"Background virtual UE stopped with error: {exc!r}", flush=True)
    finally:
        with _bg_guard:
            _bg_running = False


@router.post("/background/prepare")
async def background_prepare(request: Request):
    """Load the background JPEG (re-encoded to JPEG if it is another format); no model is loaded here."""
    global _bg_jpeg, _bg_size, _bg_error
    _bg_local_only(request)
    if not BG_IMAGE_PATH.is_file():
        raise HTTPException(status_code=400, detail=f"Missing background image: {BG_IMAGE_PATH}")
    with _bg_guard:
        if _bg_running or (_bg_thread is not None and _bg_thread.is_alive()):
            raise HTTPException(status_code=409, detail="Stop background before preparing")
    try:
        import io
        from PIL import Image
        data = BG_IMAGE_PATH.read_bytes()
        with Image.open(io.BytesIO(data)) as im:
            size = im.size
            if im.format != "JPEG":
                buf = io.BytesIO()
                im.convert("RGB").save(buf, format="JPEG", quality=90)
                data = buf.getvalue()
        with _bg_guard:
            _bg_jpeg, _bg_size, _bg_error = data, size, None
        return {"prepared": True, "image": str(BG_IMAGE_PATH), "image_width": size[0], "image_height": size[1],
                "bytes": len(data), "backend": _backend.name}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/background/start")
async def background_start(config: BackgroundStart, request: Request):
    global _bg_thread, _bg_running, _bg_error, _bg_target_fps, _bg_completed, _bg_failed, _bg_started_utc, _bg_log_path
    _bg_local_only(request)
    with _bg_guard:
        if _bg_running or (_bg_thread is not None and _bg_thread.is_alive()):
            raise HTTPException(status_code=409, detail="Background already running or stopping")
        if _bg_jpeg is None:
            raise HTTPException(status_code=409, detail="Call POST /background/prepare first")
        _bg_stop.clear()
        _bg_error = None
        _bg_target_fps = config.fps
        _bg_completed = 0
        _bg_failed = 0
        _bg_log_path = None
        _bg_started_utc = datetime.now().astimezone().isoformat()
        _bg_running = True
        _bg_thread = threading.Thread(target=_bg_worker, daemon=True, name="bg-virtual-ue")
        _bg_thread.start()
    return {"status": "starting", "target_fps": config.fps, "backend": _backend.name, "image": str(BG_IMAGE_PATH)}


@router.post("/background/stop")
async def background_stop(request: Request):
    _bg_local_only(request)
    _bg_stop.set()
    with _bg_guard:
        return {"status": "stopping" if _bg_running else "stopped", "completed": _bg_completed, "failed": _bg_failed}


@router.get("/background/status")
async def background_status(request: Request):
    _bg_local_only(request)
    with _bg_guard:
        return {"running": _bg_running, "stopping": _bg_running and _bg_stop.is_set(), "prepared": _bg_jpeg is not None,
                "backend": _backend.name if _backend else None, "image": str(BG_IMAGE_PATH),
                "image_width": _bg_size[0], "image_height": _bg_size[1], "target_fps": _bg_target_fps,
                "completed": _bg_completed, "failed": _bg_failed, "started_utc": _bg_started_utc,
                "log_path": _bg_log_path, "error": _bg_error}


@router.on_event("shutdown")
def background_shutdown():
    _bg_stop.set()
    thread = _bg_thread
    if thread is not None and thread.is_alive():
        thread.join(timeout=5)


# =====================================================
# Command-line controller (talks to the running service; does NOT run YOLO in this process)
#   python3 bg_yolo.py prepare | start [--fps 5] | stop | status      (VA_PORT, default 8000)
# =====================================================
import argparse, json, ssl, sys
from urllib.request import Request as _Req, urlopen
from urllib.error import HTTPError, URLError
URL = "https://localhost:" + os.environ.get("VA_PORT", "8000")
CTX = ssl._create_unverified_context()  # localhost self-signed certificate only


def call(method, path, payload=None):
    data = json.dumps(payload).encode() if payload is not None else (b"" if method == "POST" else None)
    req = _Req(URL + path, data=data, method=method)
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
    parser = argparse.ArgumentParser(description="Control the background virtual UE inside the VA server")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("prepare")
    start = sub.add_parser("start")
    start.add_argument("--fps", type=float, default=5)
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
