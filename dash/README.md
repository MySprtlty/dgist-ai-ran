# 실험 대시보드 (`dash.sh`, 2026-11-12)

연구자용 측정·시각화 웹 페이지다. PC 브라우저로 `http://10.150.21.161:8010/` 에 들어가 **측정 시작**(실험 이름 입력) → **측정 종료**(결과 요약)를
누르면, 그 사이의 모든 지표가 `runs/dash/<이름>_<시각>/` 에 저장되고 같은 페이지에서 **Replay**로 다시 재생된다.

- 스택에 대해 **읽기 전용**이다. 어떤 컴포넌트도 켜거나 끄거나 설정을 바꾸지 않고, UE로 아무것도 보내지 않는다.
- **하향 트래픽을 만들지 않는다.** 페이지는 LAN 전용 평문 HTTP이고 UE 망(`10.45.0.0/16`)에서 온 요청은 403으로 거절한다.
  서비스의 프레임 피드(`/metrics/frames`)와 AMF UE 표는 루프백으로만 읽는다.
- Ours / Baseline 두 모드에서 그대로 쓴다(`.env DEMO_MODE`를 2 초마다 읽어 화면이 따라 바뀐다).

## 1. 켜고 끄기

`/home/nvidia/_dgist`에서 **nvidia 계정**으로 실행한다(sudo 불필요, root로는 거부).

```bash
./dash.sh start            # 백그라운드 기동 → http://10.150.21.161:8010/
./dash.sh status           # 프로세스, URL, 응답 중인 데이터 출처, 진행 중인 측정, 저장된 측정
./dash.sh stop             # 진행 중인 측정은 요약·파일을 닫은 뒤 종료
./dash.sh restart | logs   # 재시작 | 서버 로그 따라 보기
./dash.sh list             # 저장된 측정 목록 (runs/dash/)
./dash.sh sim [ours|baseline]   # 두 번째 인스턴스 (포트 8011): 가짜 숫자로 화면·측정·Replay 연습. "SIMULATION" 배지, 기록은 runs/dash_sim/
./dash.sh sim stop | sim logs
```

대시보드는 스택과 따로 돈다. `all.sh up/down`과 무관하게 켜 둬도 되고, 스택이 내려가 있으면 각 지표가 "대기"로 보인다.
VA / non-VA 서비스는 프레임 피드(`/metrics/frames`)가 들어간 `app.py`로 떠 있어야 한다(2026-11-12에 설치했으므로 그 뒤에 시작한 서비스는 해당된다).

## 2. 측정 절차

1. 실험 세팅을 끝낸다(폰 4대 attach, VA 2대 Start, non-VA 2대 Start, 필요하면 xApp·가상 카메라).
2. 오른쪽 위 **측정 시작** → 이름 입력(한글·영문·숫자·`_.-`, 나머지 문자는 `_`로 바뀜, 60자까지). 점검 목록에서 빠진 출처를 확인하고 시작한다.
3. 측정 중에는 모든 카드에 **지금 값**(최근 1 초)과 **측정 시작 후 누적 평균**이 같이 나온다. 그래프는 탭 줄 오른쪽에서
   `누적 평균 | 순간값 | 둘 다`, 구간 `전체 | 5분 | 1분`을 고른다. 마우스를 올리면 그 시각의 정확한 값이 나온다.
4. **측정 종료** → 결과 요약 창. 파일 6개가 닫히고 "기록 · Replay" 탭 목록에 올라온다.

측정은 한 번에 하나다(브라우저 여러 대가 같은 상태를 본다). 디스크 여유가 2 GB 미만이면 시작을 거부하고, 측정 중 1 GB 아래로 내려가면 자동으로 종료한다.

## 3. 탭

