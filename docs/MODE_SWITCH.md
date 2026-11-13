# Ours / Baseline 모드 전환·실행 매뉴얼 (2026-11-12, 트래픽 생성기 7장 추가 2026-11-13)

모든 명령은 `/home/nvidia/_dgist`에서 **nvidia 계정**으로 실행한다. 예외는 MIG 전환(`scripts/mig.sh`) 하나로, root가 필요하다
(troy 계정에서 `sudo -n`; nvidia 계정에 `sudo` NOPASSWD로 이 스크립트만 허용하면 `all.sh mode`가 알아서 전환한다).

| | Ours (`DEMO_MODE=ours`, 기본) | Baseline (`DEMO_MODE=baseline`) |
|---|---|---|
| GPU | 전체 132 SM, MIG 꺼짐 | MIG 4g.48gb(64 SM) + 3g.48gb(60 SM) |
| L1 | 우리 cuphycontroller: 녹색 컨텍스트, FAPI 인지 YOLO 테넌트, xApp 에이전트 | 연구실 바닐라 cuBB 25-3(`/home/nvidia/hsw/25_3_hsw`, 읽기 전용) → MIG 4g, 컨테이너 `dgist-l1` |
| YOLO | L1 안, 슬롯마다 24~88 SM | 컨테이너 `dgist-yolo` → MIG 3g, naive(60 SM 고정, QoS 거절·분할·모델 선택 없음) |
| xApp | PPO 슬라이싱 (`--xapp`) | 없음 |
| 트래픽 생성기 (`TRAFFIC_GEN=1`, 7장) | 같은 L1에 에뮬레이터 5셀 추가 (모두 6셀) | 6셀 L1은 우리 빌드에서 dApp 기능을 끈 것 (MIG 4g), YOLO는 그대로 |
| gNB·코어·VA/non-VA·뷰어·로그 | 동일 | 동일 |

## 0. 현재 상태 보기

```bash
./all.sh mode            # 모드와 MIG 상태 한 줄
./all.sh status          # 전체 (첫 줄 mode, UE 표 포함)
./dgist.sh status        # L1/gNB/컨테이너, Err.ind, 셀 UL
```

## 1. Ours → Baseline

```bash
./all.sh down                       # xApp, VA/non-VA, gNB, L1 내림 (코어 유지)
./all.sh mode baseline              # .env DEMO_MODE=baseline
sudo -n scripts/mig.sh on           # [root] MIG 켜고 4g+3g 생성, runs/mig.env 기록  (troy 계정에서)
./all.sh up <이름>                   # 바닐라 L1(4g) + naive YOLO(3g) + gNB + VA + non-VA
```

## 2. Baseline → Ours

```bash
./all.sh down
./all.sh mode ours                  # .env DEMO_MODE=ours
sudo -n scripts/mig.sh off          # [root] 인스턴스 삭제, MIG 끔
./all.sh up <이름> --xapp           # 우리 L1 + gNB + VA + non-VA (+ 폰 4대 붙은 뒤 PPO xApp)
```

`--xapp`는 그 시점에 폰 4대가 붙어 있어야 시작된다. "RNTI mapping not possible"이 나오면 폰이 붙은 뒤에:

```bash
./xapp.sh start && ./xapp.sh moni start      # 재접속으로 RNTI가 바뀌면 guard가 자동 재시작
./xapp.sh status | ./xapp.sh trace 3 | ./xapp.sh moni logs | ./xapp.sh moni power 1
```

## 3. 켜진 뒤 공통 작업 (두 모드 동일)

