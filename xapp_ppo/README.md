# `xapp_ppo/` — hsw GPU-aware PPO 슬라이싱 xApp의 `_dgist` 포팅 (2026-11-05)

## 이 xApp이 무엇인지 (코드로 확인한 내용)

**맞습니다. 강화학습(PPO) 정책이 100 ms마다 서비스별 UL PRB 쿼터를 정하는 xApp입니다.** 구성은 두 프로세스입니다.

| 부분 | 파일 | 역할 |
|---|---|---|
| C xApp | `openairinterface5g/openair2/E2AP/flexric/examples/xApp/c/slice/xapp_slice_ppo_ctrl.c` (연구실 추가, `build_dgist/.../slice/xapp_slice_ppo_ctrl`) | nearRT-RIC에 E42로 붙어 **MAC SM(142)·Slice SM(145)을 10 ms 주기로 구독**. UE 4대(`VA_RNTIS` 2 + `NONVA_RNTIS` 2)의 UL 버퍼(BSR)·PUSCH SNR·UL 처리량·평균 PRB와 현재 쿼터를 100 ms마다 `PPO4R …` 한 줄로 Unix 소켓에 보내고, 답으로 받은 두 정수(합 273, 최소 5)를 **Slice SM 제어 `NR_UL_PRB_COUNT_V1`**(서비스 1 = VA, 서비스 2 = non-VA, 대여 없음)로 gNB에 내린다. 시작 때 137:136으로 부트스트랩하고 4 UE를 두 슬라이스에 연결(ASSOC), 종료 때 슬라이스를 지워 원상복구 |
| Python 액터 | `xapp_gpu_aware_no_lending/ppo_socket_server.py` → `runtime.py` (`BasicActor`: 19 → 128 → 128 → 1, sigmoid) + `actor.pt` | C xApp이 `PPO_BUNDLE`/`PPO_PYTHON`으로 직접 띄움. 18개 무선 입력 + **GPU 사용률(NVML)** 1개를 정규화해 액터에 넣고 VA 몫 비율 → PRB 정수 두 개로 변환. 학습은 외부 PC 시뮬레이터에서 PPO 600 에피소드 + 증류(`manifest.json`, 상태 "simulation_verified_pending_testbed") |
| venv | `xapp_basic_no_lending/.venv` (torch 2.14 CPU, 798 MB) | 두 묶음이 공유. 경로를 이 위치로 고쳐 복사했다 |

원본 위치: `/home/nvidia/hsw/25_3_hsw/xapp_{gpu_aware,basic}_no_lending/` (원본 파일은 각 `upstream_hsw/`에 보관). 연구실 실행법:
```
PPO_RADIO_INPUT=1 PPO_BUNDLE=…/xapp_gpu_aware_no_lending PPO_PYTHON=…/xapp_basic_no_lending/.venv/bin/python VA_RNTIS=a,b NONVA_RNTIS=c,d ./xapp_slice_ppo_ctrl
```
`REGISTRY= TAG=ppo-273 docker compose up -d`는 그 연구실 gNB 이미지(`oai-gnb-aerial_hsw:ppo-273`)를 올리는 명령이고 xApp과는 별개다.

## 6G 번들 (`xapp_gpu_aware_no_lending_6G`, hsw 2026-11-05 21:12) — 기본값

연구실이 올린 새 모델. **20입력 = 무선 18 + YOLO 모델 번호/4 + YOLO에 할당된 SM/80**, 3개 시드 앙상블(각 20→128 Tanh→128 Tanh→264 범주형, 확률 평균 후 argmax+5 = VA PRB),
NVML 입력 없음. `sm_runtime.py`가 `compute = {model_id, allocated_sm(24..80 정수), timestamp_ms}`를 요구하고, `sm_reader.py`가 그것을
`PPO_SM_INPUT_FILE`(JSON, atomic replace)에서 읽는다. 2 s 넘게 오래된 compute 입력은 ERR → xApp이 기존 예산 유지(HOLD). 연구실 메모: "상대 연구실
방식이 미정이라 파일 어댑터 제공" — 그 상대가 우리이고, **우리 쪽 공급자가 `dapp_compute_writer.py`** 다.