| 탭 | 내용 |
|---|---|
| 종합 | 핵심 5개(VA QoS 충족률, 셀 UL Throughput, GPU YOLO 요청, mAP 유지율, 측정 시간), UE별 E2E Latency 그래프, GPU SM 슬롯 맵, PRB 슬라이싱·서비스 Throughput, UE 현황 표 |
| VA 서비스 | 서비스 합계, UE별: 화질(해상도·JPEG 품질·입력 종류)·FPS 목표/실제·프레임 크기, QoS 충족률, Latency 추이(UL / Inference / E2E), E2E 구성 막대, 선택된 모델(5칸 표시등), 모델 사용 비율 |
| Non-VA 서비스 | 서비스 합계, UE별: 화질·FPS, Uplink Latency(평균·p90·p99)와 추이, Throughput 추이(앱 수신 / MAC), PRB 사용 |
| GPU · 슬롯 | 슬롯별 GPU SM 배분(최근 80 슬롯, 초당 5회 갱신), YOLO 요청 부하(req/s, 부하율, 감당 가능한 요청률, 대기열, 판정), SM 사용 추이, 모델 × SM 조합 히트맵, 정확도(mAP), 가상 카메라 |
| 무선 · 슬라이싱 | PRB 슬라이싱 막대(서비스 할당 → UE 사용, 지금 / 측정 평균), 서비스별 PRB 할당·사용 추이, UE별 PRB·Throughput, 서비스·셀 Throughput, UE 무선 상태(RNTI, SNR, UL MCS, UL BLER, RSRP, PH, UL 버퍼, DL MCS/BLER) |
| 기록 · Replay | 저장된 측정 목록(재생, 요약, 파일 받기), Replay 파일 열기(다른 PC에서 받은 `.replay.jsonl`), 결과 요약 |

종합·VA·Non-VA·기록 탭은 FHD(1920×1080) 한 화면에 들어온다. GPU(1200~1300 px)와 무선(≈ 1500 px) 탭은 세로로 길어서 브라우저 배율 70~80 %에서 한 화면이 된다(촬영할 때는 배율을 낮추거나 스크롤한다). 오른쪽 위 아이콘: 밝은/어두운 테마, 전체 화면.

## 4. 지표 정의와 출처

UE 이름은 `VA UE1 / VA UE2 / Non-VA UE1 / Non-VA UE2`(각 서비스 페이지의 ue1/ue2 세션)이고 색은 모든 탭에서 같다.
"지금" = 최근 1 초 창, "누적" = 측정 시작 이후. 평균은 프레임 수(또는 초) 가중이다.

### VA (프레임 단위, 출처: VA 서비스의 프레임 피드 + 폰의 E2E 보고)

| 지표 | 정의 |
|---|---|
| Uplink Latency | 폰이 프레임을 보내기 시작한 뒤 서버가 다 받을 때까지 |
| Inference Latency (AI) | 서버가 프레임을 받은 뒤 결과를 내보낼 때까지 = 대기 + JPEG 디코드 + GPU 추론 + 후처리. "GPU 추론"은 그 안의 추론 시간만 |
| E2E Latency | 폰이 프레임을 캡처한 뒤 결과를 화면에 그릴 때까지. **폰이 보고한 값**을 쓴다. 보고가 1.5 초 안에 오지 않은 프레임은 UL + Inference + 서버 기타 + 고정 DL(`VA_VIEWER_DL_MS`, 20 ms)로 **추정**하고 "추정"으로 표시한다(`frames.csv`의 `e2e_src=est`) |
| DL (결과 전달) | E2E − UL − Inference − 서버 기타 (폰 보고가 있는 프레임만) |
| QoS 충족률 | 성공했고 E2E ≤ 기한(`VA_DEADLINE_MS`, 200 ms)인 프레임 ÷ 전체 프레임. 거절·실패·업로드 포기 프레임은 미충족 |
| 선택된 모델 / SM | 그 UE의 가장 최근 성공 프레임을 실행한 모델(26n/s/m/l/x)과 SM 클래스. 5 초 동안 프레임이 없으면 꺼진다 |
| 수신 FPS, 앱 Throughput | 서버가 받은 프레임 수 / 초, JPEG 바이트 × 8 / 초 |
| p50 / p90 / p99 | 측정 시작 후 프레임 분포의 백분위(1 ms 폭 히스토그램, 값은 칸의 가운데) |

### Non-VA (출처: non-VA 서비스의 프레임 피드)

Uplink Latency(위와 같은 정의), 수신 FPS, 앱 Throughput. 추론이 없으므로 E2E·QoS는 없다.

### GPU (출처: Ours = L1 테넌트 슬롯 CSV + dApp 에이전트, Baseline = 프레임 피드)

