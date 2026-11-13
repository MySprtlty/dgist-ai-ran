# `_dgist` 운용 매뉴얼 (2026-11-13 정리)

이 문서 하나로 세 가지를 한다.

| 하고 싶은 일 | 장 |
|---|---|
| **Ours ↔ Baseline 전환** (+ 5셀 트래픽 생성기 켜고 끄기, 부하 단계) | 2장 |
| **구성요소 켜고 끄기** (한 번에 / 모듈별, 순서, 전화기 4대) | 3장 |
| **모니터링** (상태 명령, 로그, 기지국 뷰어·녹화) 과 **실험 대시보드** (측정 · Replay) | 4장, 5장 |

급하면 1장만 보면 된다. 실험 한 번의 전체 흐름은 6장, 막혔을 때는 9장.

용어: **SM** = GPU 안의 연산 블록(스트리밍 멀티프로세서; 이 GH200은 132개). **MIG** = GPU를 고정 크기 조각(인스턴스)으로 나누는 NVIDIA 기능.
**L1** = 기지국의 물리 계층 프로그램(`cuphycontroller`, GPU 사용). **gNB** = 그 위의 기지국 프로그램(OAI). **dApp** = L1 안에서 YOLO 추론을 돌리는 우리 모듈.
**RIC / xApp** = 기지국을 밖에서 제어하는 서버 / 그 위의 제어 프로그램(여기서는 UE별 상향 자원(PRB)을 나누는 PPO xApp). **RNTI** = 기지국이 접속한 전화기에 붙이는 임시 번호.
**VA** = 영상 분석 서비스(전화기 프레임 → YOLO), **non-VA** = 추론 없는 라이브 스트리밍 서비스.

## 0. 기본 규칙

- 모든 명령은 **nvidia 계정으로 `/home/nvidia/_dgist`에서** 실행한다. 다른 계정에서는 `sudo -n -u nvidia -H bash -c 'cd /home/nvidia/_dgist && ./all.sh status'` 꼴로 부른다.
- **root로 실행하지 않는다**(`sudo ./all.sh`, `sudo ./ric.sh` 금지). 프로세스와 파일이 root 소유가 되어 nvidia 계정의 스크립트가 멈출 수 없고(`all.sh down`이 RIC · 모니터를 못 내린다), 다음 `up`이 "이미 떠 있음"으로 건너뛴다. 스크립트가 root면 거부한다.
- 예외는 둘뿐이다: **코어**(`core.sh`가 안에서 sudo를 부른다 → nvidia 비밀번호)와 **MIG 전환**(`scripts/mig.sh`, root 필요. nvidia 계정에는 이 권한이 없으므로 sudo가 되는 계정(troy)에서 실행).
- 설정은 전부 `.env` 한 파일이다(8장).
- **순서**: RIC는 gNB보다 먼저 켜고, gNB가 도는 동안 RIC를 내리거나 재시작하지 않는다(gNB가 죽는다). 코어는 보통 켜 둔 채로 둔다. `all.sh`가 이 순서를 지킨다.
- GPU는 한 장을 같이 쓴다. 켜기 전에 `nvidia-smi`로 다른 사람의 프로세스가 없는지 본다. 연구실 스택(`nv-cubb`, `oai-gnb*` 컨테이너)과는 동시에 못 돈다(`./dgist.sh check`가 막는다).

## 1. 한 장 요약

```bash
cd /home/nvidia/_dgist                 # nvidia 계정

# 지금 상태
./all.sh status                        # 모드, 생성기, 코어, L1/gNB, VA/non-VA, RIC/xApp, UE 표
./all.sh mode                          # 모드와 MIG 상태 한 줄
./tgen.sh status                       # 5셀 트래픽 생성기 스위치와 부하 단계

# 켜기 / 끄기
./all.sh up <이름> --xapp              # Ours: 코어 → RIC → L1+gNB → VA → non-VA → (전화기 4대가 붙어 있으면) PPO xApp
./all.sh up <이름>                     # Baseline: xApp 없음 (--xapp은 무시된다)
./all.sh down                          # 전부 끄기 (코어는 둔다; 코어까지는 --core)

# 모드 전환 (스택을 내린 뒤)
./all.sh mode baseline                 # 또는 ours  → .env DEMO_MODE
sudo -n /home/nvidia/_dgist/scripts/mig.sh on      # [troy 계정] Baseline = on, Ours = off

# 트래픽 생성기
./tgen.sh on | off                     # 다음 ./all.sh up 부터 적용 (L1이 6셀 또는 1셀로 시작)
./tgen.sh load low|mid|high|auto       # 도는 중에 바로 바뀜

# 보기
./logs.sh all                          # gNB 사건 + xApp 결정 + L1 한 줄 (Ctrl-C로 나옴)
./dash.sh start                        # 대시보드  http://10.150.21.161:8010/  (PC 브라우저)
#   기지국 뷰어  https://10.150.21.161:8000/viewer   전화기: VA https://10.45.0.1:8000/  non-VA https://10.45.0.1:8001/
```

## 2. 모드 전환: Ours ↔ Baseline

### 2.1 두 모드의 차이

| | Ours (`DEMO_MODE=ours`) | Baseline (`DEMO_MODE=baseline`) |
|---|---|---|
| GPU | 전체 132 SM, MIG 꺼짐 | MIG 인스턴스 둘: 4g.48gb(L1용, 64 SM) + 3g.48gb(YOLO용, 60 SM) |
| L1 | 우리 빌드(`build.aarch64_f1002`): 녹색 컨텍스트(GPU 일부 SM만 쓰는 CUDA 컨텍스트), FAPI를 보고 YOLO에 SM을 내주는 스케줄러, xApp 에이전트 | 연구실 바닐라 cuBB 25-3(`BASELINE_TREE`, 읽기 전용) → MIG 4g. 생성기를 켜면 우리 빌드에서 dApp 기능을 모두 끈 것(2.5절) |
| YOLO | L1 프로세스 안, 슬롯마다 24~88 SM(`.env DAPP_TENANT_CLASSES_FHD`; 전화기가 붙어 있으면 실제로는 80까지, 부록 B), QoS 거절 · 그래프 분할, 다섯 모델(26n~26x)을 모두 올려 두고 요청이 지정한 모델로 실행 | 별도 컨테이너 `dgist-yolo` → MIG 3g, 60 SM 고정, 도착 순서대로 실행(QoS 거절 · 분할 없음), 모델은 `VA_MODEL` 하나(`VA_MODEL_SELECT=1`이면 MIG 엔진이 있는 규칙 모델도 올린다, 3.7절) |
| xApp | PPO 슬라이싱(`--xapp`) | 없음(RIC는 gNB 때문에 그대로 뜬다) |
| gNB, 코어, VA / non-VA 서비스, 뷰어, 대시보드, 로그 | 같음 | 같음 |

### 2.2 Ours → Baseline

```bash
./all.sh down                                       # xApp, VA/non-VA, gNB, L1 내림 (코어 유지)
./all.sh mode baseline                              # .env DEMO_MODE=baseline. MIG 명령을 안내해 준다
sudo -n /home/nvidia/_dgist/scripts/mig.sh on       # [troy 계정] MIG 켜고 4g+3g 생성, runs/mig.env 에 UUID 기록
./all.sh mode                                       # 확인: "mode: baseline | MIG: Enabled 2 instance(s)"
./all.sh up <이름>                                   # L1(4g) + YOLO 서버(3g) + gNB + VA + non-VA
```

### 2.3 Baseline → Ours