```
dApp agent (cuphycontroller, TCP 9810) ──sm_report 100 ms / estimate──▶ dapp_compute_writer.py ──atomic JSON──▶ runs/xapp/compute_state.json
                                                                                                     ▲ sm_reader.py (6G 브리지)가 매 결정마다 읽음
```
- `model_id` = 가장 최근에 끝난 요청의 모델(`sm_report.last`; 구 L1은 창 기반 폴백) 없으면 `VA_MODEL`(26x)
- `allocated_sm` = 그 요청이 2 s 안에 끝났으면 그때 받은 SM(`last.sm`), 아니면 에이전트의 현재 추정 `est_sm[model]`(지금 그 모델 요청이 받을 SM) → 24..80로 클램프
- `timestamp_ms` = 에이전트 보고 시각(관측 시각; 오래된 상태에 현재 시각을 덧씌우지 않음). 에이전트가 없거나 보고가 1 s 이상 오래되면 쓰지 않아 정책이 HOLD(연구실 계약 그대로)
- 번들 파일은 손대지 않았다(`sm_runtime/ppo_socket_server/sm_reader` 원본 그대로; `upstream_hsw/`에 보관). 우리 추가: `dapp_compute_writer.py`, `dapp_reader.py`.
- 선택: `.env PPO_BUNDLE_NAME` (`xapp_gpu_aware_no_lending_6G` 기본 | `xapp_gpu_aware_no_lending` 19입력). `xapp.sh start`가 6G면 writer를 먼저 띄우고 종료 때 함께 내린다. `xapp.sh compute [restart]`로 파일 상태 확인.
- 검증 21:50–21:55: `verify_6g.py` PASS(우리 venv), xApp READY → compute 파일 뜨기 전 HOLD(계약대로) → 파일(26x, sm 80, 추정 경로) 뒤 결정 재개 → gNB `E2_SLICE_APPLIED`. trace: `xapp.sh trace` (6G는 번들 `logs/`에 기록, compute_model/allocated_sm 필드).

## dApp(FAPI-Aware Scheduler) 인터페이스 연결

연구실 xApp은 GPU 상태를 NVML 사용률로만 봤다. 우리 cuPHY Controller의 dApp 에이전트(TCP 9810, 역할 `slicing`)가 주는 값을 받도록 다음을 넣었다.

- 에이전트(`cuPHY-CP/dapp_hook/tenant/dapp_tenant.cu`, L1 재빌드 필요): `sm_report`에 **`last` = 가장 최근에 끝난 요청의 모델·SM 수·추론 시간·상태·시각** 추가 (`docs/XAPP_INTERFACE.md` 3-1).
- `xapp_gpu_aware_no_lending/dapp_reader.py`(신규): 에이전트에 붙어 `sm_report`(100 ms)·`estimate`를 받아 `last_model`, `last_sm`, `yolo_sm_time_mean`, `ran_sm_time_mean`, `yolo_sm_now`, `est_ms`, `gpu_busy_share_percent`(= (YOLO+RAN SM)/132)를 스냅숏으로 제공. 끊기면 2 s마다 재접속, 정책 틱을 막지 않음.
- `ppo_socket_server.py`(수정): 매 결정마다 dApp 스냅숏을 입력 trace(`runs/xapp/ppo_input_log/*.log`, 필드 `dapp`)에 기록하고 10 s마다 `[DAPP] last 26x sm 80 …` 한 줄 출력. `PPO_GPU_SOURCE=nvml`(기본, 학습된 입력 그대로) 또는 `dapp`(실험: 19번째 입력을 NVML 대신 dApp의 GPU 점유율로 대체 — 학습 분포와 달라 결과 보장 없음).
- 왜 액터 입력에 바로 넣지 않았나: 액터는 19입력으로 학습되어 입력을 추가하면 재학습이 필요하다. 지금은 "받아서 기록·표시"까지이고, 재학습 뒤 `runtime.py`의 `FEATURES`에 추가하면 된다.

## RNTI 매핑과 재접속 (guard)

연구실 xApp은 시작 때 받은 RNTI 4개(`VA_RNTIS`, `NONVA_RNTIS`)가 MAC 보고에 모두 있어야 결정을 내리고, 전화기가 다시 붙어 RNTI가 바뀌면 그 UE를
못 찾아 HOLD(기존 쿼터 유지)에 머문다. 그래서 이 포팅은 **SIM(IMSI) → 서비스**를 고정값으로 두고(`.env VA_IMSIS`, `NONVA_IMSIS`), `ue_table.py`가
AMF/SMF 저널로 IMSI → 현재 RNTI를 풀어 `xapp.sh start`에 넘긴다. `XAPP_GUARD=1`이면 `xapp.sh start`가 guard(`xapp.sh guard`, 5 s 주기)를 함께
띄워, 4대의 현재 RNTI가 시작 때 값과 달라지면 xApp을 멈추고(쿼터 제거) 새 RNTI로 다시 시작한다(재연결까지 약 5–10 s HOLD/기본 273). 전화기가 한 대라도
빠져 있으면 모두 돌아올 때까지 기다린다. 확인: `xapp.sh status`의 `RNTIs: started with […] | now […] | guard: running`, 로그 `runs/xapp/guard.log`.
21:59 시험: 저장값을 일부러 바꿔 guard가 감지 → 정지(쿼터 제거) → 새 RNTI로 재시작 READY → 결정 재개까지 약 4 s.