| 지표 | 정의 |
|---|---|
| YOLO 요청 (req/s) | 에이전트 `sm_report`의 요청 수 ÷ 창 길이. 없으면 VA 프레임 수 + 가상 카메라 요청률 |
| GPU 부하율 | Ours: YOLO가 실행 중이던 슬롯의 비율(슬롯 CSV). Baseline·CSV 없음: 요청률 × 요청당 GPU 추론 시간(추정). NVML 사용률과는 다른 값이다 |
| 감당 가능한 요청률 | 요청률 ÷ 부하율 (부하율 5 % 이상일 때). 지금의 화질·모델 구성이 유지된다는 가정의 **추정값** |
| 판정 | 포화: 부하율 ≥ 90 % 또는 요청 거절 또는 대기열 ≥ 3 또는 대기 시간 > 기한의 50 %. 주의: 부하율 ≥ 70 % 또는 대기열 ≥ 2 또는 대기 시간 > 기한의 20 %. 그 밖은 여유 |
| 슬롯별 SM (Ours) | 슬롯 CSV 한 줄 = 한 슬롯(0.5 ms). RAN 예약 = PDSCH(`gap_N`) + ORDER 8 + PUSCH(132 − `gap_P`), YOLO 제공 = 그 슬롯에 테넌트가 쓸 수 있는 틈, YOLO 실행 = 추론이 실제로 점유한 SM 클래스(`run_cls`, 0 = 유휴), 사용 불가 = 132 − RAN − 제공 |
| 슬롯별 SM (Baseline) | 슬롯 CSV가 없다. MIG 고정 분할(L1 64 SM, YOLO 60 SM)을 그리고 YOLO 쪽은 요청 유무만 표시한다 |
| 모델 × SM 조합 | 측정 시작 후 (모델, SM 클래스) 조합의 비율. 요청 수 기준(프레임 피드)과 실행 슬롯 수 기준(슬롯 CSV) 두 가지 |
| 정확도 (mAP) | 성공한 VA 요청을 실행한 모델의 mAP를 요청 수로 가중 평균. 유지율 = 평균 ÷ 기준 모델(26x) mAP, 감소율 = 1 − 유지율. 모델별 mAP는 **Ultralytics 공개값**(COCO val2017, 640 px, mAP50-95: 26n 40.9, 26s 48.6, 26m 53.1, 26l 55.0, 26x 57.5)이며 우리 FHD 파이프라인에서 잰 값이 아니다 |
| NVML | GPU 사용률, 전력, 온도, 메모리 (2 초마다) |
| 가상 카메라 | `runs/vcam/status.json`: 대수, FPS, 모델, 기한, 동시 요청, 요청률, QoS 충족률, 응답 시간 p50/p90/p99. 폰이 없으므로 E2E는 없다 |

### 무선 · 슬라이싱 (출처: gNB 로그, xApp 로그)

| 지표 | 정의 |
|---|---|
| RNTI, SNR, UL MCS, UL BLER, RSRP, PH, DL MCS/BLER | gNB의 1.28 초 주기 UE 통계(`docker logs dgist-gnb`). 누적 UL BLER = 재전송 수 ÷ 첫 전송 수 |
| MAC Throughput | gNB가 센 UE별 UL MAC 바이트 증가량(헤더·재전송 포함). 앱 Throughput과 다르다 |
| 셀 UL Throughput | 화면의 값은 UE별 MAC Throughput의 합이다. L1이 초당 찍는 `Cell 0 … UL` 값은 화면에 없고 `timeseries.csv`의 `cell.ul`과 요약에 같이 기록된다 |
| 서비스 PRB 할당 (quota) | gNB 로그의 `E2_SLICE_TRANSITION_DONE`(다음 전환까지 유효). 없으면 PPO xApp 로그, 모니터 xApp 로그. 아무것도 없으면 "슬라이싱 없음 (PF 공유)" |
| UE별 PRB 사용 | UL 슬롯당 평균 PRB. 출처 우선순위: ① PPO xApp 입력 로그(E2 KPM) ② 모니터 xApp 로그 ③ gNB 로그 표본. ③은 MAC이 100 프레임마다 한 프레임만 찍는 **약 1 % 표본**(최근 10 프레임 = 60 UL 슬롯 평균)이라 값이 거칠다. 화면에 출처가 표시된다 |
| 서비스 PRB 사용, 셀 미사용 | UE별 값의 합, 273 − 합 |

