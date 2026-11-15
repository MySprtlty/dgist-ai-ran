# DGIST AI-RAN 운용 매뉴얼 (2026-11-15 개정, 브랜치 `demo`)

5G 기지국의 신호 처리(L1)와 영상 분석 AI(YOLO)를 **GPU 한 장**에서 함께 돌리는 DGIST AI-RAN 데모 스택의 운용 설명서다.
실제 무선 장치(Benetel RU) 1셀에 전화기 4대를 붙여 쓰고, 같은 스택을 두 방식(**Ours** / **Baseline**)으로 띄워 비교한다.
새 서버에 설치하는 방법은 `SETUP.md`에 있다.

| 절 | 내용 |
|---|---|
| 0 | 기본 규칙 |
| 1 | Baseline과 Ours 환경 소개 |
| 2 | 모드 전환 (Ours ↔ Baseline) |
| 3 | 대시보드와 기지국 뷰어 |
| 4 | 모듈별 소개와 On / Off / Restart 명령 |
| 5 | 설정 파일 `.env` |
| 6 | 로그 보는 명령 |
| 부록 | A 문제 해결 · B 관련 문서 |

**용어**

| 용어 | 뜻 |
|---|---|
| SM | GPU 안의 연산 묶음(Streaming Multiprocessor). 이 서버의 GPU는 132개다. "누가 몇 SM을 쓰는가"가 이 데모의 주제다 |
| MIG | GPU를 고정 크기 조각으로 나누는 NVIDIA 기능. 여기서는 4g.48gb(64 SM)와 3g.48gb(60 SM) 두 조각을 만든다 |
| L1 | 5G 물리 계층. NVIDIA Aerial의 `cuphycontroller`가 GPU로 상향(PUSCH) · 하향(PDSCH) 신호를 처리한다. 한 슬롯은 0.5 ms |
| gNB | 기지국의 상위 계층(OAI). 누구에게 무선 자원(PRB)을 줄지 정하고, 그 내용을 슬롯마다 FAPI 메시지로 L1에 알린다 |
| dApp | L1 프로세스 안에 넣은 우리 코드. SM 배분, YOLO 실행, 요청 접수(agent, TCP 9810)를 맡는다 |
| RIC / xApp | 기지국을 밖에서 제어하는 틀(FlexRIC nearRT-RIC)과 그 위에서 도는 제어 프로그램 |
| VA / non-VA | VA = 전화기가 FHD 영상을 올리면 YOLO 결과를 돌려주는 영상 분석 서비스(기한 200 ms). non-VA = 추론 없이 720p 영상만 올리는 서비스(기한 250 ms) |
| RNTI | 기지국이 접속한 전화기에 붙이는 임시 번호. 다시 접속하면 바뀐다 |

## 0. 기본 규칙

- 모든 명령은 **패키지 디렉터리**(이 저장소를 클론한 곳, 아래에서 `<패키지>`)에서 **그 디렉터리를 가진 계정**으로 실행한다. root나 sudo로 스크립트를 실행하면 거부한다.
  다른 계정에서는 `sudo -n -u <계정> -H bash -c 'cd <패키지> && ./all.sh status'`처럼 쓴다.
- 패키지 밖의 경로는 쓰지 않는다. `.env`의 경로 값은 패키지 기준 상대 경로이고(`scripts/paths.sh`가 절대 경로로 바꾼다), 주소 예시의 `<서버 IP>`는 이 서버의 유선망 주소다.
- sudo가 필요한 것은 둘뿐이다: 5G 코어(`core.sh`가 안에서 sudo를 부른다)와 MIG 전환(`scripts/mig.sh`, sudo가 있는 계정으로 실행).
- 설정은 `.env` 한 파일에 있다(5절).
- **RIC가 gNB보다 먼저** 떠 있어야 한다. gNB가 도는 동안 RIC를 끄거나 재시작하지 않는다(gNB가 같이 죽는다).
- GPU는 여러 사람이 같이 쓴다. 켜기 전에 `nvidia-smi`로 다른 작업이 없는지 본다. 연구실 스택(`nv-cubb`, `oai-gnb*` 컨테이너)과는 동시에 돌 수 없다(`./dgist.sh check`가 막는다).
- 실험 이름은 매번 새로 짓는다. 로그가 `runs/<이름>/`에 쌓이므로 같은 이름을 다시 쓰면 이전 실험의 로그와 섞인다.

## 1. 환경 소개: Baseline과 Ours

### 1.1 무엇이 어디서 도는가 (두 모드 공통)

```
전화기 4대 (VA 2 + non-VA 2) ──5G──> Benetel RU ──프런트홀──> L1 [GPU] <──FAPI──> gNB <──> 5G 코어 <──> VA / non-VA 서비스
VA 서비스 ──YOLO 요청 (TCP 9810)──> Ours: L1 안의 dApp   |   Baseline: YOLO 서버 (dgist-yolo)
gNB <──E2──> nearRT-RIC <──> PPO 슬라이싱 xApp (Ours)
```

| 구성 요소 | 실체 | 주소 · 비고 |
|---|---|---|
| 무선 장치 (RU) | Benetel RU, 실제 전파 1셀 | 프런트홀 NIC `aerial00`, RU 10.10.0.100, PTP 동기 |
| L1 | 컨테이너 `dgist-l1` (`cuphycontroller`) | GPU 사용. Ours에서는 YOLO도 이 프로세스 안에서 돈다 |
| YOLO 서버 (Baseline만) | 컨테이너 `dgist-yolo` | MIG 3g 조각에서 돈다 |
| gNB | 컨테이너 `dgist-gnb` (OAI, 연구실 수정판: 서비스별 PRB 할당 + E2 agent) | 설정 `config/gnb.conf` |
| 5G 코어 | Open5GS, 호스트의 systemd 서비스 | 전화기 망 10.45.0.0/16 (서버 쪽 주소 10.45.0.1), 가입자 관리 WebUI :9999 |
| nearRT-RIC | FlexRIC, 호스트 프로세스 | 127.0.0.2 |
| PPO 슬라이싱 xApp (Ours만) | 호스트 프로세스 | VA / non-VA 서비스의 상향 PRB 몫을 조절한다 |
| VA 서비스 | 호스트 프로세스 | 전화기 `https://10.45.0.1:8000/`, 기지국 뷰어(PC) `https://<서버 IP>:8000/viewer` |
| non-VA 서비스 | 호스트 프로세스 | 전화기 `https://10.45.0.1:8001/` |
| 5셀 트래픽 생성기 (선택) | L1 컨테이너 안의 RU 에뮬레이터 + testMAC | 가상 셀 5개로 L1에 부하를 준다(1.3절) |
| 대시보드 | 호스트 프로세스 | `http://<서버 IP>:8010/`, `https://<서버 IP>:8012/` (3절) |

### 1.2 Ours와 Baseline의 차이

