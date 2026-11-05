# `va_service/` — COTS UE용 VA(영상 분석) 서비스, dApp 연동판 (2026-11-05)

hsw 연구실의 `yolo_service`(전화기 카메라 → HTTPS → GH200에서 YOLO26x 검출 → UL/AI/DL 지연 기록)를 그대로 옮겨 오고,
**추론만 우리 cuPHY Controller 안의 dApp 에이전트에 RUN 요청으로 보내도록** 바꾼 것이다. 전화기가 보는 화면·API·CSV 형식은 hsw 것과
같고, 열이 몇 개 늘었다. 원본은 `upstream_hsw/`에 그대로 두었다 (차이: `../docs/patches/va_service_dapp.patch`).

```
전화기(UE, 10.45.0.x)                      GH200 (host)                                      dgist-l1 컨테이너 (network host)
  브라우저 https://10.45.0.1:8000/   ──POST /detect_frame (4K JPEG, ue_id)──▶  va_service app.py (uvicorn, yolo_env)
  getUserMedia 3840x2160, 5 req/s                                            │ dapp_backend.py: RUN {model 26x, qos 200 ms} + JPEG
  JPEG q0.90 ≈ 2.3 MB/프레임                                                 └──TCP 9810 JSON 줄──▶ dApp agent (cuphycontroller 프로세스)
  ◀── JSON {detections, 서버 타임스탬프, dapp{sm, wait, decode, infer, latency}} ◀── result {status, sm, infer_ms, dets[...]}
  박스 그리기, E2E = UL + AI + 기타 + DL, QoS 판정, Stop VA 때 CSV 저장(/save_latency_log)      GPU: 녹색 컨텍스트로 L1과 SM 공유, nvjpeg 디코드
```

## 설정 (`../.env`의 `VA_*`, `va.sh`가 export)

| 변수 | 기본값 | 뜻 |
|---|---|---|
| `VA_BACKEND` | `dapp` | `dapp` = RUN을 에이전트로 보냄. `local` = hsw 원본(이 프로세스 안의 ultralytics, 별도 PyTorch 프로세스가 GPU를 같이 씀) — A/B 비교용 |
| `VA_MODEL` | `26x` | 모든 요청이 쓰는 고정 모델 (26n/26s/26m/26l/26x) |
| `VA_DEADLINE_MS` | `200` | QoS. RUN의 `qos.deadline_ms`(에이전트가 못 지킬 요청은 즉시 `fail/overload`)이자 전화기가 프레임별로 판정하는 E2E 목표 |
| `VA_TARGET_FPS` | `5` | UE별 요청 속도 (전화기 페이지의 전송 주기 = 200 ms) |
| `VA_CAMERA_WIDTH/HEIGHT` | `3840/2160` | 전화기 카메라 요청 해상도 (4K UHD; 기기가 지원해야 실제 4K가 잡힌다) |
| `VA_JPEG_QUALITY` | `0.90` | 프레임 JPEG 품질 (4K q0.90 ≈ 2.3 MB → 5 req/s면 UE당 UL 약 92 Mbps) |
| `VA_MAX_INFLIGHT` | `4` | 응답을 못 받은 프레임이 이만큼 쌓이면 다음 캡처를 건너뜀 |
| `VA_PORT`, `VA_AGENT` | `8000`, `127.0.0.1:9810` | HTTPS 포트, dApp 에이전트 주소 |
| `VA_VERBOSE` | `0` | 1이면 프레임마다 한 줄 로그 |

페이지의 JS 상수(해상도·fps·품질·QoS)는 서버가 `.env` 값으로 채워서 내려 준다 (`app.py` `_page()`). 바꾼 뒤 `../va.sh restart`.

## 실행 (`../va.sh`)

