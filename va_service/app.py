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
Run (va.sh start):  uvicorn app:app --host 0.0.0.0 --port 8000 --ssl-keyfile certs/server.key --ssl-certfile certs/server.crt

ICNL 2026-11-12 (iitp_demo/va_service_ra): with VA_RA_HOOK=host:port the IITP demo Resource Allocator chooses each frame's model and
server deadline (ra_link.py: frame_start / progress from an ASGI middleware while the upload arrives, decide before the RUN, done after
it). The page sends X-Frame-Id / X-Ue-Id / X-T0-Ms headers for that. VA_RA_HOOK empty (default) = the DGIST behaviour above.
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
VA_MODEL = _env("VA_MODEL", "26x")                      # the one dApp model every frame asks for
VA_DEADLINE_MS = _env("VA_DEADLINE_MS", 200, int)       # QoS: RUN qos.deadline_ms, and the per-frame E2E target judged on the phone
VA_AGENT = _env("VA_AGENT", "127.0.0.1:9810")           # dApp agent (TCP; the dgist-l1 container runs with network host)
VA_VERBOSE = _env("VA_VERBOSE", 0, int)                 # 1 = one console line per frame
# ICNL: Resource Allocator link (ra_link.py). Empty = off
VA_RA_HOOK = _env("VA_RA_HOOK", "")                     # RA hook address, e.g. 127.0.0.1:9822 (ra_main.py --hook)
VA_RA_TIMEOUT_MS = _env("VA_RA_TIMEOUT_MS", 5.0, float) # how long a frame waits for the RA's decision before it runs with VA_MODEL
VA_RA_PROGRESS_MS = _env("VA_RA_PROGRESS_MS", 5.0, float)   # upload progress reports to the RA at most this often

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

print(f"VA service: backend {VA_BACKEND}, model {VA_MODEL}, QoS deadline {VA_DEADLINE_MS} ms, {TARGET_FPS:g} req/s per UE, "
      f"camera {CAMERA_WIDTH}x{CAMERA_HEIGHT} JPEG q{JPEG_QUALITY:g}, agent {VA_AGENT}", flush=True)
backend = make_backend(VA_BACKEND, agent=VA_AGENT, model=VA_MODEL, deadline_ms=VA_DEADLINE_MS, local_model_path=MODEL_PATH, device=DEVICE)

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