| | **Ours** (`DEMO_MODE=ours`) | **Baseline** (`DEMO_MODE=baseline`) |
|---|---|---|
| GPU | 132 SM 전체를 한 덩어리로 쓴다. MIG 꺼짐 | MIG로 고정 분할: 4g(64 SM) = L1, 3g(60 SM) = YOLO. 나머지 8 SM은 쓰지 않는다 |
| L1 | 우리 빌드(`build.aarch64_f1002`). 슬롯마다 L1에 필요한 SM 수를 계산해 그만큼만 쓴다 | **같은 우리 빌드**에서 dApp 기능을 모두 끈 것: MPS와 고정 `mps_sm_*` 분할(연구실 Benetel 기준 설정값), green context · 훅 · 테넌트 없음(`config/cuphycontroller_dgist_base.yaml`). SM 수 고정 |
| YOLO | L1 프로세스 안에서, L1이 쓰지 않는 SM으로 돈다. 추론 한 번마다 SM 등급 하나(24~88)를 고른다 | 별도 컨테이너의 단순 서버. 항상 60 SM, 도착 순서대로 하나씩 |
| 요청 처리 | 기한을 지킬 수 없는 요청은 거절하고, 추론을 조각으로 나눠 L1 차례가 오면 조각 사이에서 멈춘다 | 거절 · 분할 없음 |
| 모델 | 26n ~ 26x 다섯 개가 모두 올라가 있다 | MIG용 엔진을 빌드해 둔 모델만. 없는 모델의 요청은 `VA_MODEL`로 돈다 |
| PUSCH 결과 순서 보장 | 켜짐 | 꺼짐 |
| xApp | PPO 슬라이싱 xApp (`./all.sh up … --xapp`) | 없음 (RIC는 gNB 때문에 켜 둔다) |
| gNB, 코어, VA / non-VA 서비스, 뷰어, 대시보드 | 같음 | 같음 |

Ours가 GPU를 나누는 방법을 차례로 적으면:

1. **슬롯마다 L1 몫 정하기 (sizer)**: gNB가 보내는 FAPI 메시지에는 다음 슬롯의 작업량이 들어 있다. 이것을 보고 PUSCH · PDSCH 처리에 줄 SM 수를
   미리 정해 둔 후보(`DAPP_PUSCH_PROFILES`, `DAPP_PDSCH_PROFILES`) 가운데서 고른다.
2. **남는 SM으로 YOLO 돌리기 (tenant)**: 추론을 시작할 때 그 시점에 남는 SM에 맞는 등급을 고른다. 등급 88은 엔진은 있지만 실제 상향 트래픽이 있는
   실행에서는 선택된 적이 없어 실질 최대는 80이다.
3. **요청 접수 (agent, TCP 9810)**: VA 서비스와 가상 카메라가 프레임을 보내는 창구다. 기한(`VA_DEADLINE_MS`) 검사와 추론 분할이 여기서 이뤄진다.
4. **PUSCH 결과 순서 보장 (in-order gate, 2026-11-14)**: PUSCH 결과가 상향 슬롯 순서대로 gNB에 가도록 L1이 잠깐(최대 3000 µs) 잡아 둔다. YOLO와 같이 돌 때
   이웃한 두 슬롯의 결과가 뒤바뀌면 gNB가 재전송 정보를 버려 재전송이 연쇄로 늘어나던 문제를 막는다.
5. **sizer의 YOLO 항 (2026-11-14)**: YOLO 요청이 들어와 있는 동안에는 지정한 슬롯의 PUSCH 계획에 여유 시간을 더 잡는다(`DAPP_SIZER_YOLO_US`, 현재 프레임 안
   7 · 8 · 17 · 18번 슬롯에 120 µs). YOLO가 옆 SM에서 돌면 PUSCH가 조금 늦게 끝나는 것을 계획에 반영한 것이다.
6. **요청마다 모델 고르기 (VA 서비스의 규칙)**: `VA_MODEL_SELECT=1`이면 VA 서비스가 `VA_MODEL_WEIGHTS` 비율로 모델을 섞는다
   (현재 26n : 26s : 26m : 26l : 26x = 10 : 10 : 60 : 10 : 10). Baseline에서도 같은 규칙이 돌지만 엔진이 있는 모델만 실제로 쓰인다.

> **슬롯 단위 SM 전환**(추론 하나의 조각마다 SM 등급을 바꾸는 방식)은 이 브랜치(`demo`)에 없다. 그 코드와 설정은 브랜치 `demo-slot-level`에 있다(`demo` + 슬롯 단위 전환).
> 이 매뉴얼의 절차는 모두 "추론 한 번에 등급 하나" 기준이다.

### 1.3 5셀 트래픽 생성기 (두 모드 공통, 선택)

RU 에뮬레이터와 testMAC(가짜 무선 장치와 가짜 상위 계층)이 **같은 L1 안에서** 가상 셀 5개(L1 셀 0~4)를 돌려 GPU에 5G 부하를 만든다.
전화기 셀은 그대로 Benetel RU이고 L1 셀 번호만 0 → 5로 바뀐다(모두 6셀). 부하 단계에 따라 바뀌는 것은 5셀의 **상향(PUSCH)** 뿐이다.
가상 셀의 테스트 벡터(187개 파일, 0.85 GB)는 git에 없고 `scripts/fetch_testvectors.sh`가 `tgen/testVectors/`에 설치한다(`SETUP.md`).

| 단계 | 가상 셀 · 상향 슬롯당 PUSCH UE 수 | 5셀 상향 합 (측정) |
|---|---|---|
| `low` | 1 | 245 Mbps |
| `mid` | 2 | 494 Mbps |
| `high` | 4 | 979 Mbps |
| `auto` | 3초마다 1 → 2 → 4 → 3 | 3 UE일 때 741 Mbps |

5셀 하향 합은 단계와 무관하게 약 6.6~6.7 Gbps다(측정). 모드와 생성기 스위치로 네 가지 구성이 나온다:

| 구성 | L1 |
|---|---|
| Ours, 전화기 셀만 | 우리 빌드, 1셀 |
| Ours + 생성기 | 우리 빌드, 6셀. YOLO가 받는 SM 수가 부하에 따라 달라진다 |
| Baseline, 전화기 셀만 | 우리 빌드에서 dApp 기능을 모두 끈 것, 1셀, MIG 4g (`config/cuphycontroller_dgist_base.yaml`) |
| Baseline + 생성기 | 우리 빌드에서 dApp 기능을 모두 끈 것, 6셀, MIG 4g (`config/tgen/cuphycontroller_dgist_tgbase.yaml`) |