```bash
./all.sh down
./all.sh mode ours                                  # .env DEMO_MODE=ours
sudo -n /home/nvidia/_dgist/scripts/mig.sh off      # [troy 계정] 인스턴스 삭제, MIG 끔
./all.sh mode                                       # 확인: "mode: ours | MIG: Disabled 0 instance(s)"
./all.sh up <이름> --xapp                            # 우리 L1 + gNB + VA + non-VA (+ 전화기 4대가 붙어 있으면 PPO xApp)
```

`--xapp`은 그 순간 전화기 4대가 붙어 있어야 시작된다. "RNTI mapping not possible"이 나오면 전화기가 붙은 뒤에 `./xapp.sh start && ./xapp.sh moni start`.

### 2.4 전환이 됐는지 확인

- `./all.sh status` 첫 줄 `mode : …`, `./dgist.sh status` 첫 줄(Baseline이면 MIG UUID와 컨테이너 `dgist-l1`, `dgist-yolo`, `dgist-gnb`).
- 기지국 뷰어 칸 아래 줄: "26x · 60 SM" = Baseline, "· 80 SM" = Ours.
- 대시보드는 `.env DEMO_MODE`를 2초마다 읽어 화면이 따라 바뀐다.

### 2.5 5셀 트래픽 생성기 (두 모드 공통)

RU 에뮬레이터와 testMAC(가짜 무선 장치와 가짜 상위 계층)이 **같은 L1 안에서** 셀 5개(L1 셀 0~4)를 돌려 GPU에 5G 부하를 만든다.
전화기 4대의 셀은 그대로 Benetel RU이고 L1 셀 번호만 0 → 5로 바뀐다(모두 6셀). 부하 단계에 따라 바뀌는 것은 5셀의 **상향(PUSCH)** 뿐이다.

```bash
./tgen.sh status                     # 스위치, 실행 여부, 부하 단계, 5셀 DL/UL 합, RU 정시율, 로그 크기
./tgen.sh on | off                   # .env TRAFFIC_GEN=1|0 → 반영: ./all.sh down && ./all.sh up <이름>  (L1·gNB 재시작, 전화기 재접속)
./tgen.sh load low|mid|high|auto     # 실행 중에 바로 반영(약 0.3초, 재시작 없음). 시작할 때의 단계는 .env TRAFFIC_LOAD
./logs.sh tgen | ru | testmac        # 5셀의 초당 DL/UL 줄 + 단계 전환 기록 | RU 에뮬레이터 | testMAC
```

| 단계 | 셀 · UL 슬롯당 PUSCH UE 수 | 5셀 UL 합 (측정) |
|---|---|---|
| `low` | 1 | 245 Mbps |
| `mid` | 2 | 494 Mbps |
| `high` | 4 | 979 Mbps |
| `auto` | 3초마다 1 → 2 → 4 → 3 | 3 UE일 때 741 Mbps |

5셀 DL 합은 단계와 무관하게 약 6.6~6.7 Gbps다(측정). 켜고 끄는 것은 재시작이 필요하고, 부하 단계만 실행 중에 바뀐다.

**네 가지 구성**

| 구성 | 만드는 법 (스택을 내린 뒤) | L1 |
|---|---|---|
| Ours, 전화기 셀만 | `mode ours` + `tgen off` | 우리 빌드, 1셀 |
| Ours + 생성기 | `mode ours` + `tgen on` | 우리 빌드, 6셀. YOLO SM 수가 부하에 따라 바뀜 |
| Baseline, 전화기 셀만 | `mode baseline` + `tgen off` | 연구실 바닐라 빌드, 1셀, MIG 4g |
| Baseline + 생성기 | `mode baseline` + `tgen on` | **우리 빌드에서 dApp 기능을 모두 끈 것**, 6셀, MIG 4g (바닐라 빌드에는 Benetel 셀과 에뮬레이터 셀을 한 L1에 올리는 수정이 없다) |

예: Baseline, 고부하로 시작

```bash
./all.sh down && ./all.sh mode baseline
sudo -n /home/nvidia/_dgist/scripts/mig.sh on       # [troy 계정]
./tgen.sh on
./all.sh up base_high
./tgen.sh load high                                 # 또는 .env TRAFFIC_LOAD=high 로 두고 시작
```

### 2.6 전환할 때 주의

- 스택이 떠 있으면 `mode`가 거부한다. GPU에 프로세스가 하나라도 있으면 `mig.sh on`도 `off`도 거부한다(다른 사람의 프로세스도 포함: 먼저 `nvidia-smi`).
- `mig.sh`는 GPU 전체를 바꾼다: 호스트의 MPS 데몬을 모두 끄고, 모드가 바로 안 바뀌면 GPU를 리셋한다. 다른 사람이 GPU를 쓰는 시간에는 실행하지 않는다.
- Baseline의 YOLO 엔진은 MIG 3g 안에서 한 번 빌드해 둔다: `scripts/build_yolo_engines_mig.sh x`(MIG 켠 뒤, 10~15분). 26x용 `models/migfhd26x/seg00_sm60.engine`은 이미 있다. `VA_MODEL`을 바꾸면 그 모델 것을 빌드한다(없으면 `mode`와 `check`가 알려 준다). `VA_MODEL_SELECT=1`로 모델을 섞으려면 그 모델들 것도 빌드한다: `scripts/build_yolo_engines_mig.sh n s m l`(3.7절).
- 생성기를 켠 실행은 전체 로그가 `/data/dgist_tgen/log/<이름>/`에 시간당 26~29 GB 쌓인다(측정). 시작 전 `df -h / /data`, 끝난 실행은 지워서 관리한다.
- `./tgen.sh status`로 스위치를 확인하고 켠다. `.env TRAFFIC_GEN=1`이면 `./all.sh up`이 6셀로 뜬다.
- 4g 인스턴스의 SM 수는 64다(2026-11-12 실측). `scripts/mig.sh`와 `docker-compose.baseline.yaml`의 주석, 슬롯 맵 렌더러의 기본 벽(`--wall 72`)에는 실측 전의 값 72가 남아 있다.

더 보기: `docs/MODE_SWITCH.md`(모드별 L1 설정, 생성기의 GPU 메모리 · MTU 8192 이유 · 파일럿 결과).

## 3. 구성요소 켜고 끄기

### 3.1 한 번에

```bash
./all.sh up <이름> [--xapp]     # 이미 떠 있는 것은 건너뛰고 꺼진 것만 올린다. 끝줄 "== all up" 또는 "== FAILED components: …"
./all.sh down [--core]          # 끝줄 "== all down (core left running)" 또는 "== still running: …"
./all.sh restart <이름>         # down + up
./all.sh status
```

- 이름은 실행 로그 디렉터리 `runs/<이름>/`가 된다(생략하면 `all_<월일_시분>`).
- 코어가 꺼져 있으면 `up`이 sudo 비밀번호를 묻는다.
- L1이 준비될 때까지 gNB는 시작하지 않는다(최대 약 10분 기다림). 끝에 "running - a COTS UE can attach now"가 나오면 전화기를 붙인다.

### 3.2 순서

**켜기**: 코어 → **RIC** → L1 + gNB → VA → non-VA → (전화기 접속 · 스트리밍) → PPO xApp → (모니터)
**끄기**: xApp → 모니터 → non-VA → VA → gNB + L1 → RIC → (코어)

