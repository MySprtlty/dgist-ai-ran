# `_dgist` 운용 매뉴얼 — 모듈별 켜고 끄는 스크립트 (2026-11-05)

모든 명령은 **nvidia 계정, `/home/nvidia/_dgist`에서** 실행한다. 설정은 전부 `.env` 한 파일. sudo(비밀번호)는 코어(`core.sh`)에만 필요하다.

**절대 `sudo ./all.sh` / `sudo ./ric.sh` 처럼 root로 실행하지 않는다** — 프로세스와 파일이 root 소유가 되어 nvidia 계정의 스크립트가 멈출 수 없고(`all.sh down`이 RIC/모니터를 못 내림), 다음 `up`이 "이미 떠 있음"으로 건너뛴다. 스크립트가 root면 거부한다. 코어가 필요한 sudo는 `core.sh` 안에서만 호출된다.
`./all.sh up`은 **멱등**이다: 이미 떠 있는 것은 건너뛰고 꺼진 것만 올리며, 끝에 실패한 구성요소를 나열한다. `./all.sh down`은 xApp(+writer, guard) → 모니터 → non-VA → VA → gNB+L1 → RIC 순으로 내리고 끝에 아직 도는 것을 나열한다.

## 1. 한눈에 보기

| 모듈 | 무엇 | 켜기 | 끄기 | 상태 / 로그 |
|---|---|---|---|---|
| 코어 (Open5GS 18 유닛 + UE NAT) | 호스트 systemd 서비스 | `./core.sh up` (sudo) | `./core.sh down` (sudo) | `./core.sh status`, `./logs.sh core` |
| nearRT-RIC (FlexRIC, 127.0.0.2) | E2 36421 / E42 36422 | `./ric.sh start` | `./ric.sh stop` (gNB가 돌면 거부, `--force`) | `./ric.sh status`, `./logs.sh ric` |
| L1 + gNB (cuphycontroller + OAI, 컨테이너 dgist-l1 / dgist-gnb) | FAPI-Aware Scheduler L1, hsw OAI(E2 agent) | `./dgist.sh up [이름]` | `./dgist.sh down` | `./dgist.sh status`, `./dgist.sh ue`, `./logs.sh gnb\|mac\|l1\|dapp` |
| VA 서비스 (전화기 → YOLO via dApp, :8000) | `va_service/` uvicorn | `./va.sh start` | `./va.sh stop` | `./va.sh status`, `./logs.sh va` |
| non-VA 서비스 (라이브 스트리밍, :8001) | `nonva_service/` uvicorn | `./nonva.sh start` | `./nonva.sh stop` | `./nonva.sh status`, `./logs.sh nonva` |
| PPO 슬라이싱 xApp (+ compute writer + guard) | `xapp_slice_ppo_ctrl` + Python 액터 | `./xapp.sh start` | `./xapp.sh stop` | `./xapp.sh status`, `./xapp.sh trace`, `./logs.sh ppo\|xapp` |
| 모니터 xApp (읽기 전용) | `xapp_ul_input_moni` | `./xapp.sh moni start` (전경: `./xapp.sh moni`) | `./xapp.sh moni stop` | `./xapp.sh moni status`, `./logs.sh moni` |
| 실험 대시보드 (측정 · 그래프 · Replay, :8010) | `dash/` uvicorn, 스택에 대해 읽기 전용 | `./dash.sh start` | `./dash.sh stop` | `./dash.sh status`, `./dash.sh logs`, `./dash.sh list` |
| **전체** | 위 순서대로 | `./all.sh up [이름] [--xapp]` | `./all.sh down [--core]` | `./all.sh status` |

`./dgist.sh <모듈> …`로도 같은 스크립트를 부를 수 있다 (`./dgist.sh va start`, `./dgist.sh ric status` 등).

## 2. 순서 (중요)