같은 서비스 안의 UE 간 배분은 gNB MAC 스케줄러(PF)가 정한다. 대시보드는 결과만 보여 준다.

### 트래픽 생성기 (2026-11-13; 출처: L1 콘솔의 셀별 초당 줄, `/dev/shm/dgist_tgen_load`, 프로세스 목록)

`./tgen.sh on`으로 켠 스택에서는 L1이 6셀이다. 폰 4대의 셀은 **번호가 가장 큰 셀(5)** 이고 그 아래 0~4가 RU 에뮬레이터 셀이다.
대시보드는 이 규칙으로 셀을 나누므로 설정을 바꿀 필요가 없다(생성기가 꺼져 있으면 폰 셀은 0).

| 표시 | 정의 |
|---|---|
| 위쪽 `트래픽 생성기 · 저부하/중부하/고부하` 표시 | RU 에뮬레이터와 testMAC 프로세스가 둘 다 돌 때 켜진다. 부하 단계는 두 도구가 함께 읽는 8바이트 제어 값(`/dev/shm/dgist_tgen_load`)에서 읽는다(`./tgen.sh load …`가 쓰는 값) |
| 셀 UL/DL, CRC (폰 셀) | L1 셀 5의 초당 줄. 에뮬레이터 셀의 값은 섞이지 않는다 |
| 에뮬레이터 5셀 UL 합, DL 합, CRC 오류 | L1 셀 0~4의 초당 줄을 더한 값. `timeseries.csv`의 `tgen.*` 열과 요약의 "Traffic generator" 줄(단계별 측정 시간(초), 종료 시점의 단계·UL·DL·CRC)에 기록된다 |
| 측정 이름 제안 | 생성기가 돌면 `ours_tglow_…`, `baseline_tghigh_…`처럼 단계가 붙는다 |

부하 단계는 실행 중에 바뀌므로, 한 측정 안에서 단계를 바꿨다면 요약의 "단계별 측정 시간"으로 구간을 확인한다.
에뮬레이터 셀에는 UE·RNTI가 없다. UE 표와 서비스 지표는 폰 4대만 다룬다.

### UE ↔ RNTI ↔ IMSI ↔ IP

서비스 세션(ue1/ue2)의 폰 IP → SMF가 준 IMSI → AMF가 아는 gNB UE id → gNB 로그의 RNTI 순으로 잇는다. gNB UE id는 gNB 연결마다 다시 시작하고
한 연결 안에서도 재사용되며, Service request로 돌아온 폰은 "Registration complete" 없이 새 id를 받는다. 그래서

1. AMF 저널을 상태 기계로 읽고(`DASH_CORE_SINCE`, 기본 10 일),
2. AMF 자체의 UE 표(`http://127.0.0.5:9090/ue-info`, Open5GS 2.8, 루프백·읽기 전용)가 답하면 그 표를 우선하며,
3. 코어를 읽은 시각 직전·직후에 나타난 RNTI는 다음 읽기(15 초 주기, attach 직후 2.5 초)까지 보류한다.

확실하지 않으면 추측하지 않고 **"RNTI 미확인"**으로 둔다(무선 지표만 비고 서비스 지표는 그대로 나온다). 계속 미확인이면 그 폰의 비행기 모드를 껐다 켠다.

## 5. 저장 파일 (`runs/dash/<이름>_<YYYYMMDD_HHMMSS>/`)

| 파일 | 내용 |
|---|---|
| `<이름>.replay.jsonl` | 페이지가 받은 메시지 그대로: 첫 줄 `meta`(설정·UE·mAP 표), 1 초마다 `tick`, 0.2 초마다 `scope`(슬롯 80개), 마지막 줄 `end`(요약). **이 파일 하나로 Replay가 된다** |
| `<이름>.summary.json` / `.summary.txt` | 결과 요약(UE·서비스·GPU·정확도·셀·출처·주의 사항). txt는 사람이 읽는 표 |
| `<이름>.timeseries.csv` | 1 초에 한 줄, tick의 모든 숫자(열 이름 = `ue.va1.e2e`, `ue.va1.c.e2e`(누적), `gpu.req`, `svc.va.quota` …) |
| `<이름>.frames.csv` | VA 프레임 한 줄씩: `t_s, ue, service_ue, frame_id, ip, rnti, ul_ms, ai_ms, wait_ms, decode_ms, infer_ms, server_other_ms, e2e_ms, e2e_src, dl_ms, qos_met, status, reason, model, sm, sm_mean, kb, w, h, detections` |
| `<이름>.slots.csv.gz` | 슬롯 한 줄씩(Ours): `abs_slot, pos, gap_P, gap_N, run_cls, queue, model, t_ns`. `DASH_REC_SLOTS=0`이면 만들지 않는다 |