- gNB가 도는 동안 RIC를 내리면 gNB가 죽는다(OAI E2 에이전트, `e2_agent.c:225`). `ric.sh stop`은 gNB가 돌면 거부한다.
- 코어를 재시작하면 옛 임시 ID(GUTI)를 가진 전화기가 "Authentication failure"로 못 붙을 수 있다 → 전화기 전원 재시작.
- gNB만 재시작하면 전화기는 스스로 다시 붙는다(코어에 세션을 가진 채 Service Request. 이 경로의 OAI NULL 참조 결함은 패치했다: `docs/patches/oai_pdusession_nas_null.patch`).
- 전화기 데모 중에는 L1/gNB를 재빌드하거나 재시작하지 않는다. L1 재빌드(`./dgist.sh build-l1`)는 전화기가 없는 시간에 한다.

### 3.3 모듈별 스위치

| 모듈 | 켜기 | 끄기 | 상태 / 로그 |
|---|---|---|---|
| 코어 (Open5GS, 호스트 서비스) | `./core.sh up` (sudo) | `./core.sh down` (sudo) | `./core.sh status`, `./logs.sh core` |
| nearRT-RIC (127.0.0.2) | `./ric.sh start` | `./ric.sh stop` (gNB가 돌면 거부) | `./ric.sh status`, `./logs.sh ric` |
| L1 + gNB (컨테이너 `dgist-l1`, `dgist-gnb`; Baseline은 `dgist-yolo`도) | `./dgist.sh up [이름]` | `./dgist.sh down` | `./dgist.sh status`, `./dgist.sh ue`, `./logs.sh gnb\|mac\|l1\|dapp` |
| 5셀 트래픽 생성기 (L1 컨테이너 안) | `./tgen.sh on` 뒤 L1 재시작 | `./tgen.sh off` 뒤 L1 재시작 | `./tgen.sh status`, `./logs.sh tgen\|ru\|testmac` |
| VA 서비스 (:8000) | `./va.sh start` | `./va.sh stop` | `./va.sh status`, `./logs.sh va` |
| non-VA 서비스 (:8001) | `./nonva.sh start` | `./nonva.sh stop` | `./nonva.sh status`, `./logs.sh nonva` |
| PPO 슬라이싱 xApp: `xapp_slice_ppo_ctrl` + Python 액터 (+ compute writer, guard; Ours만) | `./xapp.sh start` | `./xapp.sh stop` | `./xapp.sh status`, `./xapp.sh trace`, `./logs.sh ppo\|xapp` |
| 모니터 xApp `xapp_ul_input_moni` (읽기 전용) | `./xapp.sh moni start` (전경: `./xapp.sh moni`) | `./xapp.sh moni stop` | `./xapp.sh moni status`, `./logs.sh moni` |
| 가상 카메라 (GPU 부하만 추가, 무선 미사용) | `./vcam.sh start <N> [fps] [모델]` | `./vcam.sh stop` | `./vcam.sh status` |
| 실험 대시보드 (:8010, 스택과 따로 돈다) | `./dash.sh start` | `./dash.sh stop` | `./dash.sh status`, `./dash.sh logs` |
| **전체** | `./all.sh up [이름] [--xapp]` | `./all.sh down [--core]` | `./all.sh status` |

`./dgist.sh <모듈> …`로도 같은 스크립트를 부를 수 있다(`./dgist.sh va start`, `./dgist.sh ric status`). 켜기 전에 `./dgist.sh check`가 빠진 것을 `!!` 줄로 알려 준다.

### 3.4 전화기 4대

Wi-Fi를 끄고 5G로 붙인 뒤(인증서는 자체 서명이라 경고를 한 번 승인):

| 전화기 | 주소 | 누르는 순서 |
|---|---|---|
| VA 2대 | `https://10.45.0.1:8000/` | UE1 / UE2 선택 → Start Camera(또는 Video File) → Start VA |
| non-VA 2대 | `https://10.45.0.1:8001/` | UE1 / UE2 선택 → Start Camera(또는 Video File) → Start Streaming |

서비스를 재시작했으면 전화기 페이지를 새로 고친 뒤 다시 시작한다. 누가 누구인지는 `python3 ue_table.py`.

**동영상 파일로 할 때 (2026-11-14)**: 두 페이지 모두 `Video File` 아래에 단추가 있다.

- `Restart Video` — 누르는 즉시 영상을 처음부터 다시 재생한다(전체 화면에서는 왼쪽 위 단추). 송신은 끊기지 않는다.
- `Start video at [시:분]` → `Set` — 그 시각이 되면 영상이 처음부터 시작된다(예: 10:00 → 10:00:00). 지난 시각을 고르면 내일로 잡힌다. `Cancel`로 취소.
  **네 대를 같은 순간에 시작하려면 폰마다 Start VA / Start Streaming을 먼저 누른 뒤 같은 시각을 예약한다**(그러면 모두 기지국 서버 시계로 맞춘다;
  `Clock:` 줄에 "base station clock"이라고 나온다). 예약해 둔 동안 페이지를 앞에 둔다(화면이 꺼지거나 다른 앱으로 가면 늦게 시작할 수 있다).
- 두 기능은 폰 안의 동영상만 다룬다: 서버로 가는 요청도, 내려오는 트래픽도 없다. 헤드리스 Chromium으로만 확인했다(시작 오차 ±2 ms); 실제 폰에서는 아직.

### 3.5 자주 쓰는 흐름

```bash
# (A) 서비스만 다시 (.env 의 VA_* / NONVA_* 를 바꾼 뒤; 전화기 페이지 새로 고침)
./va.sh restart ; ./nonva.sh restart

# (B) xApp만
./xapp.sh stop                 # 쿼터를 지우고(원상복구) 종료
./xapp.sh start                # 전화기 4대가 붙어 있어야 한다. 기본은 6G 번들, 19입력 번들은 .env PPO_BUNDLE_NAME 을 바꾼 뒤
#   전화기가 다시 붙어 RNTI가 바뀌면 guard가 5초 안에 감지해 PPO xApp을 다시 띄운다. 모니터: ./xapp.sh moni stop; ./xapp.sh moni start
#   모니터 xApp은 ./xapp.sh moni start 로만 띄운다 (빌드 디렉터리에서 직접 실행하면 우리 RIC(127.0.0.2)에 붙지 못한다)

# (C) gNB가 죽었을 때 (docker ps 에 dgist-gnb 없음) — RIC는 그대로 둔 채
./dgist.sh down ; ./dgist.sh up <이름> ; ./xapp.sh restart        # RIC가 먼저 떠 있어야 한다 (./ric.sh status)

# (D) L1 + gNB만 다시 (DAPP_* / L1_CONFIG / 생성기 스위치를 바꾼 뒤)
./dgist.sh down ; ./dgist.sh up <이름>          # 서비스는 떠 있는 채로 두면 첫 프레임에서 새 L1에 다시 붙는다
```

### 3.6 설정을 바꿨을 때 무엇을 다시 켜나

| `.env`에서 바꾼 것 | 다시 켤 것 |
|---|---|
| `VA_*` (모델, 모델 선택 규칙, QoS 기한, 해상도, 뷰어, 녹화) | `./va.sh restart` + 전화기 페이지 새로 고침 |
| `NONVA_*` | `./nonva.sh restart` + 전화기 페이지 새로 고침 |
| `DAPP_*`, `L1_CONFIG`, `BASELINE_*`, `TRAFFIC_GEN`, (Baseline에서) `VA_MODEL`, `VA_MODEL_SELECT`, `VA_MODEL_WEIGHTS` | `./dgist.sh down && ./dgist.sh up <이름>` |
| `DEMO_MODE` | 2장 절차 (`./all.sh mode …`로 바꾼다) |
| `PPO_*`, `XAPP_GUARD*`, `VA_IMSIS`, `NONVA_IMSIS` | `./xapp.sh restart` |
| `DASH_*` | `./dash.sh restart` |

### 3.7 요청마다 모델 고르기 (규칙 기반 모델 선택)