# ICNL: Resource Allocator link (off without VA_RA_HOOK)
ra_link = None
if VA_RA_HOOK:
    from ra_link import RaLink, RaMiddleware, SCOPE_KEY as RA_SCOPE_KEY   # noqa: E402
    ra_link = RaLink(VA_RA_HOOK, timeout_ms=VA_RA_TIMEOUT_MS, progress_ms=VA_RA_PROGRESS_MS)
    app.add_middleware(RaMiddleware, link=ra_link)
    print(f"VA service: Resource Allocator link on, hook {VA_RA_HOOK}, decision wait <= {VA_RA_TIMEOUT_MS:g} ms "
          f"(no answer: {VA_MODEL}, {VA_DEADLINE_MS} ms)", flush=True)


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
    # ICNL: Resource Allocator decision of this frame (ra_link.py; empty without VA_RA_HOOK)
    ra_answered: bool | None = None
    ra_model: str | None = None
    ra_deadline_ms: float | None = None
    ra_pred_ms: float | None = None
    ra_feasible: bool | None = None
    ra_ul_ms: float | None = None


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
                 ("__VA_BACKEND__", VA_BACKEND), ("__VA_MODEL__", VA_MODEL),
                 ("__VA_TRACK__", VA_TRACK), ("__VA_QOS_DROP_MISSED__", VA_QOS_DROP_MISSED), ("__VA_DISPLAY_CONF__", f"{VA_DISPLAY_CONF:g}"),
                 ("__VA_TRACK_TTL_MS__", VA_TRACK_TTL_MS), ("__VA_ENCODE_WORKERS__", VA_ENCODE_WORKERS),
                 ("__VA_FILE_WIDTH__", VA_FILE_WIDTH), ("__VA_FILE_HEIGHT__", VA_FILE_HEIGHT),
                 ("__VA_VIEWER_DL_MS__", f"{VA_VIEWER_DL_MS:g}"), ("__VA_VIEWER_QOS_FREEZE__", VA_VIEWER_QOS_FREEZE), ("__VA_VIEWER_BADGE__", VA_VIEWER_BADGE),
                 ("__VA_VIEWER_NONVA__", VA_VIEWER_NONVA), ("__NONVA_PORT__", NONVA_PORT)):
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
        if (video.paused) video.play().catch(() => {});
    }
});
document.getElementById("camera").addEventListener("loadedmetadata", updateStage);
document.getElementById("camera").addEventListener("resize", updateStage);      // the camera frame turns with the phone


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
        if (fileVideo.paused) { try { await fileVideo.play(); } catch (e) {} }
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
                    // ICNL: who / which frame / when it left the phone (server clock) for the Resource Allocator (ra_link.py)
                    headers: {
                        "X-Frame-Id": String(currentFrameId),
                        "X-Ue-Id": String(frameTelemetry.ue),
                        "X-T0-Ms": (clientSendMs + clockOffsetMs).toFixed(3)
                    },
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
            detections: data.detections.length,
            ra_answered: data.ra ? data.ra.answered : null,
            ra_model: data.ra ? (data.ra.model ?? null) : null,
            ra_deadline_ms: data.ra ? (data.ra.deadline_ms ?? null) : null,
            ra_pred_ms: data.ra ? (data.ra.pred_ms ?? null) : null,
            ra_feasible: data.ra ? (data.ra.feasible ?? null) : null,
            ra_ul_ms: data.ra ? (data.ra.ul_ms ?? null) : null
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
               + " inf " + fmt(dapp.infer_ms, 1) + " srv " + fmt(dapp.latency_ms, 1) + " ms | det " + data.detections.length
               + (data.ra ? (" | RA " + (data.ra.answered ? (data.ra.model + " server deadline " + fmt(data.ra.deadline_ms, 1) + " ms"
                                                            + (data.ra.feasible ? "" : " (infeasible)")) : "no answer")) : ""))
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
.hint { position:absolute; left:12px; bottom:12px; display:none; padding:6px 10px; border-radius:8px; background:rgba(11,15,20,.8); color:var(--muted); font-size:12px; }
.hint.show { display:block; }
</style>
</head>
<body>
<header>
  <h1>VA 서비스 · 기지국 뷰어</h1>
  <div class="sub">VA: UE 카메라 → 5G 상향 → cuPHY 안의 YOLO(__VA_MODEL__) → 검출 결과. QoS __QOS_DEADLINE_MS__ ms = 상향(실측) + 추론(실측) + 하향 __VA_VIEWER_DL_MS__ ms(고정), 미달 결과는 화면을 갱신하지 않음. non-VA: 720p 영상 중계(추론 없음).</div>
</header>
<main id="panes"></main>
<script>
const DEADLINE_MS = __QOS_DEADLINE_MS__, DL_MS = __VA_VIEWER_DL_MS__, DISPLAY_CONF = __VA_DISPLAY_CONF__, TARGET_FPS = __TARGET_FPS__;
const FREEZE = __VA_VIEWER_QOS_FREEZE__ ? true : false, BADGE = __VA_VIEWER_BADGE__ ? true : false;
const pathParts = location.pathname.split("/").filter(Boolean);   // ["viewer"] | ["viewer", "va"] | ["viewer", "ue1"]
const SHOW_NONVA = __VA_VIEWER_NONVA__ ? true : false, NONVA_PORT = __NONVA_PORT__;
const UES = pathParts.length > 1 && pathParts[1] !== "va" ? [pathParts[1]] : ["ue1", "ue2"];
const NV_UES = (pathParts.length > 1 || !SHOW_NONVA) ? [] : ["ue1", "ue2"];
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
    return { ue, chip, stage, canvas, ctx: canvas.getContext("2d"), badge, badgeMain, badgeSub, wait, tiles, footL, footR,
             meta: null, lastDrawAt: 0, draws: [], seq: 0, stat: 0, connected: false };
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

