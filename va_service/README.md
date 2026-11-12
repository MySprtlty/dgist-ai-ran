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
| `VA_TRACK` | `1` | 전화기 페이지 박스 추적(2026-11-11): 결과마다 같은 클래스의 박스를 IoU로 이어 붙여 ID를 유지, 위치·크기 평활, 속도로 지연만큼 앞을 예측, 새 결과가 없으면 `VA_TRACK_TTL_MS` 동안 흐려지다 사라짐. 0 = 결과마다 지우고 다시 그리는 원본 방식 |
| `VA_QOS_DROP_MISSED` | `1` | E2E가 `VA_DEADLINE_MS`를 넘긴 결과는 그리지 않음(기존 박스는 관성으로 흐르다 사라짐). 검출 목록 첫 줄에 "QoS missed ... not drawn"과 누계가 표시됨 |
| `VA_DISPLAY_CONF` | `0.40` | 이 신뢰도 아래의 박스는 그리지 않음 (dApp은 0.25까지 보고) |
| `VA_TRACK_TTL_MS` | `700` | 새 검출 없이 트랙을 유지하는 시간(250 ms는 그대로, 이후 흐려짐) |
| `VA_ENCODE_WORKERS` | `2` | 전화기 페이지 JPEG 인코딩을 Web Worker N개로 병렬화(OffscreenCanvas.convertToBlob, 2026-11-11). 메인 스레드는 createImageBitmap으로 프레임만 뜨고, 인코딩 60~90 ms인 폰도 ~20 fps까지 송신. 0 = 원본(메인 스레드 직렬). 미지원 브라우저는 자동으로 원본 경로, rate 로그에 `encode 2 workers`/`unsupported browser` 표시 |
| `VA_FILE_WIDTH/HEIGHT` | `1920/1080` | 전화기 페이지의 **동영상 파일 소스**(2026-11-12, 아래 절)가 보내는 프레임 크기. 파일의 모든 프레임을 화면비를 유지한 채(남는 곳은 검은 띠) 이 크기로 변환해 JPEG로 만든다. 카메라 모드는 카메라 원본 프레임 그대로 |
| `VA_VIEWER` | `1` | 기지국 뷰어(2026-11-11): `/viewer`(UE 전부), `/viewer/ue1`. 서버가 UE별 **최신 QoS 충족 프레임 + 박스**를 보관하고 브라우저가 롱폴(`/viewer_frame/<ue>`)로 받아 캔버스에 그린다. 0 = 끔 |
| `VA_VIEWER_DL_MS` | `20` | 뷰어 판정의 **고정 하향 지연**. 판정 = UL(프레임마다 실측: 전화기 송신 시각 + 클럭 오프셋) + AI(RUN 왕복 실측) + 서버 기타 + 이 값 ≤ `VA_DEADLINE_MS`. 11-10/11 FHD 전화기 로그 20만 프레임: DL 평균 21.1, p50 17.6, p90 37 ms |
| `VA_VIEWER_QOS_FREEZE` | `1` | 1 = 판정에 미달한 결과는 **화면을 갱신하지 않음**(다음 충족 결과까지 정지 = 렉 걸린 화면). 0 = 성공한 결과는 모두 표시 |
| `VA_VIEWER_BADGE` | `1` | 1 = 정지 중 "QoS 미달 · 화면 유지 N s"(UL+AI+DL 내역, 연속 횟수) 배지와 화면 어둡게 처리, 송신이 끊기면 회색 "UE 송신 없음". 0 = 표시 없이 그냥 멈춤 |
| `VA_VIEWER_NONVA` | `1` | `/viewer`에 non-VA UE 2대 칸도 함께 표시(2026-11-12): non-VA 서비스의 MJPEG 중계(`https://<host>:NONVA_PORT/video_feed`)를 `<img>`로, 통계는 `/nonva_stats/<ue>`(VA 서비스가 같은 호스트의 `/receiver_stats`를 대신 가져옴). `/viewer/va`는 VA 2대만, `/viewer/ue1`은 한 대 |
| `NONVA_PORT` | `8001` | 위 중계·통계를 가져올 non-VA 서비스 포트 (`.env`의 non-VA 설정과 같음) |

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
설치하거나 경고를 승인한다. 페이지에서 UE1/UE2 선택 → Start Camera → Start VA; Stop VA에서 CSV가 저장된다. 카메라 대신 폰에 저장된 동영상을 쓰려면 Start Camera 자리에
Video File(아래 절), 영상과 박스만 크게 보려면 Full Screen.