```bash
cd /home/nvidia/_dgist
./va.sh start            # yolo_env 활성화 + uvicorn (HTTPS, certs/server.*) 백그라운드 기동 → https://10.45.0.1:8000/
./va.sh status           # 프로세스·포트·에이전트 연결·카운터·에이전트 추정치·UE 세션·최근 CSV
./va.sh test ue1 25 5    # 전화기 대역(ue_sim.py): 4K 프레임 25장을 5 req/s로 → 전화기와 같은 CSV
./va.sh test2 50 5       # ue1 + ue2 동시
./va.sh bg start --fps 5 # 서비스 안의 가상 UE(bg_yolo.py)가 같은 경로로 부하 추가;  bg status | bg stop
./va.sh logs | stop | restart [local]
```
`source yolo_env/bin/activate` → `uvicorn ...` → `deactivate` 를 손으로 하던 것을 `start`/`stop`이 대신한다. 실행 순서는
**L1(`./dgist.sh up`) → VA 서비스**가 자연스럽지만, 먼저 떠 있어도 된다(첫 프레임에서 에이전트에 다시 붙는다; L1을 재시작해도 같다).
전화기: Wi-Fi 끄고 5G로 붙은 뒤 `https://10.45.0.1:8000/`. 인증서는 자체 서명(CN/SAN 10.45.0.1)이라 `certs/ca.crt`를 전화기에
설치하거나 경고를 승인한다. 페이지에서 UE1/UE2 선택 → Start Camera → Start VA; Stop VA에서 CSV가 저장된다.

## 파일

| 파일 | 내용 |
|---|---|
| `app.py` | hsw `app.py`에서 추론 부분만 교체. `/detect_frame`이 `backend.infer(JPEG)`를 부르고, 응답에 `dapp{...}`, `qos_deadline_ms`, `model`, `backend`를 추가. `/va/status` 추가. 페이지 JS에 QoS 판정·dApp 응답 표시·CSV 열 추가 |
| `dapp_backend.py` | `DappBackend`: `xapp_client.DappAgent`(Aerial 트리 `cuPHY-CP/dapp_hook/python/`)로 role `va`로 접속(estimate 구독), `run(jpeg, model, qos, want_dets)` → 결과·검출(COCO 이름 매핑, 4K 원본 좌표). 끊기면 다음 프레임에서 재접속, 실패는 `status fail / reason agent_unreachable|agent_lost|timeout|overload|...`로 응답. `LocalBackend`: hsw 원본 경로 |
| `bg_yolo.py` | hsw 가상 UE. 모델 대신 `backend.infer`를 호출하므로 dApp 경로로 부하를 건다. 기본 이미지 `test_frames/uhd_3840x2160_q90.jpg`. 끝의 CLI는 `va.sh bg` |
| `ue_sim.py` | 전화기 대역(표준 라이브러리만): telemetry start → clock_sync → multipart `/detect_frame` × N (속도·동시 수 제한) → `/save_latency_log` → telemetry stop. 프레임별 한 줄 + 요약(QoS 충족률, E2E 평균/p50/p95, dApp sm/wait/dec/inf) |
| `va_status.py` | `GET /va/status`를 읽기 쉽게 출력 (`va.sh status`) |
| `service_common/` | hsw 텔레메트리 모듈 그대로 (`/telemetry/*` → `/tmp/oai-service-telemetry/va_ue{1,2}.json`; hsw PPO xApp이 읽음) |
| `certs/` | hsw 인증서 그대로 (CN/SAN 10.45.0.1, 2026-09-30 ~ 2027-09-30, CA `ca.crt`) |
| `yolo_env/` | hsw venv 복사(8.4 GB, Python 3.10, fastapi/uvicorn/pillow/pynvml + torch/ultralytics). 경로는 이 위치로 고쳐 놓았다 |
| `yolo26x.pt`, `bg_test_image.jpg` | `local` 백엔드용 모델, hsw 배경 이미지 |
| `test_frames/` | `va.sh make-frame`이 만든 3840×2160 q90 JPEG (Aerial 트리 `models/img4k/img4k_0.jpg` 중앙 크롭) |
| `upstream_hsw/` | hsw 원본 `app.py`, `bg_yolo.py` (참고용, 실행 안 함) |

