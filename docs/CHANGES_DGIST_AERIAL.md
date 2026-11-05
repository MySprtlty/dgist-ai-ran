# DGIST Aerial(cuPHY-CP) 변경점 — 기능 → 파일

트리: `_dgist/aerial-cuda-accelerated-ran/` = `/home/troy/build/aerial-cuda-accelerated-ran` 브랜치 `hybrid`의 작업 트리 복사본 (2026-11-05, prof/·sweep 로그 제외).
기준: NVIDIA Aerial CUDA-Accelerated RAN **Release 25-3** (커밋 `654c249`). NVIDIA 파일의 변경은 `docs/patches/aerial_25-3_dgist_nvidia_files.patch`
(30 파일, +1449/-46, yaml 제외), 우리 고유 파일은 `cuPHY-CP/dapp_hook/` 아래에 있다. 빌드물: `build.aarch64_f1002/` (Ninja, Release,
`SCF_FAPI_10_04=OFF ENABLE_CONFORMANCE_TM_PDSCH_PDCCH=OFF SCF_FAPI_10_04_SRS=ON ENABLE_DAPP_HOOK=ON`; 트리와 같은 마운트 경로 `/opt/nvidia/cuBB`라
`ninja`가 "no work to do"로 확인됨).

| 기능 | 파일 |
|---|---|
| **FAPI hooking** — L2A가 받는 DL/UL_TTI.request·PDU를 슬롯 단위로 공유 링(`/aerial_dapp_ring`)에 내보내고(비동기 export 스레드), 이를 사이저/추정기 입력으로 사용 | `cuPHY-CP/scfl2adapter/lib/scf_5g_fapi/scf_5g_fapi_dapp_export.{cpp,hpp}` (신규), `scf_5g_fapi_phy.cpp`, `cuPHY-CP/cuphyl2adapter/lib/nvPHY/nv_phy_module.cpp` + `nv_phy_config_option.hpp` (yaml `dapp_hook:` 항목, staticSlotCellMask), `cuPHY-CP/dapp_hook/include/dapp_hook/*.hpp` (링 ABI) |
| **채널별 SM 추정(사이저)** — PUSCH/PDSCH/PRACH 등 채널별 최소 SM 모델(`sm_model.txt`)로 슬롯마다 cuPHY 프로파일(SM 수)을 고르고 남는 SM을 계산 | `cuPHY-CP/dapp_hook/include/dapp_hook/dapp_sizer.hpp`, `cuPHY-CP/dapp_hook/estimator/` (모델·캘리브레이션·재적합 도구), `cuPHY-CP/cuphydriver/src/common/context.cpp` (`DAPP_PUSCH_PROFILES`/`DAPP_PDSCH_PROFILES` 프로파일 선택) |
| **Green context 기반 동적 SM 할당 + YOLO 추론 테넌트** — cuphydriver가 채널별 CUDA green context를 만들고(`use_green_contexts`), 테넌트가 슬롯마다 비는 SM 묶음(클래스 24..80)에 맞는 TensorRT 엔진으로 추론, CUDA graph를 chunk 단위로 투입 | `cuPHY-CP/cuphydriver/{include/context.hpp,include/cuphydriver_api.hpp,src/common/context.cpp,src/common/cuphydriver_api.cpp}`, `cuPHY-CP/cuphycontroller/examples/cuphycontroller_scf.cpp` (테넌트 스레드 기동), `cuPHY-CP/dapp_hook/tenant/dapp_tenant.cu`, `cuPHY-CP/dapp_hook/e2e/` (nvjpeg 디코드), `cuPHY-CP/dapp_hook/models/full26{n,s,m,l,x}/seg*_sm*.engine` (모델×클래스 엔진) |
| **타 연구실 인터페이스 (xApp agent)** — TCP 9810 JSON 줄: hello/welcome, 슬롯별 `estimate`, `run`(JPEG)→`result`, `sm_report`, `get`; FlexRIC xApp 예제와 C 헤더 클라이언트, Python 클라이언트 | `cuPHY-CP/dapp_hook/tenant/dapp_agent.cpp`, `cuPHY-CP/dapp_hook/docs/XAPP_INTERFACE.md`, `cuPHY-CP/dapp_hook/python/xapp_client.py`·`ra_stub.py`·`slicing_stub.py`, `cuPHY-CP/dapp_hook/xapp_flexric/` (`dapp_client.h`, `xapp_dapp_run.c`, 빌드물 `build/xapp_dapp_run{,_noric}`) |
| **COTS UE/하이브리드 수정** — PRACH occasion→버퍼 맵 수정과 가드(UE RRC 연결 직후 segfault/UL stall 원인), 환경변수 PRACH 임계값(`CUPHY_PRACH_FORCE_THR0`), UCI/SSB/CSI-RS slot command 수정 | `cuPHY-CP/cuphydriver/src/uplink/phyprach_aggr.cpp`, `phypusch_aggr.cpp`, `task_function_ul_aggr.cpp`, `cuPHY-CP/scfl2adapter/lib/scf_5g_fapi/scf_5g_slot_commands*.cpp` |
| **1셀 COTS 데모 패키지 / 측정 도구** | `cuPHY-CP/dapp_hook/cots/` (cots_run.sh, 매뉴얼), `cuPHY-CP/dapp_hook/sweep/` (RU 에뮬레이터 루프백 측정 도구, hybrid), `cuPHY-CP/testMAC/testMAC/launch_pattern.*`·`fapi_handler.cpp`, `cuPHY-CP/ru-emulator/ru_emulator/utils.hpp`, `testBenches/phase4_test_scripts/*` (루프백 측정용; 실 RU 구동에는 쓰이지 않음) |
| **설정** | `_dgist/config/cuphycontroller_dgist.yaml`, `l2_adapter_dgist.yaml` = 연구실 Benetel 1셀 baseline(`config_benetal.yaml`) + `use_green_contexts 1`, `mps_sm_*` 60/2/2/8/40, `dapp_hook:` 줄 |

실행 시 환경변수(`.env` → `scripts/l1_entrypoint.sh`): `DAPP_TENANT=1 DAPP_AGENT=1`, `DAPP_PUSCH_PROFILES`, `DAPP_PDSCH_PROFILES`, `DAPP_TENANT_CLASSES`,
`DAPP_TENANT_CPU/DAPP_AGENT_CPU`, `DAPP_AGENT_PORT`, `DAPP_AGENT_GRAPH_CHUNK/CHUNK_AHEAD/QOS`, `CUPHY_PRACH_FORCE_THR0`.
