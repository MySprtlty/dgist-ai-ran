"""Non-VA Real-Time Video Streaming Service (hsw video_stream_service), DGIST copy 2026-11-05

Unchanged behaviour: the phone uploads camera frames (/upload_frame), the server keeps the latest one per UE and relays it as MJPEG
to /viewer/<ue> (/video_feed); UL latency per frame -> CSV at /stop_session; telemetry "nonva" for the hsw PPO xApp. No inference.
DGIST changes: the capture settings come from the environment with the hsw values as defaults (NONVA_WIDTH 1280, NONVA_HEIGHT 720,
NONVA_FPS 20, NONVA_JPEG_QUALITY 0.80, NONVA_VIEWER_FPS 20), the page JS constants are filled by the server, the CSV directory is
<_dgist>/runs/nonva_service_log (NONVA_LOG_DIR), service_common/ lives next to this file, GET /nonva/status added. Run: nonva.sh start
(uvicorn app:app --host 0.0.0.0 --port 8001 --ssl-keyfile certs/server.key --ssl-certfile certs/server.crt). Original: upstream_hsw/app.py
"""
from fastapi import (
    FastAPI,
    File,
    Form,
    UploadFile
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
import warnings
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

<video id="camera"
       autoplay
       playsinline
       muted>
</video>


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
    Stop Camera
</button>


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
<p>Camera Resolution: <strong id="resolution">-</strong></p>
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
// Start Camera
// =====================================================

async function startCamera() {

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


    document.getElementById(
        "status"
    ).innerText =
        "Camera Stopped";
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

    if (stream === null) {

        alert(
            "Please start the camera first."
        );

        return;
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

<img id="liveFrame"
     src=""
     alt="Waiting for video stream...">


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
    # Existing definition: E2E is a provisional UL-only estimate.
    e2e_latency_ms = ul_latency_ms
    session.latest_ul_latency_ms = ul_latency_ms
    session.latest_frame = frame_data
    session.latest_frame_id = frame_id
    session.latest_frame_size = frame_size
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
    session = get_session(session_id)
    last_sent_id = -1
    try:
        while sessions.get(session_id) is session:
            if not session.active and session.latest_frame_id == last_sent_id:
                break
            frame = session.latest_frame
            frame_id = session.latest_frame_id
            if frame is None or frame_id == last_sent_id:
                await asyncio.sleep(VIEWER_INTERVAL)
                continue
            last_sent_id = frame_id
            yield (b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                   + str(len(frame)).encode() + b"\r\n\r\n" + frame + b"\r\n")
            await asyncio.sleep(VIEWER_INTERVAL)
    except asyncio.CancelledError:
        return

@app.get("/video_feed")
async def video_feed(session_id: str):
    get_session(session_id)
    return StreamingResponse(
        mjpeg_generator(session_id),
        media_type="multipart/x-mixed-replace; boundary=frame",
        headers={"Cache-Control": "no-store, no-cache, must-revalidate",
                 "Pragma": "no-cache"},
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
    }


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
                   "viewer_fps": VIEWER_FPS, "log_dir": str(LOG_DIR), "telemetry_dir": str(telemetry.directory)},
        "sessions": {sid: {"active": s.active, "frames": len(s.records), "latest_frame_id": s.latest_frame_id,
                           "latest_frame_kb": round(s.latest_frame_size / 1024.0, 1), "latest_ul_latency_ms": s.latest_ul_latency_ms}
                     for sid, s in sessions.items()},
        "ue": ue,
    }
