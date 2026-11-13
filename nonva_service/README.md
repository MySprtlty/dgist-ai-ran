# `nonva_service/` — non-VA 라이브 스트리밍 서비스 (hsw `video_stream_service` 그대로, 2026-11-05)

hsw 연구실의 두-슬라이스 실험에서 **VA가 아닌 쪽 UE**가 쓰는 서비스다. 전화기가 카메라 프레임(1280×720, 20 fps, JPEG 0.80)을
`/upload_frame`으로 올리면 서버가 UE별 최신 프레임을 보관해 `/viewer/<ue>` 페이지에 MJPEG(20 fps)로 다시 내보낸다. **추론은 없다.**
프레임별 UL 지연(이 서비스의 E2E 정의 = UL)을 기록해 Stop 때 CSV로 저장하고, 텔레메트리 `nonva`를 `/tmp/oai-service-telemetry/nonva_ue{1,2}.json`에
쓴다(hsw PPO xApp이 `live_1/live_2`로 읽음; VA 쪽은 `yolo_1/yolo_2`).

```
전화기(UE2)  https://10.45.0.1:8001/  → Start Camera 또는 Video File → Start Streaming (UE1/UE2 선택)
             POST /start_session(clock_offset, ue_id) → POST /upload_frame(frame_id, upload_start_ms, JPEG) × 20/s → POST /stop_session → CSV
보는 쪽      https://10.45.0.1:8001/viewer/ue2  (호스트 PC 브라우저나 다른 UE; /video_feed MJPEG)
```

## 설정 (`../.env`의 `NONVA_*`; 기본값 = hsw 코드에 박혀 있던 값)

| 변수 | 기본값 | 뜻 |
|---|---|---|
| `NONVA_WIDTH` × `NONVA_HEIGHT` | 1280 × 720 | 전화기 카메라 요청 해상도. 동영상 파일 소스의 프레임도 이 크기로 변환해 보낸다 |
| `NONVA_FPS` | 20 | 업로드 속도 (전송 주기 50 ms) |
| `NONVA_JPEG_QUALITY` | 0.80 | 프레임 JPEG 품질 (720p q0.80 ≈ 200 KB → 20 fps면 약 33 Mbps) |
| `NONVA_VIEWER_FPS` | 20 | `/viewer` 중계 속도 |
| `NONVA_PORT` | 8001 | HTTPS 포트 (VA 서비스는 8000) |
| `NONVA_DEADLINE_MS` | 250 | non-VA 서비스의 QoS 기한(2026-11-14): 프레임의 실측 UL 지연이 이 값을 넘으면 미달 |
| `NONVA_VIEWER_QOS_FREEZE` | 1 | 1 = 미달 프레임은 뷰어 화면을 갱신하지 않음(다음 충족 프레임까지 정지; VA 뷰어와 같은 규칙). 0 = 모든 프레임 중계, 판정만 표시 |

바꾼 뒤 `../nonva.sh restart`, 전화기 페이지 새로 고침. 페이지 JS 상수는 서버가 채워 내려 준다(`_page()`).

`/video_feed`는 **같은 호스트에서 온 페이지**(VA 서비스의 `/viewer`, 포트만 다름)에게만 `Access-Control-Allow-Origin`을 붙여 준다(2026-11-13).
VA 뷰어의 녹화 기능이 중계 영상을 `fetch()`로 읽어 캔버스에 그리기 위한 것으로, 중계 내용·속도·연결 수는 그대로다. 다른 호스트의 페이지에는
헤더가 붙지 않는다.

## QoS 판정과 뷰어 정지 (2026-11-14)

이 서비스의 QoS = **프레임의 실측 UL 지연 ≤ `NONVA_DEADLINE_MS`(250 ms)**. 추론도 하향 결과도 없으므로 UL 하나로 판정한다(UL = 프레임을 다 받은 시각 −
(폰 `upload_start_ms` + 클럭 오프셋), CSV `ul_latency_ms`와 같은 값). 송신 시각을 쓸 수 없는 프레임(유한수가 아님)은 미달로 센다.

```
POST /upload_frame → qos_update(): 충족 → UE의 "현재 화면" 교체(shown_seq+1)   미달 → 화면 그대로(정지), 판정·연속 횟수·누계만 갱신
GET /video_feed    → 현재 화면이 바뀔 때 조각 하나(X-NV-Meta: 판정 JSON, "pic": true) + 그림 없이 판정만 바뀌면 멈춘 프레임을 한 번 더("pic": false)
GET /receiver_stats → 기존 필드 + "qos": {verdict, ul_ms, deadline_ms, streak, met, missed, stale, met_pct, shown_seq, held_ms, ...}
```

- VA 뷰어(`../va_service`의 `/viewer`)의 규칙을 그대로 옮긴 것이다: 판정은 프레임마다, 화면은 충족 프레임으로만 바뀐다. 먼저 보여 준 것보다 **오래된 캡처**가
  나중에 도착하면 화면을 되돌리지 않는다(`stale`). 충족률은 최근 100 프레임.
- 중계 조각 수는 전과 같거나 적다(미달 프레임은 내보내지 않음). 정지 중에 접속한 뷰어는 멈춘 프레임을 한 번 받는다.
- 이 서비스의 `/viewer/<ue>` 페이지: 멈춘 동안 그림이 어두워지고 "QoS miss - picture held N s" / "UL 402 ms > 250 ms, N in a row" 배지, 아래에 기한·판정·
  누계 줄. VA 서비스의 4칸 뷰어는 같은 정보를 `X-NV-Meta`(녹화 켬) 또는 `/receiver_stats`(1초마다)로 받아 한글 배지로 보여 준다.
