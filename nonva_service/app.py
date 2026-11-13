"""Non-VA Real-Time Video Streaming Service (hsw video_stream_service), DGIST copy 2026-11-05

Unchanged behaviour: the phone uploads camera frames (/upload_frame), the server keeps the latest one per UE and relays it as MJPEG
to /viewer/<ue> (/video_feed); UL latency per frame -> CSV at /stop_session; telemetry "nonva" for the hsw PPO xApp. No inference.
DGIST changes: the capture settings come from the environment with the hsw values as defaults (NONVA_WIDTH 1280, NONVA_HEIGHT 720,
NONVA_FPS 20, NONVA_JPEG_QUALITY 0.80, NONVA_VIEWER_FPS 20), the page JS constants are filled by the server, the CSV directory is
<_dgist>/runs/nonva_service_log (NONVA_LOG_DIR), service_common/ lives next to this file, GET /nonva/status added. Run: nonva.sh start
2026-11-12: the phone page can take a video file stored on the phone as the source instead of the camera (frames scaled to
NONVA_WIDTH x NONVA_HEIGHT, aspect kept; read locally, nothing is downloaded) and has a full-screen button.
2026-11-14: QoS for the viewers. A frame whose measured UL latency is above NONVA_DEADLINE_MS (250) missed the deadline; the MJPEG relay
then keeps the last frame that met it (NONVA_VIEWER_QOS_FREEZE=1, the rule of the VA viewer) and reports the verdict in the part header
X-NV-Meta and in /receiver_stats ("qos"); /viewer shows a badge over the held picture. The phone page (video-file source) got "Restart
Video" and a start at a chosen clock time; both happen on the phone alone. Nothing new is sent to a phone: the upload reply is unchanged.
(uvicorn app:app --host 0.0.0.0 --port 8001 --ssl-keyfile certs/server.key --ssl-certfile certs/server.crt). Original: upstream_hsw/app.py
"""
from fastapi import (
    FastAPI,
    File,
    Form,
    UploadFile,
    Request
)

from fastapi.responses import (
    HTMLResponse,
    StreamingResponse
)

from pathlib import Path
from collections import deque
from datetime import datetime, timezone

import asyncio
import csv
import time
import os
import logging
import json
import math
import warnings
from urllib.parse import urlsplit
warnings.filterwarnings("ignore", message=r"(?s).*on_event is deprecated")   # FastAPI 0.141 still accepts @app.on_event (hsw code)


# =====================================================
# Configuration
# =====================================================

TARGET_WIDTH = int(os.environ.get("NONVA_WIDTH") or 1280)        # hsw: 1280x720, 20 fps, JPEG 0.80 (phone capture + upload)
TARGET_HEIGHT = int(os.environ.get("NONVA_HEIGHT") or 720)

TARGET_FPS = float(os.environ.get("NONVA_FPS") or 20)
JPEG_QUALITY = float(os.environ.get("NONVA_JPEG_QUALITY") or 0.80)

VIEWER_FPS = float(os.environ.get("NONVA_VIEWER_FPS") or 20)             # MJPEG relay rate to the /viewer page
VIEWER_INTERVAL = 1.0 / VIEWER_FPS
# DGIST 2026-11-14: QoS of this service for the viewers = the measured UL latency of a frame against this deadline. A frame that missed it
# does not replace the picture the viewers get (1; 0 = every frame is relayed as before and only the verdict is reported)
NONVA_DEADLINE_MS = float(os.environ.get("NONVA_DEADLINE_MS") or 250)
NONVA_VIEWER_QOS_FREEZE = int(os.environ.get("NONVA_VIEWER_QOS_FREEZE") or 1)

CLOCK_SYNC_SAMPLES = 30
CLOCK_SYNC_BEST_SAMPLES = 5


# =====================================================
# FastAPI
# =====================================================

app = FastAPI(
    title="Non-VA Real-Time Video Streaming Service"
)
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))   # ./service_common (hsw module, unchanged)
from service_common.service_telemetry import attach_telemetry
telemetry = attach_telemetry(app, 'nonva')

# ---- DGIST measurement dashboard feed (dash/dash_server.py, 2026-11-12): one event per uploaded frame in a ring the dashboard
# polls from this host (GET /metrics/frames?since=N, loopback only: nothing of it travels over the cell).
import threading
DASH_RING = deque(maxlen=int(os.environ.get("NONVA_DASH_RING", "8000") or 8000))
_dash_lock = threading.Lock()
_dash_seq = 0


def dash_event(ev):
    global _dash_seq
    with _dash_lock:
        _dash_seq += 1
        ev["s"] = _dash_seq
        DASH_RING.append(ev)

# GPU-wide telemetry only; never used for scheduling or latency/TP calculation.
# Match the VA service's default physical GPU and CSV column names.
gpu_nvml = None
gpu_handle = None
gpu_read_warning = False


@app.on_event("startup")
def start_gpu_metrics():
    global gpu_nvml, gpu_handle
    try:
        import pynvml
        pynvml.nvmlInit()
        gpu_nvml = pynvml
        device = os.environ.get("PPO_GPU_DEVICE", "0")
        gpu_handle = (pynvml.nvmlDeviceGetHandleByIndex(int(device))
                      if device.isdecimal()
                      else pynvml.nvmlDeviceGetHandleByUUID(device))
        logging.getLogger(__name__).info("Non-VA GPU logging enabled: %s", device)
    except Exception as exc:
        logging.getLogger(__name__).warning(
            "Non-VA GPU logging unavailable; CSV GPU fields will be empty: %s", exc)


@app.on_event("shutdown")
def stop_gpu_metrics():
    global gpu_nvml, gpu_handle
    if gpu_nvml is not None:
        try:
            gpu_nvml.nvmlShutdown()
        except Exception:
            logging.getLogger(__name__).warning("NVML shutdown failed", exc_info=True)
    gpu_nvml = gpu_handle = None


def read_gpu_metrics():
    """Recent NVML sampling-window utilization, not per-frame/per-UE GPU use."""
    global gpu_read_warning
    if gpu_handle is None:
        return None, None
    try:
        util = gpu_nvml.nvmlDeviceGetUtilizationRates(gpu_handle).gpu
        memory = gpu_nvml.nvmlDeviceGetMemoryInfo(gpu_handle).used
        gpu_read_warning = False
        return float(util), round(memory / 1024 ** 3, 3)
    except Exception as exc:
        if not gpu_read_warning:
            logging.getLogger(__name__).warning("GPU metric read failed: %s", exc)
        gpu_read_warning = True
        return None, None


# =====================================================
# Log Directory
# =====================================================

