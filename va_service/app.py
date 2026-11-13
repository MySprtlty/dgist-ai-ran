"""YOLO26x VA Service - UL/DL Latency Test (hsw yolo_service), DGIST dApp edition 2026-11-05

What stays: the phone page (camera -> JPEG -> POST /detect_frame -> boxes + latency breakdown), /clock_sync, /save_latency_log (CSV),
the PPO telemetry (/telemetry/*, service_common/) and the background virtual UE (bg_yolo.py). The hsw original is kept in upstream_hsw/.
What changes: the inference. Instead of ultralytics YOLO inside this process, every frame is sent as one RUN request
(model, QoS deadline, 4K JPEG) to the DGIST dApp agent inside the cuphycontroller (L1) process, and its reply - result figures and
the detections - comes back to the phone (dapp_backend.py; protocol aerial-cuda-accelerated-ran/cuPHY-CP/dapp_hook/docs/XAPP_INTERFACE.md).

Settings come from the environment (_dgist/.env, exported by va.sh):
  VA_BACKEND=dapp|local  VA_MODEL=26x  VA_DEADLINE_MS=200  VA_TARGET_FPS=5  VA_CAMERA_WIDTH=3840  VA_CAMERA_HEIGHT=2160
  VA_JPEG_QUALITY=0.90  VA_MAX_INFLIGHT=4  VA_AGENT=127.0.0.1:9810  VA_LOG_DIR=<_dgist>/runs/va_service_log  VA_VERBOSE=0
  VA_FILE_WIDTH=1920  VA_FILE_HEIGHT=1080   (phone page, video-file source: the size every frame of a file is converted to)
  VA_MODEL_SELECT=0|1  VA_MODEL_WEIGHTS=10,10,10,10,60   (rule-based model selection, model_select.py: requests per block for
                                                          26n,26s,26m,26l,26x in a random order; off = every frame asks for VA_MODEL)
  VA_VIEWER_FIT=1   (viewer pages, 2026-11-14: the panes size themselves to the window so that all of them are on the screen at once)
Run (va.sh start):  uvicorn app:app --host 0.0.0.0 --port 8000 --ssl-keyfile certs/server.key --ssl-certfile certs/server.crt
2026-11-14: the phone page (video-file source) got "Restart Video" and a start at a chosen clock time - both happen on the phone alone, no
request is sent for them. The non-VA panes of /viewer show the non-VA service's QoS verdict (the UL latency of a frame against its
NONVA_DEADLINE_MS) and keep the picture while frames miss it, as the VA panes do.
"""
from fastapi import FastAPI, File, Form, UploadFile, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel
from PIL import Image
from pathlib import Path
from datetime import datetime

import asyncio
import csv
import io
import json
import os
import sys
import time
import threading
from collections import deque
from starlette.concurrency import run_in_threadpool
import pynvml


# =====================================================
# Configuration (environment; see the module docstring)
# =====================================================

HERE = Path(__file__).resolve().parent
DGIST = HERE.parent


def _env(name, default, cast=str):
    v = os.environ.get(name)
    if v is None or v == "":
        return default
    try:
        return cast(v)
    except ValueError:
        print(f"[VA] bad {name}={v!r}, using {default!r}", flush=True)
        return default


VA_BACKEND = _env("VA_BACKEND", "dapp")                 # dapp = DGIST agent in the L1 process | local = hsw original (ultralytics here)
VA_MODEL = _env("VA_MODEL", "26x")                      # the dApp model every frame asks for (VA_MODEL_SELECT=1: the model that replaces one the agent lacks)
VA_DEADLINE_MS = _env("VA_DEADLINE_MS", 200, int)       # QoS: RUN qos.deadline_ms, and the per-frame E2E target judged on the phone
VA_AGENT = _env("VA_AGENT", "127.0.0.1:9810")           # dApp agent (TCP; the dgist-l1 container runs with network host)
VA_VERBOSE = _env("VA_VERBOSE", 0, int)                 # 1 = one console line per frame

CAMERA_WIDTH = _env("VA_CAMERA_WIDTH", 3840, int)       # 4K UHD capture on the phone
CAMERA_HEIGHT = _env("VA_CAMERA_HEIGHT", 2160, int)
TARGET_FPS = _env("VA_TARGET_FPS", 5.0, float)          # requests per second per UE
MAX_INFLIGHT = _env("VA_MAX_INFLIGHT", 4, int)          # unanswered frames a UE tolerates before skipping a capture
JPEG_QUALITY = _env("VA_JPEG_QUALITY", 0.90, float)
# DGIST 2026-11-11: phone-page box tracker (one box per object across results) and the QoS rule for drawing
VA_TRACK = _env("VA_TRACK", 1, int)                     # 1 = track boxes across results on the phone (IoU matching, smoothing, prediction, fade-out)
VA_QOS_DROP_MISSED = _env("VA_QOS_DROP_MISSED", 1, int) # 1 = a result whose E2E missed VA_DEADLINE_MS is not drawn (its tracks coast, then fade)
VA_DISPLAY_CONF = _env("VA_DISPLAY_CONF", 0.40, float)  # boxes below this confidence are not drawn (the dApp reports down to 0.25)
VA_TRACK_TTL_MS = _env("VA_TRACK_TTL_MS", 700, int)     # a track without a fresh detection fades and is dropped after this long
VA_ENCODE_WORKERS = _env("VA_ENCODE_WORKERS", 0, int)  # phone page: N Web Workers encode JPEGs in parallel (OffscreenCanvas); 0 = main thread, one at a time
# phone page, video-file source (2026-11-12): a video stored on the phone replaces the camera; every frame is scaled to this size
# (aspect kept, black bars) before the JPEG, so the uplink carries the same FHD frames as the camera mode. Read locally: no downlink.
VA_FILE_WIDTH = _env("VA_FILE_WIDTH", 1920, int)
VA_FILE_HEIGHT = _env("VA_FILE_HEIGHT", 1080, int)
# DGIST 2026-11-11: base-station viewer (/viewer, /viewer/<ue>): the latest frame of each UE with its boxes. The picture changes only
# when the server-side verdict of the result holds: UL (measured per frame from the phone's send time + clock offset) + AI (the RUN
# round trip) + server other + a FIXED downlink term <= VA_DEADLINE_MS. A result that misses leaves the picture as it is (frozen).
VA_VIEWER = _env("VA_VIEWER", 1, int)                   # 1 = keep the latest frame per UE and serve the viewer pages; 0 = off (no state kept)
VA_VIEWER_DL_MS = _env("VA_VIEWER_DL_MS", 20.0, float)  # fixed downlink term of the verdict (FHD phone logs 11-10/11, n 202k: DL mean 21.1, p50 17.6, p90 37 ms)
VA_VIEWER_QOS_FREEZE = _env("VA_VIEWER_QOS_FREEZE", 1, int)  # 1 = a missed result does not update the picture; 0 = every successful result is shown
VA_VIEWER_BADGE = _env("VA_VIEWER_BADGE", 1, int)       # 1 = the viewer shows a "QoS miss, picture held" badge, held time and a dimmed picture while frozen; 0 = it just stops
VA_VIEWER_NONVA = _env("VA_VIEWER_NONVA", 1, int)       # 1 = /viewer also shows the two non-VA UEs (their MJPEG relay from the non-VA service, stats via /nonva_stats); /viewer/va = VA only
NONVA_PORT = _env("NONVA_PORT", 8001, int)             # the non-VA service (same host) whose /video_feed and /receiver_stats the 4-UE viewer uses
# DGIST 2026-11-14: the viewer pages fit themselves to the window: every pane is on the screen at once (no scrolling), the pictures as large as that
# allows. The page chooses the grid (4 panes: 2 x 2), the place of the figures (under or beside the picture) and the picture height. 0 = as before
VA_VIEWER_FIT = _env("VA_VIEWER_FIT", 1, int)
# viewer recording (2026-11-13): "전체 녹화" / "녹화 종료" in the viewer's header and "녹화 시작" / "녹화 종료" on every pane. The viewer's browser
# records each pane's picture area as shown (frame + boxes + QoS badge, fitted into 1920x1080) and saves one H.264 MP4 per pane. The server
# and the UEs take no part: no extra uplink or downlink traffic, no server CPU.
VA_VIEWER_REC = _env("VA_VIEWER_REC", 1, int)           # 1 = record buttons on the viewer pages (the non-VA panes then read the relay with fetch() into a canvas); 0 = the page as before
VA_VIEWER_REC_FPS = min(60, max(1, _env("VA_VIEWER_REC_FPS", 30, int)))   # frame rate of the files, constant (the UEs send 15 fps; 30 keeps every source frame)
VA_VIEWER_REC_MBPS = min(50.0, max(0.5, _env("VA_VIEWER_REC_MBPS", 6.0, float)))   # H.264 target bit rate per pane; 6 Mbit/s = at most about 45 MB per minute and pane
VA_VIEWER_REC_MAX_MIN = max(0.0, _env("VA_VIEWER_REC_MAX_MIN", 20.0, float))   # a recording stops and saves itself after this many minutes (the browser holds it until then); 0 = no limit
VA_VIEWER_REC_BADGE = _env("VA_VIEWER_REC_BADGE", 1, int)   # 1 = the files show the QoS badge, the dimmed frozen picture and the waiting text as the screen does; 0 = picture and boxes only
VA_VIEWER_REC_PREFIX = "".join(c for c in _env("VA_VIEWER_REC_PREFIX", os.environ.get("DEMO_MODE") or "") if c.isascii() and (c.isalnum() or c in "-_"))[:32]   # file names <prefix>_va_ue1_<date>_<time>.mp4; default = .env DEMO_MODE (ours | baseline)

MODEL_PATH = _env("VA_LOCAL_MODEL", str(HERE / "yolo26x.pt"))   # local backend only
DEVICE = 0

CLOCK_SYNC_SAMPLES = 30
CLOCK_SYNC_BEST_SAMPLES = 5


# =====================================================
# Inference backend: dApp agent RUN (default) or the local YOLO
# =====================================================

if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
from dapp_backend import make_backend   # noqa: E402
import model_select                     # noqa: E402

# rule-based model selection (.env VA_MODEL_SELECT / VA_MODEL_WEIGHTS, dApp backend only): None = every frame asks for VA_MODEL
selector = model_select.from_env(fallback=VA_MODEL, log=lambda m: print(f"[VA] {m}", flush=True)) if VA_BACKEND == "dapp" else None
VA_MODEL_PAGE = "mix" if selector else VA_MODEL                                  # phone page text (as long as a model name: the page does not grow)
VA_MODEL_VIEW = f"{selector.label()} 규칙 선택" if selector else VA_MODEL       # viewer header

print(f"VA service: backend {VA_BACKEND}, model {('rule ' + selector.label() + ' per ' + str(selector.block) + ' requests, else ' + VA_MODEL) if selector else VA_MODEL}, "
      f"QoS deadline {VA_DEADLINE_MS} ms, {TARGET_FPS:g} req/s per UE, "
      f"camera {CAMERA_WIDTH}x{CAMERA_HEIGHT} JPEG q{JPEG_QUALITY:g}, agent {VA_AGENT}", flush=True)
backend = make_backend(VA_BACKEND, agent=VA_AGENT, model=VA_MODEL, deadline_ms=VA_DEADLINE_MS, local_model_path=MODEL_PATH, device=DEVICE,
                       selector=selector)

# GPU-wide NVML metrics (not per-UE). Initialized once at startup.
pynvml.nvmlInit()
gpu_handle = pynvml.nvmlDeviceGetHandleByIndex(DEVICE)

def read_gpu_metrics():
    """Read recent GPU activity and current VRAM usage before inference."""
    try:
        util = pynvml.nvmlDeviceGetUtilizationRates(gpu_handle).gpu
        used_bytes = pynvml.nvmlDeviceGetMemoryInfo(gpu_handle).used
        return float(util), round(used_bytes / (1024 ** 3), 3)
    except pynvml.NVMLError as exc:
        print(f"NVML metric read failed: {exc}", flush=True)
        return None, None


# =====================================================
# Log Directory (per-session CSVs of the phone / ue_sim.py)
# =====================================================

LOG_DIR = Path(_env("VA_LOG_DIR", str(DGIST / "runs" / "va_service_log")))
LOG_DIR.mkdir(parents=True, exist_ok=True)


# =====================================================
# FastAPI
# =====================================================

app = FastAPI(
    title="YOLO26x VA Service (DGIST dApp) - UL/DL Latency Test"
)
from service_common.service_telemetry import attach_telemetry   # noqa: E402  (./service_common, hsw module unchanged)
telemetry = attach_telemetry(app, 'va')

# ---- DGIST measurement dashboard feed (dash/dash_server.py, 2026-11-12): one event per served frame ("f") and one per phone E2E report ("e")
# in a ring the dashboard polls from this host (GET /metrics/frames?since=N, loopback only: nothing of it travels over the cell).
DASH_RING = deque(maxlen=_env("VA_DASH_RING", 8000, int))
_dash_lock = threading.Lock()
_dash_seq = 0


def dash_event(ev):
    global _dash_seq
    with _dash_lock:
        _dash_seq += 1
        ev["s"] = _dash_seq
        DASH_RING.append(ev)


_telemetry_result = telemetry.result


def _telemetry_result_dash(ue, token, frame_id, latency_ms=None, payload_bytes=0, sample_ms=None, success=True):
    """the /telemetry/result route calls store.result: the E2E latency the phone measured for every frame passes here (hsw module unchanged)"""
    out = _telemetry_result(ue, token, frame_id, latency_ms, payload_bytes, sample_ms, success)
    try:
        dash_event({"k": "e", "t": time.time_ns() / 1e6, "ue": ue, "tok": str(token or "")[:8], "fid": frame_id, "ok": bool(success),
                    "e2e": float(latency_ms) if success and latency_ms is not None else None})
    except Exception:
        pass
    return out


telemetry.result = _telemetry_result_dash


# =====================================================
# CSV Data Models
# =====================================================

class LatencyRecord(BaseModel):

    frame_id: int
    timestamp: str
    yolo_enabled: bool = True

    image_width: int
    image_height: int

    frame_size_kb: float
    actual_fps: float
    ul_payload_mbps: float
    dl_payload_mbps: float
    dl_response_kb: float

    e2e_latency_ms: float

    ul_latency_ms: float
    ai_latency_ms: float
    server_other_ms: float
    dl_latency_ms: float

    network_latency_ms: float
    breakdown_residual_ms: float

    clock_offset_ms: float
    sync_rtt_ms: float
    gpu_util_percent: float | None = None
    gpu_memory_used_gb: float | None = None
    # DGIST additions (dApp backend): QoS verdict and the agent's per-request figures
    qos_deadline_ms: float | None = None
    qos_met: bool | None = None
    dapp_status: str | None = None
    dapp_reason: str | None = None
    dapp_model: str | None = None
    dapp_sm: int | None = None
    dapp_wait_ms: float | None = None
    dapp_decode_ms: float | None = None
    dapp_infer_ms: float | None = None
    dapp_latency_ms: float | None = None
    detections: int | None = None


class LatencySession(BaseModel):
    ue_id: str
    session_token: str
    records: list[LatencyRecord]


# =====================================================
# Web Page
# =====================================================

def _page(s):
    """fill the configuration placeholders of the page (camera size, rate, in-flight cap, JPEG quality, QoS, backend, model)"""
    for k, v in (("__CAMERA_WIDTH__", CAMERA_WIDTH), ("__CAMERA_HEIGHT__", CAMERA_HEIGHT), ("__TARGET_FPS__", f"{TARGET_FPS:g}"),
                 ("__MAX_INFLIGHT__", MAX_INFLIGHT), ("__JPEG_QUALITY__", f"{JPEG_QUALITY:g}"), ("__QOS_DEADLINE_MS__", VA_DEADLINE_MS),
                 ("__VA_BACKEND__", VA_BACKEND), ("__VA_MODEL__", VA_MODEL_PAGE), ("__VA_MODEL_VIEW__", VA_MODEL_VIEW),
                 ("__VA_TRACK__", VA_TRACK), ("__VA_QOS_DROP_MISSED__", VA_QOS_DROP_MISSED), ("__VA_DISPLAY_CONF__", f"{VA_DISPLAY_CONF:g}"),
                 ("__VA_TRACK_TTL_MS__", VA_TRACK_TTL_MS), ("__VA_ENCODE_WORKERS__", VA_ENCODE_WORKERS),
                 ("__VA_FILE_WIDTH__", VA_FILE_WIDTH), ("__VA_FILE_HEIGHT__", VA_FILE_HEIGHT),
                 ("__VA_VIEWER_DL_MS__", f"{VA_VIEWER_DL_MS:g}"), ("__VA_VIEWER_QOS_FREEZE__", VA_VIEWER_QOS_FREEZE), ("__VA_VIEWER_BADGE__", VA_VIEWER_BADGE),
                 ("__VA_VIEWER_REC__", VA_VIEWER_REC), ("__VA_VIEWER_REC_FPS__", VA_VIEWER_REC_FPS), ("__VA_VIEWER_REC_MBPS__", f"{VA_VIEWER_REC_MBPS:g}"),
                 ("__VA_VIEWER_REC_MAX_MIN__", f"{VA_VIEWER_REC_MAX_MIN:g}"), ("__VA_VIEWER_REC_BADGE__", VA_VIEWER_REC_BADGE), ("__VA_VIEWER_REC_PREFIX__", VA_VIEWER_REC_PREFIX),
                 ("__VA_VIEWER_NONVA__", VA_VIEWER_NONVA), ("__NONVA_PORT__", NONVA_PORT), ("__VA_VIEWER_FIT__", VA_VIEWER_FIT)):
        s = s.replace(k, str(v))
    return s