**켜기**: 코어 → **RIC** → L1+gNB → VA → non-VA → (전화기 attach·스트리밍) → PPO xApp → (모니터)
**끄기**: xApp → 모니터 → non-VA → VA → gNB+L1 → RIC → (코어)

- RIC는 **gNB보다 먼저** 올리고, gNB가 도는 동안 **RIC를 내리거나 재시작하지 않는다.** OAI E2 agent는 RIC가 사라지면 gNB를 abort시킨다(`e2_agent.c:225`). `ric.sh stop`은 gNB가 돌면 거부한다.
- 코어는 보통 **켜 둔 채로** 둔다. 재시작하면 TMSI가 다시 배정되어, 옛 GUTI를 가진 전화기가 "Authentication failure"로 못 붙을 수 있다(전화기 전원 재시작으로 해소).
- gNB만 재시작해도 전화기는 자동으로 다시 붙는다(코어에 세션을 가진 채 Service Request). 이 경로의 OAI NULL 참조 결함은 패치했다(`docs/patches/oai_pdusession_nas_null.patch`).
- 전화기 데모 중에는 L1/gNB 재빌드·재시작을 하지 않는다. L1 재빌드(`dgist.sh build-l1`)는 전화기가 없는 시간에.

## 3. 자주 쓰는 흐름

```bash
# (A) 전체 올리기 (가장 간단)
./all.sh up demo1              # 코어가 꺼져 있으면 sudo 비밀번호를 묻는다
#   → 전화기 4대 켜서 붙이고, 각 페이지에서 Start: VA 두 대 https://10.45.0.1:8000/ , non-VA 두 대 https://10.45.0.1:8001/
./xapp.sh start                # 4대가 붙으면 (RNTI는 .env의 VA_IMSIS/NONVA_IMSIS로 자동)
./xapp.sh moni start           # 선택: 모니터

# (B) 전체 내리기
./all.sh down                  # 코어는 둔다;  ./all.sh down --core 면 코어까지

# (C) 서비스만 다시
./va.sh restart ; ./nonva.sh restart           # .env의 VA_*/NONVA_* 바꾼 뒤 (전화기 페이지 새로 고침)

# (D) xApp만
./xapp.sh stop                 # 쿼터 제거(원상복구) 후 종료
./xapp.sh start                # 6G 번들(기본). 19입력 번들은 .env PPO_BUNDLE_NAME 변경 후

# (D-2) 모니터 xApp은 ./xapp.sh moni start 로만 띄운다. 빌드 디렉터리에서 ./xapp_ul_input_moni 를 직접 실행하면 기본 설정(127.0.0.1)으로
#       우리 RIC(127.0.0.2)에 못 붙은 채 영원히 재시도하고, 스크립트가 세지도 못한다 (pgrep -af xapp_ul_input_moni 로 찾아 kill)
# (E) 전화기가 다시 붙어 RNTI가 바뀜
#   PPO xApp: guard(기본 켜짐)가 5 s 안에 감지해 자동 재시작. 모니터: ./xapp.sh moni stop; ./xapp.sh moni start

# (F) gNB가 죽었을 때 (docker ps에 dgist-gnb 없음)
./dgist.sh down ; ./dgist.sh up 이름           # RIC는 그대로 둔 채 (먼저 떠 있어야 함). xApp은 guard가 따라오지만 ./xapp.sh restart 권장

# (G) 상태 한 화면
./all.sh status ; ./xapp.sh status ; ./dgist.sh ue

# (H) 측정 (실험 대시보드) — 스택과 따로 돌므로 한 번 켜 두면 된다
./dash.sh start                # PC 브라우저: http://10.150.21.161:8010/  (폰·UE 망에서는 403)
#   → 폰 4대가 Start한 뒤 페이지의 "측정 시작"(실험 이름 입력) … "측정 종료"(결과 요약)
#   → 기록 runs/dash/<이름>_<시각>/ , 같은 페이지 "기록 · Replay" 탭에서 다시 재생
./dash.sh sim ours             # 연습용: 가짜 숫자로 같은 화면 (http://10.150.21.161:8011/, 끄기 ./dash.sh sim stop)
```