LOG_DIR = Path(os.environ.get("NONVA_LOG_DIR") or (Path(__file__).resolve().parent.parent / "runs" / "nonva_service_log"))
LOG_DIR.mkdir(parents=True, exist_ok=True)


# =====================================================
# Independent UE sessions (single Uvicorn worker)
# =====================================================
from dataclasses import dataclass, field
from fastapi import HTTPException

@dataclass
class UESession:
    session_id: str
    session_token: str
    active: bool = True
    records: list = field(default_factory=list)
    recent_frames: deque = field(default_factory=deque)
    latest_frame: bytes | None = None
    latest_frame_id: int = 0
    latest_frame_size: int = 0
    latest_receive_time: float = 0.0
    latest_ul_latency_ms: float = 0.0
    clock_offset_ms: float = 0.0
    sync_rtt_ms: float = 0.0
    clock_sync_time_ms: float = 0.0
    session_start_time_ms: float = 0.0
    total_startup_time_ms: float = 0.0
    # DGIST 2026-11-14: QoS verdict and the picture the viewers get (qos_update)
    qos_sid: int = field(default_factory=lambda: int(time.time() * 1000))   # tells the sessions of one UE apart
    qos_seq: int = 0                  # frames judged
    qos_verdict: str = "none"         # met | missed | none (verdict of the latest frame)
    qos_ul_ms: float | None = None    # its UL latency; None = not measurable (no usable send time)
    qos_met: int = 0
    qos_missed: int = 0
    qos_streak: int = 0               # misses in a row
    qos_stale: int = 0                # frames not shown because a newer capture was on the screen already
    qos_recent: deque = field(default_factory=lambda: deque(maxlen=100))   # verdicts of the last 100 frames
    shown_frame: bytes | None = None  # JPEG the viewers get, exactly as the phone sent it
    shown_frame_id: int = 0
    shown_seq: int = 0                # picture changes
    shown_time: float = 0.0           # time.monotonic() of the last picture change
    shown_capture_ms: float = 0.0     # send time of the shown frame (server clock)

sessions: dict[str, UESession] = {}
VALID_UE_IDS = {"ue1", "ue2"}

def get_session(session_id: str) -> UESession:
    session = sessions.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Unknown session ID")
    return session

def update_receive_stats(session: UESession, frame_size: int):
    now = time.monotonic()
    session.latest_receive_time = now
    session.recent_frames.append((now, frame_size))
    while session.recent_frames and now - session.recent_frames[0][0] > 2.0:
        session.recent_frames.popleft()

def get_receive_stats(session: UESession):
    now = time.monotonic()
    while session.recent_frames and now - session.recent_frames[0][0] > 2.0:
        session.recent_frames.popleft()
    if len(session.recent_frames) < 2:
        return 0.0, 0.0
    duration = session.recent_frames[-1][0] - session.recent_frames[0][0]
    if duration <= 0:
        return 0.0, 0.0
    fps = (len(session.recent_frames) - 1) / duration
    mbps = sum(size for _, size in session.recent_frames) * 8 / duration / 1_000_000
    return fps, mbps
def qos_update(session: UESession, frame: bytes, frame_id: int, capture_ms: float, ul_ms: float | None):
    """DGIST 2026-11-14: verdict of one uploaded frame for the viewers - the rule of the VA viewer (va_service ViewerState.update): the
    status changes with every frame, the picture only with a frame that met the deadline (NONVA_VIEWER_QOS_FREEZE)"""
    met = ul_ms is not None and ul_ms <= NONVA_DEADLINE_MS      # a frame without a usable send time counts as missed
    session.qos_seq += 1
    session.qos_ul_ms = ul_ms
    if met:
        session.qos_verdict = "met"; session.qos_met += 1; session.qos_streak = 0
    else:
        session.qos_verdict = "missed"; session.qos_missed += 1; session.qos_streak += 1
    session.qos_recent.append(met)
    if met or not NONVA_VIEWER_QOS_FREEZE:
        if not math.isfinite(capture_ms):
            capture_ms = session.shown_capture_ms
        if capture_ms < session.shown_capture_ms:
            session.qos_stale += 1            # an older capture arrived after a newer one was shown: keep the newer picture
        else:
            session.shown_frame, session.shown_frame_id = frame, frame_id
            session.shown_capture_ms, session.shown_time = capture_ms, time.monotonic()
            session.shown_seq += 1

def qos_state(session: UESession) -> dict:
    """what a viewer needs to show the verdict: X-NV-Meta header of every MJPEG part, "qos" in /receiver_stats"""
    ul, recent = session.qos_ul_ms, session.qos_recent
    return {"sid": session.qos_sid, "qseq": session.qos_seq, "verdict": session.qos_verdict,
            "ul_ms": round(ul, 1) if ul is not None else None, "deadline_ms": NONVA_DEADLINE_MS, "freeze": NONVA_VIEWER_QOS_FREEZE,
            "streak": session.qos_streak, "met": session.qos_met, "missed": session.qos_missed, "stale": session.qos_stale,
            "met_pct": round(100.0 * sum(recent) / len(recent), 1) if recent else None,
            "shown_frame_id": session.shown_frame_id, "shown_seq": session.shown_seq,
            "held_ms": round((time.monotonic() - session.shown_time) * 1000.0) if session.shown_seq else None}

# =====================================================
# iPhone Sender Page
# =====================================================

def _page(s):
    """fill the configuration placeholders of the sender page (NONVA_* environment, hsw defaults)"""
    for k, v in (("__NONVA_WIDTH__", TARGET_WIDTH), ("__NONVA_HEIGHT__", TARGET_HEIGHT), ("__NONVA_FPS__", f"{TARGET_FPS:g}"),
                 ("__NONVA_JPEG_QUALITY__", f"{JPEG_QUALITY:g}")):
        s = s.replace(k, str(v))
    return s