> Baseline의 L1은 네 구성 모두 Ours와 **같은 바이너리**다(2026-11-15부터. 그 전의 "Baseline, 전화기 셀만"은 연구실의 순정 cuBB 25-3 빌드였다).
> "Baseline, 전화기 셀만"을 이 바이너리로 실행한 측정은 아직 없다(설정 파일과 시작 스크립트의 정적 검사만 했다). 처음 켤 때 `runs/<이름>/entrypoint.log`와 `./dgist.sh status`를 확인한다.

### 1.4 이전 판(2026-11-13)과 달라진 것

- L1 (Ours): PUSCH 결과 순서 보장(in-order gate)과 sizer의 YOLO 항이 들어갔다(1.2절 4 · 5번, 설정은 5절).
- VA 서비스: 규칙 기반 모델 선택이 켜진 상태가 기본이다(10 : 10 : 60 : 10 : 10).
- 대시보드: 읽기 전용 6개 탭 → 9개 탭. HTTPS 주소(:8012), "뷰어 화면" · "컴포넌트 제어" · "데모 영상" 탭, Capture(MP4 내려받기), Non-VA QoS(250 ms)가 추가됐다(3절).
- 값 정정: non-VA 전송은 15 fps, MIG 4g 조각은 64 SM.
- 자급자족(2026-11-15): 연구실 트리(`BASELINE_TREE`)와 호스트 고정 경로(`/home/…`, `/data/…`)를 쓰지 않는다. Baseline L1은 모든 구성에서 우리 빌드이고,
  생성기의 테스트 벡터와 전체 로그는 패키지의 `tgen/` 아래에 둔다.

## 2. 모드 전환

지금 상태는 `./all.sh mode`가 한 줄로 알려 준다(예: `mode: ours | MIG: Disabled 0 instance(s)`).
전환은 항상 **스택을 내린 뒤**에 한다. MIG 명령만 sudo로 실행한다(스택을 돌리는 계정에 비밀번호 없는 sudo가 없으면 `./all.sh mode`는 실행할 MIG 명령을 알려 주기만 한다. 그 명령을 sudo가 있는 계정에서 실행한다).

**Ours → Baseline**

```bash
./all.sh down                                      # xApp, 서비스, gNB, L1, RIC를 내린다 (코어는 그대로)
./all.sh mode baseline                             # .env DEMO_MODE=baseline
sudo <패키지>/scripts/mig.sh on                     # [sudo가 있는 계정] MIG를 켜고 4g + 3g 조각을 만든다
./all.sh mode                                      # "mode: baseline | MIG: Enabled 2 instance(s)" 확인
./all.sh up <이름>                                  # dApp을 끈 L1(4g) + 단순 YOLO 서버(3g) + gNB + VA + non-VA
```

**Baseline → Ours**

```bash
./all.sh down
./all.sh mode ours                                 # .env DEMO_MODE=ours
sudo <패키지>/scripts/mig.sh off                    # [sudo가 있는 계정] 조각을 지우고 MIG를 끈다
./all.sh mode                                      # "mode: ours | MIG: Disabled 0 instance(s)" 확인
./all.sh up <이름> --xapp                           # 우리 L1 + gNB + VA + non-VA (+ PPO xApp)
```

`--xapp`은 그 시점에 전화기 4대가 붙어 있어야 xApp을 띄운다. "RNTI mapping not possible"이 나오면 전화기가 붙은 뒤 `./xapp.sh start`를 따로 실행한다.

**5셀 트래픽 생성기 켜고 끄기** (어느 모드에서나)

```bash
./tgen.sh status                       # 스위치, 실행 여부, 부하 단계, 5셀 DL/UL 합
./tgen.sh on | off                     # .env TRAFFIC_GEN=1|0. 다음 ./all.sh up 부터 적용 (L1 · gNB 재시작, 전화기 재접속)
./tgen.sh load low|mid|high|auto       # 실행 중에 바로 바뀐다 (약 0.3초). 시작할 때의 단계는 .env TRAFFIC_LOAD
```

**전환이 됐는지 확인**

- `./all.sh status`의 첫 줄 `mode`와 둘째 줄 `traffic gen`.
- 뷰어 화면 아래 줄의 SM 수: Baseline은 항상 60 SM, Ours는 요청마다 다르다(최대 80).
- 대시보드는 `.env DEMO_MODE`를 2초마다 읽어 화면이 따라 바뀐다.

**주의**

- 스택이 떠 있으면 `./all.sh mode <모드>`가 거부한다(`!! stack is up - ./all.sh down first`).
- GPU에 프로세스가 하나라도 있으면(다른 사람 것 포함) `mig.sh on`도 `off`도 거부한다. 먼저 `nvidia-smi`를 본다.
- `mig.sh`는 GPU 전체를 바꾼다: 호스트의 MPS 데몬을 모두 끄고, 바로 안 바뀌면 GPU를 리셋한다. 다른 사람이 GPU를 쓰는 시간에는 실행하지 않는다.
- Baseline의 YOLO 엔진은 MIG 3g 조각 안에서 모델마다 한 번 빌드해 둔다. MIG를 켠 뒤
  `scripts/build_yolo_engines_mig.sh <n|s|m|l|x>`(26x 기준 7~15분). 없는 엔진은 `./all.sh mode`가 알려 준다.
- 생성기를 켠 실행은 전체 로그가 `tgen/log/<이름>/`(`.env TRAFFIC_GEN_LOG_DIR`)에 시간당 26~29 GB 쌓인다(측정). 시작 전에 `df -h tgen/log`를 보고, 끝난 실행은 정리한다. `tgen`은 큰 디스크를 가리키는 링크여도 된다.
- 4g 조각의 SM 수는 64다(실측).

## 3. 대시보드와 기지국 뷰어

### 3.1 대시보드란

연구자용 측정 · 시각화 웹 페이지다. PC 브라우저로 들어가 **측정 시작**(이름 입력) → **측정 종료**를 누르면 그 사이의 모든 지표가 저장되고, 같은 페이지에서 다시 재생(Replay)된다.

| 주소 | 용도 |
|---|---|
| `http://<서버 IP>:8010/` | 평소에 보는 주소 (연구실 유선망 전용) |
| `https://<서버 IP>:8012/` | 같은 페이지의 HTTPS. **Capture**(MP4 내려받기)는 이 주소에서만 된다. 인증서 경고를 한 번 넘긴다 |

```bash
./dash.sh start | stop | restart       # 켜기 | 끄기(진행 중인 측정은 파일을 닫은 뒤 종료) | 재시작
./dash.sh status                       # 프로세스, 주소, 응답 중인 데이터 출처, 진행 중인 측정
./dash.sh logs | list                  # 서버 로그 | 저장된 측정 목록 (runs/dash/)
./dash.sh sim [ours|baseline]          # 연습용 두 번째 페이지(:8011 / :8013, 가짜 숫자, "SIMULATION" 배지). 끄기: ./dash.sh sim stop
```