## 4. 모듈별 상세

### core.sh — Open5GS 코어
`up | down | status` (sudo), `config`(설정 복사본 `core/etc/` 목록, 라이브와 차이), `diff [nf]`, `install-config <nf>`(복사본을 /etc/open5gs에 설치 + 유닛 재시작, 백업 남김), `build`(소스 `open5gs/` meson 빌드; dev 패키지 필요), `subs`(가입자 목록), `logs`. 가입자 추가는 WebUI `http://<host>:9999`.

### ric.sh — nearRT-RIC
`start | stop [--force] | restart | status | logs | install-sm | build`. 우리 빌드 `build_dgist`(E2AP_V2, KPM_V2_03), RIC IP 127.0.0.2(`config/flexric.conf`; 연구실의 옛 모니터 프로세스들이 127.0.0.1을 폴링하므로 분리). SM 라이브러리는 `flexric_sm/`에 두고 gNB 컨테이너에 마운트한다(`install-sm`; 빌드 뒤 한 번).

## 모드 전환: Ours ↔ Baseline (2026-11-12)

| | Ours (`DEMO_MODE=ours`, 기본) | Baseline (`DEMO_MODE=baseline`) |
|---|---|---|
| GPU | 전체 132 SM, MIG 꺼짐 | MIG 4g.48gb(64 SM) + 3g.48gb(60 SM) |
| L1 | 우리 cuphycontroller(`build.aarch64_f1002`): 녹색 컨텍스트, FAPI 인지 YOLO 테넌트, xApp 에이전트 | 연구실 **바닐라 cuBB 25-3** 바이너리(`BASELINE_TREE=/home/nvidia/hsw/25_3_hsw/...`, 읽기 전용 마운트, MPS). 설정은 연구실 config 디렉터리 사본 `config/baseline_cuphy_config/`를 덮어 마운트하고 그중 `cuphycontroller_baseline.yaml` = 연구실 `config_benetal` yaml에서 `mps_sm_pusch` 120 → 64만 바꾼 것(4g 인스턴스가 64 SM) → 컨테이너 `dgist-l1` |
| YOLO | L1 프로세스 안, 슬롯마다 24~88 SM, QoS 거절·그래프 분할·모델 선택 | 별도 컨테이너 `dgist-yolo`(3g): 우리 테넌트 바이너리를 **naive 모드**로 — 인스턴스 전체(60 SM, 자동 감지) 고정, FAPI 없음, QoS 거절 없음, 분할 없음, 모델 1개(`VA_MODEL`, batch-3 FHD 엔진 `models/migfhd26x/seg00_sm60.engine`, 디코드·NMS 동일), 도착 순서대로 실행 |
| xApp | PPO 슬라이싱 (`all.sh up --xapp`) | 없음(`--xapp` 무시). RIC는 gNB E2 에이전트 때문에 그대로 둠 |
| gNB, 코어, VA/non-VA 서비스, 뷰어, 로그 | 동일 | 동일 (`VA_AGENT` 같은 포트 9810) |

전환: 스택을 내린 뒤 `./all.sh mode baseline`(또는 `ours`) → `.env DEMO_MODE`가 바뀌고 MIG를 켜거나 끈다. **MIG 전환은 root가 필요**해서 nvidia 계정에서는 안내만 나온다: sudo 가능한 계정에서 `sudo -n scripts/mig.sh on|off` (`runs/mig.env`에 UUID 기록). Baseline 엔진은 한 번만 `scripts/build_yolo_engines_mig.sh x`(MIG 켠 뒤, 3g 안에서 빌드, 10~15분 → `models/migfhd26x/seg00_sm60.engine`). 그 뒤 `./all.sh up <이름>`은 모드에 맞게 `docker-compose.yaml` 또는 `docker-compose.baseline.yaml`을 쓴다. `./all.sh status`, `./dgist.sh status` 첫 줄에 모드가 나온다. 뷰어 아래 줄의 "모델 26x · 60 SM"이 Baseline, "· 80 SM"이 Ours.