@app.get("/", response_class=HTMLResponse)
async def home():

    return _page("""
<!DOCTYPE html>

<html>

<head>

<meta charset="UTF-8">

<meta name="viewport"
      content="width=device-width, initial-scale=1.0">

<title>YOLO26x VA (DGIST dApp) - UL/DL Latency Test</title>


<style>

body {
    font-family: Arial, sans-serif;
}

h1 {
    font-size: 20px;
    margin: 6px 0;
}

#vaConfig, #vaTarget {
    font-size: 12px;
    margin: 2px 0;
    color: #444;
}

/* DGIST: the controls sit above the video, so the video can take the full width (hsw sizing; a 4K portrait camera is tall - scroll) */
#videoContainer {
    position: relative;
    display: inline-block;
    max-width: 100%;
}

#camera {
    display: block;
    max-width: 100%;
    max-height: 75vh;
    width: auto;
    height: auto;
    background: black;
}

#overlay {
    position: absolute;
    top: 0;
    left: 0;

    width: 100%;
    height: 100%;

    pointer-events: none;
}

/* DGIST 2026-11-12: video-file source + full screen. #stage is the element that goes full screen. With a file, and in full screen,
   the picture box takes the shape of the frame that is sent (--stage-ar) and the video is letterboxed in it exactly as in that frame,
   so the overlay (drawn in the coordinates of the sent frame) lines up. The camera layout outside full screen is unchanged. */
#stage {
    position: relative;
}

#videoContainer.filemode {
    display: block;
    aspect-ratio: var(--stage-ar, 1.77778);
    width: min(100%, calc(75vh * var(--stage-ar, 1.77778)));
}

#stage:fullscreen {
    width: 100vw;
    height: 100vh;
    background: black;
    display: flex;
    align-items: center;
    justify-content: center;
}

#stage:fullscreen #videoContainer {
    display: block;
    max-width: none;
    aspect-ratio: var(--stage-ar, 1.77778);
    width: min(100vw, calc(100vh * var(--stage-ar, 1.77778)));
}

#videoContainer.filemode #camera,
#stage:fullscreen #camera {
    position: absolute;
    top: 0;
    left: 0;
    width: 100%;
    height: 100%;
    max-width: none;
    max-height: none;
    object-fit: contain;
}

#fsExit {
    display: none;
    position: absolute;
    top: 8px;
    right: 8px;
    z-index: 5;
    margin: 0;
    padding: 6px 12px;
    font-size: 16px;
    color: white;
    background: rgba(0, 0, 0, 0.45);
    border: 1px solid rgba(255, 255, 255, 0.5);
    border-radius: 6px;
}

#stage:fullscreen #fsExit {
    display: block;
}

/* DGIST 2026-11-14: video-file source: restart button and schedule note laid over the picture in full screen */
#fsRestart {
    display: none;
    position: absolute;
    top: 8px;
    left: 8px;
    z-index: 5;
    margin: 0;
    padding: 6px 12px;
    font-size: 16px;
    color: white;
    background: rgba(0, 0, 0, 0.45);
    border: 1px solid rgba(255, 255, 255, 0.5);
    border-radius: 6px;
}

#stage.filesrc:fullscreen #fsRestart {
    display: block;
}

#fsSched {
    display: none;
    position: absolute;
    left: 8px;
    bottom: 8px;
    z-index: 5;
    padding: 4px 10px;
    font-size: 14px;
    color: white;
    background: rgba(0, 0, 0, 0.45);
    border-radius: 6px;
    pointer-events: none;
}

#stage:fullscreen #fsSched.show {
    display: block;
}

#schedTime {
    font-size: 18px;
    padding: 6px;
}
button {
    font-size: 18px;
    margin: 5px;
    padding: 10px 15px;
}

.info-box {
    margin-top: 15px;
    padding: 10px;

    border: 1px solid #999;

    max-width: 580px;
}

</style>

</head>


<body>


<h1>Real-Time Video Analytics Service (DGIST dApp)</h1>



<!-- =================================================
     Camera + Bounding Box
     ================================================= -->

<div id="stage">

<div id="videoContainer">

    <video id="camera"
           autoplay
           playsinline
           muted>
    </video>

    <canvas id="overlay">
    </canvas>

</div>

<button id="fsExit" onclick="exitFullscreen()">
    Exit Full Screen
</button>
<!-- DGIST 2026-11-14: shown over the picture in full screen: restart of a video file, note of a scheduled start -->
<button id="fsRestart" onclick="restartVideo()">
    Restart Video
</button>

<div id="fsSched"></div>

</div>


<canvas id="captureCanvas"
        style="display:none;">
</canvas>

<!-- =================================================
     Control
     ================================================= -->

<button onclick="startCamera()">
    Start Camera
</button>

<button onclick="stopCamera()">
    Stop Camera / Video
</button>

<!-- DGIST 2026-11-12: a video file stored on this phone as the source instead of the camera (read locally, nothing is downloaded) -->
<button onclick="pickVideoFile()">
    Video File
</button>

<input type="file" id="videoFile" accept="video/*" style="display:none;" onchange="onVideoFilePicked(this)">

<button onclick="enterFullscreen()">
    Full Screen
</button>
<!-- DGIST 2026-11-14: video-file source: play the file again from its first frame, now or at a chosen clock time (on the phone only:
     no request is sent, nothing is downloaded) -->
<button onclick="restartVideo()">
    Restart Video
</button>

<p>
Start video at:
<input type="time" id="schedTime">
<button onclick="scheduleVideoStart()">Set</button>
<button onclick="cancelVideoStart()">Cancel</button>
<br>
Scheduled start: <strong id="schedStatus">Not set</strong>
<br>
Last video start: <strong id="schedLast">-</strong>
<br>
Clock: <strong id="schedClock">-</strong>
</p>

<br><br>

<button onclick="startVA()">
    Start VA (streaming)
</button>

<label>VA phone:
<select id="ueSelector"><option value="ue1">UE1</option><option value="ue2">UE2</option></select>
</label>

<button onclick="stopVA()">
    Stop VA
</button>

<br><br>

<!-- YOLO inference can be switched independently of video upload. -->
<button onclick="setYOLOEnabled(true)">
    YOLO ON
</button>

<button onclick="setYOLOEnabled(false)">
    YOLO OFF
</button>

<p>
YOLO Inference:
<strong id="yoloStatus">ON (inference enabled)</strong>
</p>

<p>
Latest Frame Mode:
<strong id="frameYoloStatus">-</strong>
</p>

<p id="vaConfig">backend __VA_BACKEND__ | model __VA_MODEL__ | QoS __QOS_DEADLINE_MS__ ms | __TARGET_FPS__ req/s per UE | __CAMERA_WIDTH__ x __CAMERA_HEIGHT__ JPEG q__JPEG_QUALITY__</p>
<p id="vaTarget">
<strong>Target:</strong> __CAMERA_WIDTH__x__CAMERA_HEIGHT__ / __TARGET_FPS__ FPS per UE / JPEG Quality __JPEG_QUALITY__ / QoS __QOS_DEADLINE_MS__ ms / model __VA_MODEL__ via dApp
</p>



<hr>


<!-- =================================================
     Service Status
     ================================================= -->

<div class="info-box">

<h2>Service Status</h2>

<p>
Status:
<strong id="status">Stopped</strong>
</p>

<p>
Frame Count:
<strong id="frameCount">0</strong>
</p>

<p>
Log Status:
<strong id="logStatus">No log</strong>
</p>

</div>


<!-- =================================================
     Clock Synchronization
     ================================================= -->

<div class="info-box">

<h2>Clock Synchronization</h2>

<p>
Clock Sync:
<strong id="clockSyncStatus">Not synchronized</strong>
</p>

<p>
Estimated Clock Offset:
<strong id="clockOffset">-</strong>
</p>

<p>
Best Sync RTT:
<strong id="syncRTT">-</strong>
</p>

</div>


<!-- =================================================
     Traffic
     ================================================= -->

<div class="info-box">

<h2>VA Service Traffic</h2>

<p>
Camera / Video Resolution:
<strong id="resolution">-</strong>
</p>

<p>
UL JPEG Frame Size:
<strong id="frameSize">-</strong>
</p>

<p>
DL Response Size:
<strong id="dlResponseSize">-</strong>
</p>

<p>
Actual FPS:
<strong id="actualFPS">-</strong>
</p>

<p>
UL Payload Rate:
<strong id="ulRate">-</strong>
</p>

<p>
DL Payload Rate:
<strong id="dlRate">-</strong>
</p>

</div>


<!-- =================================================
     Latency Breakdown
     ================================================= -->

<div class="info-box">

<h2>VA Service Latency</h2>

<p>
QoS (E2E within __QOS_DEADLINE_MS__ ms):
<strong id="qosStatus">-</strong>
</p>

<p>
dApp reply (backend model status | SM | wait dec inf srv):
<strong id="dappInfo">-</strong>
</p>

<p>
VA Service E2E Latency:
<strong id="e2eLatency">-</strong>
</p>

<p>
UL Latency:
<strong id="ulLatency">-</strong>
</p>

<p>
AI Processing Latency:
<strong id="aiLatency">-</strong>
</p>

<p>
Server Other Time:
<strong id="serverOther">-</strong>
</p>

<p>
DL Latency:
<strong id="dlLatency">-</strong>
</p>

<p>
Network Latency (E2E - AI):
<strong id="networkLatency">-</strong>
</p>

</div>


<div class="info-box">

<h2>Latest Detection</h2>

<ul id="detections">
    <li>No result</li>
</ul>

</div>


<script src="/telemetry_client.js"></script>
<script>


// =====================================================
// Configuration
// =====================================================

// filled by the server from _dgist/.env (VA_*): 4K capture, 5 req/s per UE, QoS 200 ms, model 26x by default
const CAMERA_WIDTH = __CAMERA_WIDTH__;
const CAMERA_HEIGHT = __CAMERA_HEIGHT__;

const TARGET_FPS = __TARGET_FPS__;
const targetInterval = 1000 / TARGET_FPS;
const MAX_INFLIGHT = __MAX_INFLIGHT__;

const JPEG_QUALITY = __JPEG_QUALITY__;
// DGIST 2026-11-11: parallel JPEG encoding (.env VA_ENCODE_WORKERS). The main thread only grabs the frame (createImageBitmap, a few ms);
// an idle worker draws it into an OffscreenCanvas and encodes it (convertToBlob). With N workers up to N frames encode at once, so a phone
// whose encode takes 60-90 ms can still send 15-20 fps; the encode latency of one frame is unchanged. 0 = the original main-thread path.
const ENCODE_WORKERS = __VA_ENCODE_WORKERS__;
const encPool = { workers: [], busy: 0, ok: false, seq: 0, pending: new Map(), errors: 0, lastError: "", note: "off" };
function encodeInit() {
    if (!(ENCODE_WORKERS > 0)) return false;
    if (typeof OffscreenCanvas === "undefined" || typeof createImageBitmap !== "function" || typeof Worker === "undefined"
        || !OffscreenCanvas.prototype.convertToBlob) { encPool.note = "unsupported browser"; return false; }
    // tw x th > 0 (video-file source): the frame is scaled into a tw x th canvas, aspect kept, black bars; 0 = the native frame (camera)
    const code = 'self.onmessage = async function (e) { var id = e.data.id, bmp = e.data.bmp, q = e.data.q, tw = e.data.tw || 0, th = e.data.th || 0;'
        + ' try { var c, g; if (tw > 0 && th > 0) { c = new OffscreenCanvas(tw, th); g = c.getContext("2d");'
        + ' var s = Math.min(tw / bmp.width, th / bmp.height), w = Math.round(bmp.width * s), h = Math.round(bmp.height * s);'
        + ' g.fillStyle = "black"; g.fillRect(0, 0, tw, th); g.imageSmoothingQuality = "high";'
        + ' g.drawImage(bmp, Math.floor((tw - w) / 2), Math.floor((th - h) / 2), w, h); }'
        + ' else { c = new OffscreenCanvas(bmp.width, bmp.height); g = c.getContext("2d"); g.drawImage(bmp, 0, 0); } bmp.close();'
        + ' var blob = await c.convertToBlob({ type: "image/jpeg", quality: q }); self.postMessage({ id: id, blob: blob }); }'
        + ' catch (err) { self.postMessage({ id: id, error: String(err) }); } };';
    let url;
    try { url = URL.createObjectURL(new Blob([code], { type: "text/javascript" })); } catch (e) { encPool.note = "no blob url: " + e; return false; }
    for (let i = 0; i < ENCODE_WORKERS; i++) {
        let w;
        try { w = new Worker(url); } catch (e) { encPool.note = "worker failed: " + e; return false; }
        w.idle = true;
        w.onmessage = (e) => {
            const req = encPool.pending.get(e.data.id); if (!req) return;
            encPool.pending.delete(e.data.id); req.release();
            if (e.data.error) req.reject(new Error(e.data.error)); else req.resolve(e.data.blob);
        };
        w.onerror = (e) => { encPool.errors++; encPool.lastError = "worker: " + (e.message || e); };
        encPool.workers.push(w);
    }
    encPool.ok = true; encPool.note = ENCODE_WORKERS + " workers"; return true;
}
function encodeFrame(video, quality, tw, th) {   // Promise<Blob|null>; null = no idle worker or an encode error (the tick is skipped)
    const w = encPool.workers.find(x => x.idle); if (!w) return Promise.resolve(null);
    w.idle = false; encPool.busy++;
    const id = ++encPool.seq; const req = { w: w, released: false, resolve: null, reject: null };
    req.release = () => { if (!req.released) { req.released = true; w.idle = true; encPool.busy--; } };
    return createImageBitmap(video).then(bmp => new Promise((resolve, reject) => {
        req.resolve = resolve; req.reject = reject; encPool.pending.set(id, req);
        w.postMessage({ id: id, bmp: bmp, q: quality, tw: tw || 0, th: th || 0 }, [bmp]);
    })).catch(err => { encPool.pending.delete(id); req.release(); encPool.errors++; encPool.lastError = String(err); return null; });
}
encodeInit();

const QOS_DEADLINE_MS = __QOS_DEADLINE_MS__;
const VA_BACKEND = "__VA_BACKEND__";
const VA_MODEL = "__VA_MODEL__";
// DGIST 2026-11-11: box tracker + QoS drawing rule (.env VA_TRACK, VA_QOS_DROP_MISSED, VA_DISPLAY_CONF, VA_TRACK_TTL_MS)
const TRACK = { on: __VA_TRACK__ === 1, dropMissed: __VA_QOS_DROP_MISSED__ === 1, conf: __VA_DISPLAY_CONF__, ttlMs: __VA_TRACK_TTL_MS__,
                confirmHits: 2, iou: 0.3, posAlpha: 0.7, velAlpha: 0.5, holdMs: 250, maxPredictMs: 600, deadband: 0.03, predict: true };
// =====================================================
// DGIST box tracker. A result arrives about one E2E late and only TARGET_FPS times a second, and a detector flickers from
// frame to frame. The tracker keeps one box per object across results (same-class IoU matching), smooths its position and
// size, predicts where the object is now from its velocity (dead reckoning over the latency) and lets a lost track fade
// out over TRACK.ttlMs instead of vanishing. With TRACK.dropMissed a result whose E2E missed the QoS deadline is not
// drawn: its tracks coast and fade, so a late system shows no boxes.
// =====================================================
function boxIoU(a, b) {
    const ix = Math.max(0, Math.min(a.x2, b.x2) - Math.max(a.x1, b.x1));
    const iy = Math.max(0, Math.min(a.y2, b.y2) - Math.max(a.y1, b.y1));
    const inter = ix * iy;
    const uni = (a.x2 - a.x1) * (a.y2 - a.y1) + (b.x2 - b.x1) * (b.y2 - b.y1) - inter;
    return uni > 0 ? inter / uni : 0;
}
class Track {
    constructor(id, d, t) {
        this.id = id; this.cls = d.class; this.conf = Number(d.confidence);
        this.mx = (d.x1 + d.x2) / 2; this.my = (d.y1 + d.y2) / 2;      // last measured centre
        this.cx = this.mx; this.cy = this.my; this.w = d.x2 - d.x1; this.h = d.y2 - d.y1;   // smoothed box
        this.vx = 0; this.vy = 0; this.t = t;                             // t = capture time (performance.now() ms) of the last observation
        this.hits = 1; this.misses = 0; this.seenAt = performance.now();
    }
    boxAt(now) {                                                          // predicted box at time now (same clock as t)
        const dt = TRACK.predict ? Math.min(Math.max(0, now - this.t), TRACK.maxPredictMs) : 0;
        const cx = this.cx + this.vx * dt, cy = this.cy + this.vy * dt;
        return { x1: cx - this.w / 2, y1: cy - this.h / 2, x2: cx + this.w / 2, y2: cy + this.h / 2 };
    }
    observe(d, t) {
        const mcx = (d.x1 + d.x2) / 2, mcy = (d.y1 + d.y2) / 2, dt = t - this.t;
        if (dt > 0) {
            let vx = (mcx - this.mx) / dt, vy = (mcy - this.my) / dt;    // px per ms between the two captures
            if (Math.hypot(vx, vy) < TRACK.deadband) { vx = 0; vy = 0; }
            this.vx += TRACK.velAlpha * (vx - this.vx); this.vy += TRACK.velAlpha * (vy - this.vy);
        }
        this.mx = mcx; this.my = mcy;
        this.cx += TRACK.posAlpha * (mcx - this.cx); this.cy += TRACK.posAlpha * (mcy - this.cy);
        this.w += TRACK.posAlpha * ((d.x2 - d.x1) - this.w); this.h += TRACK.posAlpha * ((d.y2 - d.y1) - this.h);
        this.conf = Number(d.confidence); this.t = t; this.hits++; this.misses = 0; this.seenAt = performance.now();
    }
}
const tracker = {
    tracks: [], nextId: 1, imgW: 0, imgH: 0, raf: null, dropped: 0,
    reset() {
        this.tracks = []; this.dropped = 0;
        if (this.raf !== null) { cancelAnimationFrame(this.raf); this.raf = null; }
        clearBoundingBoxes();
    },
    // one result: the detections of the frame captured at captureTs (performance.now() clock), judged qosMet on the phone.
    // Returns true when the result was drawn / fed to the tracks.
    update(dets, w, h, captureTs, qosMet) {
        this.imgW = w; this.imgH = h;
        if (!TRACK.on) {                                                  // original behaviour, plus the QoS rule
            if (!qosMet && TRACK.dropMissed) { this.dropped++; clearBoundingBoxes(); return false; }
            drawBoundingBoxes(dets, w, h); return true;
        }
        if (!qosMet && TRACK.dropMissed) { this.dropped++; this.run(); return false; }
        const cands = dets.filter(d => Number(d.confidence) >= TRACK.conf);
        const pairs = [];
        this.tracks.forEach((tr, ti) => {
            const p = tr.boxAt(captureTs);
            cands.forEach((c, ci) => { if (c.class !== tr.cls) return; const s = boxIoU(p, c); if (s >= TRACK.iou) pairs.push([s, ti, ci]); });
        });
        pairs.sort((a, b) => b[0] - a[0]);
        const usedT = new Set(), usedC = new Set();
        for (const [, ti, ci] of pairs) {
            if (usedT.has(ti) || usedC.has(ci)) continue;
            usedT.add(ti); usedC.add(ci); this.tracks[ti].observe(cands[ci], captureTs);
        }
        this.tracks.forEach((tr, ti) => { if (!usedT.has(ti)) tr.misses++; });
        cands.forEach((c, ci) => { if (!usedC.has(ci)) this.tracks.push(new Track(this.nextId++, c, captureTs)); });
        // a track seen once that already missed the next result was a flicker: drop it at once
        this.tracks = this.tracks.filter(tr => !(tr.hits < TRACK.confirmHits && tr.misses > 0));
        this.run(); return true;
    },
    run() { if (this.raf === null) this.raf = requestAnimationFrame(() => this.render()); },
    render() {
        this.raf = null;
        const now = performance.now(), list = [];
        this.tracks = this.tracks.filter(tr => now - tr.seenAt <= TRACK.ttlMs);
        for (const tr of this.tracks) {
            if (tr.hits < TRACK.confirmHits) continue;                    // a new object is shown from its 2nd detection on
            const age = now - tr.seenAt;                                  // since its last fresh detection arrived
            const alpha = age <= TRACK.holdMs ? 1 : Math.max(0.15, 1 - (age - TRACK.holdMs) / (TRACK.ttlMs - TRACK.holdMs));
            const b = tr.boxAt(now);
            list.push({ class: tr.cls, confidence: tr.conf, alpha: alpha,
                        x1: Math.max(0, b.x1), y1: Math.max(0, b.y1), x2: Math.min(this.imgW, b.x2), y2: Math.min(this.imgH, b.y2) });
        }
        if (list.length) drawBoundingBoxes(list, this.imgW, this.imgH); else clearBoundingBoxes();
        if (this.tracks.length) this.run();
    }
};

// DGIST diagnostics: the phone reports JS errors, camera settings and button presses to the server log (POST /client_log)
function clientLog(kind, detail) {
    try {
        fetch("/client_log", {method: "POST", headers: {"Content-Type": "application/json"}, keepalive: true,
               body: JSON.stringify({kind: kind, detail: String(detail).slice(0, 500), secure: window.isSecureContext,
                                     ua: navigator.userAgent.slice(0, 120), t: Date.now()})}).catch(() => {});
    } catch (e) {}
}
window.addEventListener("error", e => clientLog("js_error", e.message + " @" + e.filename + ":" + e.lineno));
window.addEventListener("unhandledrejection", e => clientLog("promise_rejection", e.reason && (e.reason.message || e.reason)));
clientLog("page", "loaded; mediaDevices=" + !!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia)
          + " TelemetryClient=" + (typeof TelemetryClient) + " camera " + CAMERA_WIDTH + "x" + CAMERA_HEIGHT + " " + TARGET_FPS + " fps");

const CLOCK_SYNC_SAMPLES = 30;
const CLOCK_SYNC_BEST_SAMPLES = 5;


// =====================================================
// Global Variables
// =====================================================

let stream = null;
let running = false;
let telemetryClient = null;
let startingVA = false;
let sendTimer = null;
let inFlight = 0;
let captureBusy = false;
let frameSequence = 0;
let latestDisplayedFrameId = 0;
let droppedFrames = 0;
let qosMetCount = 0;
let qosMissCount = 0;
// DGIST rate diagnostics: why the achieved fps differs from TARGET_FPS (reported to the server every 5 s as [CLIENT ...] rate:)
let skipBusy = 0, skipInflight = 0, encMsSum = 0, encMsMax = 0, encN = 0, sentN = 0, lastRateReport = 0;
function rateReport() {
    const now = performance.now();
    if (lastRateReport === 0) { lastRateReport = now; return; }
    if (now - lastRateReport < 5000) return;
    const sec = (now - lastRateReport) / 1000.0;
    clientLog("rate", "target " + TARGET_FPS + " fps | sent " + (sentN / sec).toFixed(2) + " fps | JPEG encode mean " + (encN ? (encMsSum / encN).toFixed(0) : "-") + " ms max " + encMsMax.toFixed(0)
        + " ms | ticks skipped: capture busy " + skipBusy + ", inflight>=" + MAX_INFLIGHT + " " + skipInflight + " | inflight now " + inFlight + " | encode " + encPool.note + (encPool.errors ? " errors " + encPool.errors : ""));
    skipBusy = 0; skipInflight = 0; encMsSum = 0; encMsMax = 0; encN = 0; sentN = 0; lastRateReport = now;
}
let runToken = 0;

// Per-frame YOLO switch: upload always stays enabled while VA is running.
// Default ON preserves the original VA behavior.
let yoloEnabled = true;

function setYOLOEnabled(enabled) {
    yoloEnabled = Boolean(enabled);
    document.getElementById("yoloStatus").innerText =
        yoloEnabled ? "ON (inference enabled)" : "OFF (upload only)";

    // Do not display stale detection boxes when YOLO is switched off.
    if (!yoloEnabled) {
        tracker.reset();
        document.getElementById("detections").innerHTML =
            "<li>YOLO OFF - frames are still uploaded</li>";
    }
}

let frameCount = 0;

let latencyRecords = [];
let sessionSaved = true;

let sessionStartTime = 0;
let totalPayloadBytes = 0;
let totalDlPayloadBytes = 0;


// Clock offset definition:
//
// clockOffsetMs = GH200 clock - iPhone clock
//
// Therefore:
//
// GH200 timestamp converted to iPhone clock
// = serverTimestamp - clockOffsetMs

let clockOffsetMs = 0;

let clockSynced = false;

let syncBestRttMs = 0;


// =====================================================
// Bounding Box Colors
// =====================================================

const boxColors = [

    "#FF3B30",
    "#007AFF",
    "#FF9500",
    "#AF52DE",
    "#34C759",
    "#00C7BE",
    "#FF2D55",
    "#5856D6",
    "#FFD60A",
    "#64D2FF",
    "#BF5AF2",
    "#30D158"
];


function getClassColor(className) {

    let hash = 0;

    for (
        let i = 0;
        i < className.length;
        i++
    ) {

        hash =
            className.charCodeAt(i)
            + ((hash << 5) - hash);
    }


    return boxColors[
        Math.abs(hash)
        % boxColors.length
    ];
}


// =====================================================
// Median
// =====================================================

function median(values) {

    const sorted =
        [...values].sort(
            (a, b) => a - b
        );


    const middle =
        Math.floor(
            sorted.length / 2
        );


    if (
        sorted.length % 2 === 0
    ) {

        return (
            sorted[middle - 1]
            + sorted[middle]
        ) / 2.0;
    }


    return sorted[middle];
}


// =====================================================
// Clock Synchronization
//
// NTP-style four timestamps:
//
// t0 = iPhone request send
// t1 = GH200 request receive
// t2 = GH200 response preparation
// t3 = iPhone response receive
//
// offset = server clock - client clock
// =====================================================

async function synchronizeClocks() {

    document.getElementById(
        "clockSyncStatus"
    ).innerText = "Synchronizing...";


    const samples = [];


    for (
        let i = 0;
        i < CLOCK_SYNC_SAMPLES;
        i++
    ) {

        try {

            const t0 =
                Date.now();


            const response =
                await fetch(
                    "/clock_sync?x="
                    + Math.random(),
                    {
                        cache: "no-store"
                    }
                );


            const data =
                await response.json();


            const t3 =
                Date.now();


            const t1 =
                data.server_receive_ms;


            const t2 =
                data.server_send_ms;


            // Network RTT estimate excluding
            // server processing time.

            const rtt =
                (t3 - t0)
                - (t2 - t1);


            // NTP-style clock offset:
            // server clock - client clock

            const offset =
                (
                    (t1 - t0)
                    + (t2 - t3)
                ) / 2.0;


            samples.push({
                rtt: rtt,
                offset: offset
            });


        } catch (error) {

            console.error(
                "Clock sync sample failed:",
                error
            );
        }


        await new Promise(
            resolve =>
                setTimeout(
                    resolve,
                    20
                )
        );
    }


    if (
        samples.length
        < CLOCK_SYNC_BEST_SAMPLES
    ) {

        clockSynced = false;


        document.getElementById(
            "clockSyncStatus"
        ).innerText =
            "Sync Failed";


        throw new Error(
            "Clock synchronization failed."
        );
    }


    // Lowest RTT samples are used to reduce
    // queueing-delay influence.

    samples.sort(
        (a, b) =>
            a.rtt - b.rtt
    );


    const bestSamples =
        samples.slice(
            0,
            CLOCK_SYNC_BEST_SAMPLES
        );


    clockOffsetMs =
        median(
            bestSamples.map(
                sample =>
                    sample.offset
            )
        );


    syncBestRttMs =
        bestSamples[0].rtt;


    clockSynced = true;


    document.getElementById(
        "clockSyncStatus"
    ).innerText =
        "Synchronized";


    document.getElementById(
        "clockOffset"
    ).innerText =
        clockOffsetMs.toFixed(3)
        + " ms";


    document.getElementById(
        "syncRTT"
    ).innerText =
        syncBestRttMs.toFixed(3)
        + " ms";
}

// =====================================================
// DGIST 2026-11-12: video-file source + full screen
// A video file stored on the phone replaces the camera as the picture source. It is read locally through a blob: URL - nothing is
// downloaded, the downlink carries the same result JSON as in the camera mode. The send timer, in-flight gate, encoders, tracker and
// logs are the ones of the camera mode. Every frame of a file is scaled (aspect kept, black bars) to FILE_WIDTH x FILE_HEIGHT
// (.env VA_FILE_WIDTH/HEIGHT, default FHD) before the JPEG; the camera mode sends the camera's native frame as before.
// =====================================================

const FILE_WIDTH = __VA_FILE_WIDTH__;
const FILE_HEIGHT = __VA_FILE_HEIGHT__;
let fileUrl = null;            // blob: URL of the chosen video file; null = no file source
let wakeLock = null;
let wakeLockPending = false;

function sourceReady() { return stream !== null || fileUrl !== null; }

// size of the frame that is sent = the coordinate system of the result boxes
function frameTarget(video) {
    return (fileUrl !== null) ? { w: FILE_WIDTH, h: FILE_HEIGHT } : { w: video.videoWidth, h: video.videoHeight };
}

// aspect-preserving fit of sw x sh into dw x dh, centred (the same arithmetic as in the encode workers)
function fitRect(sw, sh, dw, dh) {
    const s = Math.min(dw / sw, dh / sh), w = Math.round(sw * s), h = Math.round(sh * s);
    return { x: Math.floor((dw - w) / 2), y: Math.floor((dh - h) / 2), w: w, h: h };
}

// the picture box follows the shape of the sent frame (CSS --stage-ar: file mode and full screen)
function updateStage() {
    const t = frameTarget(document.getElementById("camera"));
    if (t.w > 0 && t.h > 0) document.getElementById("stage").style.setProperty("--stage-ar", (t.w / t.h).toFixed(5));
}

function pickVideoFile() {
    const input = document.getElementById("videoFile");
    input.value = "";            // choosing the same file again must fire "change" too
    input.click();
}

async function onVideoFilePicked(input) {
    const file = input.files && input.files[0];
    if (!file) return;

    await stopCamera();          // ends a running VA session (log saved) and releases the previous source, camera or file

    const video = document.getElementById("camera");
    const url = URL.createObjectURL(file);
    try {
        video.loop = true;
        video.muted = true;
        video.src = url;
        await video.play();
        if (video.videoWidth === 0 || video.videoHeight === 0) throw new Error("no video track");
    } catch (error) {
        console.error(error);
        video.pause(); video.loop = false; video.removeAttribute("src"); video.load();
        URL.revokeObjectURL(url);
        document.getElementById("status").innerText = "Video Error";
        clientLog("source", "file error: " + file.name + " (" + file.type + "): " + (error && (error.message || error)));
        alert("Cannot play this video file: " + (error && (error.message || error)));
        return;
    }

    fileUrl = url;
    document.getElementById("videoContainer").classList.add("filemode");
    updateStage();
    resizeOverlay();
    document.getElementById("resolution").innerText =
        video.videoWidth + " x " + video.videoHeight + " (file " + file.name + ") -> " + FILE_WIDTH + " x " + FILE_HEIGHT + " sent";
    document.getElementById("status").innerText = "Video Ready";
    clientLog("source", "file " + file.name + " " + (file.size / 1048576).toFixed(1) + " MB " + file.type + ", " + video.videoWidth + "x" + video.videoHeight
              + " " + (isFinite(video.duration) ? video.duration.toFixed(1) : "?") + " s, sent as " + FILE_WIDTH + "x" + FILE_HEIGHT);
    wakeLockUpdate();
}

// true when a file source was released
function releaseVideoFile() {
    if (fileUrl === null) return false;
    const video = document.getElementById("camera");
    video.pause(); video.loop = false; video.removeAttribute("src"); video.load();
    URL.revokeObjectURL(fileUrl);
    fileUrl = null;
    document.getElementById("videoContainer").classList.remove("filemode");
    document.getElementById("resolution").innerText = "-";
    wakeLockUpdate();
    return true;
}

function fullscreenOn() { return (document.fullscreenElement || document.webkitFullscreenElement || null) !== null; }

async function enterFullscreen() {
    const stage = document.getElementById("stage");
    updateStage();
    try {
        if (stage.requestFullscreen) await stage.requestFullscreen({ navigationUI: "hide" });
        else if (stage.webkitRequestFullscreen) stage.webkitRequestFullscreen();
        else throw new Error("not supported by this browser");
    } catch (error) {
        alert("Cannot enter full screen: " + (error && (error.message || error)));
        return;
    }
    // a file is sent as a frame of a fixed shape: turn the screen to it (the camera mode follows the phone's own rotation)
    if (fileUrl !== null && screen.orientation && screen.orientation.lock) {
        try { await screen.orientation.lock(FILE_WIDTH >= FILE_HEIGHT ? "landscape" : "portrait"); } catch (e) {}
    }
}

function exitFullscreen() {
    if (!fullscreenOn()) return;
    if (document.exitFullscreen) document.exitFullscreen(); else if (document.webkitExitFullscreen) document.webkitExitFullscreen();
}

function onFullscreenChange() {
    const on = fullscreenOn();
    if (!on && screen.orientation && screen.orientation.unlock) { try { screen.orientation.unlock(); } catch (e) {} }
    updateStage();
    wakeLockUpdate();
    clientLog("fullscreen", (on ? "on" : "off") + ", screen " + screen.width + "x" + screen.height + ", source " + (stream !== null ? "camera" : (fileUrl !== null ? "file" : "none")));
}
document.addEventListener("fullscreenchange", onFullscreenChange);
document.addEventListener("webkitfullscreenchange", onFullscreenChange);

// the screen stays on while a file plays or the picture is full screen (best effort; the camera mode outside full screen is as before)
function wakeLockWanted() { return (fileUrl !== null || fullscreenOn()) && document.visibilityState === "visible"; }
async function wakeLockUpdate() {
    if (!navigator.wakeLock || wakeLockPending) return;
    wakeLockPending = true;
    try {
        if (wakeLockWanted() && wakeLock === null) {
            const lock = await navigator.wakeLock.request("screen");
            lock.addEventListener("release", () => { if (wakeLock === lock) wakeLock = null; });
            wakeLock = lock;
        }
        if (!wakeLockWanted() && wakeLock !== null) { const lock = wakeLock; wakeLock = null; await lock.release(); }
    } catch (e) {}
    wakeLockPending = false;
}
document.addEventListener("visibilitychange", () => {
    wakeLockUpdate();
    if (document.visibilityState === "visible" && fileUrl !== null) {     // a file paused while the page was hidden plays on
        const video = document.getElementById("camera");
        if (video.paused && !schedPrerolled) video.play().catch(() => {});
    }
});
document.getElementById("camera").addEventListener("loadedmetadata", updateStage);
document.getElementById("camera").addEventListener("resize", updateStage);      // the camera frame turns with the phone
// =====================================================
// DGIST 2026-11-14: video-file source - play the file again from its first frame, either now ("Restart Video") or at a chosen
// clock time ("Start video at"). The clock is the base station's once this page has synchronised with it (Start VA does that; it is
// the offset the latency figures use) and this phone's own clock before, so phones set to the same time start together. All of it
// happens on the phone: no request is sent and nothing is downloaded for it; the VA session goes on unchanged while the file starts over.
// =====================================================

const SCHED_PREROLL_MS = 700;      // this long before the chosen time the file is stopped on its first frame, so that it starts on time
let schedTargetMs = null;          // chosen start time, ms since the epoch; null = nothing scheduled
let schedPrerolled = false;        // the file is waiting on its first frame
let schedResumeAt = 0;             // position the pre-roll left (a cancel goes back to it)
let schedPreUrl = null;            // the file the pre-roll stopped (another file chosen meanwhile is not waiting)
let schedFireTimer = null;
let schedNote = "";                // outcome of the last schedule
let schedNoteAt = 0;
let schedLast = "";                // the last start from the first frame: when, and what asked for it

function schedNowMs() { return clockSynced ? Date.now() + clockOffsetMs : Date.now(); }
function schedPad(n) { return (n < 10 ? "0" : "") + n; }
function schedClockText(ms, withMs) {
    const d = new Date(ms);
    return schedPad(d.getHours()) + ":" + schedPad(d.getMinutes()) + ":" + schedPad(d.getSeconds())
        + (withMs ? "." + String(d.getMilliseconds()).padStart(3, "0") : "");
}
function schedLeftText(ms) {
    if (ms < 10000) return (Math.max(0, ms) / 1000).toFixed(1) + " s";
    const s = Math.ceil(ms / 1000), h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
    if (h > 0) return h + " h " + schedPad(m) + " min";
    return m > 0 ? m + " min " + schedPad(s % 60) + " s" : s + " s";
}

// the file from its first frame; false when the source is not a file
function restartVideoNow() {
    if (fileUrl === null) return false;
    const video = document.getElementById("camera");
    try { video.currentTime = 0; } catch (e) {}
    video.play().catch(() => {});
    return true;
}

function restartVideo() {
    if (fileUrl === null) { alert("Restart Video works with a video file: choose one with the Video File button first."); return; }
    if (schedPrerolled && fileUrl === schedPreUrl) return;      // the scheduled start is less than a second away and does the same
    restartVideoNow();
    schedLast = schedClockText(schedNowMs(), true) + " (Restart Video button)";
    schedShow();
}

function schedDisarm() {
    if (schedFireTimer !== null) { clearTimeout(schedFireTimer); schedFireTimer = null; }
    if (schedPrerolled && fileUrl !== null && fileUrl === schedPreUrl) {      // the file goes on from where the pre-roll took it
        const video = document.getElementById("camera");
        try { video.currentTime = schedResumeAt; } catch (e) {}
        video.play().catch(() => {});
    }
    schedPrerolled = false;
    schedTargetMs = null;
}

function scheduleVideoStart() {
    const value = document.getElementById("schedTime").value;
    if (!value) { alert("Choose the start time first."); return; }
    const part = value.split(":").map(Number);
    schedDisarm();
    const now = schedNowMs(), d = new Date(now);
    d.setHours(part[0], part[1], part.length > 2 ? part[2] : 0, 0);
    let target = d.getTime();
    if (target <= now) target += 86400000;      // that time has passed today: the same time tomorrow
    schedTargetMs = target;
    schedNote = "";
    schedTick();
}

function cancelVideoStart() {
    if (schedTargetMs === null) return;
    schedDisarm();
    schedNote = "Cancelled"; schedNoteAt = Date.now();
    schedShow();
}

function schedFire() {
    schedFireTimer = null;
    if (schedTargetMs === null) return;
    const left = schedTargetMs - schedNowMs();
    if (left > 2) { schedFireTimer = setTimeout(schedFire, left > 12 ? left - 8 : 1); return; }      // woken early (or the clock was synchronised meanwhile): wait for the rest
    const target = schedTargetMs, video = document.getElementById("camera"), ready = schedPrerolled && fileUrl === schedPreUrl;
    schedTargetMs = null; schedPrerolled = false; schedNoteAt = Date.now();
    if (fileUrl === null) { schedNote = "Skipped at " + schedClockText(target, false) + ": no video file was chosen"; schedShow(); return; }
    if (!ready || video.currentTime > 0.05) { try { video.currentTime = 0; } catch (e) {} }
    video.play().then(() => {
        const at = schedNowMs();
        schedLast = schedClockText(at, true) + " (scheduled for " + schedClockText(target, false) + ", playing " + Math.round(at - target) + " ms after it)";
        schedNote = "Started at " + schedClockText(target, false); schedNoteAt = Date.now(); schedShow();
    }, error => {
        schedNote = "Could not start at " + schedClockText(target, false) + ": " + (error && (error.message || error)); schedNoteAt = Date.now(); schedShow();
    });
}

function schedShow() {
    const now = schedNowMs(), note = document.getElementById("fsSched");
    document.getElementById("stage").classList.toggle("filesrc", fileUrl !== null);
    let text = schedNote || "Not set", over = "";
    if (schedTargetMs !== null) {
        const left = schedTargetMs - now, day = new Date(schedTargetMs).getDate() !== new Date(now).getDate() ? " tomorrow" : "";
        text = schedClockText(schedTargetMs, false) + day + ", in " + schedLeftText(left) + (fileUrl === null ? " (no video file chosen yet)" : "");
        over = "Video starts at " + schedClockText(schedTargetMs, false) + day + ", in " + schedLeftText(left);
    } else if (schedNote && Date.now() - schedNoteAt < 3000) over = schedNote;
    document.getElementById("schedStatus").innerText = text;
    document.getElementById("schedLast").innerText = schedLast || "-";
    const diff = Math.round(now - Date.now());
    document.getElementById("schedClock").innerText = schedClockText(now, false) + (clockSynced
        ? " (base station clock; this phone's clock is " + Math.abs(diff) + " ms " + (diff >= 0 ? "behind" : "ahead") + ")"
        : " (this phone's clock; the base station's clock after Start VA)");
    note.innerText = over; note.className = over ? "show" : "";
}

function schedTick() {      // every 200 ms: the status lines, the pre-roll and the timer of the start itself
    if (schedTargetMs !== null) {
        const left = schedTargetMs - schedNowMs();
        if (left <= SCHED_PREROLL_MS && !schedPrerolled && fileUrl !== null) {
            const video = document.getElementById("camera");
            schedResumeAt = video.currentTime; schedPrerolled = true; schedPreUrl = fileUrl;
            video.pause();
            try { video.currentTime = 0; } catch (e) {}
        }
        if (left <= 1000 && schedFireTimer === null) schedFireTimer = setTimeout(schedFire, Math.max(0, left - 8));
    }
    schedShow();
}
setInterval(schedTick, 200);
schedTick();


// =====================================================
// Start Camera
// =====================================================

async function startCamera() {

    if (fileUrl !== null) {

        await stopCamera();      // DGIST: from the file source back to the camera (ends a running VA session, releases the file)
    }

    if (stream !== null) {

        document.getElementById(
            "status"
        ).innerText = "Camera Ready";

        return;
    }


    try {

        stream =
            await navigator.mediaDevices.getUserMedia({

                video: {

                    facingMode: {
                        ideal: "environment"
                    },

                    width: {
                        ideal: CAMERA_WIDTH
                    },

                    height: {
                        ideal: CAMERA_HEIGHT
                    }

                },

                audio: false

            });


        const video =
            document.getElementById(
                "camera"
            );


        video.srcObject = stream;


        await video.play();
        try { const st = stream.getVideoTracks()[0].getSettings();
              clientLog("camera", st.width + "x" + st.height + " @" + st.frameRate + " fps facing " + st.facingMode + ", video element " + video.videoWidth + "x" + video.videoHeight); } catch (e) {}


        document.getElementById(
            "resolution"
        ).innerText =
            video.videoWidth
            + " x "
            + video.videoHeight;


        resizeOverlay();
        updateStage();


        document.getElementById(
            "status"
        ).innerText =
            "Camera Ready";


    } catch (error) {

        console.error(error);


        stream = null;


        document.getElementById(
            "status"
        ).innerText =
            "Camera Error";


        clientLog("camera_error", error.name + ": " + error.message);
        alert(
            "Cannot access camera: "
            + error.message
        );
    }
}


// =====================================================
// Stop Camera
// =====================================================

async function stopCamera() {

    if (running) {

        await stopVA();

    } else if (
        latencyRecords.length > 0
        && !sessionSaved
    ) {

        await saveLatencyLog();
    }


    if (stream !== null) {

        stream.getTracks().forEach(
            track => track.stop()
        );


        stream = null;
    }


    document.getElementById(
        "camera"
    ).srcObject = null;

    const wasFile = releaseVideoFile();      // DGIST: the file source, if that was the source


    clearBoundingBoxes();


    document.getElementById(
        "status"
    ).innerText =
        wasFile
        ? "Video Stopped"
        : "Camera Stopped";
}


// =====================================================
// Start VA
// =====================================================

async function startVA() {
    clientLog("start_va", "pressed: camera=" + (stream !== null) + " file=" + (fileUrl !== null) + " running=" + running + " session=" + (telemetryClient !== null) + " ue=" + document.getElementById('ueSelector').value);

    if (startingVA || telemetryClient !== null) {
        alert('Stop the previous session before starting a new one.');
        return;
    }

    if (!sourceReady()) {

        alert(
            "Please start the camera or choose a video file first."
        );

        return;
    }

    if (fileUrl !== null) {      // DGIST: a file that was paused (page in the background) plays again

        const fileVideo = document.getElementById("camera");
        if (fileVideo.paused && !schedPrerolled) { try { await fileVideo.play(); } catch (e) {} }
    }


    if (running) {
        return;
    }

    startingVA = true;


    // =================================================
    // Clock Synchronization Before Experiment
    // =================================================

    try {

        document.getElementById(
            "status"
        ).innerText =
            "Clock Sync";


        await synchronizeClocks();


    } catch (error) {

        console.error(error);


        document.getElementById(
            "status"
        ).innerText =
            "Clock Sync Error";


        clientLog("clock_sync_failed", error && (error.message || error));
        alert(
            "Clock synchronization failed."
        );
        startingVA = false;
        return;
    }


    // =================================================
    // Start New Experiment Session
    // =================================================

    try {
        telemetryClient = await new TelemetryClient(document.getElementById('ueSelector').value).start();
        document.getElementById('ueSelector').disabled = true;
    } catch (error) {
        telemetryClient = null;
        startingVA = false;
        clientLog("ue_identification_failed", error.message);
        alert('UE identification failed: ' + error.message);
        return;
    }
    startingVA = false;
    running = true;

    frameCount = 0;
    frameSequence = 0;
    latestDisplayedFrameId = 0;
    droppedFrames = 0;
    qosMetCount = 0;
    qosMissCount = 0;
    skipBusy = 0; skipInflight = 0; encMsSum = 0; encMsMax = 0; encN = 0; sentN = 0; lastRateReport = 0;
    inFlight = 0;
    captureBusy = false;
    runToken++;

    latencyRecords = [];

    totalPayloadBytes = 0;
    totalDlPayloadBytes = 0;

    sessionStartTime =
        performance.now();

    sessionSaved = false;


    document.getElementById(
        "frameCount"
    ).innerText = "0";


    document.getElementById(
        "logStatus"
    ).innerText =
        "Recording";


    document.getElementById(
        "status"
    ).innerText =
        "Running";


    tracker.reset();
    setYOLOEnabled(yoloEnabled);

    sendLoop();
    sendTimer = setInterval(sendLoop, targetInterval);
}


// =====================================================
// Stop VA
// =====================================================

async function stopVA() {

    if (!running) {

        if (
            latencyRecords.length > 0
            && !sessionSaved
        ) {

            if (inFlight === 0 && !captureBusy) await saveLatencyLog();
        }
        if (inFlight === 0 && !captureBusy) await finishTelemetry();
        return;
    }


    running = false;
    runToken++;
    if (sendTimer !== null) {
        clearInterval(sendTimer);
        sendTimer = null;
    }
    tracker.reset();


    document.getElementById(
        "status"
    ).innerText =
        "Saving Log";


    // Wait for in-flight responses before saving measurements.
    const drainStart = performance.now();
    while ((inFlight > 0 || captureBusy) && performance.now() - drainStart < 15000) {
        await new Promise(resolve => setTimeout(resolve, 50));
    }
    if (inFlight > 0 || captureBusy) {
        document.getElementById("logStatus").innerText = "Pending uploads; retry Stop to save";
        return;
    }
    await saveLatencyLog();
    await finishTelemetry();


    document.getElementById(
        "status"
    ).innerText =
        (stream !== null)
        ? "Camera Ready"
        : ((fileUrl !== null) ? "Video Ready" : "Stopped");
}


// =====================================================
// Save Experiment Log
// =====================================================

async function finishTelemetry() {
    if (!telemetryClient || (latencyRecords.length > 0 && !sessionSaved)) return;
    try {
        await telemetryClient.stop();
        telemetryClient = null;
        document.getElementById('ueSelector').disabled = false;
    } catch (e) { alert('Could not stop UE telemetry; press Stop again: ' + e.message); }
}

async function saveLatencyLog() {

    if (
        latencyRecords.length === 0
        || sessionSaved
    ) {

        return;
    }


    document.getElementById(
        "logStatus"
    ).innerText =
        "Saving";


    try {

        const response =
            await fetch(
                "/save_latency_log",
                {
                    method: "POST",

                    headers: {
                        "Content-Type":
                            "application/json"
                    },

                    body: JSON.stringify({
                        ue_id: telemetryClient.ue,
                        session_token: telemetryClient.token,
                        records:
                            [...latencyRecords].sort((a, b) => a.frame_id - b.frame_id)
                    })
                }
            );


        if (!response.ok) {

            throw new Error(
                "Log save error: "
                + response.status
            );
        }


        const result =
            await response.json();


        sessionSaved = true;


        document.getElementById(
            "logStatus"
        ).innerText =
            "Saved: "
            + result.filename;


        console.log(
            "Experiment log saved:",
            result.path
        );


    } catch (error) {

        console.error(error);


        document.getElementById(
            "logStatus"
        ).innerText =
            "Save Error";
    }
}


// =====================================================
// Resize Bounding Box Overlay
// =====================================================

function resizeOverlay() {

    const video =
        document.getElementById(
            "camera"
        );


    const overlay =
        document.getElementById(
            "overlay"
        );


    if (
        video.videoWidth === 0
        || video.videoHeight === 0
    ) {

        return;
    }


    // DGIST: the overlay has the size of the frame that is sent (the camera's native frame, or the fixed size of the file mode)
    const target = frameTarget(video);

    overlay.width =
        target.w;


    overlay.height =
        target.h;
}


// =====================================================
// Clear Bounding Boxes
// =====================================================

function clearBoundingBoxes() {

    const overlay =
        document.getElementById(
            "overlay"
        );


    const context =
        overlay.getContext("2d");


    context.clearRect(
        0,
        0,
        overlay.width,
        overlay.height
    );
}


// =====================================================
// Draw Bounding Boxes
// =====================================================

function drawBoundingBoxes(
    detections,
    imageWidth,
    imageHeight
) {

    const overlay =
        document.getElementById(
            "overlay"
        );


    const context =
        overlay.getContext("2d");


    if (
        overlay.width !== imageWidth
        || overlay.height !== imageHeight
    ) {

        overlay.width =
            imageWidth;


        overlay.height =
            imageHeight;
    }


    context.clearRect(
        0,
        0,
        overlay.width,
        overlay.height
    );


    detections.forEach(
        detection => {


            const x =
                detection.x1;


            const y =
                detection.y1;


            const width =
                detection.x2
                - detection.x1;


            const height =
                detection.y2
                - detection.y1;


            const boxColor =
                getClassColor(
                    detection.class
                );


            // =========================================
            // Bounding Box
            // =========================================

            context.globalAlpha = (detection.alpha === undefined) ? 1 : detection.alpha;   // DGIST: track fade-out
 context.strokeStyle =
                boxColor;


            context.lineWidth =
                4;


            context.strokeRect(
                x,
                y,
                width,
                height
            );


            // =========================================
            // Class + Confidence
            // =========================================

            const confidence =
                Number(
                    detection.confidence
                ).toFixed(2);


            const label =
                detection.class
                + " "
                + confidence;


            context.font =
                "bold 24px Arial";


            context.textBaseline =
                "top";


            const textWidth =
                context.measureText(
                    label
                ).width;


            const labelHeight =
                32;


            let labelY =
                y - labelHeight;


            if (labelY < 0) {

                labelY = y;
            }


            context.fillStyle =
                boxColor;


            context.fillRect(
                x,
                labelY,
                textWidth + 14,
                labelHeight
            );


            context.fillStyle =
                "#000000";


            context.fillText(
                label,
                x + 7,
                labelY + 3
            );

        }
    );
    context.globalAlpha = 1;
}


// =====================================================
// Keep Overlay Correct on Screen Resize
// =====================================================

window.addEventListener(
    "resize",
    () => {

        resizeOverlay();

    }
);

// =====================================================
// Capture + Send Loop
// =====================================================

async function sendLoop() {
    if (!running) return;
    rateReport();
    const encBusy = encPool.ok ? (encPool.busy >= ENCODE_WORKERS) : captureBusy;   // DGIST: with workers, up to ENCODE_WORKERS frames encode at once
    if (encBusy || inFlight >= MAX_INFLIGHT) {
        droppedFrames++;
        if (encBusy) skipBusy++; else skipInflight++;
        return;
    }
    if (!encPool.ok) captureBusy = true;
    const thisRun = runToken;
    const frameTelemetry = telemetryClient;
    const loopStart = performance.now();

    const video =
        document.getElementById(
            "camera"
        );


    const canvas =
        document.getElementById(
            "captureCanvas"
        );


    const context =
        canvas.getContext("2d");


    // =================================================
    // Wait Until Camera Is Ready
    // =====================================================

    if (
        video.videoWidth === 0
        || video.videoHeight === 0
    ) {

        captureBusy = false;
        return;
    }


    // =================================================
    // Capture Camera Frame + JPEG (DGIST: in a worker when VA_ENCODE_WORKERS > 0, else on the main thread as before)
    // =====================================================
    const encStart = performance.now();
    let blob;
    if (encPool.ok) {
        blob = await encodeFrame(video, JPEG_QUALITY, (fileUrl !== null) ? FILE_WIDTH : 0, (fileUrl !== null) ? FILE_HEIGHT : 0);
        if (blob === null) { if (encPool.errors && encPool.errors % 20 === 1) clientLog("encode", "worker encode skipped: " + encPool.lastError); return; }
    } else {
        if (fileUrl !== null) {      // file source: FILE_WIDTH x FILE_HEIGHT, aspect kept, black bars (as the encode workers do)
            const fit = fitRect(video.videoWidth, video.videoHeight, FILE_WIDTH, FILE_HEIGHT);
            canvas.width = FILE_WIDTH;
            canvas.height = FILE_HEIGHT;
            context.fillStyle = "black";
            context.fillRect(0, 0, FILE_WIDTH, FILE_HEIGHT);
            context.imageSmoothingQuality = "high";
            context.drawImage(video, fit.x, fit.y, fit.w, fit.h);
        } else {
            canvas.width = video.videoWidth;
            canvas.height = video.videoHeight;
            context.drawImage(video, 0, 0, canvas.width, canvas.height);
        }
        blob = await new Promise(resolve => { canvas.toBlob(resolve, "image/jpeg", JPEG_QUALITY); });
    }
    const encMs = performance.now() - encStart; encN++; encMsSum += encMs; if (encMs > encMsMax) encMsMax = encMs;


    if (blob === null) {
        captureBusy = false;
        running = false;
        if (sendTimer !== null) { clearInterval(sendTimer); sendTimer = null; }


        document.getElementById(
            "status"
        ).innerText =
            "JPEG Error";


        return;
    }


    if (!running || thisRun !== runToken) {
        captureBusy = false;
        return;
    }
    const currentFrameId = ++frameSequence;
    inFlight++; sentN++;
    captureBusy = false;

    // =================================================
    // UL Frame Size
    // =====================================================

    const frameSizeBytes =
        blob.size;


    const frameSizeKB =
        frameSizeBytes
        / 1024.0;


    totalPayloadBytes +=
        frameSizeBytes;


    const formData =
        new FormData();


    formData.append(
        "file",
        blob,
        "frame.jpg"
    );

    // Explicitly send the selected mode with EACH frame.
    // No global server switch: multiple UE sessions cannot change each other's mode.
    formData.append(
        "enable_yolo",
        yoloEnabled ? "true" : "false"
    );
    formData.append('ue_id', frameTelemetry.ue);
    formData.append('session_token', frameTelemetry.token);
    formData.append('frame_id', String(currentFrameId));   // DGIST dashboard: joins this upload with the E2E the phone reports for it (uplink only)
    const uploadAbort = new AbortController();
    const uploadTimeout = setTimeout(() => uploadAbort.abort(), 5000);
    try {

        // =================================================
        // t0
        //
        // iPhone starts sending frame.
        //
        // Date.now() is used for cross-device
        // timestamp comparison.
        //
        // performance.now() is used for accurate
        // local E2E measurement.
        // =====================================================

        const clientSendMs =
            Date.now();
        // DGIST viewer: the server computes this frame's UL latency (clock offset = GH200 clock - phone clock, as below) for its verdict
        formData.append("client_send_ms", String(clientSendMs));
        formData.append("clock_offset_ms", String(clockOffsetMs));


        const e2eStart =
            performance.now();


        // =================================================
        // Send Frame to GH200
        // =====================================================

        const response =
            await fetch(
                "/detect_frame",
                {
                    method: "POST",
                    body: formData,
                    signal: uploadAbort.signal
                }
            );


        if (!response.ok) {

            throw new Error(
                "Server error: "
                + response.status
            );
        }


        // =================================================
        // Receive Complete DL Response
        //
        // response.text() waits until the complete
        // response body has been received.
        // =====================================================

        const responseText =
            await response.text();


        // =================================================
        // t5
        //
        // Complete response received at iPhone.
        // =====================================================

        const clientReceiveMs =
            Date.now();


        const e2eEnd =
            performance.now();


        // =================================================
        // AI Service E2E Latency
        // =====================================================

        const e2eLatency =
            e2eEnd
            - e2eStart;
        clearTimeout(uploadTimeout);


        // =================================================
        // DL Response Size
        // =====================================================

        const dlResponseBytes =
            new TextEncoder().encode(
                responseText
            ).length;


        const dlResponseKB =
            dlResponseBytes
            / 1024.0;

        totalDlPayloadBytes += dlResponseBytes;


        // Parse JSON after complete response
        // has already been received.

        const data =
            JSON.parse(
                responseText
            );


        // =================================================
        // Server Timestamps
        //
        // All are GH200 Unix timestamps in ms.
        //
        // t1 = request/frame received
        // t2 = YOLO start
        // t3 = YOLO end
        // t4 = response preparation
        // =====================================================

        const serverReceiveMs =
            data.server_receive_ms;


        const aiStartMs =
            data.ai_start_ms;


        const aiEndMs =
            data.ai_end_ms;


        const serverSendMs =
            data.server_send_ms;


        // =================================================
        // Convert GH200 timestamps into estimated
        // iPhone clock domain.
        //
        // clockOffsetMs =
        // GH200 clock - iPhone clock
        // =====================================================

        const serverReceiveClientClock =
            serverReceiveMs
            - clockOffsetMs;


        const serverSendClientClock =
            serverSendMs
            - clockOffsetMs;


        // =================================================
        // Estimated UL Latency
        //
        // iPhone frame send
        //       ->
        // GH200 complete request receive
        // =====================================================

        const ulLatency =
            Math.max(
                0,
                serverReceiveClientClock
                - clientSendMs
            );


        // =================================================
        // AI Processing Latency
        // =====================================================

        const aiLatency =
            aiEndMs
            - aiStartMs;


        // =================================================
        // Server Other Time
        //
        // Request received -> YOLO start
        // +
        // YOLO end -> response preparation
        // =====================================================

        const serverBeforeAI =
            Math.max(
                0,
                aiStartMs
                - serverReceiveMs
            );


        const serverAfterAI =
            Math.max(
                0,
                serverSendMs
                - aiEndMs
            );


        const serverOther =
            serverBeforeAI
            + serverAfterAI;


        // =================================================
        // Estimated DL Response Latency
        //
        // GH200 response preparation
        //       ->
        // iPhone complete response receive
        // =====================================================

        const dlLatency =
            Math.max(
                0,
                clientReceiveMs
                - serverSendClientClock
            );


        // =================================================
        // Previous Research Definition
        //
        // Network Latency =
        // E2E - AI Processing
        //
        // This includes UL + DL + other overhead.
        // =====================================================

        const networkLatency =
            Math.max(
                0,
                e2eLatency
                - aiLatency
            );


        // =================================================
        // Breakdown Residual
        //
        // Ideally:
        //
        // E2E
        // ≈ UL
        // + AI
        // + Server Other
        // + DL
        //
        // Residual shows timestamp/sync/measurement error.
        // =====================================================

        const breakdownSum =
            ulLatency
            + aiLatency
            + serverOther
            + dlLatency;


        const breakdownResidual =
            e2eLatency
            - breakdownSum;

        // DGIST: QoS verdict of this frame = the VA E2E stayed within the deadline (and, with YOLO on, the dApp answered success)
        const dapp = data.dapp || {};
        const qosDeadline = (data.qos_deadline_ms !== undefined && data.qos_deadline_ms !== null) ? data.qos_deadline_ms : QOS_DEADLINE_MS;
        const qosMet = data.yolo_enabled ? (dapp.status === "success" && e2eLatency <= qosDeadline) : (e2eLatency <= qosDeadline);
        if (qosMet) qosMetCount++; else qosMissCount++;


        if (thisRun !== runToken && !running) {
            // Still record responses for the stopped session until log saving.
        }

        frameCount++;


        // =================================================
        // Actual FPS
        // =====================================================

        const sessionElapsedSec =
            (
                performance.now()
                - sessionStartTime
            ) / 1000.0;


        const actualFPS =
            frameCount
            / sessionElapsedSec;


        // =================================================
        // UL Application Payload Rate
        // =====================================================

        const ulPayloadMbps =
            (
                totalPayloadBytes
                * 8.0
            )
            / sessionElapsedSec
            / 1000000.0;

        const dlPayloadMbps =
            (totalDlPayloadBytes * 8.0)
            / sessionElapsedSec
            / 1000000.0;


        // =================================================
        // Save Measurement in Browser Memory
        // =====================================================

        void frameTelemetry.result(currentFrameId, e2eLatency, frameSizeBytes,
                                    clientReceiveMs + clockOffsetMs);
        latencyRecords.push({

            frame_id:
                currentFrameId,

            yolo_enabled:
                data.yolo_enabled,

            timestamp:
                new Date(serverReceiveMs).toISOString(),

            image_width:
                data.image_width,

            image_height:
                data.image_height,

            frame_size_kb:
                Number(
                    frameSizeKB.toFixed(3)
                ),

            actual_fps:
                Number(
                    actualFPS.toFixed(3)
                ),

            ul_payload_mbps:
                Number(
                    ulPayloadMbps.toFixed(3)
                ),

            dl_payload_mbps:
                Number(
                    dlPayloadMbps.toFixed(3)
                ),

            dl_response_kb:
                Number(
                    dlResponseKB.toFixed(3)
                ),

            e2e_latency_ms:
                Number(
                    e2eLatency.toFixed(3)
                ),

            ul_latency_ms:
                Number(
                    ulLatency.toFixed(3)
                ),

            ai_latency_ms:
                Number(
                    aiLatency.toFixed(3)
                ),

            server_other_ms:
                Number(
                    serverOther.toFixed(3)
                ),

            dl_latency_ms:
                Number(
                    dlLatency.toFixed(3)
                ),

            network_latency_ms:
                Number(
                    networkLatency.toFixed(3)
                ),

            breakdown_residual_ms:
                Number(
                    breakdownResidual.toFixed(3)
                ),

            clock_offset_ms:
                Number(
                    clockOffsetMs.toFixed(3)
                ),

            sync_rtt_ms:
                Number(
                    syncBestRttMs.toFixed(3)
                ),

            gpu_util_percent: data.gpu_util_percent,
            gpu_memory_used_gb: data.gpu_memory_used_gb,
            qos_deadline_ms: qosDeadline,
            qos_met: qosMet,
            dapp_status: dapp.status ?? null,
            dapp_reason: dapp.reason ?? null,
            dapp_model: dapp.model ?? null,
            dapp_sm: dapp.sm ?? null,
            dapp_wait_ms: dapp.wait_ms ?? null,
            dapp_decode_ms: dapp.decode_ms ?? null,
            dapp_infer_ms: dapp.infer_ms ?? null,
            dapp_latency_ms: dapp.latency_ms ?? null,
            detections: data.detections.length
        });


        // =================================================
        // Only the newest completed frame may update the live overlay/UI.
        if (currentFrameId >= latestDisplayedFrameId && thisRun === runToken) {
            latestDisplayedFrameId = currentFrameId;
        // Update Traffic UI
        // =====================================================

        document.getElementById(
            "frameCount"
        ).innerText =
            frameCount;


        document.getElementById(
            "resolution"
        ).innerText =
            data.image_width
            + " x "
            + data.image_height;


        document.getElementById(
            "frameSize"
        ).innerText =
            frameSizeKB.toFixed(2)
            + " KB";


        document.getElementById(
            "dlResponseSize"
        ).innerText =
            dlResponseKB.toFixed(3)
            + " KB";


        document.getElementById(
            "actualFPS"
        ).innerText =
            actualFPS.toFixed(2)
            + " FPS";


        document.getElementById(
            "ulRate"
        ).innerText =
            ulPayloadMbps.toFixed(2)
            + " Mbps";

        document.getElementById(
            "dlRate"
        ).innerText =
            dlPayloadMbps.toFixed(3)
            + " Mbps";


        // =================================================
        // Update Latency UI
        // =====================================================

        document.getElementById(
            "e2eLatency"
        ).innerText =
            e2eLatency.toFixed(2)
            + " ms";


        document.getElementById(
            "ulLatency"
        ).innerText =
            ulLatency.toFixed(2)
            + " ms";


        document.getElementById(
            "aiLatency"
        ).innerText =
            data.yolo_enabled
                ? aiLatency.toFixed(2) + " ms"
                : "0.00 ms (YOLO OFF)";

        document.getElementById(
            "frameYoloStatus"
        ).innerText =
            data.yolo_enabled ? "ON" : "OFF";


        document.getElementById(
            "serverOther"
        ).innerText =
            serverOther.toFixed(2)
            + " ms";


        document.getElementById(
            "dlLatency"
        ).innerText =
            dlLatency.toFixed(2)
            + " ms";


        document.getElementById(
            "networkLatency"
        ).innerText =
            networkLatency.toFixed(2)
            + " ms";

        document.getElementById("qosStatus").innerText =
            (qosMet ? "met" : "MISSED") + " | met " + qosMetCount + " / missed " + qosMissCount
            + " (" + (100.0 * qosMetCount / Math.max(1, qosMetCount + qosMissCount)).toFixed(1) + " %)";
        const fmt = (v, d) => (v === undefined || v === null) ? "-" : Number(v).toFixed(d);
        document.getElementById("dappInfo").innerText = data.yolo_enabled
            ? ((dapp.backend || VA_BACKEND) + " " + (dapp.model || VA_MODEL) + " " + (dapp.status || "-") + (dapp.reason ? "/" + dapp.reason : "")
               + " | SM " + fmt(dapp.sm, 0) + " | wait " + fmt(dapp.wait_ms, 1) + " dec " + fmt(dapp.decode_ms, 1)
               + " inf " + fmt(dapp.infer_ms, 1) + " srv " + fmt(dapp.latency_ms, 1) + " ms | det " + data.detections.length)
            : "YOLO OFF";



        // =================================================
        // Bounding Boxes
        // =====================================================

        if (data.yolo_enabled && yoloEnabled) {
            // DGIST: tracks persist and follow between results; a QoS-missed result is not drawn (TRACK.dropMissed)
            tracker.update(data.detections, data.image_width, data.image_height, encStart, qosMet);
        } else {
            tracker.reset();
        }


        // =================================================
        // Detection List
        // =====================================================

        const detectionList =
            document.getElementById(
                "detections"
            );


        detectionList.innerHTML = "";
        if (TRACK.dropMissed && data.yolo_enabled && yoloEnabled && !qosMet) {
            const li = document.createElement("li");
            li.innerText = "QoS " + qosDeadline + " ms missed (E2E " + e2eLatency.toFixed(0) + " ms): boxes of this frame not drawn ("
                + tracker.dropped + " frames this session)";
            detectionList.appendChild(li);
        }
        if (!yoloEnabled || !data.yolo_enabled) {

            const li = document.createElement("li");
            li.innerText = yoloEnabled
                ? "Waiting for next YOLO result"
                : "YOLO OFF - frames are still uploaded";
            detectionList.appendChild(li);

        } else if (
            data.detections.length === 0
        ) {

            const li =
                document.createElement(
                    "li"
                );


            li.innerText =
                "No objects detected";


            detectionList.appendChild(
                li
            );


        } else {


            data.detections.forEach(
                detection => {


                    const li =
                        document.createElement(
                            "li"
                        );


                    li.innerText =
                        detection.class
                        + " (confidence: "
                        + detection.confidence
                        + ")";


                    detectionList.appendChild(
                        li
                    );

                }
            );
        }


        }

    } catch (error) {
        console.error("Frame upload/inference error:", error);
        clientLog("frame_error", error.name + ": " + error.message);
        void frameTelemetry.failure(currentFrameId);
        droppedFrames++;
        if (running && thisRun === runToken) {
            document.getElementById("status").innerText = "Running (some frames failed)";
        }
    } finally {
        clearTimeout(uploadTimeout);
        inFlight = Math.max(0, inFlight - 1);
    }
}


</script>


</body>

</html>
""")