```bash
# 폰: VA https://10.45.0.1:8000/  non-VA https://10.45.0.1:8001/  (서비스를 재시작했으면 페이지 새로 고침 후 시작)
#     소스: Start Camera(카메라) 또는 Video File(폰에 저장된 동영상; VA는 FHD로 자동 변환, 하향 트래픽 없음) → Start VA / Start Streaming → Full Screen
# 뷰어(PC): https://10.150.21.161:8000/viewer   (/viewer/va = VA 2대, /viewer/ue1 = 한 대; 아래 줄 "26x · 60 SM" = Baseline, "80 SM" = Ours)
# 대시보드(PC): http://10.150.21.161:8010/   ("측정 시작" → 이름 입력 … "측정 종료" → 요약; 왼쪽 위 배지가 Ours / Baseline, 기록·Replay는 같은 페이지)
./dash.sh start | status | stop    # 실험 대시보드 (스택과 따로 돈다; 모드를 바꿔도 다시 켤 필요 없음)
./va.sh status | ./nonva.sh status | ./va.sh logs
./dgist.sh test 26x 10 20           # YOLO 서버 직접 부하 (Baseline은 dgist-yolo 안에서 ra_stub)
./va.sh test2 60 10                 # UE 대역 2개로 VA 경로 시험 (루프백, 무선 없음)
./vcam.sh start 2 | status | stop   # 가상 카메라 2대×10 fps = GPU +20 req/s (무선 미사용)
./xapp.sh moni power 1              # UE별 RSRP·PH·송신전력·grant·MCS·SNR 표 (+ CSV log/moni_log/ue_power_*.csv)
python3 scripts/quota_exp.py 0 120 --no-control --label <라벨>     # 2분 기록 (UL/AI 백분위, QoS, 무선, 셀) -> runs/quota_exp/
python3 scripts/quota_exp.py 100 90 --label va100                  # 고정 quota 실험 (PPO xApp 멈춘 상태에서)
```

설정 변경은 `.env`를 고친 뒤 해당 서비스만 재시작한다: `VA_*` → `./va.sh restart`, `NONVA_*` → `./nonva.sh restart`
(폰 페이지 새로 고침 필요). `DAPP_*`·`L1_CONFIG`·`BASELINE_*`는 `./dgist.sh down && ./dgist.sh up <이름>`.

## 4. 끄기

```bash
./all.sh down                       # 코어는 유지 (코어까지: ./all.sh down --core)
```

## 5. 로그 위치

| 무엇 | 어디 |
|---|---|
| L1 (두 모드) | `runs/<이름>/l1_console.log`, `phy_warn.log`, `l1_persec.log`, `entrypoint.log` (Baseline은 `aerial_log/phy.log`도) |
| Baseline YOLO 서버 | `runs/<이름>/yolo_entrypoint.log`, `yolo_console.log`, `yolo_tenant.log` |
| gNB | `./dgist.sh logs gnb`, 내리면 `runs/<이름>/oai.log` |
| VA / non-VA 서비스 | `runs/va_service/uvicorn.log`, `runs/nonva_service/uvicorn.log`; 폰 CSV `runs/va_service_log/`, `runs/nonva_service_log/` |
| xApp | `runs/xapp/xapp.log`, `log/ppo_output_log/`, 모니터 `log/moni_log/` |
| 측정 | `runs/quota_exp/*.csv`, `*.summary.txt`; 가상 카메라 `runs/vcam/` |
| 트래픽 생성기 | `runs/<이름>/ru_console.log`, `testmac_console.log`, `tgen_load.log`, `l1_persec_emu.log`; 전체 nvlog `/data/dgist_tgen/log/<이름>/` |
| 대시보드 | 측정 기록 `runs/dash/<이름>_<시각>/` (`.replay.jsonl`, `.summary.txt/.json`, `.timeseries.csv`, `.frames.csv`, `.slots.csv.gz`), 서버 로그 `runs/dash_service/` |

## 6. 주의

