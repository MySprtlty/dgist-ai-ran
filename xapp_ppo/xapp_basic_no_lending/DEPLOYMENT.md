# Basic xApp PPO: 78 UL PRBs, no lending

This standalone package reuses the existing PPO actor trained for 1200 episodes
with seed 7 in `models/basic_http_no_borrow`. No new training was performed.
It is an integration prototype, not a validated real-network QoS guarantee.
The older top-level `xapp_bundle` is a different, lending-enabled experiment.

## Run an offline check

```powershell
cd "C:\DRL\PPO8 (basic)\xapp_basic_no_lending"
python runtime.py --offline
python verify.py
```

Only PyTorch is required. The actor runs on CPU; the simulator and critic are
not needed. `--offline` replays the example timestamp and sends no E2 control.

## Input contract

The eight neural-network inputs, in order, are:

1. YOLO UL buffer in bytes (not an encoded BSR index).
2. YOLO UL SNR in dB (not CQI or linear SNR).
3. YOLO measured UL throughput in Mbps over the preceding 100 ms.
4. Live UL buffer in bytes.
5. Live UL SNR in dB.
6. Live measured UL throughput in Mbps over the preceding 100 ms.
7. Currently applied YOLO PRB budget.
8. Currently applied live PRB budget.

Use `sample_input.json` for the exact JSON structure. Timestamp is Unix time
in milliseconds. The timestamp and window are validation metadata, not model
features. Runtime performs the training normalization; do not normalize twice.
UE identifiers must be mapped to `yolo_1` and `live_1` by the host xApp.

## Output and integration

The actor predicts a continuous share, rounded into integer budgets summing to
78, with at least one PRB per service. Ratios are derived from these integers.
These are budgets, not measured PRB usage. This version cannot disable a slice
with a zero-PRB allocation.

Load `BasicPolicy` once. Call `XAppController(policy, send_control).tick(sample,
now_ms)` from the xApp's 100-ms timer. Implement `send_control` using your actual
E2 control API; no E2 adapter, service model, or container is supplied here.

The DU must enforce these budgets for the next control interval with NO
cross-service lending. Unused PRBs remain unused. Merely forwarding the JSON
field `borrow_unused_prbs: false` does not configure a scheduler automatically.
Use actual confirmed applied budgets in subsequent telemetry, not unconfirmed
model commands. Reject stale/invalid telemetry, log transport errors, and retain
a previously confirmed safe configuration through host-side error handling.

## Limits

The model was trained on a frame-level simulation with provisional latency
targets of 150 ms for YOLO and 200 ms for live. These are not agreed testbed QoS
requirements. GPU state and latency are not inference inputs. Simulation radio
capacity and SNR are assumptions because measured SNR was unavailable.
This no-lending actor has not demonstrated superior QoS; validate it in shadow
mode before enabling real control. Local inference timing does not include E2
transport or guarantee a real-time deadline.