# =====================================================
# Base-station viewer (DGIST 2026-11-11): /viewer, /viewer/<ue>, /viewer_frame/<ue>, /viewer_status
# =====================================================

class ViewerState:
    """the latest picture of one UE for the base-station viewer. detect_frame calls update() with the server-side verdict of every
    result; the picture (frame + boxes) changes only on a met result (VA_VIEWER_QOS_FREEZE), the status fields on every result.
    Viewers long-poll /viewer_frame/<ue>?seq=&stat= and are woken through the event."""

    def __init__(self, ue):
        self.ue = ue
        self.seq = 0                      # picture updates
        self.stat_seq = 0                 # status updates (every result)
        self.frame = None                 # JPEG bytes exactly as the phone sent them (no re-encode)
        self.dets = []
        self.w = self.h = 0
        self.capture_ms = 0.0             # send time of the shown frame (server clock) - a later reply to an OLDER capture never replaces it
        self.shown_ms = 0.0               # server time the picture last changed
        self.last_ms = 0.0                # server time of the last result, any verdict
        self.verdict = "none"             # met | missed | fail | none
        self.reason = None
        self.ul_ms = self.ai_ms = self.sum_ms = None
        self.ul_known = True
        self.model = None
        self.sm = None
        self.results = self.met = self.missed = self.failed = self.stale = self.streak = 0
        self.recent = deque(maxlen=100)   # verdicts of the last 100 results
        self.load = deque()               # (server ms, bytes) of the results of the last 5 s -> req/s and Mbps the UE offers
        self.hist = deque()               # (server ms, ul_ms, ai_ms, sum_ms, met) of the last 60 s -> percentiles in meta (experiments)
        self.kb = 0.0                     # size of the shown frame
        self.event = asyncio.Event()

    def update(self, now_ms, frame, dets, w, h, capture_ms, ul_ms, ai_ms, other_ms, dapp, success):
        self.results += 1
        self.last_ms = now_ms
        self.load.append((now_ms, len(frame)))
        while self.load and now_ms - self.load[0][0] > 5000.0:
            self.load.popleft()
        self.hist.append((now_ms, ul_ms, ai_ms, (ul_ms or 0.0) + ai_ms + other_ms + VA_VIEWER_DL_MS, None))
        while self.hist and now_ms - self.hist[0][0] > 60000.0:
            self.hist.popleft()
        self.ul_known = ul_ms is not None
        total = (ul_ms or 0.0) + ai_ms + other_ms + VA_VIEWER_DL_MS
        self.ul_ms, self.ai_ms, self.sum_ms = ul_ms, ai_ms, total
        d = dapp or {}
        self.model, self.sm = d.get("model"), d.get("sm")
        if not success:
            self.verdict, self.reason = "fail", d.get("reason") or d.get("status") or "fail"
            self.failed += 1; self.streak += 1; met = False
        elif total <= VA_DEADLINE_MS:
            self.verdict, self.reason = "met", None
            self.met += 1; self.streak = 0; met = True
        else:
            self.verdict, self.reason = "missed", None
            self.missed += 1; self.streak += 1; met = False
        self.recent.append(met)
        self.hist[-1] = self.hist[-1][:4] + (met,)
        if success and (met or not VA_VIEWER_QOS_FREEZE):
            if capture_ms < self.capture_ms:
                self.stale += 1               # an older capture answered after a newer one was shown: keep the newer picture
            else:
                self.frame, self.dets, self.w, self.h = frame, dets, w, h
                self.kb = len(frame) / 1024.0
                self.capture_ms, self.shown_ms = capture_ms, now_ms
                self.seq += 1
        self.stat_seq += 1
        ev, self.event = self.event, asyncio.Event()
        ev.set()
        return met, total

    def meta(self, now_ms, with_dets):
        r1 = lambda x: None if x is None else round(x, 1)
        m = {"ue": self.ue, "seq": self.seq, "stat_seq": self.stat_seq, "verdict": self.verdict, "reason": self.reason,
             "ul_ms": r1(self.ul_ms), "ai_ms": r1(self.ai_ms), "dl_ms": VA_VIEWER_DL_MS, "sum_ms": r1(self.sum_ms),
             "deadline_ms": VA_DEADLINE_MS, "ul_known": self.ul_known, "model": self.model, "sm": self.sm, "w": self.w, "h": self.h,
             "results": self.results, "met": self.met, "missed": self.missed, "failed": self.failed, "stale": self.stale,
             "streak": self.streak, "met_pct": round(100.0 * sum(self.recent) / len(self.recent), 1) if self.recent else None,
             "age_ms": round(now_ms - self.shown_ms) if self.shown_ms else None,
             "since_last_ms": round(now_ms - self.last_ms) if self.last_ms else None,
             "active": bool(self.last_ms and now_ms - self.last_ms < 2000.0), "freeze": VA_VIEWER_QOS_FREEZE,
             "kb": round(self.kb, 1), "req_s": round(len(self.load) / 5.0, 1), "mbps": round(sum(b for _, b in self.load) * 8 / 5.0 / 1e6, 1)}
        if self.hist:
            def pct(vals, p):
                vals = sorted(v for v in vals if v is not None)
                return round(vals[int(p * (len(vals) - 1))], 1) if vals else None
            h = list(self.hist)
            m["last60"] = {"n": len(h), "ul_p50": pct([x[1] for x in h], .5), "ul_p90": pct([x[1] for x in h], .9), "ul_p99": pct([x[1] for x in h], .99),
                           "ai_p50": pct([x[2] for x in h], .5), "sum_p50": pct([x[3] for x in h], .5), "sum_p90": pct([x[3] for x in h], .9), "sum_p99": pct([x[3] for x in h], .99),
                           "met_pct": round(100.0 * sum(1 for x in h if x[4]) / len(h), 1), "req_s": round(len(h) / min(60.0, max(1.0, (h[-1][0] - h[0][0]) / 1000.0 + 0.07)), 1)}
        if with_dets:
            m["dets"] = sorted(self.dets, key=lambda x: -x["confidence"])[:64]
        return m