크기는 UE 4대 기준 시간당 약 0.13 GB다(167 초 실측 5.8 MB: slots 3.0, replay 2.2, frames 0.3, timeseries 0.25 MB; 프레임률이 높으면 조금 더 는다).

## 6. Replay

"기록 · Replay" 탭에서 측정을 고르면 실시간과 **같은 화면 코드**로 그래프·슬롯 맵·표가 다시 움직인다. 재생/일시정지, 위치 이동, 1×/2×/4×/8×,
"요약", "실시간으로 돌아가기". 다른 PC에서 받은 기록은 `.replay.jsonl`을 "Replay 파일 열기"에 끌어다 놓는다(서버에 올라가지 않고 브라우저 안에서만 읽는다).
Replay 중에는 탭 아래에 REPLAY 띠(이름·재생 위치·배속)가 나온다.

## 7. 설정 (`.env`, 모두 선택 사항 — 바꾼 뒤 `./dash.sh restart`)

| 키 | 기본값 | 뜻 |
|---|---|---|
| `DASH_PORT` / `DASH_SIM_PORT` | 8010 / 8011 | 실제 / 시뮬레이션 인스턴스 포트 |
| `DASH_DENY_NETS` | `10.45.0.0/16` | 이 대역의 요청은 403 (UE 망 차단) |
| `DASH_MIN_FREE_GB` | 2.0 | 이보다 여유가 적으면 측정 시작 거부, 절반 아래면 자동 종료 |
| `DASH_REC_SLOTS` | 1 | 0 = 슬롯 CSV를 기록에 넣지 않음 |
| `DASH_MAP` | (공개값) | 모델별 mAP 교체: `26n=40.9,26s=48.6,26m=53.1,26l=55.0,26x=57.5` 형식. 우리 파이프라인 실측값이 생기면 여기에 넣는다 |
| `DASH_MAP_SRC` | Ultralytics 공개값 문구 | 화면에 표시할 mAP 출처 문구 |
| `DASH_BASE_MODEL` | 26x | 정확도 비교 기준 모델 |
| `DASH_BASELINE_RAN_SM` / `DASH_BASELINE_YOLO_SM` | 64 / 60 | Baseline 슬롯 맵에 그리는 MIG 4g / 3g의 SM 수 |
| `DASH_CORE_SINCE` | `10 days ago` | AMF 저널을 어디서부터 읽을지 (`journalctl --since`) |
| `DASH_AMF_INFO` | `127.0.0.5:9090` | AMF UE 표 주소. `off`면 저널만 쓴다 |
| `DASH_L1_CONTAINER` / `DASH_GNB_CONTAINER` | dgist-l1 / dgist-gnb | 컨테이너 이름 |
| `DASH_SLOT_CSV` | `/tmp/dapp_tenant_slots.csv` | L1 컨테이너 안의 슬롯 CSV 경로 (`DAPP_SLOT_CSV=1`일 때 생김) |
| `DASH_L1_FOLLOW` | 1 | 0 = L1 초당 줄을 읽지 않음 |

그 밖에 `VA_*`, `NONVA_*`, `VCAM_*`, `DAPP_*`, `VA_IMSIS`, `NONVA_IMSIS`, `DEMO_MODE`, `PPO_BUNDLE_NAME`을 읽기만 한다.

## 8. 출처별 가용성과 한계