// non-VA panes: the non-VA service's MJPEG relay (same host, port NONVA_PORT) in an <img>, statistics through /nonva_stats/<ue> (proxied here)
function makeNonvaPane(ue) {
    const pane = el("div", "pane");
    const top = el("div", "top"); const title = el("div", "ue", "non-VA " + ue); title.appendChild(el("span", "", "720p 영상 중계 · 추론 없음"));
    const chip = el("div", "chip", "대기"); top.appendChild(title); top.appendChild(chip);
    const stage = el("div", "stage"); const img = document.createElement("img"); img.alt = ""; stage.appendChild(img);
    const wait = el("div", "wait show", "UE 대기 중"); stage.appendChild(wait);
    const hint = el("div", "hint", "영상이 안 보이면 https://" + location.hostname + ":" + NONVA_PORT + "/viewer/" + ue + " 를 한 번 열어 인증서를 수락하세요"); stage.appendChild(hint);
    const metrics = el("div", "metrics four"); const tiles = {};
    for (const [k, label] of [["fps", "수신 fps"], ["mbps", "상향 Mbps"], ["kb", "프레임 KB"], ["ul", "상향 지연 (UL)"]]) {
        const t = el("div", "m"); t.appendChild(el("div", "k", label)); const v = el("div", "v", "-"); t.appendChild(v); tiles[k] = { box: t, v: v }; metrics.appendChild(t);
    }
    const foot = el("div", "foot"); const footL = el("div", "", "-"); const footR = el("div", "", "non-VA 서비스 :" + NONVA_PORT); foot.appendChild(footL); foot.appendChild(footR);
    pane.appendChild(top); pane.appendChild(stage); pane.appendChild(metrics); pane.appendChild(foot);
    document.getElementById("panes").appendChild(pane);
    const p = { ue, chip, stage, img, wait, hint, tiles, footL, active: false, connected: false, loaded: false, srcSetAt: 0, retryAt: 0, lastFrameId: -1, lastChangeAt: 0 };
    img.onload = () => { p.loaded = true; };
    img.onerror = () => { p.loaded = false; p.connected = false; };
    return p;
}
function nonvaFeed(p) {   // (re)open the MJPEG relay; a new UE session gets a new stream
    const now = Date.now(); if (now < p.retryAt) return;
    p.retryAt = now + 2000; p.connected = true; p.loaded = false; p.srcSetAt = now;
    p.img.src = "https://" + location.hostname + ":" + NONVA_PORT + "/video_feed?session_id=" + encodeURIComponent(p.ue) + "&t=" + now;
}
async function nonvaLoop(p) {
    while (true) {
        try {
            const r = await fetch("/nonva_stats/" + encodeURIComponent(p.ue) + "?t=" + Date.now(), { cache: "no-store" });
            const d = await r.json();
            if (d.ok) {
                const wasActive = p.active; p.active = !!d.active;
                if (d.frame_id !== p.lastFrameId) { p.lastFrameId = d.frame_id; p.lastChangeAt = Date.now(); }
                p.tiles.fps.v.textContent = fmt(d.receive_fps, 1); p.tiles.mbps.v.textContent = fmt(d.receive_mbps, 1);
                p.tiles.kb.v.textContent = fmt(d.frame_size_kb); p.tiles.ul.v.textContent = fmt(d.ul_latency_ms); if (!p.tiles.ul.v.querySelector("small")) p.tiles.ul.v.appendChild(el("small", "", "ms"));
                p.footL.textContent = "프레임 #" + d.frame_id + " · 중계 " + fmt(d.viewer_target_fps) + " fps";
                if (p.active && (!wasActive || !p.connected)) nonvaFeed(p);
                p.chip.textContent = p.active ? "송출 중" : (d.frame_id > 0 ? "UE 정지" : "대기"); p.chip.className = "chip" + (p.active ? " ok" : "");
                p.wait.className = "wait" + (p.active || d.frame_id > 0 ? "" : " show");
                p.hint.className = "hint" + (p.active && p.connected && !p.loaded && Date.now() - p.srcSetAt > 4000 ? " show" : "");
            } else {
                p.chip.textContent = "non-VA 서비스 없음"; p.chip.className = "chip bad"; p.wait.className = "wait show"; p.wait.textContent = "non-VA 세션 없음";
            }
        } catch (e) { p.chip.textContent = "연결 끊김"; p.chip.className = "chip bad"; }
        await new Promise(res => setTimeout(res, 1000));
    }
}