viewers = {u: ViewerState(u) for u in ("ue1", "ue2")}


def _viewer_meta_headers(st, now_ms, with_dets):
    return {"X-VA-Meta": json.dumps(st.meta(now_ms, with_dets), ensure_ascii=True, separators=(",", ":")),
            "Cache-Control": "no-store, no-cache, must-revalidate", "Pragma": "no-cache"}


@app.get("/viewer_frame/{ue_id}")
async def viewer_frame(ue_id: str, seq: int = 0, stat: int = 0, wait: float = 1000.0):
    """long poll: waits up to `wait` ms for a picture newer than `seq` or a status newer than `stat`. 200 + JPEG body when there is a
    new picture, 204 otherwise; the X-VA-Meta header carries the status (and the boxes with a picture)."""
    st = viewers.get(ue_id)
    if st is None or not VA_VIEWER:
        raise HTTPException(status_code=404, detail="Unknown UE or viewer off")
    loop = asyncio.get_running_loop()
    end = loop.time() + min(max(wait, 0.0), 5000.0) / 1000.0
    while st.seq <= seq and st.stat_seq <= stat:
        left = end - loop.time()
        if left <= 0:
            break
        ev = st.event
        try:
            await asyncio.wait_for(ev.wait(), left)
        except asyncio.TimeoutError:
            break
    now_ms = time.time_ns() / 1_000_000.0
    if st.seq > seq and st.frame is not None:
        return Response(content=st.frame, media_type="image/jpeg", headers=_viewer_meta_headers(st, now_ms, True))
    return Response(status_code=204, headers=_viewer_meta_headers(st, now_ms, False))