| 출처 | Ours | Baseline | 없을 때 |
|---|---|---|---|
| VA / non-VA 프레임 피드 | ○ | ○ | 서비스 지표 전체가 빈다 (서비스를 새 `app.py`로 재시작) |
| 폰 E2E 보고 | ○ | ○ | E2E를 추정값으로 대체하고 표시 (`ue_sim.py` 루프백·가상 카메라는 보고 없음) |
| gNB 로그 | ○ | ○ | 무선 지표·RNTI·MAC Throughput이 빈다 |
| L1 초당 줄 | ○ | ○ | 기록의 `cell.ul`·`cell.dl`만 빈다 (화면은 영향 없음) |
| 슬롯 CSV | ○ (`DAPP_SLOT_CSV=1`) | × | 슬롯별 SM, 실행 슬롯 기준 조합 없음; 부하율은 추정식 |
| dApp 에이전트 (`estimate`, `sm_report`) | ○ | △ (naive YOLO 서버의 에이전트, 실기 미검증) | 요청률을 VA 프레임 수 + 가상 카메라 요청률로 센다 |
| PPO xApp / 모니터 xApp 로그 | xApp 켰을 때 | × | PRB가 gNB 1 % 표본, 할당은 gNB 로그의 전환 기록만 |
| Open5GS (저널 + AMF UE 표) | ○ | ○ | "RNTI 미확인" (무선 지표가 UE에 붙지 않는다) |

- 모델 선택은 지금 `VA_MODEL=26x` 고정이라 표시등·조합·정확도는 26x 한 칸만 켜진다(`auto`로 바꾸면 그대로 반영된다).
- mAP는 공개 COCO 값이다. "정확도 감소율"은 실측 정확도가 아니라 **모델 구성에서 나온 기대값**이다.
- Baseline의 MIG 4g SM 수는 문서에 64와 72 두 값이 있다. 슬롯 맵은 `DASH_BASELINE_RAN_SM`(기본 64)을 쓴다.
- p99.99 같은 꼬리 지표는 내지 않는다(측정 길이가 그만큼 길지 않다).
- 시뮬레이션 인스턴스의 숫자는 전부 가짜다(배지와 요약에 표시된다).

## 9. 문제 해결

| 증상 | 확인 |
|---|---|
| 페이지가 안 열림 | `./dash.sh status`. 폰(UE 망)에서는 일부러 403이다. PC는 LAN(10.150.21.x)에서 접속 |
| VA / Non-VA 카드가 계속 "대기" | 서비스가 프레임 피드 이전의 `app.py`로 돌고 있다 → 실험 사이에 `./va.sh restart`, `./nonva.sh restart` 후 폰 페이지 새로 고침 |
| "RNTI 미확인" | 15 초 기다린다. 계속이면 그 폰의 비행기 모드를 껐다 켠다. 코어가 내려가 있으면 `./core.sh status` |
| 슬롯 맵이 "수신 대기" | Ours에서 `.env DAPP_SLOT_CSV=1`인지, L1이 떠 있는지. Baseline은 원래 없다 |
| PRB 값이 거칠다 | 출처가 "gNB 로그 표본 (~1 %)"이다. xApp(`./xapp.sh start` 또는 `moni start`)이 돌면 KPM 값으로 바뀐다 |
| 측정 시작이 거부됨 | 디스크 여유(`DASH_MIN_FREE_GB`), 이미 진행 중인 측정 |

로그: `runs/dash_service/uvicorn.log`(최신 링크), `uvicorn_<MMDD_HHMMSS>.log`, 시뮬레이션 `sim.log`.

## 10. 코드

| 파일 | 역할 |
|---|---|
| `dash_server.py` | FastAPI 앱: SSE `/api/stream`, `/api/init`, 측정 시작/종료, 기록 목록·파일, 정적 파일, UE 망 차단 |
| `hub.py` | 모든 수집값을 모아 1 초 tick·0.2 초 슬롯 스코프를 만들고, 누적 통계·요약·기록 파일을 쓴다 |
| `sources.py` | 수집기: gNB 로그, 슬롯 CSV, L1 초당 줄, PPO·모니터 xApp 로그, dApp 에이전트, VA/non-VA 피드, 시스템(NVML·컨테이너·가상 카메라), 코어(AMF 저널 + UE 표) |
| `sim.py` | 시뮬레이션 데이터 (`dash.sh sim`) |
| `static/` | 페이지: `index.html`, `app.css`, `js/util.js`(공통), `charts.js`(그래프), `views.js`(탭), `app.js`(피드·측정·Replay), 글꼴 Pretendard(SIL OFL, `fonts/Pretendard-LICENSE.txt`) |

외부 CDN을 쓰지 않으므로 인터넷이 없어도 동작한다. Python 의존성은 VA 서비스 venv(`va_service/yolo_env`: fastapi, uvicorn)를 그대로 쓴다.