기본은 모든 VA 요청이 `VA_MODEL` 하나로 실행된다. `.env`의 아래 두 줄을 바꾸면 VA 서비스가 요청마다 모델을 정해진 비율로 섞어 보낸다(두 모드 공통, L1은 받은 이름의 모델을 실행).

```bash
VA_MODEL_SELECT=1                   # 0 = 끔(기본), 1 = 켬
VA_MODEL_WEIGHTS=10,10,10,10,60     # 26n,26s,26m,26l,26x 순서: 요청 100개(= 값의 합)마다 정확히 10 · 10 · 10 · 10 · 60개, 어느 요청이 어느 모델인지는 무작위
```

```bash
./va.sh restart                     # 적용 + 전화기 페이지 새로 고침 (Baseline은 ./dgist.sh down && ./dgist.sh up <이름> 도)
./va.sh status                      # "model selection:" 줄 = 블록 구성과 지금까지 모델별 요청 수
./va.sh test2 100 15                # 전화기 없이 확인: 끝의 "models:" 줄 = UE별 모델 요청 수
```

- 값의 합이 블록 크기다. `1,1,1,1,6`이면 요청 10개마다 1 · 1 · 1 · 1 · 6개, `0,0,0,50,50`이면 26l과 26x만 반반이다.
- VA UE 두 대와 가상 UE의 요청이 도착 순서대로 같은 블록을 채운다. UE 한 대만 보거나 블록 경계와 어긋난 구간을 세면 비율은 대략만 맞는다.
- Ours는 L1이 다섯 모델을 모두 올려 두므로 바로 된다. Baseline은 MIG 엔진이 있는 모델만 올라간다(지금은 26x뿐): MIG를 켠 뒤
  `scripts/build_yolo_engines_mig.sh n s m l`(모델당 7~15분). 엔진이 없는 모델의 몫은 `VA_MODEL`로 실행되고 `./dgist.sh check`와 `./va.sh status`가 알려 준다.
- 값이 잘못되면(5개가 아님, 숫자가 아님, 모두 0) 서비스는 뜨되 끈 것처럼 동작하고 `./logs.sh va`에 `!! VA_MODEL_SELECT=1 ignored` 한 줄이 남는다.
- 전화기로 가는 응답은 늘지 않는다(필드 그대로, 전화기 페이지의 모델 표시는 `mix`). 섞인 모델은 뷰어 머리말과 대시보드 VA 서비스 탭에서 본다.
- PPO xApp의 입력과 대시보드의 대체 추정값은 계속 `VA_MODEL` 하나를 쓴다. 실제 L1에서 섞어 돌릴 때의 모델별 지연과 Baseline 다중 모델 서버는 아직 측정 전이다(2026-11-13).

## 4. 모니터링

### 4.1 상태 명령

| 명령 | 보여 주는 것 |
|---|---|
| `./all.sh status` | 한 화면 요약: 모드, 생성기, 코어, L1 / gNB / dApp 에이전트 포트 / 실행 이름, VA / non-VA, RIC / PPO xApp(마지막 결정), 모니터 · compute writer · guard, L1 · GPU · gNB 줄, UE 표 |
| `./dgist.sh status` | 컨테이너, RAN 프로세스, GPU 사용률 · 메모리, **L1 Err.ind / FATAL 수**, 전화기 셀의 초당 DL / UL · CRC, dApp 한 줄, gNB의 NG Setup과 UE 수(RA / RRC / PDU / RA 실패) |
| `./dgist.sh ue` | 전화기 접속 단계별 첫 시각과 횟수(RA → RRC → PDU 세션), 실패 줄 |
| `./tgen.sh status` | 생성기: 부하 단계, 5셀 DL / UL 합, RU 정시율, L1 Err.ind / FATAL, 전체 로그 크기, 최근 단계 전환 |
| `./va.sh status` / `./nonva.sh status` | 프로세스 · 포트, dApp 에이전트 연결과 카운터, (모델 선택 규칙이 켜져 있으면) 모델별 요청 수, UE 세션, 최근 CSV, 뷰어 요약 |
| `./ric.sh status` | RIC 프로세스와 E2 연결 |
| `./xapp.sh status` / `trace [n]` / `dapp` | xApp 상태 / 최근 n개 결정(dApp 값 포함) / dApp → xApp 피드 확인 |
| `./xapp.sh moni power [초]` | UE별 RSRP · PH · 송신 전력 · 감당 PRB 대 실제 grant · MCS / BLER / SNR 표 (CSV `log/moni_log/ue_power_*.csv`) |
| `python3 ue_table.py` | RNTI, IMSI, UE IP, in-sync, RSRP, MCS, BLER, SNR, 어느 서비스로 보내는지 |
| `./vcam.sh status`, `./dash.sh status`, `./core.sh status` | 가상 카메라, 대시보드(응답 중인 데이터 출처, 진행 중인 측정), 코어 |

정상일 때: `./all.sh up` 끝줄 `== all up`; `./dgist.sh status`의 **Err.ind 0, FATAL 0**, `gNB: NGSetup 1`, 전화기 4대면 `RA 4 RRC 4 PDU 4 RA-fail 0`.
Err.ind는 L1이 슬롯을 제때 처리하지 못했다는 신호다. 숫자가 계속 늘면 `./logs.sh l1`. (생성기를 켠 Ours 파일럿 9분에서 Err.ind 10이 한 번 나왔고 원인은 아직 모른다.)

### 4.2 실시간 로그 (`logs.sh`)

`./logs.sh <대상> [-n N] [--raw]` — 따라 보기(Ctrl-C로 나옴). 빨강 = 오류 / 실패, 초록 = 성공 / 접속, 노랑 = 경고 / HOLD.

| 대상 | 내용 |
|---|---|
| `all` | gNB 사건 + xApp 결정 + 전화기 셀의 L1 줄을 한 화면에 |
| `gnb` / `mac` | 전화기 접속 · 해제, RRC, NGAP, E2, 슬라이스 쿼터 / UE별 MAC 통계 줄 |
| `l1` | L1 콘솔: 준비 / START, 전화기 셀의 초당 DL / UL, Err.ind, FATAL / ERR / WRN (전화기 셀 = 셀 0, 생성기를 켜면 셀 5) |
| `tgen` / `ru` / `testmac` | 생성기: 5셀의 초당 줄 + 단계 전환 / RU 에뮬레이터 정시율 / testMAC |
| `dapp` | L1 안의 dApp: 에이전트 ok / fail / 대기열, SM 프로파일 줄 (Ours) |
| `va` / `nonva` | 서비스: 프레임, 전화기 진단, 에이전트 연결, 오류 |
| `ric` / `xapp` / `ppo` / `moni` | RIC / xApp 전체 / xApp 결정만(STORED · unchanged · HOLD) / 모니터 xApp의 초당 UE · 서비스 UL 수치 |
| `core` (= `amf`, `smf`, `upf`) | Open5GS: 등록, PDU 세션, 인증, 해제 |
| `ue` | UE 표를 2초마다 갱신 |

### 4.3 기지국 뷰어와 녹화

PC 브라우저(연구실 LAN)에서 `https://10.150.21.161:8000/viewer` — 한 페이지에 4칸: 위 줄 VA ue1 · ue2(프레임 + 박스), 아래 줄 non-VA ue1 · ue2(720p 중계).
`/viewer/va`는 VA 2대만, `/viewer/ue1`은 한 대만 크게. non-VA만 따로 보려면 8001 포트의 `/viewer`(`/viewer/ue1`, `/viewer/ue2`). non-VA 칸은 브라우저가 8001 포트에서 직접 받으므로 `https://10.150.21.161:8001/`의 인증서 경고도 한 번 승인한다.