## 동영상 파일 소스 · 전체 화면 (전화기 페이지, 2026-11-12)

카메라로 화면을 찍는 대신 **폰에 미리 저장해 둔 동영상 파일**을 소스로 쓸 수 있다. 카메라 모드는 그대로 있고, 두 소스는 버튼으로 오간다.

```
Video File → 파일 선택 (미리보기: 반복·무음 재생) → Start VA → Full Screen          카메라로 돌아가기: Start Camera,  끄기: Stop Camera / Video
```

- **하향 트래픽 없음.** 파일은 `<input type="file">` + `URL.createObjectURL`로 폰 안에서만 읽는다. 서버로 올라가지도, 서버에서 내려오지도 않는다.
  무선으로 나가는 것은 카메라 모드와 같은 `/detect_frame` JPEG뿐이고, 내려오는 것도 같은 검출 JSON뿐이다.
- 요청 속도·품질·QoS·박스 추적·CSV는 카메라 모드와 같다(`VA_TARGET_FPS`, `VA_JPEG_QUALITY`, `VA_DEADLINE_MS`, ...). 동영상은 제 속도로 재생되고,
  전송 주기마다 그 순간의 프레임을 떠서 보낸다.
- **FHD 자동 변환**: 파일 해상도와 상관없이 모든 프레임을 `VA_FILE_WIDTH × VA_FILE_HEIGHT`(기본 1920×1080)로 맞춰 보낸다 — 화면비 유지, 남는 곳은
  검은 띠(4:3 파일 → 좌우 240 px씩, 세로 720×1280 파일 → 가운데 608 px 폭). 큰 파일(예: 4K)은 줄이고 작은 파일(720p)은 늘린다. 변환은 JPEG 인코딩과
  같은 자리(Web Worker, `VA_ENCODE_WORKERS=0`이면 메인 스레드)에서 한다. 서버·dApp이 받는 크기가 항상 같으므로 FHD 엔진(`DAPP_INPUT=fhd`)과 맞는다.
  카메라 모드는 전과 같이 카메라 원본 프레임을 보낸다.
- 박스는 보낸 프레임 좌표(검은 띠 포함)로 그려지고, 화면의 동영상도 같은 비율의 틀 안에 놓이므로 박스가 영상 위에 정확히 겹친다.
- **Full Screen**: 영상 + 박스만 화면 전체에 띄운다(카메라 모드에서도 된다). 파일 모드에서는 가로 방향 고정을 시도한다(기기가 허용할 때). 나가기 =
  화면 위 `Exit Full Screen` 버튼. 전체 화면이거나 파일을 재생하는 동안에는 화면 꺼짐 방지(Wake Lock)를 건다.
- 표시: 해상도 줄 `1280 x 720 (file clip.mp4) -> 1920 x 1080 sent`, 상태 `Video Ready` / `Running` / `Video Stopped` / `Video Error`(브라우저가 재생하지
  못하는 파일. 폰이 디코드하는 코덱이어야 하며 H.264 MP4가 무난하다). 서버 쪽: uvicorn 로그의 `source: file ...`, `fullscreen: on/off` 줄,
  `GET /va/status`의 `config.file_frame`.
- 확인(11-12, 헤드리스 Chromium 157 + 폰 크기 에뮬레이션, 시험 서버): 1280×720 / 960×720 / 720×1280 / 2560×1440 파일 모두 서버 수신 1920×1080,
  15.1~15.2 req/s(목표 15), 박스와 영상의 위치 오차 ≤ 2 px(페이지·전체 화면), Web Worker 2개와 메인 스레드 인코딩 양쪽. 카메라 모드는 화면 배치와
  전송 프레임이 원본 페이지와 같다. **실제 폰 확인은 아직**: 큰 파일 선택, 동영상 디코드 + 15 fps 인코딩 부하, 방향 고정.

