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
| `VA_MODEL` | `26x` | 모든 요청이 쓰는 고정 모델 (26n/26s/26m/26l/26x). `VA_MODEL_SELECT=1`이면 규칙이 요청마다 고르고, 이 값은 에이전트가 올리지 않은 모델을 대신한다 |
| `VA_MODEL_SELECT` | `0` | 규칙 기반 모델 선택 스위치: `0` = 끔(모든 요청이 `VA_MODEL`), `1` = 켬(`VA_MODEL_WEIGHTS`대로). 아래 "규칙 기반 모델 선택" 절 |
| `VA_MODEL_WEIGHTS` | `10,10,10,10,60` | 블록 하나(= 값의 합, 여기서는 요청 100개)에서 26n,26s,26m,26l,26x가 맡는 요청 수(이 순서로 값 5개, `0` = 그 모델은 안 씀) |
| `VA_MODEL_SEED` | (없음) | 선택 사항(정수). 주면 서비스를 다시 켜도 같은 순서로 고른다 |
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
| `VA_VIEWER_REC` | `1` | 뷰어 녹화 버튼(2026-11-13, 아래 "뷰어 녹화" 절): "전체 녹화"/"녹화 종료"와 칸마다 "녹화 시작"/"녹화 종료". 1이면 non-VA 칸은 중계를 `<img>` 대신 `fetch()`로 읽어 캔버스에 그린다(연결 수·받는 양은 같음). 0 = 버튼 없이 이전 페이지 그대로 |
| `VA_VIEWER_REC_FPS` | `30` | 녹화 파일의 프레임률(고정). 전화기는 15~20 fps로 보내므로 30이면 화면에 뜬 프레임이 모두 들어간다 |
| `VA_VIEWER_REC_MBPS` | `6` | 칸 하나의 H.264 목표 비트레이트. 6 Mbit/s = 분당 약 45 MB(파일 하나) |
| `VA_VIEWER_REC_MAX_MIN` | `20` | 이 시간(분)이 지나면 스스로 끝내고 저장한다(녹화분은 그때까지 브라우저가 들고 있음). 0 = 제한 없음 |
| `VA_VIEWER_REC_BADGE` | `1` | 1 = QoS 배지·어두워진 정지 화면·"UE 대기 중" 문구도 화면에 보이는 대로 녹화. 0 = 영상과 박스만 |
| `VA_VIEWER_REC_PREFIX` | `.env DEMO_MODE` 값 | 파일 이름 머리말: `ours_va_ue1_20261113_181042.mp4`, `baseline_nonva_ue2_....mp4` (영문·숫자·`-_`만) |
| `VA_VIEWER_FIT` | `1` | 뷰어 화면 맞춤(2026-11-14, 아래 "화면 맞춤" 절): 창 크기에 맞춰 칸 전부가 스크롤 없이 한 화면에 들어오도록 격자·그림 크기·숫자 타일 자리를 정한다. 0 = 이전 배치(길면 스크롤) |
| (non-VA 서비스) `NONVA_DEADLINE_MS`, `NONVA_VIEWER_QOS_FREEZE` | `250`, `1` | 뷰어 non-VA 칸의 QoS 기한과 정지 규칙(2026-11-14, 아래 "non-VA 칸의 QoS 정지" 절). **non-VA 서비스가 읽는 값**이다(`../nonva_service/README.md`); 바꾼 뒤 `../nonva.sh restart` |

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

## 규칙 기반 모델 선택 (2026-11-13)

요청마다 쓸 모델을 정해진 비율로 섞는다. 모델은 RUN 메시지의 필드 하나라서 이 서비스가 정하고, L1(에이전트)은 받은 이름의 모델을 그대로 실행한다
(`model_select.py`가 고르고 `dapp_backend.py`의 `infer()`가 RUN에 싣는다. L1 · cuPHY는 바뀌지 않는다).

```bash
# ../.env
VA_MODEL_SELECT=1                   # 0 = 끔(기본, 모든 요청이 VA_MODEL), 1 = 켬
VA_MODEL_WEIGHTS=10,10,10,10,60     # 26n,26s,26m,26l,26x 순서
cd /home/nvidia/_dgist && ./va.sh restart     # Baseline 모드면 ./dgist.sh down && ./dgist.sh up <이름> 도
```

- **뜻**: 요청을 값의 합(위 예: 100개)만큼씩 블록으로 끊고, 블록마다 정확히 10개는 26n, 10개는 26s, 10개는 26m, 10개는 26l, 60개는 26x로 보낸다.
  블록 안에서 어느 요청이 어느 모델이 되는지는 무작위다(블록마다 다시 섞는다). `0`인 모델은 쓰지 않는다. 블록 경계와 어긋난 구간을 세면 비율은 대략만 맞는다.
