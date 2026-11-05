from fastapi import FastAPI, File, Form, UploadFile, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from ultralytics import YOLO
from PIL import Image
from pathlib import Path
from datetime import datetime

import csv
import io
import time
import threading
from starlette.concurrency import run_in_threadpool
import pynvml


# =====================================================
# Configuration
# =====================================================

MODEL_PATH = "yolo26x.pt"
DEVICE = 0

CAMERA_WIDTH = 1920
CAMERA_HEIGHT = 1080

TARGET_FPS = 10
JPEG_QUALITY = 0.90

CLOCK_SYNC_SAMPLES = 30
CLOCK_SYNC_BEST_SAMPLES = 5


# =====================================================
# YOLO Model
# =====================================================

print("Loading YOLO26x...")
model = YOLO(MODEL_PATH)
model_lock = threading.Lock()
print("YOLO26x loaded.")

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
# Log Directory
# =====================================================

LOG_DIR = Path(__file__).resolve().parent.parent / "log" / "va_service_log"
LOG_DIR.mkdir(parents=True, exist_ok=True)


# =====================================================
# FastAPI
# =====================================================

app = FastAPI(
    title="YOLO26x VA Service - UL/DL Latency Test"
)
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from service_common.service_telemetry import attach_telemetry
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


class LatencySession(BaseModel):
    ue_id: str
    session_token: str
    records: list[LatencyRecord]


# =====================================================
# Web Page
# =====================================================

@app.get("/", response_class=HTMLResponse)
async def home():

    return """
<!DOCTYPE html>

<html>

<head>

<meta charset="UTF-8">

<meta name="viewport"
      content="width=device-width, initial-scale=1.0">

<title>YOLO26x VA - UL/DL Latency Test</title>


<style>

body {
    font-family: Arial, sans-serif;
}

#videoContainer {
    position: relative;
    width: 100%;
    max-width: 720px;
}

#camera {
    display: block;
    width: 100%;
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


<h1>
    Real-Time Video
    <br>
    Analytics Service
</h1>

<p>
<strong>Target:</strong> 1080p / 10 FPS / JPEG Quality 0.90
</p>


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


<br><br>


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
    Start Streaming
</button>

<label>VA phone:
<select id="ueSelector"><option value="ue1">UE1</option><option value="ue2">UE2</option></select>
</label>

<button onclick="stopVA()">
    Stop Streaming
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

const CAMERA_WIDTH = 1920;
const CAMERA_HEIGHT = 1080;

const TARGET_FPS = 10;
const targetInterval = 1000 / TARGET_FPS;
const MAX_INFLIGHT = 4;

const JPEG_QUALITY = 0.90;

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
        alert('UE identification failed: ' + error.message);
        return;
    }
    startingVA = false;
    running = true;

    frameCount = 0;
    frameSequence = 0;
    latestDisplayedFrameId = 0;
    droppedFrames = 0;
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
    if (captureBusy || inFlight >= MAX_INFLIGHT) {
        droppedFrames++;
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
    inFlight++;
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
            gpu_memory_used_gb: data.gpu_memory_used_gb

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
"""


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
    try:
        telemetry.check(ue_id, session_token)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    # =================================================
    # t1
    #
    # FastAPI handler starts after the uploaded
    # multipart request has arrived at the server.
    # =====================================================

    server_receive_ms = (
        time.time_ns()
        / 1_000_000.0
    )


    # =================================================
    # Read Uploaded JPEG
    # =====================================================

    image_data = await file.read()


    # =================================================
    # Convert JPEG to RGB Image
    #
    # This time is NOT included in AI inference time.
    # It is included in Server Other Time.
    # =====================================================

    image = Image.open(
        io.BytesIO(image_data)
    ).convert("RGB")


    # =================================================
    # t2
    #
    # AI Processing Start
    # =====================================================

    ai_start_ms = (
        time.time_ns()
        / 1_000_000.0
    )


    # Snapshot of GPU-wide metrics associated with this frame.
    # NVML GPU Util is a recent sampling-window value, not per-frame usage.
    gpu_util_percent, gpu_memory_used_gb = read_gpu_metrics()

    # =================================================
    # YOLO26-X Inference
    # =====================================================

    if enable_yolo:
        # Original YOLO26-X inference and bounding-box extraction.
        def run_prediction():
            with model_lock:
                return model.predict(source=image, device=DEVICE, verbose=False)

        results = await run_in_threadpool(run_prediction)


        # =================================================
        # t3
        #
        # AI Processing End
        # =====================================================

        ai_end_ms = (
            time.time_ns()
            / 1_000_000.0
        )


        processing_ms = (
            ai_end_ms
            - ai_start_ms
        )


        # =================================================
        # Detection Results
        # =====================================================

        result = results[0]

        detections = []


        for box in result.boxes:

            # =============================================
            # Class
            # =============================================

            class_id = int(
                box.cls[0]
            )


            class_name = result.names[
                class_id
            ]


            # =============================================
            # Confidence
            # =============================================

            confidence = float(
                box.conf[0]
            )


            # =============================================
            # Bounding Box
            # =============================================

            x1, y1, x2, y2 = (
                box.xyxy[0].tolist()
            )


            detections.append(
                {
                    "class":
                        class_name,

                    "confidence":
                        round(
                            confidence,
                            3
                        ),

                    "x1":
                        round(
                            float(x1),
                            2
                        ),

                    "y1":
                        round(
                            float(y1),
                            2
                        ),

                    "x2":
                        round(
                            float(x2),
                            2
                        ),

                    "y2":
                        round(
                            float(y2),
                            2
                        )
                }
            )



    else:
        # No YOLO GPU kernels are submitted in this branch.
        # JPEG upload and decode still occur exactly as in the ON path.
        # AI latency is explicitly zero; decode/response work is Server Other.
        ai_end_ms = ai_start_ms
        processing_ms = 0.0
        detections = []


    # =================================================
    # t4
    #
    # GH200 has finished preparing application data
    # for the response.
    # =====================================================

    server_send_ms = (
        time.time_ns()
        / 1_000_000.0
    )


    # =================================================
    # Response to iPhone
    # =====================================================

    return {

        "processing_ms":
            processing_ms,

        "yolo_enabled":
            enable_yolo,

        "server_receive_ms":
            server_receive_ms,

        "ai_start_ms":
            ai_start_ms,

        "ai_end_ms":
            ai_end_ms,

        "server_send_ms":
            server_send_ms,

        "image_width":
            image.width,

        "image_height":
            image.height,

        "detections":
            detections,

        "gpu_util_percent": gpu_util_percent,
        "gpu_memory_used_gb": gpu_memory_used_gb
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
                "gpu_memory_used_gb"
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
                    record.gpu_memory_used_gb
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
configure_background(model, model_lock, read_gpu_metrics, LOG_DIR, DEVICE)
app.include_router(bg_router)