- 대시보드는 스택과 따로 돈다. `all.sh up / down`과 무관하게 켜 둬도 되고, 스택이 내려가 있으면 지표가 "대기"로 보인다.
- **하향 트래픽을 만들지 않는다.** 전화기 망(10.45.0.0/16)에서 온 요청은 403으로 거절하고, 서비스의 데이터는 서버 안(루프백)에서만 읽는다.
- Ours / Baseline 두 모드에서 그대로 쓴다. Baseline에는 슬롯 CSV와 xApp 로그가 없어서 SM 배분은 고정 값(L1 64 / YOLO 60)으로 그리고, GPU 부하율 같은 일부 값은 추정값으로 표시한다.

### 3.2 측정 절차

1. 실험 세팅을 끝낸다(전화기 4대 접속, VA 2대 Start VA, non-VA 2대 Start Streaming, 필요하면 xApp · 가상 카메라).
2. 오른쪽 위 **측정 시작** → 이름 입력(예: `ours_low_1`). 점검 목록에서 빠진 데이터 출처를 확인하고 시작한다.
3. 측정 중에는 모든 카드에 **지금 값**(최근 1초)과 **측정 시작 후 누적 평균**이 같이 나온다. 그래프는 `누적 평균 | 순간값 | 둘 다`, 구간은 `전체 | 5분 | 1분`에서 고른다.
4. **측정 종료** → 결과 요약 창. 기록은 `runs/dash/<이름>_<날짜_시각>/`에 남고 "기록 · Replay" 탭 목록에 올라온다.

측정은 한 번에 하나다. 디스크 여유가 2 GB 미만이면 시작을 거부하고, 측정 중 1 GB 아래로 내려가면 스스로 종료한다. 기록은 전화기 4대 기준 시간당 약 0.13 GB다.

### 3.3 탭

| 탭 | 내용 |
|---|---|
| 종합 | 핵심 다섯 가지(VA QoS 충족률, 셀 상향 Throughput, GPU YOLO 요청, mAP 유지율, 측정 시간), UE별 E2E Latency, GPU SM 슬롯 맵, PRB 슬라이싱, UE 현황 표 |
| VA 서비스 | 화질 · FPS · 프레임 크기, QoS 충족률, Latency 추이(상향 / 추론 / E2E), 선택된 모델 표시등과 모델 사용 비율 |
| Non-VA 서비스 | QoS 충족률(기한 250 ms), 상향 Latency(평균 · p90 · p99), Throughput, PRB 사용 |
| GPU · 슬롯 | 슬롯별 SM 배분(최근 80 슬롯), YOLO 요청 부하, SM 사용 추이, 모델 × SM 표, 가상 카메라 |
| 무선 · 슬라이싱 | 서비스별 · UE별 PRB와 Throughput, UE 무선 상태(RNTI, SNR, MCS, BLER, RSRP 등) |
| 뷰어 화면 | 기지국 뷰어의 네 화면(VA 2 + non-VA 2)을 대시보드 안에서 본다. 실시간 전용 |
| 컴포넌트 제어 | 모듈 On / Off / 재시작 버튼 (3.4절) |
| 기록 · Replay | 저장된 측정 목록과 재생. 다른 PC에서 받은 `.replay.jsonl`도 재생된다 |
| 데모 영상 | 발표 영상용 카드 12개: UE별 Latency 막대 4개, GPU SM 배분, PRB 슬라이싱, 누적 평균 그래프 6개 (맨 오른쪽 탭, 키 `9`) |

FHD 화면에서 종합 · VA · Non-VA · 기록 탭은 한 화면에 들어온다. GPU · 무선 탭은 브라우저 배율을 70~80 %로 줄이면 한 화면에 들어온다.

### 3.4 Capture와 컴포넌트 제어

**Capture (그래프 · 지표 · 뷰어 화면을 MP4로 내려받기)**

1. `https://<서버 IP>:8012/`로 들어가 오른쪽 위 **Capture**를 누르고, 받을 항목에 표시한 뒤 **Capture 시작**.
2. 원하는 때에 **Capture 종료**. 항목마다 MP4 하나가 **페이지를 연 PC의 다운로드 폴더**로 내려온다(서버에는 남지 않는다).

- 모든 파일이 같은 시작 · 종료 시각으로 잘려 길이가 같다. 파일 이름은 `<모드>_cap_<날짜_시각>_<번호>_<탭>_<제목>.mp4`.
- Chrome / Edge가 필요하고, 녹화하는 동안 그 브라우저 탭을 앞에 둔다. 한 번에 최대 20분(`DASH_CAP_MAX_MIN`).
- Replay 중에도 된다(뷰어 화면만 실시간 전용). "측정 시작"과는 별개라서 측정 중이 아니어도 된다.

**컴포넌트 제어 탭**

버튼을 누르면 확인 창(실행할 명령, 영향, 거절 사유)이 뜨고, "실행"을 누르면 4절의 운영 스크립트를 대시보드를 띄운 계정으로 그대로 실행한다.
대상은 L1 + gNB, 5G 코어, VA, Non-VA, PPO xApp, 트래픽 생성기(스위치 + 부하 단계)다. 한 번에 한 동작만 돌고, 기록은 `runs/dash/control.jsonl`과 `runs/dash/control/`에 남는다.

- **로그인이 없다.** 대시보드 주소를 열 수 있는 연구실 유선망의 PC는 누구나 이 버튼을 누를 수 있다. 발표 · 촬영 중 실수를 막으려면
  `.env`에 `DASH_CTL=0`(탭 끔) 또는 `DASH_CTL_DRYRUN=1`(명령을 기록만 하고 실행하지 않음)을 넣고 `./dash.sh restart`.
- 모드 전환(MIG), RIC, 가상 카메라, 모니터 xApp은 버튼이 없다. 터미널에서 한다.
- 이미 있는 실험 이름으로 L1을 켜는 것, xApp이 도는 중에 L1을 끄는 것, L1 / gNB가 떠 있는데 코어를 끄는 것은 거절한다. 코어 버튼은 그 계정에 비밀번호 없는 sudo가 없으면 거절하고 터미널 명령을 보여 준다.

더 자세한 것(지표 정의와 출처, 저장 파일 형식, 문제 해결)은 `dash/README.md`에 있다.

### 3.5 기지국 뷰어 (`/viewer`)

`https://<서버 IP>:8000/viewer`를 **PC에서** 열면 전화기 네 대의 화면이 한 번에 보인다(VA만: `/viewer/va`, 한 대만: `/viewer/ue1`).
전화기에서 열면 하향 트래픽이 생기므로 PC에서만 연다. non-VA 화면이 안 나오면 `https://<서버 IP>:8001/`의 인증서 경고를 한 번 넘긴다.