def _nonva_stats_fetch(ue_id):
    import urllib.request, ssl
    ctx = ssl._create_unverified_context()   # the non-VA service on this host uses the same self-signed certificate family
    try:
        with urllib.request.urlopen(f"https://127.0.0.1:{NONVA_PORT}/receiver_stats?session_id={ue_id}", context=ctx, timeout=1.5) as r:
            d = json.loads(r.read().decode()); d["ok"] = True; return d
    except Exception as exc:   # service down, or no session yet (404)
        return {"ok": False, "reason": str(exc)[:120]}


@app.get("/nonva_stats/{ue_id}")
async def nonva_stats(ue_id: str):
    """receiver statistics of one non-VA UE, proxied from the non-VA service (for the 4-UE viewer)"""
    if ue_id not in ("ue1", "ue2"):
        raise HTTPException(status_code=404, detail="Unknown UE")
    return await run_in_threadpool(_nonva_stats_fetch, ue_id)


@app.get("/viewer_status")
async def viewer_status():
    now_ms = time.time_ns() / 1_000_000.0
    return {"viewer": VA_VIEWER, "dl_ms": VA_VIEWER_DL_MS, "freeze": VA_VIEWER_QOS_FREEZE, "deadline_ms": VA_DEADLINE_MS,
            "ue": {u: st.meta(now_ms, False) for u, st in viewers.items()}}