- VA 칸은 **QoS를 만족한 결과만** 화면을 갱신한다: 상향(실측) + AI(실측) + 하향(고정 `VA_VIEWER_DL_MS`) ≤ `VA_DEADLINE_MS`. 미달이면 화면이 멈추고 "QoS 미달" 배지가 뜬다.
- 칸 아래 줄에 모델과 YOLO SM 수가 나온다("26x · 60 SM" = Baseline, "· 80 SM" = Ours).
- non-VA 칸도 같은 식으로 멈춘다(2026-11-14): **상향 지연(실측) ≤ `NONVA_DEADLINE_MS`(250 ms)** 인 프레임만 화면을 갱신하고, 넘으면 멈춘 채 "QoS 미달 · 화면 유지 N s"
  배지가 뜬다(non-VA에는 추론 · 하향이 없어 상향만 본다). 타일 "상향 지연 / 250 ms"와 "QoS 충족률"(최근 100 프레임). 끄려면 `.env`에 `NONVA_VIEWER_QOS_FREEZE=0`.
- 화면 맞춤(2026-11-14): 창 크기에 맞춰 4칸이 스크롤 없이 한 화면에 들어온다(그림 왼쪽, 숫자 오른쪽). 브라우저 창을 최대로 하거나 F11. 창이 작으면
  이전처럼 스크롤된다(1680 × 945 창에서는 맞춰지고 1366 × 680 창에서는 스크롤). 끄려면 `.env`에 `VA_VIEWER_FIT=0`.
- **뷰어는 PC에서만 연다.** 전화기에서 열면 그 전화기로 하향 트래픽이 생긴다.

**녹화**: 머리줄 오른쪽 **전체 녹화** → 보이는 칸 전부를 같은 순간에 녹화 시작, **녹화 종료** → 칸마다 FHD(1920×1080) MP4 한 개씩 내려받는다(Chrome이 처음 한 번 "여러 파일 다운로드"를 물으면 허용).
칸마다 **녹화 시작** ↔ **녹화 종료 mm:ss** 단추로 따로 찍을 수도 있다. 끝난 파일은 페이지 아래 "녹화 파일" 목록에서 다시 받을 수 있다(탭을 닫거나 새로 고치면 사라진다).

- 파일: H.264 30 fps, 6 Mbit/s(칸 하나 분당 약 45 MB), 소리 없음, 이름 `<모드>_<va|nonva>_<ue>_<날짜>_<시각>.mp4`. 전체 녹화로 만든 파일들은 시작 시각과 길이가 같다.
- 녹화는 전부 뷰어를 연 브라우저 안에서 한다. 서버와 전화기는 녹화를 모른다: 상향 · 하향 트래픽, 서버 CPU, GPU는 그대로다.
- 녹화분은 끝날 때까지 브라우저 메모리에 있다(4칸 × 20분 ≈ 3.6 GB). 20분이 지나면 스스로 끝내고 저장한다(`VA_VIEWER_REC_MAX_MIN`). 긴 시연은 나눠 찍는다.
- Chrome / Edge 최신판을 쓰고, 녹화 중에는 뷰어 탭을 앞에 둔다.
- 단추가 안 보이면 서비스가 옛 버전으로 떠 있는 것이다: `./va.sh restart ; ./nonva.sh restart` 뒤 페이지 새로 고침.
- 헤드리스 Chromium으로만 확인했다(4칸 3분: 5418 / 5418 프레임). 실제 뷰어 PC, 실제 전화기 4대 화면, PowerPoint에서 열기는 아직 확인하지 않았다.

더 보기: `va_service/README.md`(뷰어 판정 규칙, "뷰어 녹화" 절, `VA_VIEWER*` 설정).

### 4.4 전화기 없이 해 보는 시험 (무선 미사용)

```bash
./dgist.sh test 26x 10 20          # YOLO에 직접 부하: 모델, req/s, 초
./va.sh test2 60 10                # UE 대역 2개로 VA 경로 시험 (프레임 60장, 10 req/s; 끝의 models: 줄 = 모델별 요청 수)
./nonva.sh test ue1 100 20         # non-VA 경로 시험
./vcam.sh start 2 ; ./vcam.sh status ; ./vcam.sh stop      # 가상 카메라 2대 × 10 fps = GPU에 +20 req/s
```

## 5. 실험 대시보드

연구자용 측정 · 시각화 웹 페이지다. 스택과 따로 돌므로 한 번 켜 두면 된다(스택이 내려가 있으면 지표가 "대기"로 보인다).

```bash
./dash.sh start            # → PC 브라우저: http://10.150.21.161:8010/   (평문 HTTP, 연구실 LAN 전용)
./dash.sh status           # 프로세스, URL, 응답 중인 데이터 출처, 진행 중인 측정, 저장된 측정
./dash.sh stop             # 진행 중인 측정은 요약과 파일을 닫은 뒤 종료
./dash.sh restart | logs | list        # 재시작 | 서버 로그 | 저장된 측정 목록
./dash.sh sim [ours|baseline]          # 연습용 두 번째 인스턴스(:8011, 가짜 숫자, "SIMULATION" 배지). 끄기: ./dash.sh sim stop
```

- 스택에 대해 **읽기 전용**이다: 아무것도 켜거나 끄거나 바꾸지 않는다.
- **하향 트래픽을 만들지 않는다**: UE로 아무것도 보내지 않고, UE 망(`10.45.0.0/16`)에서 온 요청은 403으로 거절한다.
- Ours / Baseline 두 모드에서 같은 화면을 쓴다.

### 5.1 측정 절차

1. 실험 세팅을 끝낸다(전화기 4대 접속, VA 2대 Start VA, non-VA 2대 Start Streaming, 필요하면 xApp · 생성기 · 가상 카메라).
2. 오른쪽 위 **측정 시작** → 이름 입력(모드와 부하를 이름에 넣는다, 예 `ours_low_1`). 점검 목록에서 빠진 출처를 확인하고 시작.
3. 측정 중에는 모든 카드에 **지금 값**(최근 1초)과 **측정 시작 후 누적 평균**이 같이 나온다. 그래프는 `누적 평균 | 순간값 | 둘 다`, 구간 `전체 | 5분 | 1분`.
4. **측정 종료** → 결과 요약 창. 기록이 `runs/dash/<이름>_<시각>/`에 저장되고 "기록 · Replay" 탭 목록에 올라온다.

측정은 한 번에 하나다(브라우저 여러 대가 같은 상태를 본다). 디스크 여유가 2 GB 미만이면 시작을 거부하고, 1 GB 아래로 내려가면 스스로 종료한다.

### 5.2 탭

| 탭 | 내용 |
|---|---|
| 종합 | VA QoS 충족률, 셀 UL Throughput, GPU YOLO 요청, mAP 유지율, UE별 E2E Latency, GPU SM 슬롯 맵, PRB 슬라이싱, UE 현황 표 |
| VA 서비스 | UE별 화질 · FPS, QoS 충족률, Latency 추이(UL / Inference / E2E), 선택된 모델, 모델 사용 비율 |
| Non-VA 서비스 | UE별 화질 · FPS, Uplink Latency(평균 · p90 · p99), Throughput, PRB 사용 |
| GPU · 슬롯 | 슬롯별 GPU SM 배분(최근 80 슬롯), YOLO 요청 부하, SM 사용 추이, 모델 × SM 조합, 가상 카메라 |
| 무선 · 슬라이싱 | PRB 슬라이싱 막대, 서비스별 PRB 할당 · 사용 추이, UE별 PRB · Throughput, UE 무선 상태(SNR, MCS, BLER, RSRP, PH) |
| 기록 · Replay | 저장된 측정 목록(재생, 요약, 파일 받기), 다른 PC에서 받은 `.replay.jsonl` 열기 |