- VA 화면은 상향 + 추론 + 고정 하향 시간(`VA_VIEWER_DL_MS`)이 `VA_DEADLINE_MS`(200 ms) 안에 들어온 프레임만 갱신한다. 넘기면 그 프레임에서 멈추고 "QoS 미달" 표시가 뜬다.
- non-VA 화면은 상향 시간이 `NONVA_DEADLINE_MS`(250 ms)를 넘으면 멈춘다.
- **전체 녹화 / 녹화 종료**(화면별 버튼도 있다): 화면마다 FHD MP4 하나가 뷰어를 연 PC에 저장된다. 녹화는 브라우저 안에서 이뤄지므로 상향 · 하향 트래픽이 늘지 않는다. Chrome / Edge, 최대 20분.

## 4. 모듈별 소개와 On / Off / Restart

### 4.1 한 번에 켜고 끄기 (`all.sh`)

```bash
./all.sh status                  # 전체 상태: 모드, 생성기, 모듈별 up / DOWN, UE 표
./all.sh up <이름> [--xapp]      # 코어(꺼져 있으면) → RIC → L1 + gNB → VA → non-VA (→ PPO xApp)
./all.sh down [--core]           # 반대 순서로 모두 내린다. 코어는 --core를 붙일 때만 내린다
./all.sh restart <새 이름>       # down 뒤 up
```

- `<이름>`이 곧 로그 디렉터리 `runs/<이름>/`이다. 생략하면 `all_<월일_시분>`.
- gNB는 L1이 준비된 뒤에 뜬다(L1 준비에 몇 분 걸린다). 끝에 `== all up`이 나오면 정상이고, 문제가 있으면 `== FAILED components: …`가 나온다.
- 코어가 꺼져 있으면 `up`이 코어를 켜면서 sudo 비밀번호를 물을 수 있다.

### 4.2 모듈별 명령

| 모듈 | 하는 일 | On | Off | Restart |
|---|---|---|---|---|
| 5G 코어 (Open5GS) | 전화기의 등록 · 인증과 데이터 통로를 맡는다. 평소에는 켜 둔다 | `./core.sh up` | `./core.sh down` | `./core.sh down` 뒤 `./core.sh up` |
| nearRT-RIC (FlexRIC) | gNB와 xApp 사이의 제어 중계기. gNB가 시작하면서 여기에 접속하므로 gNB보다 먼저 켠다 | `./ric.sh start` | `./ric.sh stop` | `./ric.sh restart` |
| L1 + gNB | GPU 신호 처리(L1)와 기지국 상위 계층(gNB). 한 스위치로 같이 뜬다. Ours에서는 L1 안의 YOLO(dApp)가, Baseline에서는 `dgist-yolo` 컨테이너가 함께 뜬다 | `./dgist.sh up <이름>` | `./dgist.sh down` | `./dgist.sh down` 뒤 `./dgist.sh up <새 이름>` |
| 5셀 트래픽 생성기 | L1 안에서 가상 셀 5개를 돌려 GPU에 5G 부하를 준다. 스위치는 다음 L1 시작 때 적용된다 | `./tgen.sh on` | `./tgen.sh off` | 부하만 바꿀 때: `./tgen.sh load low\|mid\|high\|auto` |
| VA 서비스 | VA 전화기의 프레임을 받아 YOLO 요청으로 넘기고 결과를 돌려준다. 기지국 뷰어도 여기서 나온다 | `./va.sh start` | `./va.sh stop` | `./va.sh restart` |
| non-VA 서비스 | non-VA 전화기가 올리는 720p 영상을 받는다(추론 없음) | `./nonva.sh start` | `./nonva.sh stop` | `./nonva.sh restart` |
| PPO 슬라이싱 xApp (Ours만) | 100 ms마다 전화기 4대의 무선 상태와 YOLO 쪽 정보(모델, YOLO에 준 SM 수)를 보고 상향 PRB 할당량(합계 273)을 정해 gNB에 내린다. 전화기 4대가 붙은 뒤에 켠다 | `./xapp.sh start` | `./xapp.sh stop` | `./xapp.sh restart` |
| 모니터 xApp | 연구실의 읽기 전용 xApp. UE · 서비스별 상향 PRB와 처리량을 초마다 기록한다 | `./xapp.sh moni start` | `./xapp.sh moni stop` | `moni stop` 뒤 `moni start` |
| 가상 카메라 | 무선을 거치지 않고 FHD 영상 N줄기를 YOLO에 직접 넣어 요청 수만 늘린다 | `./vcam.sh start <N> [fps] [모델]` | `./vcam.sh stop` | `./vcam.sh restart <N> [fps] [모델]` |
| 대시보드 | 측정 · 시각화 웹 페이지(3절). 스택과 따로 돈다 | `./dash.sh start` | `./dash.sh stop` | `./dash.sh restart` |
| GPU 분할 (MIG) | Baseline용 고정 분할. 스택을 내린 뒤 sudo가 있는 계정으로 | `sudo <패키지>/scripts/mig.sh on` | `sudo <패키지>/scripts/mig.sh off` | 없음 |

모든 모듈에 `status`가 있다(`./va.sh status`, `./ric.sh status`, `./xapp.sh status`, `./tgen.sh status`, `./core.sh status` …). `./dgist.sh core|va|nonva|ric|xapp|tgen …`으로 불러도 같다(예: `./dgist.sh va restart`).

### 4.3 순서와 주의

- **켜는 순서**: 코어 → RIC → L1 + gNB → VA → non-VA → (전화기 접속) → PPO xApp → (모니터 xApp). 끄는 순서는 반대다. `all.sh`가 이 순서를 지킨다.
- gNB가 도는 동안 `./ric.sh stop`과 `./ric.sh restart`는 거부된다. RIC가 내려가면 gNB가 죽기 때문이다. RIC를 다시 켜야 하면 `./dgist.sh down`부터 한다.
- L1 + gNB를 다시 켜면 전화기가 다시 접속한다. 코어를 다시 켜면 전화기를 껐다 켜야 할 수 있다("Authentication failure").
- VA / non-VA 서비스를 재시작했으면 전화기 페이지를 새로 고친 뒤 다시 시작한다. 전화기에서 Stop을 누르기 전에 서비스를 내리면 그 세션의 프레임 로그는 저장되지 않는다.
- PPO xApp은 전화기의 RNTI를 알아야 뜬다. 전화기가 다시 접속해 RNTI가 바뀌면 보조 프로세스(guard)가 5초 간격으로 확인해 다시 띄운다. 전화기가 떨어져 있던 뒤에는 `./xapp.sh status`로 확인하고, 멈춰 있으면 `./xapp.sh start`.
- 전화기로 데모를 하는 동안에는 L1 / gNB를 재시작하거나 빌드하지 않는다.

### 4.4 전화기 4대

Wi-Fi를 끄고 5G로 붙인다(인증서가 자체 서명이라 경고를 한 번 승인한다).