VIEWER_PAGE = """<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>VA 기지국 뷰어</title>
<style>
:root { --bg:#0b0f14; --card:#121922; --line:#1f2a36; --text:#e6edf3; --muted:#8b98a5; --ok:#27c57a; --bad:#ff4d5e; --accent:#7aa2ff; }
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--text); font-family:-apple-system,"Segoe UI","Noto Sans KR","Malgun Gothic","Apple SD Gothic Neo",Roboto,sans-serif; }
header { display:flex; align-items:baseline; gap:14px; padding:14px 20px 4px; flex-wrap:wrap; }
header h1 { font-size:18px; font-weight:600; margin:0; letter-spacing:.2px; }
header .sub { color:var(--muted); font-size:13px; }
main { display:grid; grid-template-columns:repeat(auto-fit, minmax(460px, 1fr)); gap:16px; padding:10px 20px 20px; }
@media (max-width:520px) { main { grid-template-columns:1fr; padding:8px; } }
.pane { background:var(--card); border:1px solid var(--line); border-radius:14px; overflow:hidden; }
.top { display:flex; justify-content:space-between; align-items:center; padding:10px 14px; gap:10px; }
.top .ue { font-weight:600; font-size:15px; white-space:nowrap; }
.top .ue span { color:var(--muted); font-weight:400; font-size:13px; margin-left:8px; }
.chip { font-size:13px; font-weight:600; padding:4px 10px; border-radius:999px; background:#1b2634; color:var(--muted); white-space:nowrap; }
.chip.ok { background:rgba(39,197,122,.15); color:var(--ok); }
.chip.bad { background:rgba(255,77,94,.16); color:var(--bad); }
.stage { position:relative; background:#000; aspect-ratio:16/9; overflow:hidden; }
.stage canvas { position:absolute; inset:0; width:100%; height:100%; display:block; }
.stage.frozen.dim canvas { filter:saturate(.7) brightness(.8); }
.stage.frozen.dim img { filter:saturate(.7) brightness(.8); }
.badge { position:absolute; top:12px; left:12px; display:none; flex-direction:column; gap:2px; padding:8px 12px; border-radius:10px;
         background:rgba(255,77,94,.92); color:#fff; font-weight:700; font-size:15px; box-shadow:0 4px 18px rgba(0,0,0,.45); }
.badge.show { display:flex; }
.badge small { font-weight:500; font-size:12px; opacity:.92; }
.badge.grey { background:rgba(31,42,54,.92); color:var(--text); }
.wait { position:absolute; inset:0; display:none; align-items:center; justify-content:center; color:var(--muted); font-size:15px; background:rgba(11,15,20,.6); }
.wait.show { display:flex; }
.metrics { display:grid; grid-template-columns:repeat(6, 1fr); gap:1px; background:var(--line); border-top:1px solid var(--line); }
.m { background:var(--card); padding:9px 12px; min-width:0; }
.m .k { color:var(--muted); font-size:11px; letter-spacing:.4px; white-space:nowrap; }
.m .v { font-size:19px; font-weight:600; font-variant-numeric:tabular-nums; margin-top:2px; white-space:nowrap; }
.m .v small { font-size:11px; color:var(--muted); font-weight:400; margin-left:3px; }
.m.ok .v { color:var(--ok); } .m.bad .v { color:var(--bad); }
.foot { display:flex; justify-content:space-between; gap:12px; color:var(--muted); font-size:12px; padding:8px 14px; border-top:1px solid var(--line); flex-wrap:wrap; }
.stage img { position:absolute; inset:0; width:100%; height:100%; object-fit:contain; display:block; background:#000; }
main.four { grid-template-columns:repeat(2, minmax(0, 1fr)); }
@media (max-width:1000px) { main.four { grid-template-columns:1fr; } }
.metrics.four { grid-template-columns:repeat(4, 1fr); }
.metrics.five { grid-template-columns:repeat(5, 1fr); }
.hint { position:absolute; left:12px; bottom:12px; display:none; padding:6px 10px; border-radius:8px; background:rgba(11,15,20,.8); color:var(--muted); font-size:12px; }
.hint.show { display:block; }
.stage canvas.fit { object-fit:contain; }
/* DGIST 2026-11-14, fit to the window (VA_VIEWER_FIT): every pane on the screen at once. fitLayout() chooses the grid, the place of the figures (under the
   picture, or beside it: class side) and the picture height from the window size and writes them to the variables of main.fitv */
main.fitv { --sh:360px; --sw:640px; --pw:642px; --cols:2; --mcols:1; grid-template-columns:repeat(var(--cols), var(--pw)); justify-content:center; }
main.fitv .stage { aspect-ratio:auto; height:var(--sh); }
main.fitv .top, main.fitv .foot { flex-wrap:nowrap; }
main.fitv .top .ue, main.fitv .foot div, main.fitv .m .k { min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
main.fitv .topr, main.fitv .chip { flex-shrink:0; }
main.fitv.side .pane { display:grid; grid-template-columns:var(--sw) minmax(0, 1fr); grid-template-rows:auto var(--sh) auto; }
main.fitv.side .top, main.fitv.side .foot { grid-column:1 / 3; }
main.fitv.side .metrics { grid-template-columns:repeat(var(--mcols), minmax(0, 1fr)); grid-auto-rows:1fr; border-top:0; border-left:1px solid var(--line); }
main.fitv.side .m { display:flex; flex-direction:column; justify-content:center; }
main.fitv.side .m:last-child:nth-child(odd) { grid-column:1 / -1; }
.topr { display:flex; align-items:center; gap:8px; }
.recbar { margin-left:auto; display:flex; align-items:center; gap:8px; flex-wrap:wrap; }
.recstat { color:var(--muted); font-size:13px; font-variant-numeric:tabular-nums; }
.rb { display:inline-flex; align-items:center; gap:6px; font:inherit; font-size:13px; font-weight:600; padding:4px 11px; border-radius:999px; border:1px solid var(--line);
      background:#1b2634; color:var(--text); cursor:pointer; white-space:nowrap; font-variant-numeric:tabular-nums; }
.rb:hover:not(:disabled) { border-color:var(--muted); }
.rb:disabled { opacity:.45; cursor:default; }
.rb i { width:9px; height:9px; border-radius:50%; background:var(--bad); }
.rb.stop i, .rb.on i { border-radius:2px; }
.rb.on { background:rgba(255,77,94,.16); border-color:var(--bad); color:var(--bad); }
.recbar .rb { font-size:14px; padding:6px 14px; }
#recFiles { display:none; margin:0 20px 20px; padding:10px 14px; background:var(--card); border:1px solid var(--line); border-radius:14px; font-size:13px; }
#recFiles.show { display:block; }
.rfh { display:flex; align-items:center; gap:10px; flex-wrap:wrap; padding-bottom:8px; }
.rfh b { font-size:14px; }
.rfh span { color:var(--muted); flex:1; min-width:200px; }
.rf { display:flex; align-items:center; gap:12px; flex-wrap:wrap; padding:7px 0; border-top:1px solid var(--line); }
.rf a { color:var(--accent); font-weight:600; text-decoration:none; }
.rf a:hover { text-decoration:underline; }
.rf span { color:var(--muted); flex:1; min-width:200px; font-variant-numeric:tabular-nums; }
.rfn { color:var(--muted); padding:6px 0; border-top:1px solid var(--line); }
</style>
</head>
<body>
<header>
  <h1>VA 서비스 · 기지국 뷰어</h1>
  <div class="sub">VA: UE 카메라 → 5G 상향 → cuPHY 안의 YOLO(__VA_MODEL_VIEW__) → 검출 결과. QoS __QOS_DEADLINE_MS__ ms = 상향(실측) + 추론(실측) + 하향 __VA_VIEWER_DL_MS__ ms(고정), 미달 결과는 화면을 갱신하지 않음. non-VA: 720p 영상 중계(추론 없음)<span id="nvSub"></span>.</div>
</header>
<main id="panes"></main>
<section id="recFiles"></section>
<script>
const DEADLINE_MS = __QOS_DEADLINE_MS__, DL_MS = __VA_VIEWER_DL_MS__, DISPLAY_CONF = __VA_DISPLAY_CONF__, TARGET_FPS = __TARGET_FPS__;
const FREEZE = __VA_VIEWER_QOS_FREEZE__ ? true : false, BADGE = __VA_VIEWER_BADGE__ ? true : false;
const pathParts = location.pathname.split("/").filter(Boolean);   // ["viewer"] | ["viewer", "va"] | ["viewer", "ue1"]
const SHOW_NONVA = __VA_VIEWER_NONVA__ ? true : false, NONVA_PORT = __NONVA_PORT__;
const UES = pathParts.length > 1 && pathParts[1] !== "va" ? [pathParts[1]] : ["ue1", "ue2"];
const NV_UES = (pathParts.length > 1 || !SHOW_NONVA) ? [] : ["ue1", "ue2"];
const REC = __VA_VIEWER_REC__ ? true : false, REC_FPS = __VA_VIEWER_REC_FPS__, REC_MBPS = __VA_VIEWER_REC_MBPS__, REC_MAX_MIN = __VA_VIEWER_REC_MAX_MIN__;   // recording: see "recording" below
const REC_BADGE = __VA_VIEWER_REC_BADGE__ ? true : false, REC_PREFIX = "__VA_VIEWER_REC_PREFIX__";
const FIT = __VA_VIEWER_FIT__ ? true : false;   // every pane on one screen: fitLayout() below
const PALETTE = ["#7aa2ff", "#ffb547", "#27c57a", "#ff6b9d", "#4dd4e0", "#c084fc", "#f97316", "#a3e635"];
const colorCache = {};
function colorOf(name) {
    if (!(name in colorCache)) { let h = 0; for (let i = 0; i < name.length; i++) h = (h * 31 + name.charCodeAt(i)) >>> 0; colorCache[name] = PALETTE[h % PALETTE.length]; }
    return colorCache[name];
}
function roundRect(g, x, y, w, h, r) {
    g.beginPath(); g.moveTo(x + r, y); g.arcTo(x + w, y, x + w, y + h, r); g.arcTo(x + w, y + h, x, y + h, r); g.arcTo(x, y + h, x, y, r); g.arcTo(x, y, x + w, y, r); g.closePath();
}
function el(tag, cls, text) { const e = document.createElement(tag); if (cls) e.className = cls; if (text !== undefined) e.textContent = text; return e; }
function fmt(x, d) { return (x === null || x === undefined || isNaN(x)) ? "-" : Number(x).toFixed(d === undefined ? 0 : d); }

function makePane(ue) {
    const pane = el("div", "pane");
    const top = el("div", "top"); const title = el("div", "ue", "VA " + ue); title.appendChild(el("span", "", "FHD · YOLO __VA_MODEL__ · dApp (cuPHY 안)"));
    const chip = el("div", "chip", "대기"); top.appendChild(title); top.appendChild(chip);
    const stage = el("div", "stage"); const canvas = document.createElement("canvas"); canvas.width = 1920; canvas.height = 1080;
    const badge = el("div", "badge"); const badgeMain = el("div", "", "QoS 미달 · 화면 유지"); const badgeSub = el("small", "", ""); badge.appendChild(badgeMain); badge.appendChild(badgeSub);
    const wait = el("div", "wait show", "UE 대기 중"); stage.appendChild(canvas); stage.appendChild(badge); stage.appendChild(wait);
    const metrics = el("div", "metrics"); const tiles = {};
    for (const [k, label] of [["ul", "상향 (UL)"], ["ai", "추론 (AI)"], ["dl", "하향 (고정)"], ["sum", "합계 / " + DEADLINE_MS + " ms"], ["fps", "표시 fps"], ["pct", "QoS 충족률 (최근 100)"]]) {
        const t = el("div", "m"); t.appendChild(el("div", "k", label)); const v = el("div", "v", "-"); t.appendChild(v); tiles[k] = { box: t, v: v }; metrics.appendChild(t);
    }
    tiles.dl.v.textContent = fmt(DL_MS); const dlu = el("small", "", "ms"); tiles.dl.v.appendChild(dlu);
    const foot = el("div", "foot"); const footL = el("div", "", "-"); const footR = el("div", "", "-"); foot.appendChild(footL); foot.appendChild(footR);
    pane.appendChild(top); pane.appendChild(stage); pane.appendChild(metrics); pane.appendChild(foot);
    document.getElementById("panes").appendChild(pane);
    const p = { ue, chip, stage, canvas, ctx: canvas.getContext("2d"), badge, badgeMain, badgeSub, wait, tiles, footL, footR,
                meta: null, lastDrawAt: 0, draws: [], seq: 0, stat: 0, connected: false };
    recAttach(p, "va", top);
    return p;
}

function draw(p, bmp, meta) {
    const c = p.canvas;
    if (c.width !== bmp.width || c.height !== bmp.height) { c.width = bmp.width; c.height = bmp.height; }
    const g = p.ctx; g.drawImage(bmp, 0, 0); bmp.close();
    const s = Math.max(1, c.width / 960);
    for (const d of meta.dets || []) {
        const conf = Number(d.confidence); if (conf < DISPLAY_CONF) continue;
        const x = d.x1, y = d.y1, w = d.x2 - d.x1, h = d.y2 - d.y1, col = colorOf(d.class);
        g.lineWidth = 3 * s; g.strokeStyle = col; g.lineJoin = "round"; g.strokeRect(x, y, w, h);
        const label = d.class + " " + Math.round(conf * 100) + "%";
        g.font = "600 " + Math.round(15 * s) + "px -apple-system, 'Segoe UI', Roboto, sans-serif";
        const tw = g.measureText(label).width + 12 * s, th = 22 * s, ly = (y - th - 2 * s >= 0) ? y - th - 2 * s : y + 2 * s;
        g.fillStyle = col; roundRect(g, x, ly, tw, th, 5 * s); g.fill();
        g.fillStyle = "#0b0f14"; g.textBaseline = "middle"; g.fillText(label, x + 6 * s, ly + th / 2);
    }
    const now = performance.now(); p.lastDrawAt = now; p.draws.push(now); while (p.draws.length && p.draws[0] < now - 1000) p.draws.shift();
    recPic(p);
}

function applyMeta(p, meta) {
    p.meta = meta;
    const t = p.tiles, bad = meta.verdict === "missed" || meta.verdict === "fail";
    t.ul.v.textContent = meta.ul_known ? fmt(meta.ul_ms) : "?"; t.ai.v.textContent = fmt(meta.ai_ms); t.sum.v.textContent = fmt(meta.sum_ms);
    for (const k of ["ul", "ai", "dl"]) if (!t[k].v.querySelector("small")) t[k].v.appendChild(el("small", "", "ms"));
    if (!t.sum.v.querySelector("small")) t.sum.v.appendChild(el("small", "", "ms"));
    t.sum.box.className = "m " + (meta.verdict === "none" ? "" : (bad ? "bad" : "ok"));
    t.pct.v.textContent = meta.met_pct === null || meta.met_pct === undefined ? "-" : fmt(meta.met_pct, 0) + "%";
    t.pct.box.className = "m " + (meta.met_pct === null ? "" : (meta.met_pct >= 90 ? "ok" : (meta.met_pct < 70 ? "bad" : "")));
    p.footL.textContent = "모델 " + (meta.model || "-") + (meta.sm ? " · " + meta.sm + " SM" : "") + " · 결과 " + meta.results + " · 충족 " + meta.met + " · 미달 " + meta.missed + (meta.failed ? " · 실패 " + meta.failed : "") + (meta.stale ? " · 순서 뒤짐 " + meta.stale : "");
    p.footR.textContent = (meta.mbps ? "상향 " + fmt(meta.mbps, 1) + " Mbps · " + fmt(meta.req_s, 1) + " req/s · " + fmt(meta.kb) + " KB · " : "") + "화면 #" + meta.seq + (meta.w ? " · " + meta.w + "x" + meta.h : "");
}

function tick() {
    const now = performance.now();
    for (const p of panes) {
        const meta = p.meta; const held = p.lastDrawAt ? now - p.lastDrawAt : 0;
        while (p.draws.length && p.draws[0] < now - 1000) p.draws.shift();
        p.tiles.fps.v.textContent = p.lastDrawAt ? String(p.draws.length) : "-";
        const active = !!(meta && meta.active) && p.connected;
        p.wait.className = "wait" + ((!active && !(meta && meta.seq)) || !p.connected ? " show" : "");
        p.wait.textContent = p.connected ? "UE 대기 중" : "서버 연결 끊김";
        const missed = active && !!meta && (meta.verdict === "missed" || meta.verdict === "fail") && FREEZE;   // the UE is sending, its results miss
        const stalled = !!(meta && meta.seq) && p.lastDrawAt > 0 && held > Math.max(1000, DEADLINE_MS + 3000 / Math.max(1, TARGET_FPS));   // no new picture for a while
        const frozen = missed || stalled;
        p.stage.className = "stage" + (frozen ? " frozen" : "") + (BADGE ? " dim" : "");
        if (BADGE && frozen && meta && meta.seq) {
            p.badge.className = "badge show" + (missed ? "" : " grey");
            if (missed) {
                p.badgeMain.textContent = (meta.verdict === "fail" ? "추론 실패 (" + (meta.reason || "fail") + ")" : "QoS 미달") + " · 화면 유지 " + (held / 1000).toFixed(2) + " s";
                p.badgeSub.textContent = meta.verdict === "fail" ? "연속 " + meta.streak + "회" :
                    "UL " + fmt(meta.ul_ms) + " + AI " + fmt(meta.ai_ms) + " + DL " + fmt(meta.dl_ms) + " = " + fmt(meta.sum_ms) + " ms > " + meta.deadline_ms + " ms · 연속 " + meta.streak + "회";
            } else {
                p.badgeMain.textContent = (active ? "갱신 없음" : "UE 송신 없음") + " · " + (held / 1000).toFixed(1) + " s";
                p.badgeSub.textContent = active ? "새 결과가 아직 없습니다" : "마지막 결과 " + (meta.since_last_ms !== null && meta.since_last_ms !== undefined ? (meta.since_last_ms / 1000).toFixed(0) + " s 전" : "-");
            }
        } else p.badge.className = "badge";
        if (!p.connected) { p.chip.textContent = "연결 끊김"; p.chip.className = "chip bad"; }
        else if (!active) { p.chip.textContent = meta && meta.seq ? "UE 정지" : "대기"; p.chip.className = "chip"; }
        else if (missed) { p.chip.textContent = "QoS 미달 · 정지"; p.chip.className = "chip bad"; }
        else { p.chip.textContent = "QoS 충족 · 실시간"; p.chip.className = "chip ok"; }
    }
}

async function loop(p) {
    while (true) {
        try {
            const r = await fetch("/viewer_frame/" + encodeURIComponent(p.ue) + "?seq=" + p.seq + "&stat=" + p.stat + "&wait=1000", { cache: "no-store" });
            if (r.status !== 200 && r.status !== 204) throw new Error("HTTP " + r.status);
            p.connected = true;
            const meta = JSON.parse(r.headers.get("X-VA-Meta") || "{}");
            if (r.status === 200) { const bmp = await createImageBitmap(await r.blob()); draw(p, bmp, meta); p.seq = meta.seq; }
            if (meta.stat_seq !== undefined) { p.stat = meta.stat_seq; applyMeta(p, meta); }
            tick();
        } catch (e) { p.connected = false; await new Promise(res => setTimeout(res, 700)); }
    }
}

// non-VA panes: the non-VA service's MJPEG relay (same host, port NONVA_PORT) in an <img> - with REC read by nonvaStream() and painted on a
// canvas -, statistics through /nonva_stats/<ue> (proxied here)
function makeNonvaPane(ue) {
    const pane = el("div", "pane");
    const top = el("div", "top"); const title = el("div", "ue", "non-VA " + ue); title.appendChild(el("span", "", "720p 영상 중계 · 추론 없음"));
    const chip = el("div", "chip", "대기"); top.appendChild(title); top.appendChild(chip);
    const stage = el("div", "stage"); const img = document.createElement("img"); img.alt = "";
    const canvas = document.createElement("canvas"); canvas.className = "fit"; canvas.width = 1280; canvas.height = 720; stage.appendChild(REC ? canvas : img);
    const badge = el("div", "badge"); const badgeMain = el("div", "", ""); const badgeSub = el("small", "", ""); badge.appendChild(badgeMain); badge.appendChild(badgeSub); stage.appendChild(badge);   // QoS badge as on the VA panes (nvTick)
    const wait = el("div", "wait show", "UE 대기 중"); stage.appendChild(wait);
    const hint = el("div", "hint", "영상이 안 보이면 https://" + location.hostname + ":" + NONVA_PORT + "/viewer/" + ue + " 를 한 번 열어 인증서를 수락하세요"); stage.appendChild(hint);
    const metrics = el("div", "metrics five"); const tiles = {};
    for (const [k, label] of [["fps", "수신 fps"], ["mbps", "상향 Mbps"], ["kb", "프레임 KB"], ["ul", "상향 지연 (UL)"], ["pct", "QoS 충족률 (최근 100)"]]) {
        const t = el("div", "m"); const kEl = el("div", "k", label); t.appendChild(kEl); const v = el("div", "v", "-"); t.appendChild(v); tiles[k] = { box: t, v: v, k: kEl }; metrics.appendChild(t);
    }
    const foot = el("div", "foot"); const footL = el("div", "", "-"); const footR = el("div", "", "non-VA 서비스 :" + NONVA_PORT); foot.appendChild(footL); foot.appendChild(footR);
    pane.appendChild(top); pane.appendChild(stage); pane.appendChild(metrics); pane.appendChild(foot);
    document.getElementById("panes").appendChild(pane);
    const p = { ue, chip, stage, img, wait, hint, tiles, footL, active: false, connected: false, loaded: false, srcSetAt: 0, retryAt: 0, lastFrameId: -1, lastChangeAt: 0,
                canvas, ctx: canvas.getContext("2d"), imgMode: !REC, abort: null, fails: 0, streamed: false,
                badge, badgeMain, badgeSub, q: null, qAt: 0, lastPicAt: 0, statsOk: false, gone: false, relayFps: 15, waitNone: false };   // q = QoS state from the non-VA service (nvQos)
    img.onload = () => { p.loaded = true; };
    img.onerror = () => { p.loaded = false; p.connected = false; };
    recAttach(p, "nonva", top);
    return p;
}
function nonvaFeed(p) {   // (re)open the MJPEG relay; a new UE session gets a new stream
    const now = Date.now(); if (now < p.retryAt) return;
    p.retryAt = now + 2000; p.connected = true; p.loaded = false; p.srcSetAt = now;
    const url = "https://" + location.hostname + ":" + NONVA_PORT + "/video_feed?session_id=" + encodeURIComponent(p.ue) + "&t=" + now;
    if (p.imgMode) p.img.src = url; else nonvaStream(p, url);
}
// REC: the relay is read with fetch() and painted on a canvas - a picture an <img> takes from another origin cannot be copied into a recording.
// Still one stream per pane, the same bytes the <img> took. Parts: "--frame", headers with Content-Length, an empty line, the JPEG.
async function nonvaStream(p, url) {
    if (p.abort) p.abort.abort();
    const ac = p.abort = new AbortController(), dec = new TextDecoder("latin1");
    let buf = new Uint8Array(1 << 20), len = 0, next = null, busy = false, got = false;
    const paint = async () => {   // one decode at a time; of the frames that arrive meanwhile only the newest is kept
        if (busy) return;
        busy = true;
        while (next) {
            const item = next; next = null;
            try {
                const bmp = await createImageBitmap(new Blob([item.jpeg], { type: "image/jpeg" }));
                if (p.abort === ac) { const c = p.canvas; if (c.width !== bmp.width || c.height !== bmp.height) { c.width = bmp.width; c.height = bmp.height; } p.ctx.drawImage(bmp, 0, 0); p.loaded = true; p.streamed = true; p.lastPicAt = item.at; recPic(p); }
                bmp.close();
            } catch (e) {}
        }
        busy = false;
    };
    try {
        const r = await fetch(url, { cache: "no-store", signal: ac.signal });
        if (!r.ok) throw new Error("HTTP " + r.status);
        got = true;   // readable from this page, also when no part follows (2026-11-14: a session whose frames all miss the deadline sends none)
        const rd = r.body.getReader();
        while (true) {
            const { done, value } = await rd.read();
            if (done) break;
            if (len + value.length > buf.length) { const b = new Uint8Array(Math.max(2 * buf.length, len + value.length)); b.set(buf.subarray(0, len)); buf = b; }
            buf.set(value, len); len += value.length;
            while (true) {
                let h = -1;
                for (let i = 0; i + 3 < len; i++) if (buf[i] === 13 && buf[i + 1] === 10 && buf[i + 2] === 13 && buf[i + 3] === 10) { h = i; break; }
                if (h < 0) break;
                const head = dec.decode(buf.subarray(0, h)), m = /content-length:[ ]*([0-9]+)/i.exec(head), end = h + 4 + (m ? Number(m[1]) : 0);
                if (len < end) break;
                if (m) {   // 2026-11-14: every part carries the QoS state of the non-VA service (X-NV-Meta; absent with an older service)
                    let meta = null; const x = /x-nv-meta:[ ]*([ -~]+)/i.exec(head);
                    if (x) { try { meta = JSON.parse(x[1]); } catch (e) {} }
                    if (meta) nvQos(p, meta);
                    if (!meta || meta.pic !== false) {   // "pic": false = the held frame once more, sent for the verdict alone
                        next = { jpeg: buf.slice(h + 4, end), at: performance.now() - (meta && meta.held_ms ? meta.held_ms : 0) }; paint();
                    }
                }
                buf.copyWithin(0, end, len); len -= end;
            }
        }
    } catch (e) {}
    if (p.abort !== ac) return;
    p.abort = null; p.connected = false;   // ended (UE stopped, new session) or failed: nonvaLoop opens it again
    if (got) p.fails = 0;
    else if (++p.fails >= 3 && !p.streamed) {   // never readable from this page (non-VA service older than this page, or its certificate not accepted yet): show it in an <img> as before, without recording
        p.imgMode = true; p.recOff = true; p.stage.replaceChild(p.img, p.canvas);
        if (p.rec) recStop([p]);
        recNote("non-VA " + p.ue + ": 영상 중계를 이 페이지에서 직접 읽지 못해 이 칸은 녹화할 수 없습니다. https://" + location.hostname + ":" + NONVA_PORT + "/viewer/" + p.ue + " 를 열어 인증서를 수락했는지, non-VA 서비스를 다시 시작했는지(./nonva.sh restart) 확인한 뒤 새로 고치세요.");
        recUi();
    }
}
async function nonvaLoop(p) {
    while (true) {
        try {
            const r = await fetch("/nonva_stats/" + encodeURIComponent(p.ue) + "?t=" + Date.now(), { cache: "no-store" });
            const d = await r.json();
            p.gone = false;
            if (d.ok) {
                const wasActive = p.active; p.active = !!d.active; p.statsOk = true; p.relayFps = d.viewer_target_fps || 15;
                if (d.frame_id !== p.lastFrameId) { p.lastFrameId = d.frame_id; p.lastChangeAt = Date.now(); }
                p.tiles.fps.v.textContent = fmt(d.receive_fps, 1); p.tiles.mbps.v.textContent = fmt(d.receive_mbps, 1);
                p.tiles.kb.v.textContent = fmt(d.frame_size_kb);
                if (d.qos) nvQos(p, d.qos);
                else { p.tiles.ul.v.textContent = fmt(d.ul_latency_ms); p.tiles.ul.v.appendChild(el("small", "", "ms")); }   // a non-VA service older than this page: no verdict
                const q = p.q;
                p.footL.textContent = "프레임 #" + d.frame_id + " · 중계 " + fmt(d.viewer_target_fps) + " fps" + (q ? " · 충족 " + q.met + " · 미달 " + q.missed + (q.stale ? " · 순서 뒤짐 " + q.stale : "") : "");
                if (p.active && (!wasActive || !p.connected)) nonvaFeed(p);
                if (!q) { p.chip.textContent = p.active ? "송출 중" : (d.frame_id > 0 ? "UE 정지" : "대기"); p.chip.className = "chip" + (p.active ? " ok" : ""); }   // with a verdict the chip is nvTick's
                const none = !!(q && q.freeze && q.shown_seq === 0 && q.qseq > 0);   // frames arrive, but none has met the deadline yet: there is no picture to show
                p.wait.className = "wait" + (none || !(p.active || d.frame_id > 0) ? " show" : "");
                p.wait.textContent = none ? "QoS를 충족한 프레임이 아직 없습니다" : "UE 대기 중"; p.waitNone = none;
                p.hint.className = "hint" + (!none && p.active && !p.loaded && (p.fails >= 2 || (p.connected && Date.now() - p.srcSetAt > 4000)) ? " show" : "");
            } else {
                p.statsOk = false; p.chip.textContent = "non-VA 서비스 없음"; p.chip.className = "chip bad"; p.wait.className = "wait show"; p.wait.textContent = "non-VA 세션 없음";
            }
        } catch (e) { p.gone = true; p.chip.textContent = "연결 끊김"; p.chip.className = "chip bad"; }
        await new Promise(res => setTimeout(res, 1000));
    }
}
// QoS of the non-VA panes (2026-11-14). The non-VA service judges every frame - its measured UL latency against NONVA_DEADLINE_MS - and relays only
// the frames that met it, so the picture stands still while frames miss. Its state comes with every part of the relay (X-NV-Meta) and with the
// statistics (once a second; the only source when the relay is shown in an <img>); the newer one is kept.
let nvSubDone = false;
function nvQos(p, q) {
    if (p.q && q.sid === p.q.sid && q.qseq < p.q.qseq) return;
    p.q = q; p.qAt = performance.now();
    const t = p.tiles;
    t.ul.k.textContent = "상향 지연 / " + q.deadline_ms + " ms";
    t.ul.v.textContent = fmt(q.ul_ms); t.ul.v.appendChild(el("small", "", "ms"));
    t.ul.box.className = "m " + (q.verdict === "met" ? "ok" : (q.verdict === "missed" ? "bad" : ""));
    t.pct.v.textContent = q.met_pct === null || q.met_pct === undefined ? "-" : fmt(q.met_pct, 0) + "%";
    t.pct.box.className = "m " + (q.met_pct === null || q.met_pct === undefined ? "" : (q.met_pct >= 90 ? "ok" : (q.met_pct < 70 ? "bad" : "")));
    if (!nvSubDone) { nvSubDone = true; document.getElementById("nvSub").textContent = ", QoS " + q.deadline_ms + " ms = 상향(실측)" + (q.freeze ? ", 미달 프레임은 화면을 갱신하지 않음" : ""); }
}
function nvTick() {   // every 100 ms: badge, dimmed picture and chip of the non-VA panes, the way tick() does it for the VA panes
    const now = performance.now();
    for (const p of nvPanes) {
        const q = p.q;
        if (!q) continue;   // nothing received yet, or a non-VA service without the verdict: nonvaLoop sets the chip
        const pic = p.imgMode ? q.shown_seq > 0 : p.lastPicAt > 0;
        if (p.waitNone && q.shown_seq > 0) { p.waitNone = false; p.wait.className = "wait"; }   // the first frame that met the deadline is here: do not wait for the next statistics
        const held = !pic ? 0 : (p.imgMode ? q.held_ms + (now - p.qAt) : now - p.lastPicAt);
        const active = p.active && p.statsOk && !p.gone;
        const missed = active && q.verdict === "missed" && !!q.freeze;   // the UE is sending, its frames arrive too late
        const stalled = pic && held > Math.max(1000, q.deadline_ms + 3000 / Math.max(1, p.relayFps)) + (p.imgMode ? 1500 : 0);   // no new picture for a while (in an <img> the age comes with the statistics, once a second)
        const frozen = missed || stalled;
        p.stage.className = "stage" + (frozen ? " frozen" : "") + (BADGE ? " dim" : "");
        if (BADGE && frozen && pic) {
            p.badge.className = "badge show" + (missed ? "" : " grey");
            if (missed) {
                p.badgeMain.textContent = "QoS 미달 · 화면 유지 " + (held / 1000).toFixed(2) + " s";
                p.badgeSub.textContent = (q.ul_ms === null ? "UL 측정 불가" : "UL " + fmt(q.ul_ms) + " ms > " + q.deadline_ms + " ms") + " · 연속 " + q.streak + "회";
            } else {
                p.badgeMain.textContent = (active ? "갱신 없음" : "UE 송신 없음") + " · " + (held / 1000).toFixed(1) + " s";
                p.badgeSub.textContent = active ? "새 프레임이 아직 없습니다" : (p.lastChangeAt ? "마지막 프레임 " + ((Date.now() - p.lastChangeAt) / 1000).toFixed(0) + " s 전" : "");
            }
        } else p.badge.className = "badge";
        if (p.gone) { p.chip.textContent = "연결 끊김"; p.chip.className = "chip bad"; }
        else if (!p.statsOk) { p.chip.textContent = "non-VA 서비스 없음"; p.chip.className = "chip bad"; }
        else if (!p.active) { p.chip.textContent = p.lastFrameId > 0 ? "UE 정지" : "대기"; p.chip.className = "chip"; }
        else if (q.verdict === "missed") { p.chip.textContent = q.freeze ? "QoS 미달 · 정지" : "QoS 미달"; p.chip.className = "chip bad"; }
        else { p.chip.textContent = "QoS 충족 · 실시간"; p.chip.className = "chip ok"; }
    }
}

// ---- recording (2026-11-13) --------------------------------------------------------------------------------------------------------------
// "전체 녹화" / "녹화 시작" record a pane as its picture area shows it - the frame with its boxes (VA) or the relayed video (non-VA), fitted
// into 1920x1080 with black bars, plus the QoS badge / dimming / waiting text (REC_BADGE) - one file per pane. Everything happens in this
// browser and off this page's thread: each recording has a Web Worker that gets a copy of the pane's canvas whenever the pane draws a new
// picture (recPic) and the overlay state whenever it changes (recOv), puts a frame on every slot of a fixed REC_FPS grid (the previous frame
// again while nothing changed), encodes H.264 with WebCodecs and writes a plain MP4 (index in front, no fragments), the form players, editors
// and slide tools open. The server and the UEs take no part: recording adds no uplink and no downlink traffic. Without WebCodecs H.264 the
// browser's own MediaRecorder is used on this thread instead (its MP4 is fragmented; WebM as the last resort) and the file list says so.
const REC_W = 1920, REC_H = 1080, REC_TS = 90000, REC_FONT = getComputedStyle(document.body).fontFamily;   // REC_TS: MP4 time scale, ticks per second
const recPanes = [], recs = [], recFiles = [], recNotes = [];   // panes with a record button | recordings in progress | finished files | messages
let recEngine = null, recWorkerUrl = null, recUiAt = 0, recAllBtn = null, recStopBtn = null, recStat = null;
const recNow = () => performance.timeOrigin + performance.now();   // ms on one clock for this page and its workers

// MP4 writer: ftyp, moov (one H.264 video track, one sample per chunk), mdat
function mp4Str(s) { const a = new Uint8Array(s.length); for (let i = 0; i < s.length; i++) a[i] = s.charCodeAt(i); return a; }
function mp4U32() { const a = new Uint8Array(4 * arguments.length), v = new DataView(a.buffer); for (let i = 0; i < arguments.length; i++) v.setUint32(4 * i, arguments[i] >>> 0); return a; }
function mp4U16() { const a = new Uint8Array(2 * arguments.length), v = new DataView(a.buffer); for (let i = 0; i < arguments.length; i++) v.setUint16(2 * i, arguments[i]); return a; }
function mp4Box(type) {   // size, type, then the byte arrays given
    let n = 8; for (let i = 1; i < arguments.length; i++) n += arguments[i].length;
    const a = new Uint8Array(n); new DataView(a.buffer).setUint32(0, n); a.set(mp4Str(type), 4);
    let o = 8; for (let i = 1; i < arguments.length; i++) { a.set(arguments[i], o); o += arguments[i].length; }
    return a;
}
function mp4Table(head, vals, wide) {   // version/flags 0, the head words, then one 32-bit (wide: 64-bit) word per value
    const w = wide ? 8 : 4, a = new Uint8Array(4 + 4 * head.length + w * vals.length), v = new DataView(a.buffer);
    let o = 4; for (const h of head) { v.setUint32(o, h); o += 4; }
    for (const x of vals) { if (wide) { v.setUint32(o, Math.floor(x / 4294967296)); v.setUint32(o + 4, x >>> 0); } else v.setUint32(o, x); o += w; }
    return a;
}
function mp4Moov(r, dataAt, wide) {   // r: sizes, durs (REC_TS ticks), keys (sample numbers from 1), desc (avcC payload), color | dataAt: file offset of the first sample
    const n = r.sizes.length, now = Math.floor(Date.now() / 1000) + 2082844800, unity = mp4U32(0x10000, 0, 0, 0, 0x10000, 0, 0, 0, 0x40000000);
    let total = 0; const runs = [];
    for (const d of r.durs) { total += d; if (runs.length && runs[runs.length - 1] === d) runs[runs.length - 2]++; else runs.push(1, d); }
    const offs = new Array(n); let o = dataAt; for (let i = 0; i < n; i++) { offs[i] = o; o += r.sizes[i]; }
    const c = r.color, colr = c ? [mp4Box("colr", mp4Str("nclx"), mp4U16(c[0], c[1], c[2]), new Uint8Array([c[3] ? 0x80 : 0]))] : [];
    const avc1 = mp4Box("avc1", new Uint8Array(6), mp4U16(1, 0, 0), mp4U32(0, 0, 0), mp4U16(REC_W, REC_H), mp4U32(0x480000, 0x480000, 0), mp4U16(1), new Uint8Array(32), mp4U16(0x18, 0xffff),
                        mp4Box("avcC", r.desc), ...colr, mp4Box("pasp", mp4U32(1, 1)));
    const stbl = mp4Box("stbl", mp4Box("stsd", mp4U32(0, 1), avc1), mp4Box("stts", mp4Table([runs.length / 2], runs)), mp4Box("stss", mp4Table([r.keys.length], r.keys)),
                        mp4Box("stsc", mp4U32(0, 1, 1, 1, 1)), mp4Box("stsz", mp4Table([0, n], r.sizes)), mp4Box(wide ? "co64" : "stco", mp4Table([n], offs, wide)));
    const minf = mp4Box("minf", mp4Box("vmhd", mp4U32(1), mp4U16(0, 0, 0, 0)), mp4Box("dinf", mp4Box("dref", mp4U32(0, 1), mp4Box("url ", mp4U32(1)))), stbl);
    const mdia = mp4Box("mdia", mp4Box("mdhd", mp4U32(0, now, now, REC_TS, total), mp4U16(0x55c4, 0)),
                        mp4Box("hdlr", mp4U32(0, 0), mp4Str("vide"), mp4U32(0, 0, 0), mp4Str("VideoHandler"), new Uint8Array(1)), minf);
    const trak = mp4Box("trak", mp4Box("tkhd", mp4U32(3, now, now, 1, 0, total, 0, 0), mp4U16(0, 0, 0, 0), unity, mp4U32(REC_W << 16, REC_H << 16)), mdia);
    return mp4Box("moov", mp4Box("mvhd", mp4U32(0, now, now, REC_TS, total, 0x10000), mp4U16(0x100, 0), mp4U32(0, 0), unity, mp4U32(0, 0, 0, 0, 0, 0, 2)), trak);
}
function mp4File(r, forceWide) {   // -> Blob; 64-bit chunk offsets and mdat size only past 4 GB
    let bytes = 0; for (const s of r.sizes) bytes += s;
    const ftyp = mp4Box("ftyp", mp4Str("isom"), mp4U32(0x200), mp4Str("isomiso2avc1mp41"));
    const wide = !!forceWide || bytes + 16 * r.sizes.length + 65536 > 0xffffffff, head = wide ? 16 : 8;
    const moov = mp4Moov(r, ftyp.length + mp4Moov(r, 0, wide).length + head, wide);
    const mdat = new Uint8Array(head), v = new DataView(mdat.buffer); mdat.set(mp4Str("mdat"), 4);
    if (wide) { v.setUint32(0, 1); v.setUint32(8, Math.floor((bytes + 16) / 4294967296)); v.setUint32(12, (bytes + 16) >>> 0); } else v.setUint32(0, bytes + 8);
    return new Blob([ftyp, moov, mdat].concat(r.parts), { type: "video/mp4" });
}

function recPicture(g, src, dim) {   // the pane's picture fitted into 1920x1080; dim = the pane shows it frozen (.stage.frozen.dim)
    const k = Math.min(REC_W / src.width, REC_H / src.height), w = Math.round(src.width * k), h = Math.round(src.height * k), x = (REC_W - w) >> 1, y = (REC_H - h) >> 1;
    g.fillStyle = "#000"; g.fillRect(0, 0, REC_W, REC_H);
    const filter = dim && typeof g.filter === "string";
    if (filter) g.filter = "saturate(.7) brightness(.8)";
    g.drawImage(src, x, y, w, h);
    if (filter) g.filter = "none"; else if (dim) { g.fillStyle = "rgba(0,0,0,.2)"; g.fillRect(x, y, w, h); }
}
function recLayer(g, ov, font) {   // what the pane lays over its picture: the badge at twice its size (a pane is about 960 px wide on the screen), the waiting text
    if (ov.badge) {
        const a = ov.badge.a, b = ov.badge.b, fa = "700 30px " + font, fb = "500 24px " + font;
        g.font = fa; const wa = g.measureText(a).width; g.font = fb; const wb = b ? g.measureText(b).width : 0;
        g.fillStyle = ov.badge.grey ? "rgba(31,42,54,.92)" : "rgba(255,77,94,.92)"; roundRect(g, 24, 24, Math.max(wa, wb) + 48, b ? 104 : 68, 20); g.fill();
        g.fillStyle = ov.badge.grey ? "#e6edf3" : "#fff"; g.textBaseline = "middle"; g.font = fa; g.fillText(a, 48, 59);
        if (b) { g.globalAlpha = .92; g.font = fb; g.fillText(b, 48, 99); g.globalAlpha = 1; }
    }
    if (ov.wait) {
        g.fillStyle = "rgba(11,15,20,.6)"; g.fillRect(0, 0, REC_W, REC_H);
        g.fillStyle = "#8b98a5"; g.font = "400 30px " + font; g.textBaseline = "middle"; g.textAlign = "center"; g.fillText(ov.wait, REC_W / 2, REC_H / 2); g.textAlign = "left";
    }
}
function recWorker() {   // the body of a recording's Web Worker (recWorkerSrc): messages init, pic, ov, stop in; stat, max, done out
    const base = new OffscreenCanvas(REC_W, REC_H), bg = base.getContext("2d", { alpha: false }), canvas = new OffscreenCanvas(REC_W, REC_H), g = canvas.getContext("2d", { alpha: false });
    const r = { n: -1, keyN: -1e9, frames: 0, held: 0, bytes: 0, sizes: [], ts: [], keys: [], parts: [], cur: [], curBytes: 0, desc: null, color: null, error: null };
    let t0 = 0, enc = null, src = null, ov = {}, picDirty = false, dirty = false, last = null, timer = 0, stopSlot = -1, done = false, statAt = 0, maxSent = false;
    const now = () => performance.timeOrigin + performance.now();
    const group = () => { if (r.curBytes) { r.parts.push(new Blob(r.cur)); r.cur = []; r.curBytes = 0; } };   // one Blob per key-frame group: the browser can keep large Blobs out of RAM
    const color = cs => {   // the stream's colour description as the code points of the MP4 'colr' box (ISO/IEC 23091-2)
        const P = { bt709: 1, bt470bg: 5, smpte170m: 6 }, T = { bt709: 1, smpte170m: 6, "iec61966-2-1": 13 }, M = { rgb: 0, bt709: 1, bt470bg: 5, smpte170m: 6 };
        return (cs && cs.primaries in P && cs.transfer in T && cs.matrix in M) ? [P[cs.primaries], T[cs.transfer], M[cs.matrix], !!cs.fullRange] : null;
    };
    const output = (chunk, meta) => {   // chunks arrive in presentation order (WebCodecs encoders do not reorder frames)
        const d = meta && meta.decoderConfig && meta.decoderConfig.description;
        if (d && !r.desc) { r.desc = new Uint8Array(d.buffer ? d.buffer.slice(d.byteOffset, d.byteOffset + d.byteLength) : d.slice(0)); r.color = color(meta.decoderConfig.colorSpace); }
        const b = new Uint8Array(chunk.byteLength); chunk.copyTo(b);
        if (chunk.type === "key") { group(); r.keys.push(r.sizes.length + 1); }
        r.cur.push(b); r.curBytes += b.length; r.bytes += b.length; r.sizes.push(b.length); r.ts.push(Math.round(chunk.timestamp * REC_TS / 1e6));
    };
    const finish = async () => {   // -> done: the file ends with slot stopSlot (the same for every recording stopped together)
        if (done) return;
        done = true; clearInterval(timer);
        let blob = null;
        try { if (enc && enc.state === "configured") await enc.flush(); } catch (e) { if (!r.error) r.error = e.message || String(e); }
        try { if (enc && enc.state !== "closed") enc.close(); } catch (e) {}
        const end = Math.max(r.n + 1, stopSlot);
        try {
            if (r.sizes.length && r.desc) {
                group();
                const endT = Math.round(end * REC_TS / REC_FPS);   // the last frame stays until the end
                r.durs = r.ts.map((t, i) => Math.max(1, (i + 1 < r.ts.length ? r.ts[i + 1] : endT) - t));
                blob = mp4File(r);
            }
        } catch (e) { if (!r.error) r.error = e.message || String(e); }
        postMessage({ t: "done", blob, secs: end / REC_FPS, frames: r.frames, held: r.held, error: r.error });
    };
    const fail = e => { if (!r.error) r.error = (e && e.message) || String(e); finish(); };
    const tick = () => {   // every few ms: the frame of the grid slot that has begun
        if (done) return;
        const t = now(), n = Math.floor((t - t0) * REC_FPS / 1000);
        if (stopSlot >= 0 && n >= stopSlot) { finish(); return; }
        if (t - statAt >= 100) { statAt = t; postMessage({ t: "stat", n: r.n, frames: r.frames, held: r.held, bytes: r.bytes }); }
        if (!src || n <= r.n) return;
        if (REC_MAX_MIN > 0 && t - t0 >= REC_MAX_MIN * 60000) { if (!maxSent) { maxSent = true; postMessage({ t: "max" }); } return; }
        if (enc.encodeQueueSize > 2) return;   // the encoder is behind: no frame for this slot, the previous one stays on (counted in held)
        try {
            const ts = r.n < 0 ? 0 : Math.round(n * 1e6 / REC_FPS), key = n - r.keyN >= 2 * REC_FPS;   // the first frame starts the file
            let f;
            if (dirty || !last) {
                const plain = !ov.badge && !ov.wait;
                if (picDirty) { recPicture(bg, src, !!ov.dim); picDirty = false; }
                if (!plain) { g.drawImage(base, 0, 0); recLayer(g, ov, REC_FONT); }
                f = new VideoFrame(plain ? base : canvas, { timestamp: ts }); dirty = false;
            } else f = new VideoFrame(last, { timestamp: ts });   // nothing changed since the last slot
            if (key) r.keyN = n;
            enc.encode(f, { keyFrame: key });
            if (last) last.close();
            last = f;
        } catch (e) { fail(e); return; }
        if (r.n >= 0) r.held += n - r.n - 1;
        r.n = n; r.frames++;
    };
    onmessage = e => {
        const m = e.data;
        if (m.t === "pic") { if (src) src.close(); src = m.bmp; picDirty = dirty = true; }
        else if (m.t === "ov") { if (!!m.ov.dim !== !!ov.dim) picDirty = true; ov = m.ov; dirty = true; }
        else if (m.t === "stop") { stopSlot = Math.max(1, Math.floor((m.end - t0) * REC_FPS / 1000)); tick(); }
        else if (m.t === "init") {
            t0 = m.t0;
            try { enc = new VideoEncoder({ output, error: fail }); enc.configure(m.config); } catch (x) { fail(x); return; }
            timer = setInterval(tick, Math.max(4, Math.floor(250 / REC_FPS)));
        }
    };
}
function recWorkerSrc() {   // the worker's script: the constants and functions it uses, as text
    const C = { REC_W, REC_H, REC_TS, REC_FPS, REC_MAX_MIN, REC_FONT };
    return Object.keys(C).map(k => "const " + k + " = " + JSON.stringify(C[k]) + ";").join(" ") +
           [mp4Str, mp4U32, mp4U16, mp4Box, mp4Table, mp4Moov, mp4File, roundRect, recPicture, recLayer, recWorker].map(String).join("; ") + "; recWorker();";
}

function recConfig(codec) { return { codec, width: REC_W, height: REC_H, bitrate: Math.round(REC_MBPS * 1e6), framerate: REC_FPS, latencyMode: "quality", avc: { format: "avc" } }; }
async function recProbe() {   // -> how this browser can record, or null
    const force = new URLSearchParams(location.search).get("rec");   // ?rec=mr | webm picks a fallback on purpose (tests)
    if (!force && window.VideoEncoder && window.VideoFrame && window.Worker && window.OffscreenCanvas) {
        const lv = REC_FPS > 30 ? "2a" : "28";   // H.264 level 4.0 carries 1080p30, 4.2 1080p60
        for (const codec of ["avc1.6400" + lv, "avc1.4d00" + lv, "avc1.42e0" + lv]) {   // High / Main / Constrained Baseline profile
            try { if ((await VideoEncoder.isConfigSupported(recConfig(codec))).supported) return { kind: "wc", codec, ext: "mp4", label: "H.264 MP4" }; } catch (e) {}
        }
    }
    if (window.MediaRecorder && HTMLCanvasElement.prototype.captureStream) {
        for (const [mime, ext, label] of [["video/mp4;codecs=avc1", "mp4", "H.264 MP4 (조각 MP4)"],
                                          ["video/webm;codecs=vp9", "webm", "WebM (MP4 미지원 브라우저)"], ["video/webm", "webm", "WebM (MP4 미지원 브라우저)"]]) {
            if (force === "webm" && ext !== "webm") continue;
            if (MediaRecorder.isTypeSupported(mime)) return { kind: "mr", mime, ext, label };
        }
    }
    return null;
}
function recOverlay(p) {   // what the pane lays over its picture right now, as plain data (read from the page: the recording shows what the screen shows)
    const ov = { dim: false, badge: null, wait: null };
    if (!REC_BADGE) return ov;
    ov.dim = p.stage.classList.contains("frozen") && p.stage.classList.contains("dim");
    if (p.badge && p.badge.classList.contains("show")) ov.badge = { grey: p.badge.classList.contains("grey"), a: p.badgeMain.textContent, b: p.badgeSub.textContent };
    if (p.wait.classList.contains("show")) ov.wait = p.wait.textContent;
    return ov;
}
function recOv(r) { const ov = recOverlay(r.p), sig = JSON.stringify(ov); if (sig !== r.ovSig) { r.ovSig = sig; r.w.postMessage({ t: "ov", ov }); } }
function recPic(p) {   // called when a pane has drawn a new picture: a copy of its canvas goes to the pane's recording
    const r = p.rec;
    if (!r) return;
    if (r.mr) { if (!r.stopping) recMrFrame(r); return; }
    if (r.snap) { r.again = true; return; }   // one copy at a time
    r.snap = true;
    createImageBitmap(p.canvas).then(bmp => { r.snap = false; r.w.postMessage({ t: "pic", bmp }, [bmp]); if (r.again) { r.again = false; recPic(p); } }, () => { r.snap = false; });
}
function recOpenWc(r) {
    if (!recWorkerUrl) recWorkerUrl = URL.createObjectURL(new Blob([recWorkerSrc()], { type: "text/javascript" }));
    const w = r.w = new Worker(recWorkerUrl);
    r.done = new Promise(res => { r.finish = res; });
    w.onmessage = e => {
        const m = e.data;
        if (m.t === "stat") { r.n = m.n; r.frames = m.frames; r.held = m.held; r.bytes = m.bytes; recOv(r); if (performance.now() - recUiAt > 500) recUi(); }   // also the pace of the overlay check: messages keep coming in a hidden tab
        else if (m.t === "max") { recNote("최대 녹화 시간 " + REC_MAX_MIN + "분에 도달해 자동으로 저장했습니다 (.env VA_VIEWER_REC_MAX_MIN)"); recStop([r.p]); }
        else if (m.t === "done") { r.finish(m); if (!r.stopping) recStop([r.p]); }   // not asked to stop: the encoder failed, what it has is saved
    };
    w.onerror = e => { r.finish({ blob: null, error: (e && e.message) || "worker error" }); if (!r.stopping) recStop([r.p]); };
    w.postMessage({ t: "init", t0: r.t0, config: recConfig(recEngine.codec) });
}
function recOpenMr(r) {   // fallback on this thread: MediaRecorder takes a frame whenever the pane changes (it stamps the frames itself)
    r.canvas = document.createElement("canvas"); r.canvas.width = REC_W; r.canvas.height = REC_H; r.g = r.canvas.getContext("2d", { alpha: false });
    const s = r.stream = r.canvas.captureStream(0); r.track = s.getVideoTracks()[0];
    r.mr = new MediaRecorder(s, { mimeType: recEngine.mime, videoBitsPerSecond: Math.round(REC_MBPS * 1e6) });
    r.mr.ondataavailable = e => { if (e.data && e.data.size) { r.parts.push(e.data); r.bytes += e.data.size; } };
    r.mr.onerror = e => { r.error = (e.error && e.error.message) || "MediaRecorder error"; recStop([r.p]); };
    recMrFrame(r); r.mr.start(1000);
    r.timer = setInterval(() => {
        if (r.stopping) return;
        const now = recNow();
        if (REC_MAX_MIN > 0 && now - r.t0 >= REC_MAX_MIN * 60000) { recNote("최대 녹화 시간 " + REC_MAX_MIN + "분에 도달해 자동으로 저장했습니다 (.env VA_VIEWER_REC_MAX_MIN)"); recStop([r.p]); return; }
        if (JSON.stringify(recOverlay(r.p)) !== r.ovSig || now - r.frameAt >= 1000) recMrFrame(r);
        if (performance.now() - recUiAt > 500) recUi();
    }, 100);
}
function recMrFrame(r) {
    const ov = recOverlay(r.p); r.ovSig = JSON.stringify(ov);
    recPicture(r.g, r.p.canvas, ov.dim); recLayer(r.g, ov, REC_FONT);
    (r.track.requestFrame ? r.track : r.stream).requestFrame();
    r.frames++; r.frameAt = recNow(); r.n = Math.floor((r.frameAt - r.t0) * REC_FPS / 1000);
}
function recStart(list) {   // one start instant and one name stamp for all of them: the files of a "전체 녹화" line up frame by frame
    if (!recEngine) return;
    const t0 = recNow(), d = new Date(), z = x => String(x).padStart(2, "0");
    const stamp = d.getFullYear() + z(d.getMonth() + 1) + z(d.getDate()) + "_" + z(d.getHours()) + z(d.getMinutes()) + z(d.getSeconds());
    for (const p of list) {
        if (p.rec || p.recOff) continue;
        const r = { p, t0, name: (REC_PREFIX ? REC_PREFIX + "_" : "") + p.kind + "_" + p.ue + "_" + stamp + "." + recEngine.ext,
                    n: -1, frames: 0, held: 0, bytes: 0, stopping: false, error: null, parts: [], ovSig: "", snap: false, again: false, frameAt: 0, w: null, mr: null };
        try { if (recEngine.kind === "wc") recOpenWc(r); else recOpenMr(r); }
        catch (e) { if (r.w) r.w.terminate(); recNote(p.kind + " " + p.ue + ": 녹화를 시작하지 못했습니다 (" + (e.message || e) + ")"); continue; }
        p.rec = r; recs.push(r);
        if (r.w) { recOv(r); recPic(p); }
    }
    recUi();
}
async function recFinish(r) {   // -> the finished file or null
    let m;
    if (r.w) {
        m = await Promise.race([r.done, new Promise(res => setTimeout(() => res({ blob: null, error: "인코더가 응답하지 않습니다" }), 30000))]);
        r.w.terminate();
    } else {
        recMrFrame(r); await new Promise(res => setTimeout(res, 150));   // the last picture, and time for the recorder to take it
        clearInterval(r.timer);
        await new Promise(res => { r.mr.onstop = res; try { r.mr.stop(); } catch (e) { res(); } });
        r.track.stop();
        m = { blob: r.parts.length ? new Blob(r.parts, { type: recEngine.mime.split(";")[0] }) : null, secs: (recNow() - r.t0) / 1000, frames: r.frames, held: 0 };
    }
    if (m.error && !r.error) r.error = m.error;
    return m.blob && { name: r.name, blob: m.blob, url: URL.createObjectURL(m.blob), secs: m.secs, frames: m.frames, held: m.held, label: recEngine.label, cfr: !!r.w };   // cfr: a frame on every slot (MediaRecorder: one per change)
}
async function recStop(list) {
    const rs = [], end = recNow() + 120;   // a moment ahead: every worker gets the message before its clock reaches that slot, so the files end together
    for (const p of list) if (p.rec && !p.rec.stopping) { const r = p.rec; r.stopping = true; rs.push(r); if (r.w) r.w.postMessage({ t: "stop", end }); }
    if (!rs.length) return;
    recUi();
    const files = await Promise.all(rs.map(recFinish)), fresh = [];
    rs.forEach((r, i) => {
        recs.splice(recs.indexOf(r), 1); r.p.rec = null;
        if (files[i]) { recFiles.push(files[i]); fresh.push(files[i]); }
        if (!files[i] || r.error) recNote(r.name + ": " + (files[i] ? "인코더 오류로 일찍 끝났습니다" : "저장할 프레임이 없습니다") + (r.error ? " (" + r.error + ")" : ""));
    });
    recUi(); recList();
    fresh.forEach((f, i) => setTimeout(() => recSave(f), 400 * i));   // downloads one after another; the links stay in the list below
}
function recSave(f) { const a = document.createElement("a"); a.href = f.url; a.download = f.name; a.style.display = "none"; document.body.appendChild(a); a.click(); a.remove(); }
function recNote(text) {   // a line under the file list (the same text once per moment: four panes reach the time limit together)
    const line = new Date().toTimeString().slice(0, 8) + " " + text;
    if (recNotes[recNotes.length - 1] === line) return;
    recNotes.push(line); while (recNotes.length > 6) recNotes.shift();
    recList();
}
function recClock(ms) { const s = Math.max(0, Math.floor(ms / 1000)); return String(Math.floor(s / 60)).padStart(2, "0") + ":" + String(s % 60).padStart(2, "0"); }
function recSize(b) { return b >= 1e9 ? (b / 1e9).toFixed(2) + " GB" : (b / 1e6).toFixed(1) + " MB"; }
function recButton(label) { const b = el("button", "rb"); b.type = "button"; b.appendChild(el("i", "")); b.lab = el("span", "", label); b.appendChild(b.lab); return b; }
function recUi() {
    if (!recAllBtn) return;
    recUiAt = performance.now(); const now = recNow(); let on = 0, busy = 0, bytes = 0, off = 0, t0 = now;
    for (const p of recPanes) {
        const r = p.rec, b = p.recBtn;
        if (p.recOff) { off++; b.className = "rb"; b.disabled = true; b.lab.textContent = "녹화 불가"; }
        else if (!r) { b.className = "rb"; b.disabled = !recEngine; b.lab.textContent = "녹화 시작"; }
        else if (r.stopping) { busy++; b.className = "rb"; b.disabled = true; b.lab.textContent = "저장 중…"; }
        else { on++; bytes += r.bytes; t0 = Math.min(t0, r.t0); b.className = "rb on"; b.disabled = false; b.lab.textContent = "녹화 종료 " + recClock(now - r.t0); }
    }
    recAllBtn.disabled = !recEngine || on + busy + off === recPanes.length; recStopBtn.disabled = on === 0; recStopBtn.className = "rb stop" + (on ? " on" : "");
    recStat.textContent = on ? "녹화 중 " + on + "/" + (recPanes.length - off) + " · " + recClock(now - t0) + " · " + recSize(bytes) : (busy ? "저장 중…" : (recEngine ? "" : "이 브라우저는 녹화를 지원하지 않습니다"));
}
function recList() {
    const box = document.getElementById("recFiles"); box.className = recFiles.length || recNotes.length ? "show" : ""; box.textContent = "";
    const head = el("div", "rfh"); head.appendChild(el("b", "", "녹화 파일")); head.appendChild(el("span", "", "이 탭의 메모리에 있습니다. 탭을 닫거나 새로 고치기 전에 내려받으세요."));
    if (recFiles.length) {
        const all = el("button", "rb", "모두 다운로드"); all.type = "button"; all.onclick = () => recFiles.forEach((f, i) => setTimeout(() => recSave(f), 400 * i)); head.appendChild(all);
    }
    const clr = el("button", "rb", "목록 비우기"); clr.type = "button"; clr.onclick = () => { for (const f of recFiles) URL.revokeObjectURL(f.url); recFiles.length = 0; recNotes.length = 0; recList(); }; head.appendChild(clr);
    box.appendChild(head);
    for (const f of recFiles) {
        const row = el("div", "rf"), a = el("a", "", f.name); a.href = f.url; a.download = f.name; row.appendChild(a);
        row.appendChild(el("span", "", recClock(f.secs * 1000) + " · " + recSize(f.blob.size) + " · " + REC_W + "x" + REC_H + " · " + (f.cfr ? REC_FPS + " fps" : "가변 프레임률") + " · " + f.label + " · 프레임 " + f.frames + (f.held ? " (건너뜀 " + f.held + ")" : "")));
        const del = el("button", "rb", "삭제"); del.type = "button"; del.onclick = () => { URL.revokeObjectURL(f.url); recFiles.splice(recFiles.indexOf(f), 1); recList(); }; row.appendChild(del);
        box.appendChild(row);
    }
    for (const t of recNotes) box.appendChild(el("div", "rfn", t));
}
function recAttach(p, kind, top) {   // the pane's own record button, left of its status chip
    p.kind = kind; p.rec = null;
    if (!REC) return;
    const right = el("div", "topr"); top.replaceChild(right, p.chip); p.recBtn = recButton("녹화 시작"); p.recBtn.disabled = true; right.appendChild(p.recBtn); right.appendChild(p.chip);
    p.recBtn.onclick = () => { if (p.rec) recStop([p]); else recStart([p]); };
    recPanes.push(p);
}
async function recInit() {   // header buttons: all panes at once
    if (!REC || !recPanes.length) return;
    const bar = el("div", "recbar"); recStat = el("span", "recstat", ""); recAllBtn = recButton("전체 녹화"); recStopBtn = recButton("녹화 종료"); recStopBtn.classList.add("stop");
    recAllBtn.onclick = () => recStart(recPanes); recStopBtn.onclick = () => recStop(recPanes);
    bar.appendChild(recStat); bar.appendChild(recAllBtn); bar.appendChild(recStopBtn); document.querySelector("header").appendChild(bar);
    recAllBtn.disabled = true; recStopBtn.disabled = true;
    recEngine = await recProbe();
    if (recEngine && recEngine.kind !== "wc") recNote("이 브라우저는 H.264 인코더(WebCodecs)를 쓸 수 없어 " + recEngine.label + " 형식으로 저장합니다. Chrome 또는 Edge 최신판을 권장합니다.");
    window.addEventListener("beforeunload", e => { if (recs.length) { e.preventDefault(); e.returnValue = ""; } });
    recUi();
}

// fit to the window (2026-11-14, VA_VIEWER_FIT): every pane on the screen at once and the pictures as large as that allows. Of the grids with full
// rows (4 panes: 2 x 2, 4 x 1, 1 x 4) and the places for the figures (under the picture, or beside it in one or two columns) the combination with the
// tallest picture is taken; the picture stays 16:9. The sizes go to CSS variables of <main> (main.fitv); a window too small for it keeps the old layout.
const FIT_SIDE_W = 176, FIT_MIN_H = 200, FIT_MIN_W = 900;   // one column of figures beside the picture [px]; smallest picture height and window width the fit is used for
let fitKey = "";
function fitLayout(again) {
    if (!FIT) return;
    const main = document.getElementById("panes"), all = Array.from(main.children), n = all.length;
    if (!n) return;
    const W = document.documentElement.clientWidth, H = window.innerHeight, head = Math.ceil(document.querySelector("header").getBoundingClientRect().height);
    const key = W + " " + H + " " + head + " " + n;
    if (key === fitKey) return;
    fitKey = key;
    main.classList.add("fitv"); main.classList.remove("side");   // the heights below are read with the figures under the picture
    const cs = getComputedStyle(main), px = v => parseFloat(v) || 0;
    const padX = px(cs.paddingLeft) + px(cs.paddingRight), padY = px(cs.paddingTop) + px(cs.paddingBottom), gapX = px(cs.columnGap), gapY = px(cs.rowGap);
    let bar = 0, fig = 0, tile = 0, tiles = 0;   // top + foot, the row of figures, one figure, figures per pane
    for (const pane of all) {
        const m = pane.querySelector(".metrics");
        bar = Math.max(bar, pane.querySelector(".top").offsetHeight + pane.querySelector(".foot").offsetHeight);
        fig = Math.max(fig, m.offsetHeight); tile = Math.max(tile, m.firstChild.offsetHeight); tiles = Math.max(tiles, m.children.length);
    }
    let best = null;
    const take = (cols, mc, sh) => { if (!best || sh > best.sh + 0.5) best = { cols, mc, sh }; };
    for (let cols = 1; cols <= n; cols++) {
        if (n % cols) continue;
        const rows = n / cols, w = (W - padX - (cols - 1) * gapX) / cols - 2, h = (H - head - padY - (rows - 1) * gapY - 1) / rows - 2 - bar;   // room for picture + figures in one pane
        take(cols, 0, Math.min(h - fig, w * 9 / 16));
        for (const mc of [1, 2]) {
            const sh = Math.min(h, (w - mc * FIT_SIDE_W) * 9 / 16);
            if (sh >= Math.ceil(tiles / mc) * (tile + 1)) take(cols, mc, sh);
        }
    }
    if (!best || best.sh < FIT_MIN_H || W < FIT_MIN_W) { main.classList.remove("fitv"); return; }
    const sh = Math.floor(best.sh), sw = Math.floor(sh * 16 / 9);
    main.style.setProperty("--sh", sh + "px"); main.style.setProperty("--sw", sw + "px"); main.style.setProperty("--pw", (sw + best.mc * FIT_SIDE_W + 2) + "px");
    main.style.setProperty("--cols", best.cols); main.style.setProperty("--mcols", best.mc || 1);
    main.classList.toggle("side", best.mc > 0);
    if (!again && document.documentElement.clientWidth !== W) fitLayout(true);   // the scroll bar went away with the fit: once more with the full width
}
if (NV_UES.length) document.getElementById("panes").className = "four";   // 2 x 2 when the non-VA panes are shown
const panes = UES.map(makePane);
panes.forEach(loop);
const nvPanes = NV_UES.map(makeNonvaPane);
nvPanes.forEach(nonvaLoop);
setInterval(tick, 100);
setInterval(nvTick, 100);
recInit();
fitLayout();
window.addEventListener("resize", () => fitLayout());
setInterval(fitLayout, 500);   // the header changes height with the recording buttons and notes
</script>
</body>
</html>
"""