- **순서는 하나다**: VA UE 두 대와 가상 UE(`va.sh bg`)의 요청이 도착 순서대로 같은 블록을 채운다. 그래서 UE 한 대만 보면 비율이 대략만 맞는다.
  `vcam.py`는 별도 프로세스라 자기 순서를 따로 갖는다(`vcam.sh start <N> <fps> <모델>`처럼 모델을 주면 그 모델로 고정).
- **에이전트가 올리지 않은 모델**의 몫은 `VA_MODEL`로 바꿔 보낸다(접속할 때 경고 한 줄, `va.sh status`에 바뀐 요청 수). Ours L1은 다섯 모델을 모두 올린다.
  Baseline YOLO 서버는 MIG 엔진이 있는 모델만 올린다: MIG를 켠 뒤 `scripts/build_yolo_engines_mig.sh n s m l`(모델당 7~15분).
- **값이 잘못되면**(5개가 아님, 숫자가 아님, 모두 0) 서비스는 뜨고 `[VA] !! VA_MODEL_SELECT=1 ignored …` 한 줄을 낸 뒤 모든 요청을 `VA_MODEL`로 보낸다.
- **전화기로 가는 양은 늘지 않는다**: 응답 필드는 그대로이고 `"model"`(최상위와 `dapp.model`) 값만 그 요청의 모델이 된다. 전화기 페이지의 모델 표시는 `mix`(3글자)다.
  뷰어 머리말에는 규칙 전체가 나온다.
- **보는 곳**: `./va.sh status`의 `model selection:` 줄(블록 구성, 지금까지 모델별 요청 수), `./va.sh test2 100 15` 끝의 `models:` 줄, 세션 CSV의 `dapp_model` 열,
  대시보드 VA 서비스 탭의 모델 사용 비율, `/va/status`의 `config.model_select` · `config.model_weights` · `backend.select`.
- 같은 순서를 다시 쓰려면 `VA_MODEL_SEED=<정수>`. PPO xApp의 입력(`xapp.sh`의 `PPO_COMPUTE_MODEL`)과 대시보드의 대체 추정값은 계속 `VA_MODEL` 하나를 쓴다.

검증(2026-11-13, GPU · 무선 없이 에이전트 대역 프로그램 상대, `ue_sim.py` 2대 × 100프레임 15 req/s): 규칙 `10,10,10,10,60`에서 요청 200개가 26n 20 · 26s 20 · 26m 20 · 26l 20 · 26x 120
(UE가 받은 응답, 서비스 카운터, 에이전트가 받은 요청 수가 일치). 끄면 26x 200. 26x만 올린 에이전트에는 26x 200(바뀐 요청 80). 응답은 켜든 끄든 키 17개 · 평균 771바이트,
전화기 페이지는 67181바이트로 같다. 실제 L1/GPU에서 섞어 돌릴 때의 모델별 지연과 Baseline 다중 모델 서버는 측정 전이다.

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

### 처음부터 다시 재생 · 예약 시작 (2026-11-14)

동영상 파일 소스에서 쓰는 두 기능. VA 페이지와 non-VA 페이지에 같은 것이 들어 있다(여러 폰의 영상을 같은 장면에서 시작시키기 위한 것).

```
Restart Video                         누르는 즉시 파일을 0초부터 다시 재생 (전체 화면에서는 왼쪽 위에 같은 단추)
Start video at: [시:분]  Set / Cancel   고른 시각이 되면 0초부터 재생.  예: 10:00 → 10:00:00에 시작
```

- **Restart Video**: 송신 중이든 아니든 된다. 송신은 끊기지 않고 다음 프레임부터 영상 앞부분이 올라간다. 파일을 고르지 않았으면 안내창만 뜬다.
- **Start video at**: 시각을 고르고 `Set`. 이미 지난 시각이면 내일 그 시각으로 잡힌다(표시 `... tomorrow`). 예약은 하나뿐이고 다시 `Set`하면 바뀐다.
  `Cancel`로 취소. 파일을 나중에 골라도 된다(시각이 됐는데 파일이 없으면 `Skipped at ...: no video file was chosen`).
- **어느 시계인가**: `Start VA`(non-VA는 `Start Streaming`)를 누르면 폰이 서버와 시각을 맞추는데(기존 클럭 동기), 그 뒤에는 **기지국 서버 시계**로,
  그 전에는 폰 자체 시계로 잰다. `Clock:` 줄에 지금 시각과 어느 시계인지, 폰 시계가 서버보다 몇 ms 빠른지/느린지가 나온다. **여러 폰을 같은 순간에
  시작하려면 폰마다 먼저 Start를 누른 뒤 예약할 것**(모두 같은 서버 시계를 쓰게 된다).