| 전화기 | 주소 | 누르는 순서 |
|---|---|---|
| VA 2대 | `https://10.45.0.1:8000/` | UE1 / UE2 선택 → Start Camera(또는 Video File) → Start VA |
| non-VA 2대 | `https://10.45.0.1:8001/` | UE1 / UE2 선택 → Start Camera(또는 Video File) → Start Streaming |

- 누가 누구인지는 `python3 ue_table.py` 또는 `./all.sh status`의 UE 표로 본다.
- 동영상 파일을 쓸 때: `Restart Video`는 누르는 즉시 처음부터 다시 재생한다. `Start video at [시:분]` → `Set`은 그 시각에 처음부터 시작한다.
  **네 대를 같은 순간에 시작하려면** 전화기마다 Start VA / Start Streaming을 먼저 누른 뒤 같은 시각을 예약한다(서버 시계에 맞춘다).
  두 기능 모두 전화기 안의 동영상만 다루므로 서버 요청도 하향 트래픽도 없다.

### 4.5 전화기 없이 해 보는 시험

```bash
./dgist.sh test 26x 10 20        # YOLO에 직접 요청: 모델 26x, 초당 10개, 20초
./va.sh test2 60 10              # VA 서비스 경로 시험 (UE 2개 흉내, 60프레임, 초당 10개; 무선 미사용)
./nonva.sh test ue1 100 20       # non-VA 서비스 경로 시험
./vcam.sh start 2 ; ./vcam.sh status ; ./vcam.sh stop     # 가상 카메라 2대 (GPU 요청만 늘어난다)
```

### 4.6 실험 한 번의 흐름 (예: Ours + 생성기 저부하)

```bash
./all.sh mode ; ./tgen.sh status ; nvidia-smi ; df -h / /data      # 0) 모드, 생성기 스위치, GPU, 디스크 확인
./dash.sh status                                                   # 1) 대시보드 (꺼져 있으면 ./dash.sh start)
./all.sh up ours_low_1                                             # 2) 스택 (이름은 새로)
#    3) 전화기 4대: VA 2대 Start VA, non-VA 2대 Start Streaming  →  python3 ue_table.py 로 4대 확인
./xapp.sh start                                                    # 4) PPO xApp (전화기가 붙은 뒤)
#    5) PC 브라우저: 대시보드 "측정 시작", 필요하면 뷰어 "전체 녹화"
./tgen.sh load mid                                                 # 6) (필요하면) 부하 전환
#    7) 대시보드 "측정 종료", 전화기에서 Stop
./all.sh down                                                      # 8) 정리
```

## 5. 설정 파일 `.env`

패키지 디렉터리의 `.env` 한 파일이 모든 설정을 담는다. docker compose, 운영 스크립트, L1 컨테이너가 모두 이 파일을 읽는다.

- 형식: 한 줄에 `KEY=값` 하나, 주석은 `#`으로 시작하는 별도 줄(값 뒤에 주석을 붙이지 않는다).
- 값을 바꿔도 **그 값을 읽는 모듈을 재시작해야** 반영된다(아래 표의 마지막 열).
- `VA_IMSIS`, `NONVA_IMSIS`는 전화기 식별 번호다. 값을 문서나 메신저에 옮기지 않는다.

| 묶음 | 주요 키 (현재 값) | 뜻 | 바꾼 뒤 |
|---|---|---|---|
| 이미지 · 빌드 | `AERIAL_IMAGE`, `OAI_IMAGE`, `BUILD_DIR`(`build.aarch64_f1002`), `L1_CONFIG`(`dgist`), `BUILD_CPUS`, `OAI_CPUSET` | 컨테이너 이미지, L1 빌드 디렉터리와 설정 이름, 빌드 · gNB가 쓰는 CPU 코어 | 설치할 때 정하는 값 (평소에는 그대로) |
| 모드 | `DEMO_MODE`(`ours`), `BASELINE_L1_CONFIG`(`dgist_base`), `BASELINE_YOLO_SM`(`auto`) | Ours / Baseline 선택, Baseline L1의 설정 이름(`config/cuphycontroller_<이름>.yaml`), Baseline YOLO의 SM 수(`auto` = 조각 전체) | `DEMO_MODE`는 손으로 고치지 말고 `./all.sh mode` |
| 생성기 | `TRAFFIC_GEN`(`1`), `TRAFFIC_LOAD`(`low`), `TRAFFIC_GEN_TV_DIR`, `TRAFFIC_GEN_LOG_DIR` | 생성기 스위치, 시작할 때의 부하 단계, 테스트 벡터와 전체 로그 위치(기본 `./tgen/testVectors`, `./tgen/log`. 패키지 기준 상대 경로나 절대 경로) | 스위치는 `./tgen.sh on\|off` 뒤 L1 + gNB 재시작. 부하는 `./tgen.sh load` |
| L1 · dApp (Ours) | `DAPP_PUSCH_PROFILES`(`16,24,32,40,56`), `DAPP_PDSCH_PROFILES`(`24,40`) | sizer가 슬롯마다 고를 수 있는 PUSCH / PDSCH SM 후보. 생성기를 켠 6셀 실행에서는 PUSCH 후보가 시작 스크립트의 10단계(16~104)로 바뀐다 | L1 + gNB 재시작 |
| | `DAPP_INPUT`(`fhd`), `DAPP_TENANT_CLASSES_FHD`(`24,40,48,56,64,72,80,88`), `DAPP_TENANT_CLASSES` | YOLO 입력 형식(`fhd` = FHD 한 장을 3장 묶음으로 추론, `4k` = 4K 타일 8장)과 YOLO SM 등급 목록(`fhd`일 때 `_FHD` 쪽, `4k`일 때 뒤쪽 키) | L1 + gNB 재시작 |
| | `DAPP_AGENT_PORT`(`9810`), `DAPP_AGENT_QOS`(`1`), `DAPP_AGENT_GRAPH_CHUNK`(`3`), `DAPP_AGENT_CHUNK_AHEAD`(`2`) | 요청 창구 포트, 기한 검사(거절) 켬, 추론 분할 설정(0 = 분할 끔) | L1 + gNB 재시작 |
| | `DAPP_SIZER_YOLO_US`(`7:120,8:120,17:120,18:120`) | sizer의 YOLO 항: `프레임 안 슬롯 번호:여유 µs`. 비우면 꺼짐 | L1 + gNB 재시작 |
| | `DAPP_SLOT_CSV`(`1`), `DAPP_TENANT_CPU`, `DAPP_AGENT_CPU`, `PRACH_FORCE_THR0`(`20.0`) | 슬롯별 CSV 기록(분당 약 5 MB, 슬롯 맵 · 대시보드용), dApp 스레드의 CPU 코어, PRACH 검출 임계값 | L1 + gNB 재시작 |
| VA 서비스 | `VA_MODEL`(`26x`), `VA_MODEL_SELECT`(`1`), `VA_MODEL_WEIGHTS`(`10,10,60,10,10`) | 기본 모델, 규칙 기반 모델 선택 켬, 모델 비율(순서 26n, 26s, 26m, 26l, 26x) | `./va.sh restart` (Baseline에서는 L1 + gNB 재시작도) |
| | `VA_DEADLINE_MS`(`200`), `VA_TARGET_FPS`(`15`), `VA_CAMERA_WIDTH/HEIGHT`(`1920×1080`), `VA_JPEG_QUALITY`(`0.90`), `VA_MAX_INFLIGHT`(`4`) | QoS 기한, 전화기의 전송 FPS · 해상도 · JPEG 화질, 동시에 떠 있을 수 있는 프레임 수 | `./va.sh restart` + 전화기 페이지 새로 고침 |
| | `VA_TRACK`, `VA_QOS_DROP_MISSED`, `VA_DISPLAY_CONF`, `VA_ENCODE_WORKERS`, `VA_FILE_WIDTH/HEIGHT`, `VA_PORT`(`8000`), `VA_AGENT`, `VA_BACKEND`(`dapp`) | 전화기 화면의 박스 추적 · 기한 넘긴 결과 버리기 · 표시 문턱, JPEG 인코딩 워커 수, 동영상 파일 크기, 서비스 포트, YOLO 창구 주소 | 위와 같음 |
| non-VA 서비스 | `NONVA_PORT`(`8001`), `NONVA_WIDTH/HEIGHT`(`1280×720`), `NONVA_FPS`(`15`), `NONVA_JPEG_QUALITY`(`0.65`) | 서비스 포트, 전화기의 전송 해상도 · FPS · 화질 (`NONVA_VIEWER_FPS`: 뷰어 화면 갱신 FPS) | `./nonva.sh restart` + 전화기 페이지 새로 고침 |
| FlexRIC | `FLEXRIC_BUILD`, `FLEXRIC_SM_DIR`, `RIC_IP`(`127.0.0.2`) | RIC 빌드 디렉터리, 서비스 모델 라이브러리 위치, RIC 주소 | 설치할 때 정하는 값 (평소에는 그대로) |
| PPO xApp | `PPO_BUNDLE_NAME`, `VA_IMSIS`, `NONVA_IMSIS`, `XAPP_GUARD`(`1`), `XAPP_GUARD_S`(`5`), `PPO_MIN_PRBS`(`5`), `PPO_DAPP`(`1`), `PPO_GPU_SOURCE` | 정책 묶음(현재 것은 무선 지표 18개 + YOLO 모델 + YOLO에 준 SM 수를 입력으로 쓴다), 어느 전화기가 어느 서비스인지, guard 켬과 확인 주기(초), 최소 PRB, dApp에 접속해 결정마다 YOLO 모델 · SM을 함께 기록 | `./xapp.sh restart` |
| RU · 코어 | `RU_IP`, `FH_IF`(`aerial00`), `UE_SUBNET`(`10.45.0.0/16`), `NO_CORE`(`0`) | RU 주소, 프런트홀 NIC, 전화기 망, 1 = 이 서버에 코어가 없어도 켠다 | 설치할 때 정하는 값 (평소에는 그대로) |

