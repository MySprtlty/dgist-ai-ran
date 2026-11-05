# `nonva_service/` — non-VA 라이브 스트리밍 서비스 (hsw `video_stream_service` 그대로, 2026-11-05)

hsw 연구실의 두-슬라이스 실험에서 **VA가 아닌 쪽 UE**가 쓰는 서비스다. 전화기가 카메라 프레임(1280×720, 20 fps, JPEG 0.80)을
`/upload_frame`으로 올리면 서버가 UE별 최신 프레임을 보관해 `/viewer/<ue>` 페이지에 MJPEG(20 fps)로 다시 내보낸다. **추론은 없다.**
프레임별 UL 지연(이 서비스의 E2E 정의 = UL)을 기록해 Stop 때 CSV로 저장하고, 텔레메트리 `nonva`를 `/tmp/oai-service-telemetry/nonva_ue{1,2}.json`에
쓴다(hsw PPO xApp이 `live_1/live_2`로 읽음; VA 쪽은 `yolo_1/yolo_2`).

```
전화기(UE2)  https://10.45.0.1:8001/  → Start Camera → Start Streaming (UE1/UE2 선택)
             POST /start_session(clock_offset, ue_id) → POST /upload_frame(frame_id, upload_start_ms, JPEG) × 20/s → POST /stop_session → CSV
보는 쪽      https://10.45.0.1:8001/viewer/ue2  (호스트 PC 브라우저나 다른 UE; /video_feed MJPEG)
```

## 설정 (`../.env`의 `NONVA_*`; 기본값 = hsw 코드에 박혀 있던 값)

| 변수 | 기본값 | 뜻 |
|---|---|---|
| `NONVA_WIDTH` × `NONVA_HEIGHT` | 1280 × 720 | 전화기 카메라 요청 해상도 |
| `NONVA_FPS` | 20 | 업로드 속도 (전송 주기 50 ms) |
| `NONVA_JPEG_QUALITY` | 0.80 | 프레임 JPEG 품질 (720p q0.80 ≈ 200 KB → 20 fps면 약 33 Mbps) |
| `NONVA_VIEWER_FPS` | 20 | `/viewer` 중계 속도 |
| `NONVA_PORT` | 8001 | HTTPS 포트 (VA 서비스는 8000) |

바꾼 뒤 `../nonva.sh restart`, 전화기 페이지 새로 고침. 페이지 JS 상수는 서버가 채워 내려 준다(`_page()`).

## 실행 (`../nonva.sh`)

```bash
./nonva.sh start | stop | restart | status | logs
./nonva.sh test ue1 100 20      # 전화기 대역(nonva_sim.py): 720p 프레임 100장을 20 fps로 → 전화기와 같은 CSV
```
venv는 `../va_service/yolo_env`를 같이 쓴다(fastapi/uvicorn/pillow/pynvml만 필요). 인증서는 hsw 것 그대로(CN 10.45.0.1, `certs/ca.crt`).

## 파일

| 파일 | 내용 |
|---|---|
| `app.py` | hsw `app.py` + 설정의 환경변수화, 로그 디렉터리(`../runs/nonva_service_log/`), `service_common` 경로, `GET /nonva/status`. 동작 변경 없음 (차이: `../docs/patches/nonva_service_dgist.patch`) |
| `nonva_sim.py`, `nonva_status.py` | 전화기 대역 시험 클라이언트, 상태 출력 (`nonva.sh test/status`) |
| `service_common/`, `certs/` | hsw 그대로 |
| `test_frames/hd_1280x720_q80.jpg` | `nonva.sh make-frame`이 만든 시험 프레임 |
| `upstream_hsw/app.py` | hsw 원본 (참고용) |

hsw 디렉터리의 `yolo26x.pt`(118 MB)는 현재 app.py가 쓰지 않아 복사하지 않았다. 백업본 `app_2610*.py`, `logs/`도 제외.

CSV 열: `frame_id, timestamp, frame_size_kb, receive_fps, ul_payload_mbps, ul_latency_ms, e2e_latency_ms, clock_offset_ms, sync_rtt_ms,
clock_sync_time_ms, session_start_time_ms, total_startup_time_ms, gpu_util_percent, gpu_memory_used_gb` (hsw와 동일).

## 검증 (2026-11-05 18:34, 같은 호스트, `nonva.sh test ue1 100 20`)

100/100 프레임, 20.2 fps, 204 KB/프레임, 33.8 Mbps, UL 지연 평균 2.4 ms(loopback). `/`, `/viewer/ue1` 200, 텔레메트리 `nonva_ue1.json` 갱신, CSV 저장 확인.