- **맞추는 방법**: 예약 시각 0.7초 전에 영상을 첫 프레임에 세워 두고(그동안에도 그 프레임은 계속 송신) 시각이 되면 재생한다. 세워 둔 사이에
  `Cancel`하면 보던 위치에서 이어서 재생한다.
- 표시: `Scheduled start:`(예약 시각과 남은 시간 → `Started at 10:00:00`), `Last video start:`(실제로 시작한 시각, ms 단위, 예약 대비 몇 ms 뒤였는지),
  전체 화면에서는 왼쪽 아래 `Video starts at ...`.
- **요청·하향 트래픽 없음**: 두 기능은 폰 안의 `<video>`만 다룬다. 서버로 가는 요청이 하나도 없고(확인: 아래), 내려오는 것도 없다.
- **주의**: 페이지가 뒤로 가거나 화면이 꺼지면 브라우저가 타이머를 늦춘다 → 예약해 둔 동안 페이지를 앞에 둘 것(전체 화면·파일 재생 중에는 화면 꺼짐
  방지가 걸려 있다). 분 단위 입력이므로 초는 항상 00이다.
- 확인(11-14, 헤드리스 Chromium 157 + 폰 크기 에뮬레이션, 시험 인스턴스, 1280×720 20초 파일, 두 페이지 각각): Restart 0.3초 뒤 위치 0.3 s; 예약 시작 6회
  (송신 전·송신 중·서버 시계를 폰보다 5초 앞서게 한 경우)에서 영상 위치와 예약 시각의 차 −2~+1 ms, 0.7초 세워 둔 동안 송신 10~11 프레임(15 fps 유지),
  기능을 쓴 구간의 요청 경로는 평소와 같은 것뿐(`/detect_frame`, `/telemetry/*`, 주기 `/client_log`; non-VA는 `/upload_frame`, `/telemetry/heartbeat`),
  송신하지 않을 때는 요청 0건. **실제 폰 확인은 아직**: 폰의 시간 선택기, 폰에서의 시작 오차(디코더가 느리면 더 늦을 수 있음), 화면이 꺼졌을 때.

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

### 뷰어 녹화 (2026-11-13)

`/viewer`(4칸), `/viewer/va`, `/viewer/ue1` 어느 페이지에서나 된다. 머리줄 오른쪽 **전체 녹화** → 보이는 칸 전부를 같은 순간에 녹화 시작,
**녹화 종료** → 칸마다 FHD(1920×1080) MP4 한 개씩 저장되고 바로 내려받는다(Chrome이 처음 한 번 "여러 파일 다운로드"를 물으면 허용). 칸마다
**녹화 시작** ↔ **녹화 종료 mm:ss** 단추로 따로 녹화·종료할 수도 있다. 끝난 파일은 페이지 아래 "녹화 파일" 목록에 남아 다시 받을 수 있다
(탭 메모리에 있으므로 탭을 닫거나 새로 고치면 사라짐: 그 전에 받을 것).

- **무엇이 찍히나**: 칸의 그림 영역이 화면에 보이는 그대로 — VA는 프레임 + 박스 + (QoS 미달로 멈춘 동안) 어두운 정지 화면과 배지, non-VA는 중계 영상.
  1920×1080에 화면비를 지켜 맞추고(non-VA 720p는 1.5배 확대) 남는 곳은 검은 띠. 칸 위 제목줄과 아래 숫자 타일은 들어가지 않는다.
- **형식**: H.264(High, level 4.0) 30 fps 고정, 6 Mbit/s, 일반 MP4(색인이 앞에 있는 비조각 MP4, 소리 없음). 전체 녹화로 만든 파일들은 시작 시각과
  길이가 프레임 단위로 같아서 편집기에서 나란히 놓으면 그대로 맞는다. 파일 이름 `<DEMO_MODE>_<va|nonva>_<ue>_<날짜>_<시각>.mp4`.
- **어디서 하나**: 전부 뷰어를 연 **브라우저 안**. 칸마다 Web Worker 하나가 캔버스 사본을 받아 WebCodecs로 인코딩하고 MP4를 만든다
  (`app.py` 뷰어 페이지의 "recording" 블록). 서버와 전화기는 녹화를 모른다: **상향·하향 트래픽, 서버 CPU, GPU 모두 그대로**다. non-VA 칸은 같은
  중계 연결 하나를 `fetch()`로 읽을 뿐이다(녹화하려면 픽셀을 읽어야 하는데 다른 포트의 `<img>`는 읽을 수 없어서; non-VA 서비스가 같은 호스트의
  뷰어에게만 CORS 헤더를 준다). non-VA 서비스가 옛 버전이면 그 칸은 `<img>`로 돌아가고 단추가 "녹화 불가"가 된다.