@app.get(
    "/",
    response_class=HTMLResponse
)
async def sender_page():

    return _page("""
<!DOCTYPE html>

<html>

<head>

<meta charset="UTF-8">

<meta name="viewport"
      content="width=device-width, initial-scale=1.0">

<title>Non-VA Video Streaming</title>


<style>

body {
    font-family: Arial, sans-serif;
}

video {
    width: 100%;
    max-width: 720px;
    background: black;
}

/* DGIST 2026-11-12: #stage is the element that goes full screen (the video fills the screen, aspect kept) */
#stage {
    position: relative;
}

#stage:fullscreen {
    width: 100vw;
    height: 100vh;
    background: black;
}

#stage:fullscreen video {
    position: absolute;
    top: 0;
    left: 0;
    width: 100%;
    height: 100%;
    max-width: none;
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


<h1>Real-Time Video Streaming Service</h1>




<p>
<strong>Target:</strong>
__NONVA_WIDTH__x__NONVA_HEIGHT__ / __NONVA_FPS__ FPS / JPEG Quality __NONVA_JPEG_QUALITY__ (non-VA: upload + live viewer, no inference)
</p>


<!-- =================================================
     Camera
     ================================================= -->

<div id="stage">

<video id="camera"
       autoplay
       playsinline
       muted>
</video>

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


<br><br>


<!-- =================================================
     Camera Control
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


<!-- =================================================
     Streaming Control
     ================================================= -->

<button onclick="startStreaming()">
    Start Streaming
</button>


<button onclick="stopStreaming()">
    Stop Streaming
</button>


<!-- =================================================
     Service Status
     ================================================= -->
<div class="info-box">
<h2>Service Status</h2>
<p>Status: <strong id="status">Stopped</strong></p>
<p>Frame Count: <strong id="frameCount">0</strong></p>
<p>Log Status: <strong id="logStatus">No log</strong></p>
<p>UE: <select id="ueSelector"><option value="ue1">UE1</option><option value="ue2">UE2</option></select></p>
</div>

<!-- =================================================
     Clock Synchronization
     ================================================= -->
<div class="info-box">
<h2>Clock Synchronization</h2>
<p>Clock Sync: <strong id="clockSyncStatus">Not synchronized</strong></p>
<p>Estimated Clock Offset: <strong id="clockOffset">-</strong></p>
<p>Best Sync RTT: <strong id="syncRTT">-</strong></p>
</div>

<!-- =================================================
     Non-VA Service Traffic
     ================================================= -->
<div class="info-box">
<h2>Non-VA Service Traffic</h2>
<p>Camera / Video Resolution: <strong id="resolution">-</strong></p>
<p>UL JPEG Frame Size: <strong id="frameSize">-</strong></p>
<p>Actual FPS: <strong id="sendFPS">-</strong></p>
<p>UL Payload Rate: <strong id="ulRate">-</strong></p>
</div>

<!-- =================================================
     Non-VA Service Latency
     ================================================= -->
<div class="info-box">
<h2>Non-VA Service Latency</h2>
<p>UL Latency: <strong id="streamingLatency">-</strong></p>
<p>Non-VA Service E2E Latency: <strong id="e2eLatency">-</strong></p>
</div>

<script src="/telemetry_client.js"></script>
<script>


// =====================================================
// Configuration
// =====================================================

const CAMERA_WIDTH = __NONVA_WIDTH__;

const CAMERA_HEIGHT = __NONVA_HEIGHT__;


const TARGET_FPS = __NONVA_FPS__;


const TARGET_INTERVAL =
    1000 / TARGET_FPS;


const JPEG_QUALITY = __NONVA_JPEG_QUALITY__;


const MAX_INFLIGHT = 4;


const CLOCK_SYNC_SAMPLES = 30;

const CLOCK_SYNC_BEST_SAMPLES = 5;


// =====================================================
// Global Variables
// =====================================================

let stream = null;

let streaming = false;

let timerId = null;


let frameCount = 0;

let frameSequence = 0;

let droppedFrames = 0;

let inFlight = 0;


let totalPayloadBytes = 0;

let streamStartTime = 0;


let clockOffsetMs = 0;

let syncBestRttMs = 0;

let clockSynced = false;
let sessionId = null;
let telemetryClient = null;


// =====================================================
// High Resolution Client Timestamp
// =====================================================

function clientNowMs() {

    return (
        performance.timeOrigin
        + performance.now()
    );
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
// =====================================================

async function synchronizeClocks() {

    document.getElementById(
        "clockSyncStatus"
    ).innerText =
        "Synchronizing...";


    const samples = [];


    for (
        let i = 0;
        i < CLOCK_SYNC_SAMPLES;
        i++
    ) {

        try {

            const t0 =
                clientNowMs();


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
                clientNowMs();


            const t1 =
                data.server_receive_ms;


            const t2 =
                data.server_send_ms;


            const rtt =
                (t3 - t0)
                - (t2 - t1);


            // GH200 clock - iPhone clock

            const offset =
                (
                    (t1 - t0)
                    + (t2 - t3)
                ) / 2.0;


            samples.push(
                {
                    rtt: rtt,
                    offset: offset
                }
            );


        } catch (error) {

            console.error(
                "Clock sync error:",
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
// downloaded. The send timer, in-flight gate, upload and logs are the ones of the camera mode. Every frame of a file is scaled
// (aspect kept, black bars) to CAMERA_WIDTH x CAMERA_HEIGHT (.env NONVA_WIDTH/HEIGHT, the size the camera is asked for), so the
// uplink load stays the one this service is configured for; the camera mode sends the camera's native frame as before.
// =====================================================

let fileUrl = null;            // blob: URL of the chosen video file; null = no file source
let wakeLock = null;
let wakeLockPending = false;

function sourceReady() { return stream !== null || fileUrl !== null; }

// aspect-preserving fit of sw x sh into dw x dh, centred
function fitRect(sw, sh, dw, dh) {
    const s = Math.min(dw / sw, dh / sh), w = Math.round(sw * s), h = Math.round(sh * s);
    return { x: Math.floor((dw - w) / 2), y: Math.floor((dh - h) / 2), w: w, h: h };
}

function pickVideoFile() {
    const input = document.getElementById("videoFile");
    input.value = "";            // choosing the same file again must fire "change" too
    input.click();
}

async function onVideoFilePicked(input) {
    const file = input.files && input.files[0];
    if (!file) return;

    await stopCamera();          // ends a running stream (log saved) and releases the previous source, camera or file

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
        alert("Cannot play this video file: " + (error && (error.message || error)));
        return;
    }

    fileUrl = url;
    document.getElementById("resolution").innerText =
        video.videoWidth + " x " + video.videoHeight + " (file " + file.name + ") -> " + CAMERA_WIDTH + " x " + CAMERA_HEIGHT + " sent";
    document.getElementById("status").innerText = "Video Ready";
    wakeLockUpdate();
}

// true when a file source was released
function releaseVideoFile() {
    if (fileUrl === null) return false;
    const video = document.getElementById("camera");
    video.pause(); video.loop = false; video.removeAttribute("src"); video.load();
    URL.revokeObjectURL(fileUrl);
    fileUrl = null;
    document.getElementById("resolution").innerText = "-";
    wakeLockUpdate();
    return true;
}

function fullscreenOn() { return (document.fullscreenElement || document.webkitFullscreenElement || null) !== null; }

async function enterFullscreen() {
    const stage = document.getElementById("stage");
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
        try { await screen.orientation.lock(CAMERA_WIDTH >= CAMERA_HEIGHT ? "landscape" : "portrait"); } catch (e) {}
    }
}

function exitFullscreen() {
    if (!fullscreenOn()) return;
    if (document.exitFullscreen) document.exitFullscreen(); else if (document.webkitExitFullscreen) document.webkitExitFullscreen();
}

function onFullscreenChange() {
    if (!fullscreenOn() && screen.orientation && screen.orientation.unlock) { try { screen.orientation.unlock(); } catch (e) {} }
    wakeLockUpdate();
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
// =====================================================
// DGIST 2026-11-14: video-file source - play the file again from its first frame, either now ("Restart Video") or at a chosen
// clock time ("Start video at"). The clock is the base station's once this page has synchronised with it (Start Streaming does that; it is
// the offset the latency figures use) and this phone's own clock before, so phones set to the same time start together. All of it
// happens on the phone: no request is sent and nothing is downloaded for it; the streaming goes on unchanged while the file starts over.
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

function schedNowMs() { return clockSynced ? clientNowMs() + clockOffsetMs : Date.now(); }
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
        : " (this phone's clock; the base station's clock after Start Streaming)");
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

        await stopCamera();      // DGIST: from the file source back to the camera (ends a running stream, releases the file)
    }

    if (stream !== null) {

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


        video.srcObject =
            stream;


        await video.play();


        document.getElementById(
            "resolution"
        ).innerText =
            video.videoWidth
            + " x "
            + video.videoHeight;


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

    if (streaming) {

        await stopStreaming();
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


    document.getElementById(
        "status"
    ).innerText =
        wasFile
        ? "Video Stopped"
        : "Camera Stopped";
}


// =====================================================
// Start Streaming
// =====================================================

async function startStreaming() {

    if (telemetryClient !== null) {
        alert('Stop the previous session before starting a new one.');
        return;
    }

    const startupStart = performance.now();
    let clockSyncDurationMs = 0;
    let sessionStartDurationMs = 0;

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


    if (streaming) {

        return;
    }


    // =================================================
    // Clock Synchronization
    // =====================================================

    try {

        document.getElementById(
            "status"
        ).innerText =
            "Clock Sync";


        const clockSyncStart = performance.now();
        await synchronizeClocks();
        clockSyncDurationMs = performance.now() - clockSyncStart;


    } catch (error) {

        console.error(error);


        document.getElementById(
            "status"
        ).innerText =
            "Clock Sync Error";


        return;
    }


    // =================================================
    // Start Server-side Experiment Session
    // =====================================================

    try {

        const formData =
            new FormData();


        formData.append(
            "clock_offset_ms",
            clockOffsetMs.toString()
        );


        formData.append(
            "sync_rtt_ms",
            syncBestRttMs.toString()
        );
        formData.append("ue_id", document.getElementById("ueSelector").value);
        formData.append("clock_sync_time_ms", clockSyncDurationMs.toString());
        const sessionStart = performance.now();


        const response =
            await fetch(
                "/start_session",
                {
                    method: "POST",
                    body: formData
                }
            );


        if (!response.ok) {

            throw new Error(
                "Failed to start session."
            );
        }

        const sessionData = await response.json();
        sessionStartDurationMs = performance.now() - sessionStart;
        sessionId = sessionData.session_id;
        telemetryClient = await new TelemetryClient(sessionId, sessionData.session_token).start();
        document.getElementById("ueSelector").disabled = true;


    } catch (error) {

        console.error(error);


        document.getElementById(
            "status"
        ).innerText =
            "Session Error";


        return;
    }


    // =================================================
    // Reset Sender Statistics
    // =====================================================

    const totalStartupDurationMs = performance.now() - startupStart;
    // Diagnostic only: save startup timings on the server; do not modify UI.
    fetch("/startup_metrics", {
        method: "POST",
        body: (() => {
            const fd = new FormData();
            fd.append("session_id", sessionId);
            fd.append("session_token", telemetryClient.token);
            fd.append("session_start_time_ms", sessionStartDurationMs.toString());
            fd.append("total_startup_time_ms", totalStartupDurationMs.toString());
            return fd;
        })()
    }).catch(error => console.error("Startup metrics error:", error));

    streaming = true;


    frameCount = 0;

    frameSequence = 0;

    droppedFrames = 0;

    inFlight = 0;


    totalPayloadBytes = 0;


    streamStartTime =
        performance.now();


    document.getElementById(
        "frameCount"
    ).innerText =
        "0";


    document.getElementById(
        "logStatus"
    ).innerText =
        "Recording";


    document.getElementById(
        "status"
    ).innerText =
        "Running";


    // =================================================
    // First Frame Immediately
    // =====================================================

    captureAndSend();


    // =================================================
    // Continue at Target 20 FPS
    // =====================================================

    timerId =
        setInterval(
            captureAndSend,
            TARGET_INTERVAL
        );
}


// =====================================================
// Stop Streaming
// =====================================================

async function stopStreaming() {

    if (!streaming && telemetryClient === null) {

        return;
    }


    streaming = false;


    if (timerId !== null) {

        clearInterval(
            timerId
        );


        timerId = null;
    }


    document.getElementById(
        "status"
    ).innerText =
        "Saving Log";


    // =================================================
    // Wait for Remaining Uploads
    // =====================================================

    const waitStart =
        performance.now();


    while (
        inFlight > 0
        &&
        (
            performance.now()
            - waitStart
        ) < 3000
    ) {

        await new Promise(
            resolve =>
                setTimeout(
                    resolve,
                    20
                )
        );
    }


    // =================================================
    // Stop Server-side Session + Save CSV
    // =====================================================

    try {

        const response =
            await fetch(
                "/stop_session?session_id=" + encodeURIComponent(sessionId)
                    + "&session_token=" + encodeURIComponent(telemetryClient.token),
                {
                    method: "POST"
                }
            );


        if (!response.ok) {

            throw new Error(
                "Failed to save log."
            );
        }


        const result =
            await response.json();
        sessionId = null;
        telemetryClient.close();
        telemetryClient = null;
        document.getElementById("ueSelector").disabled = false;

        document.getElementById(
            "logStatus"
        ).innerText =
            "Saved: "
            + result.filename;


    } catch (error) {

        console.error(error);


        document.getElementById(
            "logStatus"
        ).innerText =
            "Save Error";
    }


    if (stream !== null) {

        document.getElementById(
            "status"
        ).innerText =
            "Camera Ready";

    } else if (fileUrl !== null) {

        document.getElementById(
            "status"
        ).innerText =
            "Video Ready";

    } else {

        document.getElementById(
            "status"
        ).innerText =
            "Stopped";
    }
}


// =====================================================
// Capture + Send Frame
// =====================================================

async function captureAndSend() {
    const frameTelemetry = telemetryClient;
    if (!streaming) {

        return;
    }


    // =================================================
    // Avoid Unlimited Request Queue
    // =====================================================

    if (inFlight >= MAX_INFLIGHT) {

        droppedFrames++;


        return;
    }


    const video =
        document.getElementById(
            "camera"
        );


    if (
        video.videoWidth === 0
        ||
        video.videoHeight === 0
    ) {

        return;
    }


    const canvas =
        document.getElementById(
            "captureCanvas"
        );


    const context =
        canvas.getContext("2d");


    // =================================================
    // Capture Camera Frame
    // =====================================================

    if (fileUrl !== null) {

        // DGIST file source: CAMERA_WIDTH x CAMERA_HEIGHT, aspect kept, black bars
        const fit = fitRect(video.videoWidth, video.videoHeight, CAMERA_WIDTH, CAMERA_HEIGHT);

        canvas.width = CAMERA_WIDTH;
        canvas.height = CAMERA_HEIGHT;

        context.fillStyle = "black";
        context.fillRect(0, 0, CAMERA_WIDTH, CAMERA_HEIGHT);
        context.imageSmoothingQuality = "high";
        context.drawImage(video, fit.x, fit.y, fit.w, fit.h);

    } else {

        canvas.width =
            video.videoWidth;


        canvas.height =
            video.videoHeight;


        context.drawImage(
            video,
            0,
            0,
            canvas.width,
            canvas.height
        );
    }


    // =================================================
    // JPEG Encoding
    // =====================================================

    const blob =
        await new Promise(
            resolve => {

                canvas.toBlob(
                    resolve,
                    "image/jpeg",
                    JPEG_QUALITY
                );

            }
        );


    if (
        blob === null
        ||
        !streaming || telemetryClient !== frameTelemetry
    ) {

        return;
    }


    // =================================================
    // t1: Upload Start
    // =====================================================

    const uploadStartMs =
        clientNowMs();


    // =================================================
    // Frame Information
    // =====================================================

    frameSequence++;


    const currentFrameId =
        frameSequence;


    const frameSizeBytes =
        blob.size;


    const frameSizeKB =
        frameSizeBytes
        / 1024.0;


    // =================================================
    // Create Upload Request
    // =====================================================

    const formData =
        new FormData();


    formData.append(
        "file",
        blob,
        "frame.jpg"
    );


    formData.append(
        "frame_id",
        currentFrameId.toString()
    );

    formData.append("session_id", sessionId);
    formData.append('session_token', frameTelemetry.token);


    formData.append(
        "upload_start_ms",
        uploadStartMs.toString()
    );


    inFlight++;
    const uploadAbort = new AbortController();
    const uploadTimeout = setTimeout(() => uploadAbort.abort(), 5000);


    try {

        const response =
            await fetch(
                "/upload_frame",
                {
                    method: "POST",
                    body: formData,
                    signal: uploadAbort.signal
                }
            );


        if (!response.ok) {

            throw new Error(
                "Upload error: "
                + response.status
            );
        }


        // =================================================
        // Frame Successfully Received by Server
        // =====================================================

        frameCount++;


        totalPayloadBytes +=
            frameSizeBytes;


        const elapsedSec =
            (
                performance.now()
                - streamStartTime
            ) / 1000.0;


        const actualFPS =
            frameCount
            / elapsedSec;


        const ulPayloadMbps =
            (
                totalPayloadBytes
                * 8.0
            )
            / elapsedSec
            / 1_000_000.0;


        // =================================================
        // Server-calculated Latency Values
        // =====================================================

        const data =
            await response.json();


        // =================================================
        // Update Sender UI
        // =====================================================

        document.getElementById(
            "frameCount"
        ).innerText =
            frameCount;


        document.getElementById(
            "frameSize"
        ).innerText =
            frameSizeKB.toFixed(2)
            + " KB";


        document.getElementById(
            "sendFPS"
        ).innerText =
            actualFPS.toFixed(2)
            + " FPS";


        document.getElementById(
            "ulRate"
        ).innerText =
            ulPayloadMbps.toFixed(2)
            + " Mbps";


        document.getElementById(
            "streamingLatency"
        ).innerText =
            data.ul_latency_ms.toFixed(2)
            + " ms";

        document.getElementById(
            "e2eLatency"
        ).innerText =
            data.e2e_latency_ms.toFixed(2)
            + " ms";


    } catch (error) {

        console.error(error);


        void frameTelemetry.failure(currentFrameId);
    } finally {
        clearTimeout(uploadTimeout);
        if (telemetryClient === frameTelemetry) inFlight = Math.max(0, inFlight - 1);
    }
}

</script>


</body>

</html>
""")


