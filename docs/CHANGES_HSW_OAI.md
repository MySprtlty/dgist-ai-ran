# hsw 연구실 OAI 변경점 (Aerial 25-3 기준 OAI 대비)

트리: `_dgist/openairinterface5g/` = `/home/nvidia/hsw/25_3_hsw/openairinterface5g`의 복사본 (2026-11-05).
기준 커밋: OAI `87c016b5842cb7e9952718fbac99f876a543d84a` (2026-01-30 "Make L1 registry overridable", Aerial 25-3 연동 커밋). 변경 전체는
`git -C openairinterface5g diff` 또는 `docs/patches/hsw_oai_mac_e2.patch`(8 파일, +531/-132)로 볼 수 있다. 여기에 DGIST의 1-hunk 수정
(`docs/patches/oai_empty_uci.patch`, NR_IF_Module.c)이 추가되어 트리의 diff는 9 파일 +544/-134 이다 (= 검증된 `oai-gnb-aerial_hsw:hybrid-uci` 소스와 동일).

## MAC 스케줄러 (`openair2/LAYER2/NR_MAC_gNB/`)

| 파일 | 내용 |
|---|---|
| `gNB_scheduler_ulsch.c` (+285, 18 hunks) | **UL PRB 개수 쿼터 스케줄링**: 목표 UL 슬롯마다 모든 UE·빔이 공유하는 PUSCH RB 예산(`UL_FIXED_TOTAL_PRB_CAP`), 서비스(슬라이스) 1/2 각각 UE 2개의 개수 쿼터(대여 없음, 주파수 창 아님). 이미 큐된 grant(재전송 포함)를 RNTI/서비스별로 집계해 예산 차감, BWP 전체에서 첫 연속 빈 구간을 찾되 길이만 잔여 예산으로 제한(`ul_fixed_find_run`), HARQ TB는 줄이지 않고 미룸. 슬라이스 전환 시 기존 HARQ drain 후 적용(`ul_slice_try_transition`). 쿼터 grant 로그. `ul_grant_wait` 관측치 갱신 |
| `ul_grant_wait.h` (신규) | UL grant 대기시간 관측기: 슬롯마다 backlog가 있는 동안의 radio-time 누적(`current_us`, `sum_us`, `count`) |
| `nr_mac_gNB.h` (+28) | `NR_UL_PRB_TOTAL_BUDGET 273`, `NR_UL_PRB_QUOTA_UES 4`, 계약 라벨 `"NR_UL_PRB_COUNT_V1"`; UE별 `ul_grant_wait`, `e2_ul_slice_id/…_revision/…_new_grants/…_max_allocated`; MAC 전역 `e2_ul_slice_prbs[2]`, `e2_ul_slice_revision`, pending(atomic pair) 상태 |
| `gNB_scheduler_RA.c` (+2) | `nr_check_Msg4_MsgB_Ack`에서 `//for test  success = true;` — Msg4 HARQ 라운드 0의 ACK 판정을 강제로 성공 처리하는 **테스트용 패치** (연구실 원본 그대로 둠; 운영 전 재검토 권장) |
| `gNB_scheduler_dlsch.c` (+1) | 빈 줄 하나 (기능 변화 없음) |
| `targets/.../gnb-vnf.sa.band78.273prb.aerial.ul-heavy.conf` (-70) | 샘플 설정 파일 삭제 |

## E2 agent (`openair2/E2AP/RAN_FUNCTION/CUSTOMIZED/`)

| 파일 | 내용 |
|---|---|
| `ran_func_slice.c` (+261) | Slice SM 제어 수신: 기존 Slice SM wire layout을 유지한 **버전 있는 개수-쿼터 계약** (label = `NR_UL_PRB_COUNT_V1`, `pos_low`=0, `pos_high+1` = PRB 개수). 서비스 1/2, 각 UE 2개, 총 273, 대여 없음. 쌍 전체가 검증될 때만 커밋(부분 갱신/삭제 거부), 축소·재매핑 시 drain 대기(`[E2_SLICE_PENDING]`), 수락/거부/연관 로그(`[E2_SLICE_ACCEPT|REJECT|ASSOC]`), indication은 커밋된 상태 보고 |
| `ran_func_mac.c` (+12) | MAC SM indication을 `sched_lock` 안에서 채움, BSR 음수 wrap 방지(signed 중간값), UE 통계에 `ul_wait_valid/ul_wait_us/ul_wait_sum_us/ul_wait_count` 추가 |

## FlexRIC 서브모듈 (`openair2/E2AP/flexric`, 기준 `ef6d722f` dev 2026-05-27)

`docs/patches/hsw_flexric.patch` (170줄): `src/sm/mac_sm/ie/mac_data_ie.h`에 `ul_wait_*` 4개 필드(위 MAC SM 확장과 쌍), `src/xApp/msg_handler_xapp.c`,
`src/xApp/sync_ui.c`, `src/ric/iApp/xapp_ric_id.c`, 예제 xApp CMake(kpm_rc, slice). 추적되지 않은 신규 예제: `examples/xApp/c/kpm_rc/xapp_ul_input_moni.c`,
`examples/xApp/c/slice/xapp_slice_ppo_ctrl.c`. **호스트 `/usr/local/lib/flexric/`의 SM 라이브러리와 nearRT-RIC(`<tree>/build/examples/ric/nearRT-RIC`)는
이 수정된 트리로 빌드된 것**이며, gNB 컨테이너는 그 디렉터리를 읽기 전용으로 마운트한다. 같은 디렉터리의 백업 `libmac_sm.so.before_ul_wait`도 dlopen 되어
RAN function 142가 두 번 등록된다(동작 지장 없음).