### dgist.sh — L1 + gNB
`check`(사전 점검) `up [이름]` `down` `status` `ue [이름]`(attach 단계) `logs [l1|gnb]` `test [모델] [req/s] [초]`(dApp RUN 스모크) `build-l1 [타겟]` `build-oai` `core|va|nonva|ric|xapp|all|logs …`(패스스루). 실행 로그는 `runs/<이름>/`.

입력 프로파일 `.env DAPP_INPUT=4k|fhd` (2026-11-09): `4k` = 3840×2160, batch-8 타일(1280 크롭 6 + 전체 2, 엔진 `models/full26*`, 기본), `fhd` = 1920×1080, batch-3(1280×1080 크롭 2 + 전체 1, 엔진 `models/fhd26*`, `scripts/build_yolo_engines.sh fhd`). `VA_CAMERA_WIDTH/HEIGHT`를 같이 맞춘다(`check`가 불일치를 알려 줌; 다른 크기 프레임도 전체 보기만으로 처리는 된다). `fhd`의 SM 클래스는 `DAPP_TENANT_CLASSES_FHD`(기본 24,40,48,56,64,72,80, 4K와 같은 사다리)다. 2026-11-10 오전에 80이 빠졌던 두 원인은 해결됐다: (1) 테넌트가 클래스별 green context에서 엔진을 역직렬화했는데 TensorRT 10.3(Myelin)이 그 시점 context의 SM 수로 GEMM 그리드를 정해 프로세스 전체에 커널별로 캐시하므로 먼저 로드된 클래스의 그리드가 다른 클래스에 적용됐다(72 먼저 → 80 엔진 실패; 24 먼저 → 큰 클래스가 24짜리 그리드로 동작). 지금은 모든 엔진을 primary context에서 역직렬화한다(`DAPP_TENANT_ENGINE_CTX=prim` 기본, 단독 측정 비용 26x +2.7 %). (2) fhd26s/fhd26l의 sm80 엔진은 고유 sm80 tactic으로 빌드하면 같은 모델의 다른 클래스와 한 프로세스에서 "Cask Gemm execution"으로 실패하므로 `scripts/build_yolo_engines.sh`가 sm72 timing cache(`models/fhd26<m>/sm72.timing.cache`)로 빌드한다(26s 3.7 / 26l 8.1 ms, 고유 tactic과 같음). 바꾼 뒤 `./dgist.sh down; ./dgist.sh up`. FHD 사다리는 `DAPP_TENANT_CLASSES_FHD=24,...,80,88` (2026-11-11: 88 = (P132,N24) 틈. UL 작업이 4슬롯 안에 없는 D 4~7 슬롯에만 생기고 class는 추론 전체 구간의 최소 틈으로 고르므로, UE가 붙어 UL 제어가 매 프레임 있으면 사실상 80까지 쓰이고 UL이 없는 프레임에서만 88이 선택된다. 검증 기록 `prof/tenant/fhd/class88_2026-11-11.md`).
- `./dgist.sh slotmap <name> [--from-sec S --sec D --slowdown 20 --fps 60 --lang ko]` (2026-11-11): `runs/<name>/dapp_tenant_slots.csv`를 GPU Slot Map 영상 `runs/<name>/slotmap.mp4`로 렌더링한다. CSV는 슬롯(0.5 ms)마다 한 줄: `t_ns`(벽시계), `gap_P/gap_N`(그 슬롯에 YOLO에 제공된 틈, 여유 SM = P−N−8, 5G 예약 = 132−여유), `run_cls`(추론 중 점유 SM), `queue`, `model`. Ours 레인은 이 기록 그대로, Baseline 레인은 같은 5G 예약·같은 요청에 MIG 72/60 벽을 씌운 계산값(추론 시간 ×1.3). 10 실초 = 1/20 배속 200초 영상. `.env DAPP_SLOT_CSV=0`이면 CSV를 쓰지 않음(분당 약 5 MB).