# =====================================================
# GH200 Viewer Page
# =====================================================

@app.get("/viewer/{ue_id}", response_class=HTMLResponse)
@app.get("/viewer", response_class=HTMLResponse)
async def viewer_page(ue_id: str | None = None):
    # The browser obtains the fixed UE ID from its URL path.
    if ue_id is not None and ue_id not in VALID_UE_IDS:
        raise HTTPException(status_code=404, detail="Unknown UE")

    return """
<!DOCTYPE html>

<html>

<head>

<meta charset="UTF-8">

<meta name="viewport"
      content="width=device-width, initial-scale=1.0">

<title>GH200 Video Receiver</title>


<style>

body {
    font-family: Arial, sans-serif;
    background: #111;
    color: white;
}

#liveFrame {
    width: 100%;
    max-width: 1280px;
    background: black;
    border: 1px solid #555;
}
/* DGIST 2026-11-14: QoS badge over the held picture (a frame above the UL deadline does not replace it) */
#liveWrap {
    position: relative;
    max-width: 1280px;
}

#liveWrap.frozen #liveFrame {
    filter: saturate(.7) brightness(.8);
}

#qosBadge {
    display: none;
    position: absolute;
    top: 12px;
    left: 12px;
    padding: 8px 14px;
    font-weight: bold;
    font-size: 18px;
    color: white;
    background: rgba(200, 30, 30, 0.9);
    border-radius: 6px;
}

#qosBadge.show {
    display: block;
}

#qosBadge.grey {
    background: rgba(60, 70, 80, 0.92);
}

#qosBadge small {
    display: block;
    font-weight: normal;
    font-size: 13px;
}

.info-box {
    margin-top: 15px;
    padding: 10px;
    border: 1px solid #555;
    max-width: 650px;
}

</style>

</head>


<body>


<h1>GH200 Real-Time Video Receiver</h1>
<p>UE Session: <strong id="viewerSession">-</strong></p>


<p>
Private 5G UL Video Streaming
</p>


<p>
Viewer Output:
<strong>20 FPS Pacing</strong>
</p>


<!-- =================================================
     MJPEG Video
     ================================================= -->

<div id="liveWrap">
<img id="liveFrame"
     src=""
     alt="Waiting for video stream...">
<div id="qosBadge"><span id="qosBadgeMain"></span><small id="qosBadgeSub"></small></div>
</div>


<!-- =================================================
     Receiver Status
     ================================================= -->

<div class="info-box">


<h2>Receiver Status</h2>


<p>
Status:
<strong id="status">
Waiting
</strong>
</p>


<p>
Received Frame ID:
<strong id="frameId">
0
</strong>
</p>


<p>
Latest Frame Size:
<strong id="frameSize">
-
</strong>
</p>


<p>
Receive FPS:
<strong id="receiveFPS">
-
</strong>
</p>


<p>
Received UL Payload Rate:
<strong id="receiveRate">
-
</strong>
</p>


<p>
Viewer Target FPS:
<strong>
20 FPS
</strong>
</p>


</div>


<!-- =================================================
     QoS (DGIST 2026-11-14): UL latency of every frame against the deadline
     ================================================= -->

<div class="info-box">


<h2>QoS</h2>


<p>
Deadline (UL latency):
<strong id="qosDeadline">
-
</strong>
</p>


<p>
Latest Frame:
<strong id="qosVerdict">
-
</strong>
</p>


<p>
Met / Missed:
<strong id="qosCounts">
-
</strong>
</p>


</div>
<!-- =================================================
     Streaming Latency
     ================================================= -->

<div class="info-box">


<h2>Streaming Latency</h2>


<p>
UL Latency:
<strong id="streamingLatency">
-
</strong>
</p>

<p>
E2E Latency (UL-based estimate):
<strong id="e2eLatency">
-
</strong>
</p>


</div>


<!-- =================================================
     Clock Information
     ================================================= -->

<div class="info-box">


<h2>Clock Synchronization</h2>


<p>
Estimated Clock Offset:
<strong id="clockOffset">
-
</strong>
</p>


<p>
Best Sync RTT:
<strong id="syncRTT">
-
</strong>
</p>


</div>



<script>


// =====================================================
// Refresh Receiver Statistics
// =====================================================

const viewerSessionId = window.location.pathname.startsWith("/viewer/")
    ? window.location.pathname.split("/").filter(Boolean).pop()
    : new URLSearchParams(window.location.search).get("session_id");
document.getElementById("viewerSession").innerText = viewerSessionId || "Missing session_id";

const liveFrame = document.getElementById("liveFrame");
let feedConnected = false;
let feedRetryAt = 0;
let lastFrameId = -1;
let lastFrameChangeAt = Date.now();

function reconnectFeed() {
    if (!viewerSessionId || Date.now() < feedRetryAt) return;
    feedRetryAt = Date.now() + 2000;
    // Do not repeatedly reset a healthy multipart MJPEG connection.
    // An <img> onload event is not reliable as a per-frame MJPEG heartbeat.
    feedConnected = true;
    liveFrame.src = "/video_feed?session_id=" + encodeURIComponent(viewerSessionId)
        + "&t=" + Date.now();
}

liveFrame.onload = () => {
    feedConnected = true;
};
liveFrame.onerror = () => {
    feedConnected = false;
    document.getElementById("status").innerText = "Reconnecting";
};

async function refreshStats() {

    try {

        const response =
            await fetch(
                "/receiver_stats?session_id=" + encodeURIComponent(viewerSessionId)
                + "&t=" + Date.now(),
                {
                    cache: "no-store"
                }
            );


        if (!response.ok) {
            document.getElementById("status").innerText = "Waiting for UE";
            if (Date.now() >= feedRetryAt) reconnectFeed();
            return;
        }

        const data =
            await response.json();
        qosTake(data);      // DGIST 2026-11-14

        if (data.frame_id !== lastFrameId) {
            lastFrameId = data.frame_id;
            lastFrameChangeAt = Date.now();
        }
        // Only reconnect when no connection was established or an error occurred.
        // Never use <img>.onload or a timer to restart a healthy MJPEG feed.
        if (data.active && !feedConnected && Date.now() >= feedRetryAt) {
            reconnectFeed();
        }

        // =================================================
        // Receiver Information
        // =====================================================

        document.getElementById(
            "frameId"
        ).innerText =
            data.frame_id;


        document.getElementById(
            "frameSize"
        ).innerText =
            data.frame_size_kb.toFixed(2)
            + " KB";


        document.getElementById(
            "receiveFPS"
        ).innerText =
            data.receive_fps.toFixed(2)
            + " FPS";


        document.getElementById(
            "receiveRate"
        ).innerText =
            data.receive_mbps.toFixed(2)
            + " Mbps";


        document.getElementById(
            "status"
        ).innerText =
            data.active
            ? "Receiving"
            : "Waiting";


        // =================================================
        // Latency Information
        // =====================================================

        document.getElementById(
            "streamingLatency"
        ).innerText =
            data.ul_latency_ms.toFixed(2)
            + " ms";

        document.getElementById(
            "e2eLatency"
        ).innerText =
            data.e2e_latency_ms.toFixed(2)
            + " ms";


        // =================================================
        // Clock Information
        // =====================================================

        document.getElementById(
            "clockOffset"
        ).innerText =
            data.clock_offset_ms.toFixed(3)
            + " ms";


        document.getElementById(
            "syncRTT"
        ).innerText =
            data.sync_rtt_ms.toFixed(3)
            + " ms";


    } catch (error) {

        console.error(error);
    }
}


// =====================================================
// Update Statistics Every 500 ms
// =====================================================

setInterval(
    refreshStats,
    500
);


refreshStats();


// =====================================================
// DGIST 2026-11-14: QoS verdict. The service compares the measured UL latency of every frame with its deadline (NONVA_DEADLINE_MS) and
// relays only the frames that met it, so the picture stands still while frames miss it; the badge says for how long.
// =====================================================

let qos = null;
let qosAt = 0;
let qosActive = false;
let qosFps = 20;

function qosTake(data) {
    if (!data.qos) return;
    qos = data.qos;
    qosAt = performance.now();
    qosActive = !!data.active;
    qosFps = data.viewer_target_fps || 20;
    document.getElementById("qosDeadline").innerText = qos.deadline_ms + " ms" + (qos.freeze ? " (a frame above it does not replace the picture)" : "");
    document.getElementById("qosVerdict").innerText = qos.verdict === "none" ? "-"
        : (qos.verdict === "met" ? "Met" : "MISSED") + (qos.ul_ms === null ? "" : " (UL " + qos.ul_ms.toFixed(1) + " ms)")
          + (qos.streak > 0 ? ", " + qos.streak + " in a row" : "");
    document.getElementById("qosCounts").innerText = qos.met + " / " + qos.missed
        + (qos.met_pct === null ? "" : " (last 100 frames: " + qos.met_pct.toFixed(1) + " % met)") + (qos.stale ? ", out of order " + qos.stale : "");
}

function qosPaint() {
    let main = "", sub = "", grey = false;
    if (qos && qos.shown_seq > 0) {
        const held = qos.held_ms + (performance.now() - qosAt);
        if (qosActive && qos.verdict === "missed" && qos.freeze) {
            main = "QoS miss - picture held " + (held / 1000).toFixed(2) + " s";
            sub = (qos.ul_ms === null ? "UL latency unknown" : "UL " + Math.round(qos.ul_ms) + " ms > " + qos.deadline_ms + " ms") + ", " + qos.streak + " in a row";
        } else if (held > Math.max(1000, qos.deadline_ms + 3000 / Math.max(1, qosFps))) {
            main = (qosActive ? "No update - " : "UE not sending - ") + (held / 1000).toFixed(1) + " s";
            grey = true;
        }
    }
    document.getElementById("liveWrap").className = main ? "frozen" : "";
    document.getElementById("qosBadge").className = main ? (grey ? "show grey" : "show") : "";
    document.getElementById("qosBadgeMain").innerText = main;
    document.getElementById("qosBadgeSub").innerText = sub;
}

setInterval(qosPaint, 100);
</script>


</body>

</html>
"""