로그: uvicorn `../runs/va_service/uvicorn_<시각>.log`(`uvicorn.log` 링크), 세션 CSV `../runs/va_service_log/va_ul_dl_latency_<ue>_<시각>.log`
(hsw 열 21개 + `qos_deadline_ms, qos_met, dapp_status, dapp_reason, dapp_model, dapp_sm, dapp_wait_ms, dapp_decode_ms, dapp_infer_ms,
dapp_latency_ms, detections`), 가상 UE `bg_virtual_ue_<시각>.log`.

## 응답 필드 (`/detect_frame`)

hsw 필드(`server_receive_ms, ai_start_ms, ai_end_ms, server_send_ms, image_width/height, detections[{class, confidence, x1, y1, x2, y2}],
gpu_util_percent, gpu_memory_used_gb, yolo_enabled, processing_ms`)는 그대로. **AI 시간(ai_start→ai_end)은 RUN 왕복 전체**(에이전트 큐 대기 +
GPU 디코드 + 추론 + 소켓)다. 추가:

```json
"backend": "dapp", "model": "26x", "qos_deadline_ms": 200, "ue_id": "ue1",
"dapp": {"status": "success", "reason": null, "model": "26x", "sm": 80, "sm_mean": 80.0,
         "wait_ms": 0.1, "decode_ms": 5.5, "infer_ms": 32.6, "latency_ms": 38.3, "rtt_ms": 39.0, "client_ms": 39.2,
         "tiles": 8, "n_detections": 88, "req_id": 1234, "deadline_ms": 200}
```
실패 시 `"status": "fail"`, `"reason"`: `overload`(QoS 안에 못 끝냄, 에이전트가 즉시 거절) · `agent_unreachable`(L1 안 떠 있음) · `agent_lost` ·
`timeout` · `no_engine` · `decode_error` · `paused`. 이때도 HTTP 200이고 `detections`는 빈 목록이라 전화기는 계속 돌며 CSV에 기록된다.

## 검증 (2026-11-05 17:40, L1 `dgist.sh up va1` + 이 서비스, 전화기 대신 `ue_sim.py`, 같은 호스트)

| 시험 | 결과 |
|---|---|
| ue1 25프레임 @5 req/s | 25/25 success, QoS 200 ms 충족 100 %, E2E 평균 49.5 ms (UL 7.3 + AI 39.4 + 기타 0.5 + DL 2.3), dApp sm 80, dec 5.5, inf 32.6, 검출 88 |
| ue1 + ue2 동시 50프레임씩 @5 req/s | 100/100 success, QoS 100 %, E2E 평균 48.6 / 48.7 ms, wait 0.1 ms (큐 대기 없음) |
| + 가상 UE 5 fps 동시 | ue2 20/20 success, E2E 48.7 ms; 가상 UE 23건 success |
| L1 | Err.ind 0, FATAL 0, 에이전트 ok 103 fail 0 (`dgist.sh status`) |

같은 호스트라 UL 7 ms는 2.3 MB 멀티파트의 loopback 전송이다. **실제 UE에서는 UL이 지배한다**: hsw 측정(1080p 435 KB × 9.3 fps = 34 Mbps)에서
UL 213 ms였고, 4K q0.90은 프레임당 2.3 MB × 5 req/s = UE당 약 92 Mbps라 1셀 UL(DDDDDDSUUU) 용량을 넘는다. 전화기 E2E가 200 ms를 넘으면
`qos_met=0`으로 기록되며, 해상도를 유지한 채 줄이려면 `VA_JPEG_QUALITY`(0.75 → 약 1.8 MB, 0.60 → 약 1.1 MB)나 `VA_TARGET_FPS`를 낮춘다.