- **브라우저**: Chrome/Edge 최신판 권장(WebCodecs H.264). 없으면 MediaRecorder로 대신 찍는다(조각 MP4 또는 WebM, 가변 프레임률; 목록에 표시).
  `?rec=mr`, `?rec=webm`으로 일부러 고를 수 있다(시험용).
- **주의**: 녹화분은 끝날 때까지 브라우저 메모리에 있다(4칸 × 20분 ≈ 3.6 GB) → 긴 시연은 나눠 찍거나 `VA_VIEWER_REC_MBPS`를 낮출 것. 녹화 중에는
  뷰어 탭을 앞에 둘 것(가려진 탭은 브라우저가 느리게 돌린다; 미확인). 녹화 중 새로 고침·닫기는 경고가 뜬다.
- 확인(11-13, 헤드리스 Chromium 157 소프트웨어 렌더링, 시험 인스턴스 + 생성 프레임 송신기 4개 × 15 fps): 4칸 동시 녹화 733/733 프레임(건너뜀 0),
  네 파일 길이 동일(24.433 s), 녹화 중 화면 15 fps 유지·메인 스레드 지연 ≤ 5 ms, OpenCV로 전 프레임 1920×1080 디코드, 프레임 번호 단조 증가
  (VA ue2: 화면에 뜬 367장 모두 포함), QoS 미달 구간의 배지·어둡게 처리, 색 오차 ≤ 3/255, Chromium `<video>` 재생·탐색, 개별 시작/종료, 3분 녹화,
  시간 제한 자동 저장, 인코더 오류 시 그때까지의 분량 저장, MediaRecorder 두 경로. **미확인**: 실제 뷰어 PC(GPU 인코더)와 실제 전화기 4대 화면,
  PowerPoint·편집기에서 열기, 가려진 탭.

### non-VA 칸의 QoS 정지 (2026-11-14)

non-VA 칸도 VA 칸과 같은 규칙으로 멈춘다. non-VA 서비스에는 추론도 하향 결과도 없으므로 판정은 **프레임마다 실측한 상향(UL) 지연 ≤ `NONVA_DEADLINE_MS`
(기본 250 ms)** 하나다(UL = 서버가 프레임을 다 받은 시각 − (폰 송신 시각 + 클럭 오프셋); 서비스 CSV의 `ul_latency_ms`와 같은 값). 판정과 정지는
non-VA 서비스가 하고(`../nonva_service/README.md`의 "QoS 판정" 절), 이 페이지는 중계 조각마다 붙어 오는 `X-NV-Meta`를 읽어 표시만 한다.

- 충족: 칩 "QoS 충족 · 실시간", 타일 "상향 지연 / 250 ms" 녹색.
- 미달: 그림이 마지막 충족 프레임에 멈추고 어두워지며 배지 "QoS 미달 · 화면 유지 N s" / "UL 401 ms > 250 ms · 연속 N회", 칩 "QoS 미달 · 정지".
  충족 프레임이 오면 바로 풀린다. 송신 시각을 쓸 수 없는 프레임은 미달로 센다("UL 측정 불가").
- 새 프레임이 없으면 회색 "갱신 없음", 더 길어지면 "UE 송신 없음"(VA 칸과 같음). 세션 시작부터 미달이면 "QoS를 충족한 프레임이 아직 없습니다".
- 타일: 수신 fps / Mbps / KB / 상향 지연(기한 대비 녹·적) / QoS 충족률(최근 100 프레임; 90 % 이상 녹, 70 % 미만 적).
  아래 줄: `프레임 #n · 중계 15 fps · 충족 n · 미달 n · 순서 뒤짐 n`.
- 녹화에는 VA 칸처럼 배지와 어두운 정지 화면이 함께 찍힌다(`VA_VIEWER_REC_BADGE`).
- `VA_VIEWER_REC=0`(`<img>` 중계)에서는 판정을 1초마다 오는 통계(`/nonva_stats/<ue>`)로 받으므로 표시가 최대 1초쯤 늦다. non-VA 서비스가 옛 버전이면
  (헤더 없음) 칸은 이전과 똑같이 동작한다(판정 없음, 칩 "송출 중").