### 5.3 기록과 Replay

`runs/dash/<이름>_<시각>/`에 `.replay.jsonl`, `.summary.json`, `.summary.txt`, `.timeseries.csv`, `.frames.csv`, `.slots.csv.gz`가 남는다(UE 4대 기준 시간당 약 0.13 GB).
`.replay.jsonl` 하나만 있으면 다른 PC의 같은 페이지에서도 다시 재생된다. 목록은 `./dash.sh list`.

### 5.4 모드 · 생성기에 따른 차이

- **Baseline**에는 슬롯 CSV와 xApp이 없다. 그래서 GPU 슬롯 맵은 MIG 고정 분할(L1 / YOLO)로, GPU 부하율은 "요청률 × 추론 시간" 추정으로, UE별 PRB는 gNB 로그 표본(약 1 %)으로 나온다.
- **생성기**를 켜면 위쪽에 `tgen` 표시와 5셀 UL / DL 합 · 부하 단계가 나오고, 전화기 셀 수치는 L1 셀 5에서 읽는다.
- 두 모드를 비교할 때는 측정을 모드마다 따로 한다(이름에 모드를 넣는다).

더 보기: `dash/README.md`(지표 정의 · 출처 · 한계).

### 5.5 그 밖의 측정 도구

```bash
python3 scripts/quota_exp.py 0 120 --no-control --label <라벨>     # 2분 기록(UL / AI 백분위, QoS, 무선, 셀) → runs/quota_exp/
python3 scripts/quota_exp.py 100 90 --label va100                  # 고정 쿼터 실험 (PPO xApp을 멈춘 상태에서)
./dgist.sh slotmap <이름> [--from-sec S --sec D --slowdown 20 --fps 60 --lang ko]     # GPU 슬롯 맵 영상 runs/<이름>/slotmap.mp4 (Ours, .env DAPP_SLOT_CSV=1, 끝난 실행)
```

## 6. 실험 한 번의 흐름 (예)

```bash
# 0) 확인: 모드, 생성기 스위치, GPU, 디스크
./all.sh mode ; ./tgen.sh status ; nvidia-smi ; df -h / /data
# 1) 대시보드 (이미 떠 있으면 생략)
./dash.sh start
# 2) 스택
./all.sh up ours_low --xapp            # Baseline이면: ./all.sh up base_low
# 3) 전화기 4대: VA 2대 Start VA, non-VA 2대 Start Streaming  →  python3 ue_table.py 로 4대 확인
./xapp.sh start && ./xapp.sh moni start        # (Ours) 2)에서 xApp이 시작되지 않았으면
# 4) PC 브라우저: 대시보드 "측정 시작" (이름 ours_low_1), 뷰어 "전체 녹화"
# 5) (생성기를 켠 경우) 부하 전환
./tgen.sh load mid ; ./tgen.sh load high
# 6) 대시보드 "측정 종료", 뷰어 "녹화 종료"
# 7) 정리
./dgist.sh status                      # Err.ind / FATAL 확인
./all.sh down                          # 끝줄의 "L1 Err.ind N, FATAL N | UE: …" 가 이 실행의 요약
```

측정하는 동안에는 같은 호스트에서 빌드나 대량 파일 검색 같은 무거운 작업을 하지 않는다. L1 전용 코어에 다른 스레드가 올라가 Err.ind(0x34)가 쏟아진 일이 있다(2026-11-13 측정).

## 7. 로그와 결과물 위치

| 내용 | 위치 |
|---|---|
| L1 / gNB 실행 로그 (`l1_console.log`, `phy_warn.log`, `l1_persec.log`, `entrypoint.log`, 내린 뒤 `oai.log`) | `runs/<이름>/` (Baseline은 `aerial_log/phy.log`도) |
| Baseline YOLO 서버 | `runs/<이름>/yolo_entrypoint.log`, `yolo_console.log`, `yolo_tenant.log` |
| dApp 슬롯 CSV (Ours, `DAPP_SLOT_CSV=1`) · 슬롯 맵 영상 | `runs/<이름>/dapp_tenant_slots.csv`, `slotmap.mp4` |
| 생성기 (`ru_console.log`, `testmac_console.log`, `tgen_load.log`, `l1_persec_emu.log`, `ru_table.txt`) | `runs/<이름>/` |
| 생성기 전체 nvlog (시간당 26~29 GB) | `/data/dgist_tgen/log/<이름>/` |
| VA / non-VA 서버 로그 | `runs/va_service/uvicorn.log`, `runs/nonva_service/uvicorn.log` |
| VA / non-VA 세션 CSV (전화기가 Stop을 누를 때) | `runs/va_service_log/`, `runs/nonva_service_log/` |
| RIC | `runs/ric/` |
| PPO xApp 콘솔, guard, compute writer | `runs/xapp/` |
| PPO 결정 trace | `xapp_ppo/xapp_gpu_aware_no_lending_6G/logs/` (`./xapp.sh trace`) |
| C xApp 로그 / 모니터 로그 / UE 전력 CSV | `log/ppo_output_log/`, `log/moni_log/` |
| 대시보드 측정 기록 | `runs/dash/<이름>_<시각>/` (연습용은 `runs/dash_sim/`), 서버 로그 `runs/dash_service/` |
| 뷰어 녹화 MP4 | 뷰어를 연 PC의 다운로드 폴더 (서버에는 남지 않는다) |
| `quota_exp.py` 결과 · 가상 카메라 상태 | `runs/quota_exp/`, `runs/vcam/` |
| MIG 인스턴스 UUID (Baseline) | `runs/mig.env` |
| 빌드 로그 | `runs/build_*.log` |

## 8. 설정 (`.env`) 요약

| 묶음 | 키 | 뜻 |
|---|---|---|
| 모드 | `DEMO_MODE` | `ours` / `baseline` (`./all.sh mode`로 바꾼다) |
| 생성기 | `TRAFFIC_GEN`, `TRAFFIC_LOAD`, `TRAFFIC_GEN_TV_DIR`, `TRAFFIC_GEN_LOG_DIR` | 스위치(`./tgen.sh on\|off`), 시작할 때의 부하 단계, 시험 벡터와 전체 로그 위치(`/data`) |
| L1 / gNB | `AERIAL_IMAGE`, `OAI_IMAGE`, `BUILD_DIR`, `L1_CONFIG`, `OAI_CPUSET`, `BUILD_CPUS` | 이미지, 우리 빌드 디렉터리, L1 설정 이름, gNB 코어 |
| dApp (Ours) | `DAPP_INPUT`, `DAPP_TENANT_CLASSES`, `DAPP_TENANT_CLASSES_FHD`, `DAPP_SLOT_CSV`, `DAPP_AGENT_*`, `DAPP_*_PROFILES` | 입력 프로파일(`4k` / `fhd`), YOLO SM 클래스 사다리, 슬롯 CSV, 에이전트(포트 9810, QoS, 분할) |
| Baseline | `BASELINE_TREE`, `BASELINE_L1_CONFIG`, `BASELINE_YOLO_SM` | 연구실 바닐라 트리, L1 설정, YOLO SM(`auto` = 인스턴스 전체) |
| VA | `VA_MODEL`, `VA_MODEL_SELECT`, `VA_MODEL_WEIGHTS`, `VA_DEADLINE_MS`, `VA_TARGET_FPS`, `VA_CAMERA_WIDTH/HEIGHT`, `VA_JPEG_QUALITY`, `VA_PORT`, `VA_AGENT`, `VA_VIEWER*`, `VA_VIEWER_REC*` | 모델(고정 또는 규칙 선택, 3.7절), QoS 기한, 전화기 송신 속도 · 해상도 · 화질, dApp 에이전트 주소(두 모드 모두 포트 9810), 뷰어와 녹화 |
| non-VA | `NONVA_PORT`, `NONVA_WIDTH/HEIGHT`, `NONVA_FPS`, `NONVA_JPEG_QUALITY`, `NONVA_VIEWER_FPS`, `NONVA_DEADLINE_MS`, `NONVA_VIEWER_QOS_FREEZE` | 720p 20 fps 중계, QoS 기한(250 ms)과 뷰어 정지 |
| RIC / xApp | `RIC_IP`, `FLEXRIC_BUILD`, `FLEXRIC_SM_DIR`, `PPO_BUNDLE_NAME`, `PPO_GPU_SOURCE`, `PPO_DAPP`, `PPO_MIN_PRBS`, `XAPP_GUARD`, `XAPP_GUARD_S`, `VA_IMSIS`, `NONVA_IMSIS` | RIC 주소, PPO 번들, guard, 전화기 SIM → 서비스 대응 |
| 무선 / 코어 | `RU_IP`, `FH_IF`, `UE_SUBNET`, `NO_CORE` | Benetel RU, 프런트홀 NIC, UE 망 |
| 그 밖 (선택) | `DASH_*`, `VCAM_*` | 대시보드, 가상 카메라 |