## DGIST가 더한 1 hunk (`openair2/NR_PHY_INTERFACE/NR_IF_Module.c`)

`NR_UL_indication`: `num_ucis == 0`인 UCI.indication을 AssertFatal 대신 버림. 하이브리드 L1(에뮬레이터 셀 동반)에서 필요했던 수정으로, 1셀에서는 발동하지 않지만
같은 소스로 유지한다.

## ICNL Resource Allocator: UE별 UL PRB 하한 (2026-11-11, 이 패키지에는 2026-11-17)

곽정호 교수님 연구실(ICNL) Resource Allocator(`ra/`, `./ra.sh`)의 E2 xApp `xapp_ccma_ra`가 E2SM-RC Control Style 2 액션 101 파라미터 5로 보내는
UE별 UL PRB 하한을 받아 MAC UL 스케줄러가 지킨다. 하한을 받지 않으면 동작은 이전과 같다. 따로 본 hunk는 `docs/patches/oai_ra_ul_prb_floor.patch`
(위 hsw·DGIST 트리 기준).

| 파일 | 내용 |
|---|---|
| `openair2/E2AP/RAN_FUNCTION/O-RAN/ran_func_rc.c` | RC 기능 정의에 Control Style 2 "Radio Resource Allocation Control", 액션 101 "CCMA UL link adaptation"(파라미터 1 C-RNTI, 2 MCS 상한, 3 PF 가중치, 4 유효 시간 ms, 5 UL PRB 하한)을 더해 광고하고, 받은 하한을 `ccma_e2_set_ul_floor`로 넘김. 2·3은 받기만 함 |
| `openair2/LAYER2/NR_MAC_gNB/ccma_e2.h` (신규) | `int ccma_e2_set_ul_floor(uint16_t rnti, int floor_prb, int validity_ms)` |
| `openair2/LAYER2/NR_MAC_gNB/gNB_scheduler_ulsch.c` (hsw판 위) | 하한 표(UE 32개, mutex). `pf_ul`: UL 데이터가 있는 동안 매 UL 슬롯 그 UE에 최소 min(하한, 버퍼를 비우는 PRB)를 줌(신규 + 재전송 합). 아직 차례가 오지 않은 하한 UE의 몫을 같은 서비스 쿼터 안에서 앞 UE들로부터 예약해 두고, 하한 위는 기본 PF와 hsw 서비스 쿼터가 정함. PHR로 전력이 모자라면 PRB 대신 MCS가 내려감. 유효 시간(RA 기본 60 ms)이 지나면 하한이 풀려 RA가 멈춰도 기본 스케줄러로 돌아감. 로그 `[RA_FLOOR]` |

hsw 회계(`AssertFatal`)는 그대로다: 모든 할당이 `ul_service_allowance`와 셀 남은 PRB 안이다. 재전송 루프와 HARQ drain은 바꾸지 않았다.
확인: DGIST `Dockerfile.gNB.aerial.ubuntu.dgist`(`-Werror`, `--build-e2`)로 x86 빌드(2026-11-11), ICNL GH200(arm64)에서 이 패치 + 영상 tap 패치를 얹은
gNB로 휴대폰 4대 시험(2026-11-14, 하한 명령 8,538개 모두 ack, gNB `[RA_FLOOR]` 집행 확인).

## 참고: 트리에 남아 있는 연구실 작업 파일

`*_original.c/h`, `*_ppo_ver*.c/h`, `*_1ue/_2ue*`, `gNB_scheduler_uci.c_logging`, `ci-scripts/yaml_files/sa_gnb_aerial/original.txt_*` 등은 연구실의 실험 버전 백업이다.
빌드에는 쓰이지 않으며(CMake 대상 아님) 그대로 보존했다. 실제 빌드 입력은 위 표의 파일이다.

## 참고: 연구실 gNB 이미지가 만들어진 방식과 이 패키지의 빌드

연구실 이미지(`oai-gnb-aerial_hsw:*`)는 호스트(또는 `oai-build-dev` 컨테이너)에서 `./build_oai -c -w AERIAL --gNB --ninja --build-e2`로 빌드한
`cmake_targets/ran_build/build/nr-softmodem`(E2_AGENT=ON, E2AP_V2, KPM_V2_03, `-Werror`)을 `docker/Dockerfile.gNB.aerial.ubuntu.oai_gnb`로 포장한 것이다.
트리의 공식 `Dockerfile.gNB.aerial.ubuntu`는 `--build-e2`가 없어 E2 agent 없는 바이너리를 만든다. 이 패키지는 `docker/Dockerfile.gNB.aerial.ubuntu.dgist`
(공식 파일 + `--build-e2 --cmake-opt -DE2AP_VERSION=E2AP_V2 --cmake-opt -DKPM_VERSION=KPM_V2_03`)로 한 번의 `docker build`에서 같은 바이너리를 만든다
(2026-11-05: 158,725,912 B, `hybrid-uci`의 nr-softmodem과 같은 크기).