- 순서를 지킨다. 스택이 떠 있으면 `mode`가 거부하고, GPU에 프로세스가 있으면 `mig.sh`가 거부한다.
- `all.sh up` 끝의 `FAILED components: ric`는 타이밍 오경보다(RIC는 정상, E2 SETUP 대기 시간 문제).
- Baseline 엔진 `models/migfhd26x/seg00_sm60.engine`은 한 번만 빌드하면 된다. `VA_MODEL`을 바꾸면 `scripts/build_yolo_engines_mig.sh <n|s|m|l>`(MIG 켠 뒤).
- Baseline L1은 연구실 `25_3_hsw` 빌드를 쓴다(PRACH 임계값 20 하드코딩; 트래픽 생성기를 켜면 우리 빌드로 바뀐다 → 7장). 순정 `25_3`은 가짜 RA가 분당 100건 생긴다.
- 폰 재접속으로 RNTI가 바뀌면 `ue_table.py`가 현재 gNB 기동 이후의 등록만 보고 매핑한다(이전엔 id 재사용으로 잘못 잡혔음).
- 대시보드(`dash.sh`)는 두 모드에서 같은 화면을 쓴다. Baseline에는 슬롯 CSV와 xApp이 없으므로 GPU 슬롯 맵은 MIG 고정 분할(L1 / YOLO)로, GPU 부하율은 요청률 × 추론 시간 추정으로, UE별 PRB는 gNB 로그 표본(~1 %)으로 나온다. 두 모드를 비교할 때는 측정 이름에 모드를 넣어 따로 기록한다 (상세 `dash/README.md`).

## 7. 5셀 트래픽 생성기 (2026-11-13, 두 모드 공통)

RU 에뮬레이터와 testMAC이 **같은 L1 안에서** 셀 5개(L1 셀 0~4, PCI 41~45)를 돌려 부하를 만든다. 폰 4대의 셀은 그대로 Benetel RU이고
L1 셀 번호만 0 → 5로 바뀐다(모두 6셀). 5셀은 시험 벡터 트래픽을 싣고, 부하 단계에 따라 바뀌는 것은 **상향(PUSCH)** 뿐이다.

```bash
./tgen.sh status                     # 스위치, 실행 여부, 부하 단계, 5셀 DL/UL 합, RU 정시율, 로그 크기
./tgen.sh on | off                   # .env TRAFFIC_GEN=1|0  → 반영: ./all.sh down && ./all.sh up <이름>  (L1·gNB 재시작, 폰 재접속)
./tgen.sh load low|mid|high|auto     # 도는 중에 바로 반영(약 0.3초, 재시작 없음). .env TRAFFIC_LOAD는 시작할 때의 단계
./logs.sh tgen | ru | testmac        # 5셀의 초당 DL/UL 줄 + 단계 전환 기록 | RU 에뮬레이터 콘솔 | testMAC 콘솔
```

| 단계 | 셀·UL 슬롯당 PUSCH UE 수 | 5셀 UL 합 (측정) | 5셀 DL 합 (측정) |
|---|---|---|---|
| `low` | 1 | 245 Mbps | 6602 Mbps |
| `mid` | 2 | 494 Mbps | 6725 Mbps |
| `high` | 4 | 979 Mbps | 6602 Mbps |
| `auto` | 3초마다 1 → 2 → 4 → 3 | 741 Mbps (3 UE일 때) | |

켜고 끄는 것은 재시작이 필요하다(L1이 6셀 또는 1셀로 시작해야 하고, gNB의 CONFIG를 받은 뒤에 testMAC이 붙는다). 부하 단계만 실행 중에 바뀐다.

**모드별 차이**

| | Ours + 생성기 | Baseline + 생성기 |
|---|---|---|
| L1 | 우리 빌드, 녹색 컨텍스트, YOLO 테넌트 (1셀 때와 같음) | **우리 빌드(`build.aarch64_f1002`)에서 dApp 기능을 모두 끈 것** → MIG 4g, MPS. 연구실 바닐라 빌드에는 Benetel 셀 + 에뮬레이터 셀을 한 L1에 올리는 수정이 없어서 쓸 수 없다 |
| L1 설정 | `config/tgen/cuphycontroller_dgist_tg.yaml` | `config/tgen/cuphycontroller_dgist_tgbase.yaml`: `use_green_contexts 0`, `mps_sm_pusch 60`, `mps_sm_pdsch 62`, Benetel NIC `mtu 8192` |
| YOLO | L1 안, 부하에 따라 SM 수가 바뀜 | `dgist-yolo` → MIG 3g, 60 SM 고정 (1셀 때와 같음) |
| GPU 메모리 (측정) | L1+YOLO 한 프로세스 77,742 MiB (75.9 GiB) | L1 36,074 MiB (35.2 GiB) / MIG 4g 47,616 MiB (46.5 GiB), YOLO 1,660 MiB |