**파일에 없어도 되는 키** (넣지 않으면 괄호 안의 기본값)

| 대상 | 키 |
|---|---|
| L1 (Ours) | `DAPP_PUSCH_INORDER`(Ours 1, Baseline 0: 순서 보장 켜기 / 끄기), `DAPP_PUSCH_INORDER_US`(3000: 한 슬롯을 기다리는 한도 µs), `DAPP_SIZER_YOLO_HOLD_MS`(1000) |
| 뷰어 | `VA_VIEWER`(1), `VA_VIEWER_DL_MS`(20), `VA_VIEWER_FIT`(1), `VA_VIEWER_REC_MAX_MIN`(20), `NONVA_DEADLINE_MS`(250), `NONVA_VIEWER_QOS_FREEZE`(1) |
| 대시보드 | `DASH_PORT`(8010), `DASH_TLS_PORT`(8012), `DASH_CTL`(1), `DASH_CTL_DRYRUN`(0), `DASH_VIEWER`(1), `DASH_CAP_FPS`(30), `DASH_CAP_MBPS`(6), `DASH_CAP_MAX_MIN`(20), `DASH_MIN_FREE_GB`(2.0), `DASH_BASE_MODEL`(26x) → 바꾼 뒤 `./dash.sh restart` |
| 가상 카메라 | `VCAM_FPS`(10), `VCAM_INFLIGHT`(4), `VCAM_JPEG_QUALITY`(0.70) |

## 6. 로그 보는 명령

### 6.1 지금 상태

```bash
./all.sh status                  # 전체 한 화면: 모드, 생성기, 모듈별 up / DOWN, UE 표
./dgist.sh status                # L1 / gNB: Err.ind · FATAL 수, 전화기 셀의 초당 DL / UL, 접속 단계별 횟수
./tgen.sh status                 # 생성기: 부하 단계, 5셀 DL / UL 합
./va.sh status ; ./nonva.sh status ; ./xapp.sh status
```

정상일 때: L1 `Err.ind 0 FATAL 0`, `gNB: NGSetup 1`, 전화기 4대가 한 번씩 붙었으면 `RA 4 RRC 4 PDU 4 RA-fail 0`. (`./all.sh up`은 끝 줄이 `== all up`이면 정상이다.)

### 6.2 실시간 로그 (`logs.sh`)

`./logs.sh <대상>`은 해당 로그를 따라가며 보여 준다(Ctrl-C로 끝냄). 빨강 = 오류, 초록 = 성공 · 접속, 노랑 = 경고.
`-n 200`을 붙이면 지난 200줄부터 보여 주고, `--raw`를 붙이면 걸러 내지 않은 원본을 보여 준다.

```bash
./logs.sh all                    # gNB 사건 + xApp 결정 + 전화기 셀의 L1 줄을 한 화면에
./logs.sh l1                     # L1: 준비 / 시작, 전화기 셀의 초당 DL / UL, Err.ind, FATAL
./logs.sh gnb                    # gNB: 전화기 접속 / 해제, RRC, 코어 연결, E2
./logs.sh dapp                   # YOLO(dApp): 처리 / 실패 / 대기 수, SM 후보별 사용 횟수
./logs.sh va                     # VA 서비스 (nonva: non-VA 서비스)
./logs.sh ppo                    # PPO xApp의 결정만 (xapp: 전체 로그)
./logs.sh tgen                   # 생성기: 5셀의 초당 DL / UL과 부하 전환 기록 (ru, testmac: 각 콘솔)
./logs.sh core                   # Open5GS: 등록, 세션, 인증
./logs.sh ue                     # UE 표를 2초마다 갱신
```