바꾼 뒤 무엇을 다시 켜는지는 3.6절.

## 9. 문제 해결

| 증상 | 할 일 |
|---|---|
| `all.sh up` 끝에 `FAILED components: ric` | 타이밍 오경보인 경우가 있다(E2 SETUP 대기). `./ric.sh status`로 RIC가 떠 있는지 확인 |
| `!! a run is registered as current (…)` | 앞 실행이 정리되지 않았다: `./dgist.sh down` 뒤 다시 |
| `precheck failed` | `./dgist.sh check`의 `!!` 줄을 하나씩 해결(MIG 상태, 엔진, 다른 L1 컨테이너, PTP, 프런트홀 링크, 코어) |
| `!! stack is up` (`mode`) / `mig.sh`가 거부 | `./all.sh down` 먼저 / GPU에 남은 프로세스 확인 `nvidia-smi` |
| `Baseline engine missing` | MIG 켠 뒤 `scripts/build_yolo_engines_mig.sh <n\|s\|m\|l\|x>` |
| xApp "RNTI mapping not possible" | 전화기 4대가 붙은 뒤 `./xapp.sh start` (`./xapp.sh rntis`로 대응 확인) |
| 전화기가 "Authentication failure"로 못 붙음 | 코어를 재시작한 뒤라면 전화기 전원 재시작 |
| gNB가 죽음 (`docker ps`에 `dgist-gnb` 없음) | 3.5 (C). RIC가 먼저 떠 있어야 한다(`./ric.sh status`) |
| `all.sh down` 끝에 `still running: …` | 나열된 것을 해당 스크립트로 내린다. root로 띄운 프로세스는 nvidia 계정이 못 내린다(0장) |
| 뷰어에 non-VA 칸이 안 보임 | `https://10.150.21.161:8001/` 인증서를 한 번 승인. non-VA 서비스가 떠 있는지 `./nonva.sh status` |
| 뷰어에 녹화 단추가 없음 / "녹화 불가" | `./va.sh restart ; ./nonva.sh restart` 뒤 새로 고침, Chrome / Edge 사용 |
| 대시보드가 403 | UE 망에서 접속했다. 연구실 LAN의 PC에서 `http://10.150.21.161:8010/` |
| 대시보드 지표가 "대기" | 스택이나 그 서비스가 내려가 있다: `./dash.sh status`의 출처 목록, `./all.sh status` |
| 생성기 L1이 시작하다 죽음 | `./logs.sh testmac`, `./logs.sh ru`, `runs/<이름>/entrypoint.log`. 원인 추적용: `TG_L1_GDB=1 ./dgist.sh up <이름>`(측정에는 쓰지 않는다) |
| 모니터 xApp이 영원히 재시도 | 빌드 디렉터리에서 직접 띄운 것이다(스크립트가 세지 못한다). `pgrep -af xapp_ul_input_moni`로 찾아 끝내고 `./xapp.sh moni start` |

## 부록 A. 스크립트별 명령

| 스크립트 | 명령 |
|---|---|
| `all.sh` | `up [이름] [--xapp]` · `down [--core]` · `status` · `restart [이름]` · `mode [ours\|baseline]` · `tgen …` |
| `dgist.sh` (L1 + gNB) | `check` · `up [이름]` · `down` · `status` · `ue [이름]` · `logs [l1\|gnb]` · `test [모델] [req/s] [초]` · `slotmap <이름> […]` · `build-l1 [타겟]` · `build-oai` · `core\|va\|nonva\|ric\|xapp\|tgen\|all …`(그 스크립트로 넘김) |
| `tgen.sh` | `on` · `off` · `load low\|mid\|high\|auto` · `status` |
| `scripts/mig.sh` (root) | `on` · `off` · `status` |
| `core.sh` | `up` · `down` · `status` (sudo) · `config` · `diff` · `build` · `subs` · `logs` (자세한 것은 부록 B "코어") |
| `ric.sh` | `start` · `stop [--force]` · `restart` · `status` · `logs` · `install-sm` · `build` |
| `va.sh` | `start [local]` · `stop` · `restart` · `status` · `logs` · `test [ue] [n] [req/s]` · `test2 [n] [req/s]` · `bg start [--fps N]` · `bg stop` · `bg status` · `make-frame` |
| `nonva.sh` | `start` · `stop` · `restart` · `status` · `logs` · `test [ue] [n] [fps]` · `make-frame` |
| `xapp.sh` | `rntis` · `start [VA1,VA2 NV1,NV2]` · `stop` · `restart` · `status` · `logs` · `trace [n]` · `dapp` · `compute [restart]` · `guard`(내부용) · `moni [start\|stop\|status\|logs\|power [초]] [VA NV]` |
| `vcam.sh` | `start <N> [fps] [모델]` · `stop` · `restart` · `status` · `logs` · `make-frames <이미지 폴더\|영상> [개수]` · `dry-run` |
| `dash.sh` | `start` · `stop` · `restart` · `status` · `logs` · `list` · `sim [ours\|baseline]` · `sim stop` · `sim logs` |
| `logs.sh` | `<대상> [-n N] [--raw]` — `gnb mac l1 tgen ru testmac dapp va nonva ric xapp ppo moni core\|amf\|smf\|upf ue all` |
| `ue_table.py` | `python3 ue_table.py` (`--rntis` = xApp용 대응 한 줄) |

모듈별 문서: `README.md`(무엇이 어디서 도는가, 디렉터리), `SETUP.md`(새 서버 설치), `docs/MODE_SWITCH.md`, `dash/README.md`, `va_service/README.md`, `nonva_service/README.md`, `xapp_ppo/README.md`.

## 부록 B. 배경 메모