## 기지국 뷰어 (`/viewer`, 2026-11-11)

non-VA 서비스의 `/viewer`(최신 프레임 MJPEG 중계)에 해당하는 VA 쪽 화면. PC 브라우저에서 `https://10.150.21.161:8000/viewer`(호스트 LAN) 또는
UE에서 `https://10.45.0.1:8000/viewer`(자체 서명 인증서 경고 1회 수락). **`/viewer` 한 페이지에 UE 4대**: 위 줄 VA ue1·ue2(박스, QoS 정지 규칙),
아래 줄 non-VA ue1·ue2(720p 중계, 수신 fps·Mbps·KB·UL). non-VA 영상은 브라우저가 8001 포트에서 직접 받으므로 그 인증서도 한 번 수락해야 한다
(안 보이면 칸 안에 안내가 뜸). `/viewer/va`는 VA 2대만, `/viewer/ue1`은 한 UE만 크게.

```
전화기 → POST /detect_frame (+ client_send_ms, clock_offset_ms)  →  서버: UL = 수신 시각 − (송신 시각 + 오프셋), AI = RUN 왕복, DL = VA_VIEWER_DL_MS(고정)
        판정 UL + AI + 기타 + DL ≤ VA_DEADLINE_MS ?  충족 → UE의 "현재 화면" 교체(JPEG 원본 + 박스, seq+1)   미달/실패 → 화면 그대로(정지), 상태만 갱신
브라우저 ← GET /viewer_frame/<ue>?seq=&stat=&wait=1000 (롱폴: 새 화면이면 200 + JPEG + X-VA-Meta(박스·지연·판정), 아니면 204 + X-VA-Meta)
```

- 화면에는 **충족한 결과만** 올라가므로 QoS를 못 맞추는 동안 영상이 멈추고(게임 렉처럼), 다시 맞추면 흐른다. 서버는 JPEG을 다시 인코딩하지 않는다(전화기가 보낸 바이트 그대로, CPU 부담 없음).
- 나중에 도착한 **더 오래된 캡처**의 충족 결과는 화면을 되돌리지 않는다(`순서 뒤짐` 카운터).
- 타일: 상향 / 추론 / 하향(고정) / 합계(기한 대비 녹·적) / 표시 fps / QoS 충족률(최근 100). 아래 줄: 모델·SM, 결과·충족·미달 누계, 화면 번호.
- 전화기 페이지의 판정(실측 E2E ≤ 기한)과 뷰어의 판정(UL + AI + 고정 DL)은 DL 변동만큼 가끔 다를 수 있다. `enable_yolo=false`(업로드만)면 AI 0으로 판정해 박스 없이 보여 준다.
- 상태 JSON: `GET /viewer_status` (UE별 seq·판정·누계), `va.sh status`에도 요약. 전화기 페이지와 `ue_sim.py`는 두 필드를 보내고, 필드가 없는 요청(옛 클라이언트)은 UL을 모르는 채(0) 판정한다.
- 확인(11-11 21:5x, `ue_sim` ue1 10 req/s + ue2에 송신 시각을 300~400 ms 당겨 보낸 요청): 충족 → seq 증가, 미달 → seq 유지·missed 증가·배지, 오래된 캡처 → stale, 헤드리스 Chromium 스크린샷으로 화면 확인.

## 파일

| 파일 | 내용 |
|---|---|
| `app.py` | hsw `app.py`에서 추론 부분만 교체. `/detect_frame`이 `backend.infer(JPEG)`를 부르고, 응답에 `dapp{...}`, `qos_deadline_ms`, `model`, `backend`를 추가. `/va/status` 추가. 페이지 JS에 QoS 판정·dApp 응답 표시·CSV 열 추가, 동영상 파일 소스·전체 화면(11-12) |
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