- **전화기 쪽은 바뀌지 않는다**: `/upload_frame` 응답, CSV 열, 텔레메트리(`nonva_ue*.json`) 모두 그대로이고 폰 페이지는 중계·통계를 받지 않는다 →
  셀 하향 트래픽 증가 없음. `GET /nonva/status`에는 `deadline_ms`, `viewer_qos_freeze`, 세션별 `qos`가 추가됐다.
- 확인(11-14, 시험 인스턴스, 생성 송신기): 충족/지연/송신 시각 없음/순서 뒤짐/송신 중단 계획대로 판정·누계·중계 조각이 나오는지 서버 수준에서 확인,
  `/viewer/ue1` 배지·정지·복귀(헤드리스 Chromium). 실제 폰 4대 운용에서는 아직 미확인.

## 실행 (`../nonva.sh`)

```bash
./nonva.sh start | stop | restart | status | logs
./nonva.sh test ue1 100 20      # 전화기 대역(nonva_sim.py): 720p 프레임 100장을 20 fps로 → 전화기와 같은 CSV
```
venv는 `../va_service/yolo_env`를 같이 쓴다(fastapi/uvicorn/pillow/pynvml만 필요). 인증서는 hsw 것 그대로(CN 10.45.0.1, `certs/ca.crt`).

## 동영상 파일 소스 · 전체 화면 (전화기 페이지, 2026-11-12)

VA 페이지와 같은 기능이다(`../va_service/README.md`의 같은 절). `Video File`로 폰에 저장된 동영상을 고르면 카메라 대신 그 영상을 `NONVA_FPS`로 올린다
(반복·무음 재생; 파일은 폰 안에서만 읽으므로 **하향 트래픽이 없다**). 파일 프레임은 화면비를 유지한 채(남는 곳은 검은 띠) `NONVA_WIDTH × NONVA_HEIGHT`로
변환해 보내므로 **UL 부하는 카메라 모드와 같은 설정을 따른다**(파일이 4K여도 720p로 올라간다). `Full Screen`은 영상만 화면 전체에 띄운다
(나가기 = `Exit Full Screen`). 카메라로 돌아가려면 `Start Camera`, 끄려면 `Stop Camera / Video`. 서버 코드는 바뀌지 않았다.
**처음부터 다시 재생 · 예약 시작(2026-11-14)**: `Restart Video`(즉시 0초부터, 전체 화면에서는 왼쪽 위 단추)와 `Start video at [시:분] Set / Cancel`(그 시각에 0초부터;
`Start Streaming` 뒤에는 기지국 서버 시계, 그 전에는 폰 시계)도 VA 페이지와 같다. 폰 안의 `<video>`만 다루므로 요청도 하향 트래픽도 없다. 자세한 것은
`../va_service/README.md`의 "처음부터 다시 재생 · 예약 시작" 절.
확인(11-12, 헤드리스 Chromium, 시험 서버 1280×720·20 fps): 4:3 파일과 세로 파일 → 수신 1280×720, 20.0~20.3 fps, 전체 화면, 카메라 모드는 원본 페이지와 같음.
실제 폰 확인은 아직.

## 파일

| 파일 | 내용 |
|---|---|
| `app.py` | hsw `app.py` + 설정의 환경변수화, 로그 디렉터리(`../runs/nonva_service_log/`), `service_common` 경로, `GET /nonva/status`. 2026-11-13까지 서버 동작 변경 없음; 전화기 페이지에 동영상 파일 소스·전체 화면 추가(2026-11-12, 위 절); QoS 판정·뷰어 정지와 `Restart Video`·예약 시작 추가(2026-11-14, 위 절들) (차이: `../docs/patches/nonva_service_dgist.patch`) |
| `nonva_sim.py`, `nonva_status.py` | 전화기 대역 시험 클라이언트, 상태 출력 (`nonva.sh test/status`) |
| `service_common/`, `certs/` | hsw 그대로 |
| `test_frames/hd_1280x720_q80.jpg` | `nonva.sh make-frame`이 만든 시험 프레임 |
| `upstream_hsw/app.py` | hsw 원본 (참고용) |

hsw 디렉터리의 `yolo26x.pt`(118 MB)는 현재 app.py가 쓰지 않아 복사하지 않았다. 백업본 `app_2610*.py`, `logs/`도 제외.

CSV 열: `frame_id, timestamp, frame_size_kb, receive_fps, ul_payload_mbps, ul_latency_ms, e2e_latency_ms, clock_offset_ms, sync_rtt_ms,
clock_sync_time_ms, session_start_time_ms, total_startup_time_ms, gpu_util_percent, gpu_memory_used_gb` (hsw와 동일).

## 검증 (2026-11-05 18:34, 같은 호스트, `nonva.sh test ue1 100 20`)

100/100 프레임, 20.2 fps, 204 KB/프레임, 33.8 Mbps, UL 지연 평균 2.4 ms(loopback). `/`, `/viewer/ue1` 200, 텔레메트리 `nonva_ue1.json` 갱신, CSV 저장 확인.