@app.get("/viewer", response_class=HTMLResponse)
@app.get("/viewer/{ue_id}", response_class=HTMLResponse)
async def viewer_page(ue_id: str | None = None):
    """base-station viewer: all four UEs (/viewer: VA ue1, ue2 + non-VA ue1, ue2), the VA UEs only (/viewer/va) or one VA UE (/viewer/ue1)"""
    if not VA_VIEWER:
        raise HTTPException(status_code=404, detail="viewer off (VA_VIEWER=0)")
    if ue_id is not None and ue_id != "va" and ue_id not in viewers:
        raise HTTPException(status_code=404, detail="Unknown UE")
    return _page(VIEWER_PAGE)


# =====================================================
# Clock Synchronization Endpoint
# =====================================================

@app.get("/clock_sync")
async def clock_sync():

    # GH200 receives synchronization request
    server_receive_ms = (
        time.time_ns()
        / 1_000_000.0
    )


    # GH200 prepares synchronization response
    server_send_ms = (
        time.time_ns()
        / 1_000_000.0
    )


    return {
        "server_receive_ms":
            server_receive_ms,

        "server_send_ms":
            server_send_ms
    }


# =====================================================
# YOLO Detection
# =====================================================

@app.post("/detect_frame")
async def detect_frame(
    file: UploadFile = File(...),
    enable_yolo: bool = Form(True),
    ue_id: str = Form(...),
    session_token: str = Form(...),
    client_send_ms: float | None = Form(None),    # DGIST viewer: the phone's send time (its clock) and
    clock_offset_ms: float | None = Form(None),   # its clock offset (server - phone) -> UL latency of this frame on the server
    frame_id: int | None = Form(None),            # DGIST dashboard: the phone's frame number (the one its /telemetry/result carries)
    request: Request = None,
):
    """one camera frame: JPEG in -> RUN to the dApp agent (model VA_MODEL or the selector's choice, qos.deadline_ms VA_DEADLINE_MS) -> its reply out.
    Timestamps t1..t4 keep the hsw meaning: AI time = ai_start..ai_end = the whole RUN round trip to the agent (queue wait +
    GPU decode + inference + socket), Server Other = everything else in this process."""
    try:
        telemetry.check(ue_id, session_token)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    # t1: the complete multipart request has arrived
    server_receive_ms = time.time_ns() / 1_000_000.0
    image_data = await file.read()

    # JPEG header only (width/height) - the 4K frame is NOT decoded on the CPU here: the dApp decodes it on the GPU (nvjpeg)
    # inside the L1 process. (local backend: the decode happens inside backend.infer and counts as AI time.)
    try:
        with Image.open(io.BytesIO(image_data)) as im:
            image_width, image_height = im.size
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"not a decodable image: {exc}") from exc

    # t2: AI start
    ai_start_ms = time.time_ns() / 1_000_000.0

    # Snapshot of GPU-wide metrics associated with this frame (NVML util = recent sampling window, not per-frame usage).
    gpu_util_percent, gpu_memory_used_gb = read_gpu_metrics()

    if enable_yolo:
        res = await run_in_threadpool(backend.infer, image_data, ue_id)
        # t3: AI end (the agent's reply is in)
        ai_end_ms = time.time_ns() / 1_000_000.0
        processing_ms = ai_end_ms - ai_start_ms
        detections = res["detections"]
        dapp = res["dapp"]
        if VA_VERBOSE:
            print(f"[VA] {ue_id} {dapp.get('status')}{('/' + str(dapp.get('reason'))) if dapp.get('reason') else ''} {dapp.get('model')} "
                  f"sm {dapp.get('sm')} wait {dapp.get('wait_ms')} dec {dapp.get('decode_ms')} inf {dapp.get('infer_ms')} "
                  f"srv {dapp.get('latency_ms')} ai {processing_ms:.1f} ms det {len(detections)} {image_width}x{image_height} "
                  f"{len(image_data) / 1024:.0f} KB", flush=True)
    else:
        # No inference request in this branch: upload + header read only, AI time explicitly zero.
        ai_end_ms = ai_start_ms
        processing_ms = 0.0
        detections = []
        dapp = None

    # t4: application data for the response is ready
    server_send_ms = time.time_ns() / 1_000_000.0

    # DGIST dashboard feed: the server-side timing of this frame (UL as the viewer computes it, the agent's wait / decode / inference, model, SMs)
    try:
        d = dapp or {}
        dash_event({"k": "f", "t": server_send_ms, "ue": ue_id, "tok": session_token[:8], "fid": frame_id,
                    "ip": request.client.host if request is not None and request.client else None, "yolo": bool(enable_yolo),
                    "ul": (max(0.0, server_receive_ms - (client_send_ms + clock_offset_ms))
                           if client_send_ms is not None and clock_offset_ms is not None else None),
                    "ai": processing_ms, "oth": max(0.0, ai_start_ms - server_receive_ms) + max(0.0, server_send_ms - ai_end_ms),
                    "wait": d.get("wait_ms"), "dec": d.get("decode_ms"), "inf": d.get("infer_ms"), "srv": d.get("latency_ms"),
                    "sm": d.get("sm"), "smm": d.get("sm_mean"), "model": d.get("model"), "st": d.get("status"), "why": d.get("reason"),
                    "kb": len(image_data) / 1024.0, "w": image_width, "h": image_height, "det": len(detections)})
    except Exception:
        pass

    # DGIST viewer: server-side verdict = UL (phone send time + clock offset, as the phone computes it) + AI + server other + fixed DL
    viewer = None
    if VA_VIEWER and ue_id in viewers:
        ul_ms = None
        if client_send_ms is not None and clock_offset_ms is not None:
            ul_ms = max(0.0, server_receive_ms - (client_send_ms + clock_offset_ms))
        other_ms = max(0.0, ai_start_ms - server_receive_ms) + max(0.0, server_send_ms - ai_end_ms)
        success = (not enable_yolo) or ((dapp or {}).get("status") == "success")
        capture_ms = (client_send_ms + clock_offset_ms) if ul_ms is not None else server_receive_ms
        met, total = viewers[ue_id].update(server_send_ms, image_data, detections, image_width, image_height, capture_ms,
                                           ul_ms, processing_ms, other_ms, dapp, success)
        viewer = {"ul_ms": None if ul_ms is None else round(ul_ms, 1), "sum_ms": round(total, 1), "dl_ms": VA_VIEWER_DL_MS, "met": met}

    return {
        "processing_ms": processing_ms,
        "yolo_enabled": enable_yolo,
        "server_receive_ms": server_receive_ms,
        "ai_start_ms": ai_start_ms,
        "ai_end_ms": ai_end_ms,
        "server_send_ms": server_send_ms,
        "image_width": image_width,
        "image_height": image_height,
        "detections": detections,
        "gpu_util_percent": gpu_util_percent,
        "gpu_memory_used_gb": gpu_memory_used_gb,
        # DGIST additions
        "backend": VA_BACKEND,
        "model": (dapp or {}).get("model", VA_MODEL) if selector else VA_MODEL,     # the model of THIS frame (same field, same length)
        "qos_deadline_ms": VA_DEADLINE_MS,
        "ue_id": ue_id,
        "dapp": dapp,
        "viewer": viewer,
    }