### va.sh — VA 서비스
`start [local] | stop | restart | status | logs | test [ue] [n] [req/s] | test2 [n] | bg start --fps N|stop|status | make-frame`. 설정 `.env VA_*`(FHD/4K, 26x, req/s, QoS 200 ms, JPEG 품질, 추적기, 병렬 인코딩). CSV `runs/va_service_log/`. **기지국 뷰어** `https://10.150.21.161:8000/viewer`(UE 4대 한 화면: VA 2대는 최신 QoS 충족 프레임 + 박스, 미달이면 화면 정지; non-VA 2대는 720p 중계; `.env VA_VIEWER*`, 2026-11-11/12). 상세 `va_service/README.md`.

### vcam.sh — 가상 카메라 (GPU 부하 추가, 무선 미사용, 2026-11-12)
`start <N> [fps] [model] | stop | restart | status | logs | make-frames <이미지 폴더|영상> [개수] | dry-run`. N개의 FHD JPEG 스트림을 폰 프레임과 같은 RUN 경로로 dApp 에이전트(role vcam)에 직접 보낸다. 개방 루프(fps 유지, inflight 4, 넘치면 건너뜀). 상태 `runs/vcam/status.json`, 프레임 `va_service/vcam_frames/`. 설정 `.env VCAM_FPS`(10) `VCAM_INFLIGHT` `VCAM_FRAMES` `VCAM_JPEG_QUALITY`(0.70).

### nonva.sh — non-VA 서비스
`start | stop | restart | status | logs | test [ue] [n] [fps] | make-frame`. 설정 `.env NONVA_*`(720p, 20 fps, 0.80). 뷰어 `https://10.45.0.1:8001/viewer/ue1|ue2`. 상세 `nonva_service/README.md`.

### xapp.sh — PPO 슬라이싱 xApp
`rntis`(UE 표 + 매핑) `start [VA1,VA2 NV1,NV2]` `stop` `restart` `status` `trace [n]` `logs` `dapp`(dApp 피드 확인) `compute [restart]`(6G compute 파일) `guard`(내부용) `moni [start|stop|status|logs|power [초]] [VA NV]`(power = `ue_power.py`: UE별 RSRP·PCMAX·PH·송신전력·감당 PRB 대 실제 grant·MCS/BLER/SNR 표, CSV `log/moni_log/ue_power_*.csv`, 2026-11-12). 번들 선택 `.env PPO_BUNDLE_NAME`(기본 6G 20입력: YOLO 모델·SM을 우리 dApp에서 받음). 상세 `xapp_ppo/README.md`.

### logs.sh — 읽기 쉬운 로그
`./logs.sh <대상> [-n N] [--raw]` — 대상: `gnb mac l1 dapp va nonva ric xapp ppo moni core|amf|smf|upf ue all`. 빨강 = 오류/실패, 초록 = 성공/attach, 노랑 = 경고/HOLD.