if (NV_UES.length) document.getElementById("panes").className = "four";   // 2 x 2 when the non-VA panes are shown
const panes = UES.map(makePane);
panes.forEach(loop);
const nvPanes = NV_UES.map(makeNonvaPane);
nvPanes.forEach(nonvaLoop);
setInterval(tick, 100);
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

def _infer_ra(image_data, ue_id, ra, t1_fallback_ms):
    """ICNL: ask the Resource Allocator for this frame's model and server deadline, RUN with them (or with VA_MODEL /
    VA_DEADLINE_MS when it does not answer in VA_RA_TIMEOUT_MS), tell it the result. Runs in the worker thread."""
    d = ra_link.decide(ra, ue_id, t1_fallback_ms)
    if d:
        res = backend.infer(image_data, ue_id, model=d["model"], deadline_ms=d["deadline_ms"])
    else:
        res = backend.infer(image_data, ue_id)
    ra_link.done(ra, res["dapp"])
    return res


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
    """one camera frame: JPEG in -> RUN to the dApp agent (model VA_MODEL, qos.deadline_ms VA_DEADLINE_MS) -> its reply out.
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

    ra = request.scope.get(RA_SCOPE_KEY) if ra_link and request is not None else None    # ICNL: this frame's Resource Allocator record (ra_link.py)

    if enable_yolo:
        if ra is not None:
            res = await run_in_threadpool(_infer_ra, image_data, ue_id, ra, server_receive_ms)
        else:
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
                  f"{len(image_data) / 1024:.0f} KB"
                  + (f" | RA {RaLink.public(ra)}" if ra is not None else ""), flush=True)
    else:
        # No inference request in this branch: upload + header read only, AI time explicitly zero.
        ai_end_ms = ai_start_ms
        processing_ms = 0.0
        detections = []
        dapp = None
        if ra is not None:
            ra_link.done(ra, None, status="no_ai", kind="done_no_ai")

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
        "model": (dapp or {}).get("model") or VA_MODEL,        # ICNL: the model this frame ran with (the RA may change it per frame)
        "qos_deadline_ms": VA_DEADLINE_MS,                      # the E2E target the page judges (the RA's server deadline is in "ra")
        "ue_id": ue_id,
        "dapp": dapp,
        "viewer": viewer,
        "ra": RaLink.public(ra) if ra is not None else None,   # ICNL: the Resource Allocator decision
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
                "detections",
                "ra_answered",
                "ra_model",
                "ra_deadline_ms",
                "ra_pred_ms",
                "ra_feasible",
                "ra_ul_ms"
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
                    record.detections,
                    None if record.ra_answered is None else int(record.ra_answered),
                    record.ra_model,
                    record.ra_deadline_ms,
                    record.ra_pred_ms,
                    None if record.ra_feasible is None else int(record.ra_feasible),
                    record.ra_ul_ms
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
        "config": {"backend": VA_BACKEND, "model": VA_MODEL, "deadline_ms": VA_DEADLINE_MS, "target_fps": TARGET_FPS,
                   "max_inflight": MAX_INFLIGHT, "camera": [CAMERA_WIDTH, CAMERA_HEIGHT], "file_frame": [VA_FILE_WIDTH, VA_FILE_HEIGHT], "jpeg_quality": JPEG_QUALITY,
                   "agent": VA_AGENT, "log_dir": str(LOG_DIR), "telemetry_dir": str(telemetry.directory)},
        "backend": backend.status(),
        "ra": ra_link.status() if ra_link else None,
        "ue": ue,
        "viewer": {"on": VA_VIEWER, "dl_ms": VA_VIEWER_DL_MS, "freeze": VA_VIEWER_QOS_FREEZE,
                   "ue": {u: {k: st.meta(time.time_ns() / 1_000_000.0, False)[k] for k in ("seq", "verdict", "results", "met", "missed", "failed", "met_pct", "active", "kb", "req_s", "mbps")}
                          for u, st in viewers.items()}},
    }