@app.get("/metrics/frames")
async def metrics_frames(request: Request, since: int = 0):
    """DGIST dashboard feed: the events after sequence number `since` (served to this host only)"""
    if request.client is None or request.client.host not in ("127.0.0.1", "::1"):
        raise HTTPException(status_code=403, detail="local only")
    with _dash_lock:
        ev = [e for e in DASH_RING if e["s"] > since]
        seq = _dash_seq
    return {"service": "va", "seq": seq, "t": time.time_ns() / 1e6, "ev": ev}


# =====================================================
# Save Experiment CSV on GH200
# =====================================================

@app.post("/save_latency_log")
async def save_latency_log(
    session: LatencySession
):
    if session.ue_id not in ('ue1','ue2'):
        raise HTTPException(status_code=400, detail='Invalid UE identity')

    # =================================================
    # Check Records
    # =====================================================

    if not session.records:

        return {
            "saved": False,
            "filename": "",
            "path": "",
            "records": 0
        }


    # =================================================
    # Create Filename
    # =====================================================

    timestamp = datetime.now().strftime(
        "%Y%m%d_%H%M%S_%f"
    )


    filename = (
        f"va_ul_dl_latency_{session.ue_id}_{timestamp}.log"
    )


    filepath = (
        LOG_DIR
        / filename
    )


    # =================================================
    # Write CSV
    # =====================================================

    with filepath.open(
        "w",
        newline="",
        encoding="utf-8"
    ) as csvfile:


        writer = csv.writer(
            csvfile
        )


        # =============================================
        # CSV Header
        # =============================================

        writer.writerow(
            [
                "frame_id",
                "timestamp",
                "yolo_enabled",

                "image_width",
                "image_height",

                "frame_size_kb",
                "dl_response_kb",

                "actual_fps",
                "ul_payload_mbps",
                "dl_payload_mbps",

                "e2e_latency_ms",

                "ul_latency_ms",
                "ai_latency_ms",
                "server_other_ms",
                "dl_latency_ms",

                "network_latency_ms",
                "breakdown_residual_ms",

                "clock_offset_ms",
                "sync_rtt_ms",
                "gpu_util_percent",
                "gpu_memory_used_gb",

                "qos_deadline_ms",
                "qos_met",
                "dapp_status",
                "dapp_reason",
                "dapp_model",
                "dapp_sm",
                "dapp_wait_ms",
                "dapp_decode_ms",
                "dapp_infer_ms",
                "dapp_latency_ms",
                "detections"
            ]
        )


        # =============================================
        # CSV Data
        # =============================================

        for record in session.records:

            writer.writerow(
                [
                    record.frame_id,
                    record.timestamp,
                    int(record.yolo_enabled),

                    record.image_width,
                    record.image_height,

                    record.frame_size_kb,
                    record.dl_response_kb,

                    record.actual_fps,
                    record.ul_payload_mbps,
                    record.dl_payload_mbps,

                    record.e2e_latency_ms,

                    record.ul_latency_ms,
                    record.ai_latency_ms,
                    record.server_other_ms,
                    record.dl_latency_ms,

                    record.network_latency_ms,
                    record.breakdown_residual_ms,

                    record.clock_offset_ms,
                    record.sync_rtt_ms,
                    record.gpu_util_percent,
                    record.gpu_memory_used_gb,

                    record.qos_deadline_ms,
                    None if record.qos_met is None else int(record.qos_met),
                    record.dapp_status,
                    record.dapp_reason,
                    record.dapp_model,
                    record.dapp_sm,
                    record.dapp_wait_ms,
                    record.dapp_decode_ms,
                    record.dapp_infer_ms,
                    record.dapp_latency_ms,
                    record.detections
                ]
            )


    # =================================================
    # Terminal Log
    # =====================================================

    print(
        f"UL/DL latency experiment saved: "
        f"{filepath} "
        f"({len(session.records)} frames)"
    )


    # =================================================
    # Response
    # =====================================================

    return {

        "saved": True,

        "filename":
            filename,

        "path":
            str(filepath),

        "records":
            len(session.records)
    }

from bg_yolo import router as bg_router, configure_background
configure_background(backend, read_gpu_metrics, LOG_DIR)
app.include_router(bg_router)


# =====================================================
# Status (va.sh status): configuration, backend / agent state, active UE sessions
# =====================================================

@app.post("/client_log")
async def client_log(body: dict, request: Request):
    """diagnostics from the phone page (JS errors, camera settings, button presses) -> uvicorn log"""
    host = request.client.host if request.client else "?"
    print(f"[CLIENT {host}] {body.get('kind')}: {body.get('detail')} | secure={body.get('secure')} | {body.get('ua')}", flush=True)
    return {"ok": True}


@app.get("/va/status")
async def va_status():
    ue = {}
    with telemetry.lock:
        for u, row in telemetry.rows.items():
            ue[u] = {k: row.get(k) for k in ("active", "status", "latency_ms", "frame_id", "failures")}
    return {
        "service": "va",
        "config": {"backend": VA_BACKEND, "model": "auto" if selector else VA_MODEL, "model_fixed": VA_MODEL,
                   "model_select": bool(selector), "model_weights": selector.snapshot()["weights"] if selector else None,
                   "deadline_ms": VA_DEADLINE_MS, "target_fps": TARGET_FPS,
                   "max_inflight": MAX_INFLIGHT, "camera": [CAMERA_WIDTH, CAMERA_HEIGHT], "file_frame": [VA_FILE_WIDTH, VA_FILE_HEIGHT], "jpeg_quality": JPEG_QUALITY,
                   "agent": VA_AGENT, "log_dir": str(LOG_DIR), "telemetry_dir": str(telemetry.directory)},
        "backend": backend.status(),
        "ue": ue,
        "viewer": {"on": VA_VIEWER, "dl_ms": VA_VIEWER_DL_MS, "freeze": VA_VIEWER_QOS_FREEZE, "fit": VA_VIEWER_FIT,
                   "ue": {u: {k: st.meta(time.time_ns() / 1_000_000.0, False)[k] for k in ("seq", "verdict", "results", "met", "missed", "failed", "met_pct", "active", "kb", "req_s", "mbps")}
                          for u, st in viewers.items()}},
    }