**Baseline의 구성 상세** — L1은 연구실 바닐라 cuBB 25-3 바이너리(`BASELINE_TREE=/home/nvidia/hsw/25_3_hsw/...`, 읽기 전용 마운트, MPS)다. 설정은 연구실 config 디렉터리의 사본 `config/baseline_cuphy_config/`를 덮어 마운트하고, 그중 `cuphycontroller_baseline.yaml`은 연구실 `config_benetal` yaml에서 `mps_sm_pusch`만 120 → 64로 바꾼 것이다(4g 인스턴스가 64 SM). 이 빌드는 PRACH 임계값 20이 하드코딩되어 있다(순정 `25_3`은 가짜 RA가 분당 100건 생긴다). YOLO는 우리 테넌트 바이너리를 naive 모드로 띄운 것이다: 인스턴스 전체(60 SM, 자동 감지) 고정, FAPI 없음, QoS 거절 없음, 분할 없음, 모델 1개(batch-3 FHD 엔진, 디코드 · NMS는 Ours와 같음), 도착 순서대로 실행. VA 서비스는 Ours와 같은 포트(9810)로 붙는다. `./dgist.sh up`은 모드에 맞게 `docker-compose.yaml` 또는 `docker-compose.baseline.yaml`을 쓰고, 생성기를 켜면 `docker-compose.tgen.yaml` / `docker-compose.tgen.baseline.yaml`을 덧붙인다. 파일: `docker-compose.baseline.yaml`, `scripts/l1_entrypoint_baseline.sh`, `scripts/yolo_entrypoint_baseline.sh`, `scripts/mig.sh`, `scripts/build_yolo_engines_mig.sh`.

**생성기와 Baseline** — Baseline + 생성기의 L1 설정은 `config/tgen/cuphycontroller_dgist_tgbase.yaml`(`use_green_contexts 0`, `mps_sm_pusch 60`, `mps_sm_pdsch 62`, Benetel NIC `mtu 8192`)이고 Ours + 생성기는 `config/tgen/cuphycontroller_dgist_tg.yaml`이다. `mtu 8192`는 6셀 L1을 MIG 4g의 메모리(46.5 GiB)에 넣기 위한 것이다(이유와 측정값은 `docs/MODE_SWITCH.md` 7장). 시험 벡터는 `/data/dgist_tgen/testVectors`(`TRAFFIC_GEN_TV_DIR`)에 있어야 한다.

**입력 프로파일 `DAPP_INPUT=4k|fhd` (2026-11-09)** — `4k` = 3840×2160, batch-8 타일(1280 크롭 6 + 전체 2, 엔진 `models/full26*`), `fhd` = 1920×1080, batch-3(1280×1080 크롭 2 + 전체 1, 엔진 `models/fhd26*`, `scripts/build_yolo_engines.sh fhd`). `VA_CAMERA_WIDTH/HEIGHT`를 같이 맞춘다(`check`가 불일치를 알려 준다; 다른 크기의 프레임도 전체 보기만으로 처리는 된다). `fhd`의 SM 클래스는 `DAPP_TENANT_CLASSES_FHD`다(기본 24,40,48,56,64,72,80; 2026-11-11부터 88 추가 가능). 88은 (P132,N24) 틈으로, UL 작업이 4슬롯 안에 없는 D 4~7 슬롯에만 생긴다. 클래스는 추론 전체 구간의 최소 틈으로 고르므로, UE가 붙어 UL 제어가 매 프레임 있으면 사실상 80까지 쓰이고 UL이 없는 프레임에서만 88이 선택된다(검증 기록 `prof/tenant/fhd/class88_2026-11-11.md`). 바꾼 뒤 `./dgist.sh down; ./dgist.sh up`.

**엔진 로드 규칙 (2026-11-10)** — 테넌트는 모든 엔진을 primary context에서 역직렬화한다(`DAPP_TENANT_ENGINE_CTX=prim` 기본, 단독 측정 비용 26x +2.7 %). 클래스별 green context에서 역직렬화하면 TensorRT 10.3(Myelin)이 그 시점 context의 SM 수로 GEMM 그리드를 정해 프로세스 전체에 캐시하므로 먼저 로드된 클래스의 그리드가 다른 클래스에 적용된다. fhd26s / fhd26l의 sm80 엔진은 고유 sm80 tactic으로 빌드하면 같은 모델의 다른 클래스와 한 프로세스에서 "Cask Gemm execution"으로 실패하므로 `scripts/build_yolo_engines.sh`가 sm72 timing cache(`models/fhd26<m>/sm72.timing.cache`)로 빌드한다(26s 3.7 / 26l 8.1 ms, 고유 tactic과 같음).

**슬롯 맵 영상 (`./dgist.sh slotmap`, 2026-11-11)** — `runs/<이름>/dapp_tenant_slots.csv`를 GPU Slot Map 영상으로 렌더링한다. CSV는 슬롯(0.5 ms)마다 한 줄: `t_ns`(벽시계), `gap_P` / `gap_N`(그 슬롯에 YOLO에 제공된 틈, 여유 SM = P−N−8, 5G 예약 = 132−여유), `run_cls`(추론 중 점유 SM), `queue`, `model`. Ours 레인은 이 기록 그대로이고, Baseline 레인은 같은 5G 예약 · 같은 요청에 MIG 벽을 씌운 **계산값**이다(추론 시간 ×1.3, 벽의 기본값 72 / 60; `--wall`, `--ai-wall`, `--baseline-factor`로 바꿀 수 있다). 10 실초 = 1/20 배속 200초 영상. `.env DAPP_SLOT_CSV=0`이면 CSV를 쓰지 않는다(분당 약 5 MB).

**코어** — `core.sh`는 `up | down | status`(sudo) 말고도 `config`(설정 복사본 `core/etc/` 목록, 라이브와의 차이), `diff [nf]`, `install-config <nf>`(복사본을 `/etc/open5gs`에 설치 + 유닛 재시작, 백업 남김), `build`(소스 `open5gs/` meson 빌드; dev 패키지 필요), `subs`(가입자 목록), `logs`가 있다. `./core.sh`는 첫 인자만 넘기므로 인자가 붙는 `diff <nf>`, `install-config <nf>`는 `bash scripts/core.sh diff amf` 꼴로 부른다. 가입자 추가는 WebUI `http://<host>:9999`.

**RIC** — 우리 빌드 `build_dgist`(E2AP_V2, KPM_V2_03), RIC IP 127.0.0.2(`config/flexric.conf`; 연구실의 옛 모니터 프로세스들이 127.0.0.1을 폴링하므로 분리). SM 라이브러리는 `flexric_sm/`에 두고 gNB 컨테이너에 마운트한다(`./ric.sh install-sm`; 빌드 뒤 한 번).

**PPO xApp** — 번들은 `.env PPO_BUNDLE_NAME`(기본 6G 20입력: YOLO 모델과 SM 수를 우리 dApp에서 받는다). RNTI는 `.env VA_IMSIS` / `NONVA_IMSIS`로 자동 대응한다. `moni power`는 `ue_power.py`(UE별 RSRP · PCMAX · PH · 송신 전력 · 감당 PRB 대 실제 grant · MCS / BLER / SNR 표)다. 그 밖의 설정: `PPO_GPU_SOURCE`(19입력 번들용), `PPO_DAPP`, `PPO_MIN_PRBS`. `ue_table.py`는 AMF / SMF 저널을 읽으므로 nvidia 계정이 `systemd-journal` 그룹에 있어야 한다(2026-11-05 적용).

**가상 카메라** — N개의 FHD JPEG 스트림을 전화기 프레임과 같은 RUN 경로로 dApp 에이전트에 직접 보낸다(개방 루프: fps 유지, inflight 4, 넘치면 건너뜀). 상태 `runs/vcam/status.json`, 프레임 `va_service/vcam_frames/`, 설정 `.env VCAM_FPS`(10), `VCAM_INFLIGHT`, `VCAM_FRAMES`, `VCAM_JPEG_QUALITY`(0.70).