- 전화기 쪽은 그대로다: 폰 페이지는 중계도 통계도 받지 않으므로 **셀 하향 트래픽은 늘지 않는다.** 중계 조각 수는 전과 같거나 적고(미달 프레임은 내보내지
  않는다), 조각마다 헤더 약 0.25 kB가 붙어 뷰어 PC로 간다(LAN).
- 확인(11-14, 시험 인스턴스 + 생성 송신기, 헤드리스 Chromium): 충족 → 지연 5프레임(배지·어둡게·연속 횟수 증가) → 충족 → 송신 시각 없는 프레임 → 송신 중단
  ("갱신 없음" → "UE 송신 없음") → 재개; 처음부터 미달인 UE; 녹화 화면에 배지 포함; 옛 non-VA 서비스; `<img>` 중계; `NONVA_VIEWER_QOS_FREEZE=0`.

### 화면 맞춤 (2026-11-14, `VA_VIEWER_FIT`)

창 크기를 재서 **칸 전부가 스크롤 없이 한 화면에** 들어오고 그림이 가장 커지는 배치를 고른다(창 크기를 바꾸거나 브라우저 확대/축소를 바꾸면 다시 계산).
4칸(`/viewer`)은 2×2, 그림은 16:9를 지키고 숫자 타일은 그림 **오른쪽**(폭 176 px 한 줄 또는 두 줄)이나 아래 중 그림이 더 커지는 쪽에 놓인다.
머리줄은 창 폭에 따라 한 줄(3840 폭) ~ 세 줄(1680 폭)이다.

| 창(브라우저 안쪽) | `/viewer` 4칸의 그림 한 장 | 비고 |
|---|---|---|
| 3840 × 2010 (4K, 100 %) | 1543 × 868 | 타일 오른쪽 한 줄 |
| 2560 × 1300 (QHD) | 912 × 513 | |
| 1920 × 950 (FHD, 또는 4K를 200 %로) | 568 × 320 | 타일 오른쪽 두 줄 |
| 1680 × 945 | 456 × 257 | 머리줄 세 줄 |
| 1366 × 680 이하 | (맞춤 안 함) | 그림이 너무 작아져서 이전 배치(스크롤)로 돌아간다: 그림 높이 200 px 미만 또는 창 폭 900 px 미만 |

`/viewer/va`(2칸)는 좌우로 929 × 523(1920 × 950 창) / 1249 × 703(2560 × 1300), `/viewer/ue1`(1칸)은 1383 × 778 / 2005 × 1128. 모니터가 클수록 그림이 커지므로
32인치(QHD나 4K)에서는 브라우저를 전체 창으로 두면 된다(F11 전체 화면이면 주소줄만큼 더 커진다). 녹화 파일은 화면 배치와 무관하게 FHD다.
`VA_VIEWER_FIT=0`이면 이전 배치. 확인(11-14): 헤드리스 Chromium에서 위 창 크기 9가지와 세 경로를 재어 모든 칸이 창 안에 있음을 확인. 실제 32인치 모니터에서는 미확인.

## 파일

| 파일 | 내용 |
|---|---|
| `app.py` | hsw `app.py`에서 추론 부분만 교체. `/detect_frame`이 `backend.infer(JPEG)`를 부르고, 응답에 `dapp{...}`, `qos_deadline_ms`, `model`, `backend`를 추가. `/va/status` 추가. 페이지 JS에 QoS 판정·dApp 응답 표시·CSV 열 추가, 동영상 파일 소스·전체 화면(11-12) |
| `dapp_backend.py` | `DappBackend`: `xapp_client.DappAgent`(Aerial 트리 `cuPHY-CP/dapp_hook/python/`)로 role `va`로 접속(estimate 구독), `run(jpeg, model, qos, want_dets)` → 결과·검출(COCO 이름 매핑, 4K 원본 좌표). 끊기면 다음 프레임에서 재접속, 실패는 `status fail / reason agent_unreachable|agent_lost|timeout|overload|...`로 응답. `LocalBackend`: hsw 원본 경로 |
| `model_select.py` | 규칙 기반 모델 선택(`VA_MODEL_SELECT`, `VA_MODEL_WEIGHTS`): 블록마다 섞은 목록에서 요청 하나에 모델 하나를 준다(잠금으로 보호, 에이전트가 올리지 않은 모델은 `VA_MODEL`로 대체). `app.py`가 만들어 `DappBackend`에 넘기고 `vcam.py`도 쓴다 |
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
`model`과 `dapp.model`은 그 요청을 실행한 모델이다. `VA_MODEL_SELECT=1`이면 요청마다 다르다(필드 수와 이름 길이는 같다).
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