### dash.sh — 실험 대시보드 (측정 · 그래프 · Replay, 2026-11-12)
`start | stop | restart | status | logs | list | sim [ours|baseline] | sim stop | sim logs`. 연구자용 웹 페이지 `http://10.150.21.161:8010/`(평문 HTTP, LAN 전용; UE 망 `10.45.0.0/16`의 요청은 403이라 하향 트래픽을 만들지 않는다). 스택을 켜거나 끄거나 바꾸지 않고 읽기만 한다: VA / non-VA 서비스의 프레임 피드(루프백), gNB 로그, L1 슬롯 CSV·초당 줄, dApp 에이전트(role dash), xApp 로그, Open5GS 저널 + AMF UE 표, NVML, 가상 카메라 상태. **측정 시작**(이름 입력) → 모든 카드에 지금 값과 측정 시작 후 누적 평균 → **측정 종료**(요약). 탭 6개: 종합, VA 서비스, Non-VA 서비스, GPU · 슬롯, 무선 · 슬라이싱, 기록 · Replay. 기록은 `runs/dash/<이름>_<시각>/`의 파일 6개(`.replay.jsonl`, `.summary.json/.txt`, `.timeseries.csv`, `.frames.csv`, `.slots.csv.gz`; UE 4대 기준 시간당 약 0.13 GB)이고 `.replay.jsonl` 하나로 다른 PC에서도 Replay가 된다. Ours / Baseline 두 모드에서 그대로 쓴다(Baseline은 슬롯 CSV·xApp이 없어 슬롯 맵이 MIG 고정 분할로 나온다). `sim`은 포트 8011의 연습용 인스턴스(가짜 숫자, "SIMULATION" 배지, 기록 `runs/dash_sim/`). 설정 `.env DASH_*`(모두 선택). 지표 정의·출처·한계는 `dash/README.md`.

### ue_table.py — 누가 누구인가
`python3 ue_table.py` → RNTI, gNB UE id, IMSI, UE IP, in-sync, RSRP, MCS, BLER, SNR, 서비스(어느 서비스에 프레임을 보내는지). `--rntis`로 xApp용 매핑 한 줄. (AMF/SMF 저널을 읽으므로 nvidia 계정은 `systemd-journal` 그룹 필요 — 2026-11-05 적용됨.)

## 5. 로그와 결과물 위치

| 내용 | 위치 |
|---|---|
| L1/gNB 실행 로그 (console, phy_warn, l1_persec, dapp_tenant, oai.log) | `runs/<이름>/` |
| VA / non-VA uvicorn 로그 | `runs/va_service/`, `runs/nonva_service/` |
| VA / non-VA 세션 CSV | `runs/va_service_log/`, `runs/nonva_service_log/` |
| RIC 로그 | `runs/ric/` |
| PPO xApp 콘솔(C + Python), guard, compute writer, compute 파일 | `runs/xapp/` |
| PPO 결정 trace (6G 번들) | `xapp_ppo/xapp_gpu_aware_no_lending_6G/logs/` (`./xapp.sh trace`) |
| C xApp 자체 로그 / 모니터 텍스트 로그 (연구실 형식) | `log/ppo_output_log/`, `log/moni_log/` |
| 대시보드 측정 기록 (replay.jsonl, summary, timeseries.csv, frames.csv, slots.csv.gz) | `runs/dash/<이름>_<시각>/` (`./dash.sh list`; 시뮬레이션은 `runs/dash_sim/`) |
| 대시보드 서버 로그 | `runs/dash_service/` |
| 빌드 로그 | `runs/build_*.log` |

## 6. 설정 (`.env`) 요약

이미지·빌드 디렉터리·코어 수(`BUILD_CPUS`), gNB cpuset, dApp(`DAPP_*`, 입력 프로파일 `DAPP_INPUT=4k|fhd`), VA(`VA_*`), non-VA(`NONVA_*`), FlexRIC(`FLEXRIC_BUILD`, `FLEXRIC_SM_DIR`, `RIC_IP`), PPO(`PPO_BUNDLE_NAME`, `PPO_GPU_SOURCE`, `PPO_DAPP`, `PPO_MIN_PRBS`), 전화기 SIM→서비스(`VA_IMSIS`, `NONVA_IMSIS`), guard(`XAPP_GUARD`, `XAPP_GUARD_S`), RU/코어(`RU_IP`, `FH_IF`, `UE_SUBNET`, `NO_CORE`). 바꾼 뒤 해당 모듈만 restart.
