"""YOLO26x VA Service - UL/DL Latency Test (hsw yolo_service), DGIST dApp edition 2026-11-05

What stays: the phone page (camera -> JPEG -> POST /detect_frame -> boxes + latency breakdown), /clock_sync, /save_latency_log (CSV),
the PPO telemetry (/telemetry/*, service_common/) and the background virtual UE (bg_yolo.py). The hsw original is kept in upstream_hsw/.
What changes: the inference. Instead of ultralytics YOLO inside this process, every frame is sent as one RUN request
(model, QoS deadline, 4K JPEG) to the DGIST dApp agent inside the cuphycontroller (L1) process, and its reply - result figures and
the detections - comes back to the phone (dapp_backend.py; protocol aerial-cuda-accelerated-ran/cuPHY-CP/dapp_hook/docs/XAPP_INTERFACE.md).

Settings come from the environment (_dgist/.env, exported by va.sh):
  VA_BACKEND=dapp|local  VA_MODEL=26x  VA_DEADLINE_MS=200  VA_TARGET_FPS=5  VA_CAMERA_WIDTH=3840  VA_CAMERA_HEIGHT=2160
  VA_JPEG_QUALITY=0.90  VA_MAX_INFLIGHT=4  VA_AGENT=127.0.0.1:9810  VA_LOG_DIR=<_dgist>/runs/va_service_log  VA_VERBOSE=0
Run (va.sh start):  uvicorn app:app --host 0.0.0.0 --port 8000 --ssl-keyfile certs/server.key --ssl-certfile certs/server.crt
"""
from fastapi import FastAPI, File, Form, UploadFile, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from PIL import Image
from pathlib import Path
from datetime import datetime

import csv
import io
import os
import sys
import time
import threading
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

CAMERA_WIDTH = _env("VA_CAMERA_WIDTH", 3840, int)       # 4K UHD capture on the phone
CAMERA_HEIGHT = _env("VA_CAMERA_HEIGHT", 2160, int)
TARGET_FPS = _env("VA_TARGET_FPS", 5.0, float)          # requests per second per UE
MAX_INFLIGHT = _env("VA_MAX_INFLIGHT", 4, int)          # unanswered frames a UE tolerates before skipping a capture
JPEG_QUALITY = _env("VA_JPEG_QUALITY", 0.90, float)

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
                 ("__VA_BACKEND__", VA_BACKEND), ("__VA_MODEL__", VA_MODEL)):
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

<div id="videoContainer">

    <video id="camera"
           autoplay
           playsinline
           muted>
    </video>

    <canvas id="overlay">
    </canvas>

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
    Stop Camera
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
Camera Resolution:
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

const QOS_DEADLINE_MS = __QOS_DEADLINE_MS__;
const VA_BACKEND = "__VA_BACKEND__";
const VA_MODEL = "__VA_MODEL__";

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
        + " ms | ticks skipped: capture busy " + skipBusy + ", inflight>=" + MAX_INFLIGHT + " " + skipInflight + " | inflight now " + inFlight);
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
        clearBoundingBoxes();
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
// Start Camera
// =====================================================

async function startCamera() {

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


    clearBoundingBoxes();


    document.getElementById(
        "status"
    ).innerText =
        "Camera Stopped";
}


// =====================================================
// Start VA
// =====================================================

async function startVA() {
    clientLog("start_va", "pressed: camera=" + (stream !== null) + " running=" + running + " session=" + (telemetryClient !== null) + " ue=" + document.getElementById('ueSelector').value);

    if (startingVA || telemetryClient !== null) {
        alert('Stop the previous session before starting a new one.');
        return;
    }

    if (stream === null) {

        alert(
            "Please start the camera first."
        );

        return;
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


    clearBoundingBoxes();
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

    clearBoundingBoxes();


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
        : "Stopped";
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


    overlay.width =
        video.videoWidth;


    overlay.height =
        video.videoHeight;
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
    if (captureBusy || inFlight >= MAX_INFLIGHT) {
        droppedFrames++;
        if (captureBusy) skipBusy++; else skipInflight++;
        return;
    }
    captureBusy = true;
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
    // Convert Frame to JPEG
    // =====================================================

    const encStart = performance.now();
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
            drawBoundingBoxes(
                data.detections,
                data.image_width,
                data.image_height
            );
        } else {
            clearBoundingBoxes();
        }


        // =================================================
        // Detection List
        // =====================================================

        const detectionList =
            document.getElementById(
                "detections"
            );


        detectionList.innerHTML = "";


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
        "model": VA_MODEL,
        "qos_deadline_ms": VA_DEADLINE_MS,
        "ue_id": ue_id,
        "dapp": dapp,
    }


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
        "config": {"backend": VA_BACKEND, "model": VA_MODEL, "deadline_ms": VA_DEADLINE_MS, "target_fps": TARGET_FPS,
                   "max_inflight": MAX_INFLIGHT, "camera": [CAMERA_WIDTH, CAMERA_HEIGHT], "jpeg_quality": JPEG_QUALITY,
                   "agent": VA_AGENT, "log_dir": str(LOG_DIR), "telemetry_dir": str(telemetry.directory)},
        "backend": backend.status(),
        "ue": ue,
    }