그 밖의 대상: `mac`(UE별 MAC 통계), `ric`, `moni`(모니터 xApp). 전화기 셀은 L1 셀 0이고, 생성기를 켠 실행에서는 셀 5다.

### 6.3 끝난 실행을 파일로 확인

`phy_warn.log`, `l1_persec.log`, `dapp_tenant.log`, `oai.log`는 **스택을 내릴 때** `runs/<이름>/`에 정리된다. 도는 중에는 6.1 · 6.2절의 명령을 쓴다.

```bash
R=runs/<이름>
grep -c 'Send Err.ind' $R/phy_warn.log ; grep -c FATAL $R/phy_warn.log     # L1 오류 수 (정상: 0, 0)
grep -a 'in-order gate' $R/l1_console.log | tail -2                        # 순서 보장: ON / OFF, 10초마다 held · timeout · out-of-order 수
grep -a 'sizer YOLO term' $R/l1_console.log                                # sizer의 YOLO 항이 적용됐는지
tail -n 3 $R/dapp_tenant.log                                               # YOLO: agent ok / fail / rejected, PUSCH · PDSCH 후보별 사용 횟수
cat $R/tgen_load.log                                                       # 생성기 부하 전환 시각
./dgist.sh ue <이름>                                                       # 전화기 접속 단계별 첫 시각과 횟수 (이름을 빼면 지금 또는 마지막 실행)
```

- 순서 보장이 정상이면 `timeout 0 out-of-order 0 setup-failed 0`이다. Baseline + 생성기 실행은 `PUSCH in-order gate OFF`로 나온다.
- L1 로그(`l1_console.log`, `phy_warn.log`)의 시각은 UTC다(한국 시각 − 9시간).

### 6.4 로그와 결과물 위치

| 내용 | 위치 |
|---|---|
| L1 / gNB: `l1_console.log`(콘솔 전체), `entrypoint.log`(시작 과정), 내린 뒤 `phy_warn.log`(경고 · 오류만), `l1_persec.log`(전화기 셀의 초당 DL / UL), `oai.log`(gNB) | `runs/<이름>/` |
| YOLO (Ours): 10초마다 한 줄 요약, 슬롯별 CSV | `runs/<이름>/dapp_tenant.log`, `dapp_tenant_slots.csv` |
| YOLO 서버 (Baseline) | `runs/<이름>/yolo_entrypoint.log`, `yolo_console.log`, `yolo_tenant.log` |
| 생성기 (`ru_console.log`, `testmac_console.log`, `tgen_load.log`, `l1_persec_emu.log`) | `runs/<이름>/` |
| 생성기 전체 로그 (시간당 26~29 GB) | `tgen/log/<이름>/` (`.env TRAFFIC_GEN_LOG_DIR`, `runs/<이름>/tglog` 링크로도 열린다) |
| VA / non-VA 서버 로그 | `runs/va_service/uvicorn.log`, `runs/nonva_service/uvicorn.log` |
| VA / non-VA 세션 CSV (전화기에서 Stop을 누를 때 저장) | `runs/va_service_log/`, `runs/nonva_service_log/` |
| RIC, PPO xApp | `runs/ric/`, `runs/xapp/` (PPO 결정 기록: `./xapp.sh trace`) |
| 모니터 xApp | `log/moni_log/` |
| 대시보드 측정 기록, 제어 기록 | `runs/dash/<이름>_<날짜_시각>/`, `runs/dash/control.jsonl` |
| Capture · 뷰어 녹화 MP4 | 페이지를 연 PC의 다운로드 폴더 (서버에는 남지 않는다) |
| MIG 조각 정보 (Baseline) | `runs/mig.env` |

## 부록 A. 문제 해결

| 증상 | 조치 |
|---|---|
| `./all.sh up` 끝에 `FAILED components: ric`만 나온다 | E2 연결을 기다리는 시간이 짧아서 생기는 오경보일 수 있다. `./ric.sh status`와 `./all.sh status`로 RIC가 up인지 확인한다 |
| `!! a run is registered as current (…)` | 이전 실행이 남아 있다. `./dgist.sh down` 뒤 다시 |
| `precheck failed` / `== fix the !! lines first` | `./dgist.sh check`가 알려 주는 `!!` 줄(떠 있는 다른 L1 스택, PTP, 프런트홀 링크, 코어, 빠진 엔진 · 설정 파일 등)을 먼저 해결한다 |
| `!! stack is up - ./all.sh down first` | 모드 전환은 스택을 내린 뒤에 한다 |
| `mig.sh`가 거부한다 | GPU에 프로세스가 남아 있다. `nvidia-smi`로 확인한다 |
| `Baseline engine missing: …` | MIG를 켠 뒤 `scripts/build_yolo_engines_mig.sh <모델>` |
| "RNTI mapping not possible" | 전화기 4대가 붙은 뒤 `./xapp.sh start` |
| 전화기가 "Authentication failure" | 코어를 재시작한 뒤다. 전화기를 껐다 켠다 |
| gNB만 죽었다 (`docker ps`에 `dgist-gnb` 없음) | RIC는 그대로 두고 `./dgist.sh down` → `./dgist.sh up <새 이름>` → `./xapp.sh restart` |
| `./all.sh down` 끝에 `== still running: …` | 적힌 모듈을 해당 스크립트로 한 번 더 내린다 |
| 뷰어에 non-VA 화면이 없다 | PC에서 `https://<서버 IP>:8001/`을 한 번 열어 인증서 경고를 넘긴다 |
| 대시보드가 403 | 전화기 망에서 연 것이다. 연구실 유선망의 PC에서 연다 |
| 대시보드 지표가 "대기" | 그 데이터를 내는 모듈이 꺼져 있다. `./dash.sh status`의 출처 목록을 본다 |
| 생성기를 켠 L1이 뜨다 죽는다 | `./logs.sh testmac`, `./logs.sh ru`, `runs/<이름>/entrypoint.log`를 본다 |

## 부록 B. 관련 문서

| 문서 | 내용 |
|---|---|
| `SETUP.md` | 새 서버에 설치하는 방법 |
| `dash/README.md` | 대시보드 상세: 지표 정의와 출처, 저장 파일, Replay, Capture, 컴포넌트 제어, 데모 영상 탭 |
| `docs/MODE_SWITCH.md` | 모드별 L1 설정, 생성기의 배경(GPU 메모리, MTU) |
| `docs/CHANGES_DGIST_AERIAL.md`, `docs/CHANGES_HSW_OAI.md`, `docs/patches/` | Aerial(L1)과 OAI(gNB)에 넣은 변경 |
| `va_service/README.md`, `nonva_service/README.md`, `xapp_ppo/README.md` | 각 서비스와 PPO xApp |
| `aerial-cuda-accelerated-ran/cuPHY-CP/dapp_hook/docs/XAPP_INTERFACE.md` | YOLO 요청 창구(agent, TCP 9810)의 메시지 형식 |