## 실행 (`../xapp.sh`, `../ric.sh`)

```bash
./ric.sh start                 # nearRT-RIC (우리 빌드, 127.0.0.2) - 반드시 gNB보다 먼저
./dgist.sh up ; ./va.sh start ; ./nonva.sh start      # 또는 ./all.sh up
./xapp.sh rntis                # UE 표 + VA_RNTIS/NONVA_RNTIS 자동 매핑 (전화기 4대가 각 서비스로 프레임을 보내고 있어야 함)
./xapp.sh start [VA1,VA2 NV1,NV2]                     # READY 뒤 100 ms마다 결정; ./xapp.sh trace / status / logs ; ./logs.sh ppo
./xapp.sh stop                 # 쿼터 제거(원상복구) 후 종료
./xapp.sh dapp                 # dApp 피드만 확인
```
빌드: `./ric.sh build`(cmake+ninja, E2AP_V2/KPM_V2_03/번들 sqlite3, 약 10분) → RIC·SM 라이브러리(`flexric_sm/`, gNB 컨테이너에 마운트)·연구실 xApp 전부.

## 2026-11-05 밤 검증 (해결됨, 21:40)

- **gNB segfault의 원인은 OAI 코드 결함**(gdb 백트레이스): `openair3/NGAP/ngap_gNB_handlers.c:988`이 PDU Session Resource Setup Request의
  optional IE `pDUSessionNAS_PDU`를 검사 없이 참조한다. 전화기가 코어에 PDU 세션을 가진 채 gNB만 재시작되면 Service Request로 세션을
  재활성화하고, 이때 AMF는 NAS PDU 없이 Setup을 보내므로 매번 첫 PDU 세션에서 죽었다(RIC·L1·이미지와 무관). 가드 패치
  `docs/patches/oai_pdusession_nas_null.patch`(RRC는 이미 빈 NAS PDU를 처리)로 이미지를 재빌드한 뒤 재현되지 않는다.
- 그 결과 **xApp 전 경로 동작**: RIC(127.0.0.2) → gNB E2 SETUP → `xapp.sh start cde6,8c2d 86a4,4581` → E42 등록(xApp ID 7) → Slice(145)·MAC(142)
  구독 → 쿼터 137:136 부트스트랩 + 4 UE 연결 → 100 ms마다 결정(20 s에 166건, 예: 168:105) → gNB `[E2_SLICE_APPLIED] service=1 quota=168 / service=2 quota=105`.
  dApp 피드 연결(`[DAPP] connected … YOLO 0 / RAN 35 SM`). 전화기 트래픽이 없을 때의 결정이며, 트래픽이 시작되면 입력(버퍼·처리량)이 채워진다.
- 아직 남은 것: (1) 지금 도는 L1은 아침(10:37) 바이너리라 `sm_report.last`가 없어 피드가 창(window) 기반 폴백(`last_from=window`)을 쓴다 —
  `last`가 들어간 재빌드 바이너리는 `build.aarch64_f1002/cuPHY-CP/cuphycontroller/examples/cuphycontroller_scf.last_2046`로 보존(전화기 없는 때 교체).
  (2) PPO 입력에 dApp 값을 직접 넣는 것은 재학습 필요(`PPO_GPU_SOURCE=dapp`는 실험 옵션). (3) 전화기 4대의 서비스 매핑은 프레임이 흐를 때
  `xapp.sh rntis`가 자동으로 한다(오늘은 IMSI로 직접 지정: VA …992/…998, non-VA …994/…996).
- 교훈: OAI E2 agent는 RIC가 사라지면 gNB를 abort(e2_agent.c:225) → RIC 먼저, gNB 도는 동안 RIC 재시작 금지(`ric.sh stop`이 거부). 연구실의
  `xapp_ul_input_moni` 2개(10월 9일부터 실행 중)가 127.0.0.1:36422를 폴링하므로 우리 RIC는 127.0.0.2. 데모 중 L1/gNB 재시작·재빌드 금지.