# =====================================================
# Clock Synchronization Endpoint
# =====================================================

@app.get("/clock_sync")
async def clock_sync():

    server_receive_ms = (
        time.time_ns()
        / 1_000_000.0
    )


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
# Per-UE experiment sessions
# =====================================================
@app.post("/start_session")
async def start_session(
    clock_offset_ms: float = Form(...),
    sync_rtt_ms: float = Form(...),
    ue_id: str = Form(...),
    clock_sync_time_ms: float = Form(0.0),
):
    if ue_id not in VALID_UE_IDS:
        raise HTTPException(status_code=400, detail="UE must be ue1 or ue2")
    existing = sessions.get(ue_id)
    if existing is not None and existing.active:
        raise HTTPException(status_code=409, detail=f"{ue_id} is already streaming")
    try:
        token = telemetry.start(ue_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    sessions[ue_id] = UESession(
        session_id=ue_id,
        session_token=token,
        clock_offset_ms=clock_offset_ms,
        sync_rtt_ms=sync_rtt_ms,
        clock_sync_time_ms=clock_sync_time_ms,
    )
    print(f"Non-VA {ue_id} session started", flush=True)
    return {"started": True, "session_id": ue_id, "session_token": token,
            "viewer_url": f"/viewer/{ue_id}"}

@app.post("/startup_metrics")
async def startup_metrics(
    session_id: str = Form(...),
    session_token: str = Form(...),
    session_start_time_ms: float = Form(...),
    total_startup_time_ms: float = Form(...),
):
    session = get_session(session_id)
    try:
        telemetry.check(session_id, session_token)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    session.session_start_time_ms = session_start_time_ms
    session.total_startup_time_ms = total_startup_time_ms
    return {"ok": True}

@app.post("/upload_frame")
async def upload_frame(
    file: UploadFile = File(...),
    frame_id: int = Form(...),
    upload_start_ms: float = Form(...),
    session_id: str = Form(...),
    session_token: str = Form(...),
    request: Request = None,
):
    session = get_session(session_id)
    try:
        telemetry.check(session_id, session_token)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if not session.active:
        raise HTTPException(status_code=409, detail="Session stopped")
    server_receive_ms = time.time_ns() / 1_000_000.0
    frame_data = await file.read()
    frame_size = len(frame_data)
    raw_ul_latency_ms = server_receive_ms - (upload_start_ms + session.clock_offset_ms)
    ul_latency_ms = max(0.0, raw_ul_latency_ms)
    # Upload completion, not viewer delivery; preserve this service's boundary.
    # Bad clock alignment must not become a fictitious zero-latency PPO input.
    import math
    if math.isfinite(raw_ul_latency_ms) and -5 <= raw_ul_latency_ms <= 60000:
        telemetry.result(session_id, session_token, frame_id, ul_latency_ms,
                         frame_size, server_receive_ms)
    else:
        telemetry.result(session_id, session_token, frame_id, success=False)
    try:   # DGIST dashboard feed
        dash_event({"k": "f", "t": server_receive_ms, "ue": session_id, "fid": frame_id,
                    "ip": request.client.host if request is not None and request.client else None,
                    "ul": ul_latency_ms if math.isfinite(raw_ul_latency_ms) and -5 <= raw_ul_latency_ms <= 60000 else None,
                    "kb": frame_size / 1024.0})
    except Exception:
        pass
    # Existing definition: E2E is a provisional UL-only estimate.
    e2e_latency_ms = ul_latency_ms
    session.latest_ul_latency_ms = ul_latency_ms
    session.latest_frame = frame_data
    session.latest_frame_id = frame_id
    session.latest_frame_size = frame_size
    # DGIST 2026-11-14: QoS verdict for the viewers = this frame's measured UL latency against NONVA_DEADLINE_MS; a frame that missed it
    # does not replace the picture they get. The reply to the phone below is unchanged.
    qos_update(session, frame_data, frame_id, upload_start_ms + session.clock_offset_ms,
               ul_latency_ms if math.isfinite(raw_ul_latency_ms) else None)
    update_receive_stats(session, frame_size)
    receive_fps, receive_mbps = get_receive_stats(session)
    gpu_util_percent, gpu_memory_used_gb = read_gpu_metrics()
    session.records.append({
        "frame_id": frame_id,
        "timestamp": datetime.fromtimestamp(server_receive_ms / 1000.0, tz=timezone.utc).isoformat(),
        "frame_size_kb": frame_size / 1024.0,
        "receive_fps": receive_fps,
        "ul_payload_mbps": receive_mbps,
        "ul_latency_ms": ul_latency_ms,
        "e2e_latency_ms": e2e_latency_ms,
        "clock_offset_ms": session.clock_offset_ms,
        "sync_rtt_ms": session.sync_rtt_ms,
        "gpu_util_percent": gpu_util_percent,
        "gpu_memory_used_gb": gpu_memory_used_gb,
    })
    return {"frame_id": frame_id, "ul_latency_ms": ul_latency_ms,
            "e2e_latency_ms": e2e_latency_ms}

@app.post("/stop_session")
async def stop_session(session_id: str, session_token: str):
    session = get_session(session_id)
    try:
        telemetry.stop(session_id, session_token)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    session.active = False
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    filename = f"video_stream_latency_{session_id}_{timestamp}.log"
    filepath = LOG_DIR / filename
    columns = ["frame_id", "timestamp", "frame_size_kb", "receive_fps",
               "ul_payload_mbps", "ul_latency_ms", "e2e_latency_ms",
               "clock_offset_ms", "sync_rtt_ms",
               "clock_sync_time_ms", "session_start_time_ms", "total_startup_time_ms",
               "gpu_util_percent", "gpu_memory_used_gb"]
    with filepath.open("w", newline="", encoding="utf-8") as fp:
        writer = csv.DictWriter(fp, fieldnames=columns)
        writer.writeheader()
        for record in session.records:
            row = dict(record)
            row.update({
                "clock_sync_time_ms": session.clock_sync_time_ms,
                "session_start_time_ms": session.session_start_time_ms,
                "total_startup_time_ms": session.total_startup_time_ms,
            })
            writer.writerow({key: round(value, 3) if isinstance(value, float)
                             else value for key, value in row.items()})
    print(f"Non-VA session {session_id} saved: {filepath}", flush=True)
    return {"saved": True, "filename": filename, "path": str(filepath),
            "records": len(session.records), "session_id": session_id}

async def mjpeg_generator(session_id: str):
    # DGIST 2026-11-14: relays the frame the QoS rule lets through (session.shown_frame: with NONVA_VIEWER_QOS_FREEZE only frames that met
    # the deadline) - never more parts than before. Every part carries the verdict in an X-NV-Meta header; when the verdict changes
    # without a new picture (the first miss after a met frame), the held frame is sent once more ("pic": false) so that a viewer
    # reading the stream learns it at once.
    session = get_session(session_id)
    last_seq = 0
    last_verdict = None
    try:
        while sessions.get(session_id) is session:
            frame = session.shown_frame
            seq = session.shown_seq
            verdict = session.qos_verdict
            new_pic = frame is not None and seq != last_seq
            if not new_pic and (frame is None or verdict == last_verdict):
                if not session.active:
                    break
                await asyncio.sleep(VIEWER_INTERVAL)
                continue
            last_seq = seq
            last_verdict = verdict
            meta = qos_state(session)
            meta["pic"] = new_pic
            yield (b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: " + str(len(frame)).encode()
                   + b"\r\nX-NV-Meta: " + json.dumps(meta, separators=(",", ":")).encode("ascii")
                   + b"\r\n\r\n" + frame + b"\r\n")
            await asyncio.sleep(VIEWER_INTERVAL)
    except asyncio.CancelledError:
        return

@app.get("/video_feed")
async def video_feed(session_id: str, request: Request):
    get_session(session_id)
    headers = {"Cache-Control": "no-store, no-cache, must-revalidate",
               "Pragma": "no-cache"}
    # the VA service's 4-UE viewer (same host, other port) reads this stream with fetch() to paint and record it (2026-11-13): a page served
    # from this host may read it; any other origin gets no CORS header (an <img> shows the stream without one, as before)
    origin = request.headers.get("origin") or ""
    if origin and urlsplit(origin).hostname and urlsplit(origin).hostname == request.url.hostname:
        headers["Access-Control-Allow-Origin"] = origin
        headers["Vary"] = "Origin"
    return StreamingResponse(
        mjpeg_generator(session_id),
        media_type="multipart/x-mixed-replace; boundary=frame",
        headers=headers,
    )

@app.get("/receiver_stats")
async def receiver_stats(session_id: str):
    session = get_session(session_id)
    receive_fps, receive_mbps = get_receive_stats(session)
    active = (session.latest_receive_time > 0 and
              time.monotonic() - session.latest_receive_time < 1.0)
    return {
        "active": active,
        "frame_id": session.latest_frame_id,
        "frame_size_kb": session.latest_frame_size / 1024.0,
        "receive_fps": receive_fps,
        "receive_mbps": receive_mbps,
        "viewer_target_fps": VIEWER_FPS,
        "ul_latency_ms": session.latest_ul_latency_ms,
        "e2e_latency_ms": session.latest_ul_latency_ms,
        "clock_offset_ms": session.clock_offset_ms,
        "sync_rtt_ms": session.sync_rtt_ms,
        "qos": qos_state(session),      # DGIST 2026-11-14: verdict against NONVA_DEADLINE_MS for the viewers
    }


@app.get("/metrics/frames")
async def metrics_frames(request: Request, since: int = 0):
    """DGIST dashboard feed: the events after sequence number `since` (served to this host only)"""
    if request.client is None or request.client.host not in ("127.0.0.1", "::1"):
        raise HTTPException(status_code=403, detail="local only")
    with _dash_lock:
        ev = [e for e in DASH_RING if e["s"] > since]
        seq = _dash_seq
    return {"service": "nonva", "seq": seq, "t": time.time_ns() / 1e6, "ev": ev}


# =====================================================
# Status (nonva.sh status): configuration, UE sessions, telemetry rows
# =====================================================

@app.get("/nonva/status")
async def nonva_status():
    ue = {}
    with telemetry.lock:
        for u, row in telemetry.rows.items():
            ue[u] = {k: row.get(k) for k in ("active", "status", "latency_ms", "frame_id", "failures")}
    return {
        "service": "nonva",
        "config": {"width": TARGET_WIDTH, "height": TARGET_HEIGHT, "fps": TARGET_FPS, "jpeg_quality": JPEG_QUALITY,
                   "viewer_fps": VIEWER_FPS, "deadline_ms": NONVA_DEADLINE_MS, "viewer_qos_freeze": NONVA_VIEWER_QOS_FREEZE,
                   "log_dir": str(LOG_DIR), "telemetry_dir": str(telemetry.directory)},
        "sessions": {sid: {"active": s.active, "frames": len(s.records), "latest_frame_id": s.latest_frame_id,
                           "latest_frame_kb": round(s.latest_frame_size / 1024.0, 1), "latest_ul_latency_ms": s.latest_ul_latency_ms,
                           "qos": qos_state(s)}
                     for sid, s in sessions.items()},
        "ue": ue,
    }