Baseline 쪽 `mtu 8192`의 이유: 프론트홀 드라이버는 NIC마다 GPU 송신 버퍼를 "2^21 패킷 × (MTU를 2의 거듭제곱으로 올림)"으로 잡는다
(`aerial-fh-driver/lib/nic.cpp:971-984`). MTU 9000이면 32 GiB, 8192면 16 GiB다. 32 GiB로는 6셀 L1이 MIG 4g(46.5 GiB)에 들어가지 않아
에뮬레이터 셀 생성이 메모리 부족으로 실패한다(2026-11-13 측정). 폰 셀의 한 심볼(273 PRB × 28 B + 헤더 ≈ 7.7 kB)은 8192에서도 한 패킷이다.
Ours 설정은 9000 그대로다.

**파일럿 결과 (2026-11-13, 26x, 10 req/s, 단계마다 168건 — 본 측정 아님)**

| 단계 | Ours E2E 평균 (YOLO SM 평균) | Baseline E2E 평균 (60 SM 고정) |
|---|---|---|
| `low` | 15.9 ms (88) | 18.8 ms |
| `mid` | 17.2 ms (78) | 측정 안 함 |
| `high` | 19.2 ms (64) | 18.8 ms |

같은 두 실행에서: 폰 4대 접속(RA 4, RRC 4, PDU 4, RA 실패 0), 폰 셀 DL 0.04 Mbps(늘지 않음), FATAL 0.
Baseline(3분): Err.ind 0, RU 늦은 슬롯 0. Ours(9분): Err.ind 10(한 슬롯에서 에뮬레이터 5셀의 DL C-plane 송신이 0.68 ms 늦음, 원인 미확인), RU DL-U 늦은 슬롯 셀당 2.
폰 셀의 CRC 실패 비율은 Baseline(MTU 8192) 18.5 %, Ours(MTU 9000) 15.9 %와 21.7 %(다른 실행)로, 실행마다 달라지는 범위 안이다.

**로그**: `runs/<이름>/ru_console.log`, `testmac_console.log`, `tgen_load.log`(단계 전환 시각), `l1_persec_emu.log`, `ru_table.txt`(종료 때 늦은 슬롯 표).
전체 nvlog는 `/data/dgist_tgen/log/<이름>/`에 쌓인다(두 모드 모두 26~29 GB/시간 — 루트 디스크가 아니라 `/data`). 오래된 실행은 지워서 관리한다.

**주의**
- 스택이 도는 동안 호스트에서 무거운 파일 검색(스레드를 많이 쓰는 재귀 grep 등)을 돌리지 않는다. 격리 코어에 스레드가 올라가 L1 슬롯 틱이 늦어지고 Err.ind(0x34)가 쏟아진 일이 있다(2026-11-13 측정).
- L1이 시작하다 죽으면 `TG_L1_GDB=1 ./dgist.sh up <이름>`으로 gdb 아래에서 띄워 백트레이스를 `l1_console.log`에 남길 수 있다(원인 추적용, 측정에는 쓰지 않는다).
- 시험 벡터는 `/data/dgist_tgen/testVectors`(.env `TRAFFIC_GEN_TV_DIR`)에 있어야 한다. 없으면 `./dgist.sh check`가 알려 준다.
- 대시보드는 생성기가 켜지면 위쪽에 `tgen` 표시와 5셀 UL/DL 합·부하 단계를 보여 주고, 폰 셀 수치는 자동으로 L1 셀 5에서 읽는다.
