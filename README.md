# `_dgist` — 두 연구실 결과물을 합친 1셀 COTS UE 스택 (2026-11-05)

> 모듈별 켜기/끄기 스크립트 요약: **`MANUAL.md`** (코어 → RIC → L1+gNB → VA → non-VA → xApp 순서, 각 모듈의 start/stop/status/logs, 로그 위치).
> 저장소: **https://github.com/MySprtlty/dgist-ai-ran** (이 디렉터리 = git 작업 트리). 새 서버 설치 순서와 생성물 재생성 방법: **`SETUP.md`**.

hsw 연구실의 **OAI gNB(MAC 스케줄러 UL 개수-쿼터 + E2 agent 확장)** 와 DGIST의 **Aerial cuPHY controller(FAPI hooking, 채널별 SM 추정,
green context 기반 동적 SM 할당 + YOLO 추론 테넌트, xApp 인터페이스)** 를 한 디렉터리의 코드·빌드물·스크립트로 묶었다.
nvidia 계정에서 `./dgist.sh up` / `./dgist.sh down` 으로 올리고 내린다. 코어망(Open5GS)은 이 호스트의 systemd 서비스를 그대로 쓴다.

## Ours / Baseline 모드 (2026-11-12)

`.env DEMO_MODE=ours|baseline`, 전환 `./all.sh mode <모드>`(스택 내린 뒤; MIG는 `sudo -n scripts/mig.sh on|off`). Baseline = 연구실 바닐라 cuBB 25-3을 MIG 4g에서,
naive YOLO 서버(batch-3 FHD, 60 SM 고정, QoS·분할·모델 선택 없음)를 MIG 3g에서, xApp 없이. 파일: `docker-compose.baseline.yaml`,
`scripts/l1_entrypoint_baseline.sh`, `scripts/yolo_entrypoint_baseline.sh`, `scripts/mig.sh`, `scripts/build_yolo_engines_mig.sh`. 자세한 표는 MANUAL.md.

## 1. 무엇이 어디서 도는가

| 구성 요소 | 어디서 | 소스 / 빌드물 | 설정 |
|---|---|---|---|
| L1 `cuphycontroller_scf` (DGIST) — CUDA green context, 슬롯별 SM 사이저, 프로세스 내 YOLO dApp 테넌트, xApp agent(TCP 9810) | 컨테이너 **dgist-l1** (`nvcr.io/nvidia/aerial/aerial-cuda-accelerated-ran:25-3-cubb`, privileged, host net/ipc) | `aerial-cuda-accelerated-ran/` (hybrid 브랜치 작업 트리) → `build.aarch64_f1002/cuPHY-CP/cuphycontroller/examples/cuphycontroller_scf`, 엔진 `cuPHY-CP/dapp_hook/models/full26*/` | `config/cuphycontroller_dgist.yaml`, `config/l2_adapter_dgist.yaml` (Benetel 1셀, nvIPC prefix `nvipc`) |
| OAI gNB `nr-softmodem` (hsw MAC/E2 + DGIST UCI 수정) | 컨테이너 **dgist-gnb** (`dgist-oai-gnb-aerial:latest`, host net/ipc, cpuset 56-62) | `openairinterface5g/` (hsw 트리 + `docs/patches/oai_empty_uci.patch`) → 이미지는 `./dgist.sh build-oai` | `config/gnb.conf` (= hsw `original.txt`, `tr_s_poll_core` 62만 다름; AMF 127.0.0.5, PLMN 999/70, TAC 1, E2 agent → 127.0.0.1) |
| 코어망 Open5GS | 호스트 systemd 18개 서비스 (AMF 127.0.0.5, UE 10.45.0.0/16, WebUI :9999) | 연구실 설치 그대로 | `./dgist.sh core up/down/status`, `core/routing*.sh` (UE NAT) |
| RU Benetel | 프런트홀 `aerial00`, 관리 10.10.0.100, PTP `ptp4l`/`phc2sys` (호스트) | — | yaml의 `nics`/RU MAC = 연구실 baseline |
| (선택) nearRT-RIC / xApp | 연구실 FlexRIC 빌드 `/home/nvidia/hsw/25_3_hsw/openairinterface5g/openair2/E2AP/flexric/build/examples/ric/nearRT-RIC`; SM 라이브러리 `/usr/local/lib/flexric` (gNB 컨테이너에 ro 마운트) | xApp 예제 `aerial-cuda-accelerated-ran/cuPHY-CP/dapp_hook/xapp_flexric/` | `config/flexric_xapp.conf` |

순서: `docker compose up -d` 가 **L1 컨테이너가 healthy(‘L1 is ready’ + dApp 엔진 로드 + agent 포트 열림)가 된 뒤에만 gNB 컨테이너를 시작**한다
(`depends_on: condition: service_healthy`). gNB가 cell 0의 CONFIG/START.request를 보내면 셀이 켜지고, AMF와 NG Setup이 되면 UE가 붙을 수 있다.

## 2. 디렉터리

```
_dgist/
├── dgist.sh                      up | down | status | logs | ue | test | check | core | va | build-l1 | build-oai
├── core.sh                       Open5GS 코어 on/off (up | down | status; sudo → nvidia 비밀번호)
├── va.sh                         VA 서비스 on/off (start | stop | status | logs | test | test2 | bg)  ← source yolo_env/bin/activate + uvicorn 대신
├── nonva.sh                      non-VA(라이브 스트리밍) 서비스 on/off (start | stop | status | logs | test), 포트 8001
├── ric.sh                        FlexRIC nearRT-RIC on/off (start | stop | status | build | install-sm), 127.0.0.2 — gNB보다 먼저 올릴 것
├── xapp.sh                       hsw GPU-aware PPO 슬라이싱 xApp (rntis | start | stop | status | trace | dapp | moni) — xapp_ppo/README.md
├── all.sh                        전체 up | down | status (core → RIC → L1+gNB → VA → non-VA [→ xApp])
├── logs.sh                       읽기 쉬운 실시간 로그 (gnb | mac | l1 | dapp | va | nonva | ric | xapp | ppo | core | ue | all)
├── dash.sh                       실험 대시보드 on/off (start | stop | status | logs | list | sim), http://10.150.21.161:8010/ — dash/README.md
├── ue_table.py                   UE 표: RNTI·IMSI·IP·서비스 (xapp.sh의 RNTI 자동 매핑에 사용)
├── .env                          모든 설정 (이미지, 빌드 디렉터리, 코어 수, dApp 파라미터, VA_* , RU/코어)
├── docker-compose.yaml           서비스 l1(dgist-l1), gnb(dgist-gnb)
├── config/                       cuphycontroller_dgist.yaml, l2_adapter_dgist.yaml, gnb.conf, flexric_xapp.conf
├── scripts/                      l1_entrypoint.sh(컨테이너 안), build_l1.sh, build_oai_image.sh, core.sh
├── core/                         routing.sh, routing_restore.sh (hsw 연구실 스크립트, UE NAT)
├── docs/                         CHANGES_HSW_OAI.md, CHANGES_DGIST_AERIAL.md, patches/*.patch
├── runs/<이름>/                  실행 로그 (l1_console, phy_warn, l1_persec, phy_tail, dapp_tenant.log/csv, oai.log, l1_env.txt)
├── runs/va_service/              VA 서비스 uvicorn 로그 + pid;  runs/va_service_log/  세션별 지연 CSV (전화기·ue_sim·가상 UE)
├── runs/dash/<이름>_<시각>/      대시보드 측정 기록 (replay.jsonl, summary, timeseries.csv, frames.csv, slots.csv.gz);  runs/dash_service/  서버 로그
├── dash/                         실험 대시보드: 측정 시작/종료, 실시간 그래프(UE·서비스·GPU 슬롯·PRB·무선), Replay. 스택에 대해 읽기 전용, UE 망 차단 — dash/README.md
├── va_service/                   VA 서비스: hsw yolo_service(앱·인증서·텔레메트리·venv 8.4 GB) + dapp_backend.py (추론을 dApp RUN으로) — va_service/README.md
├── nonva_service/                non-VA 서비스: hsw video_stream_service 그대로(720p 20 fps 업로드 → /viewer 중계, 추론 없음), venv는 va_service 것 공유 — nonva_service/README.md
├── xapp_ppo/                     hsw PPO 묶음 복사: 6G 20입력(기본, YOLO 모델·SM을 우리 dApp에서 받음) + 19입력 GPU-util + venv 798 MB — xapp_ppo/README.md
├── flexric_sm/                   우리 FlexRIC 빌드의 E2 서비스 모델 .so (gNB 컨테이너에 /usr/local/lib/flexric로 마운트; ric.sh install-sm)
├── core/etc/                     Open5GS 설정 복사본(/etc/open5gs, 수정용; core.sh config|diff|install-config)
├── open5gs/                      Open5GS v2.8.0 소스(설치판과 같은 버전; core.sh build — 빌드 의존 패키지 설치 필요)
├── aerial-cuda-accelerated-ran/  DGIST Aerial 트리 + build.aarch64_f1002 + 모델/엔진 (약 12 GB)
└── openairinterface5g/           hsw OAI 트리 + DGIST UCI hunk (gNB 이미지의 docker build context, 약 2.2 GB)
```

## 3. 사전 조건

- nvidia 계정: `docker` 그룹(있음). `sudo`는 `core` 명령(systemctl, iptables)에만 쓰인다.
- 이미지: `nvcr.io/nvidia/aerial/aerial-cuda-accelerated-ran:25-3-cubb`(있음), `dgist-oai-gnb-aerial:latest`(2026-11-05 15:30 빌드, E2 agent 포함; 다시 만들 때 `./dgist.sh build-oai`, 베이스 `ran-base:latest` 필요).
- **같은 GPU/NIC에 L1은 하나만.** 연구실 스택(`nv-cubb`, `oai-gnb-aerial_hsw`)이나 다른 `phy_main`이 떠 있으면 `up`이 거부한다. 먼저 그쪽을 내릴 것
  (`cd /home/nvidia/hsw/25_3_hsw/openairinterface5g/ci-scripts/yaml_files/sa_gnb_aerial && docker compose down`).
- GPU MIG 꺼짐, MPS 꺼짐(L1 entrypoint가 끈다 — green context는 MPS와 함께 쓰지 않음).
- Open5GS가 떠 있어야 한다 (`./dgist.sh core status` → 18/18). 다른 호스트의 코어를 쓰면 `gnb.conf`의 `amf_ip_address`를 바꾸고 `NO_CORE=1 ./dgist.sh up`.
- 전화기 가입자: Open5GS WebUI(:9999)에 등록된 PLMN 999/70 SIM (연구실이 쓰던 것).

## 4. 명령어

| 명령 | 동작 |
|---|---|
| `./dgist.sh check` | 호스트 사전 점검만 (이미지·바이너리·엔진·설정·다른 L1·MPS·GPU 프로세스·hugepage·PTP·프런트홀 링크·RU ping·코어·디스크) |
| `./dgist.sh up [이름]` | 점검 → `docker compose up -d` (L1 → healthy → gNB) → cell 0 CONFIG/START, NG Setup 확인 → 결과 출력. 로그는 `runs/<이름>/` (기본 이름 `dgist_MMDD_HHMM`) |
| `./dgist.sh down` | gNB 로그 저장 → `docker compose down` (gNB 먼저, L1은 SIGTERM 받고 phy_warn/persec/tail/dApp 로그를 저장) → 요약(Err.ind/FATAL, RRC/PDU 수) |
| `./dgist.sh status` | 컨테이너·RAN 프로세스·MPS·코어·agent 포트·GPU·cell 0 트래픽 줄·UE 카운터 |
| `./dgist.sh logs [l1\|gnb]` | 실시간 로그 (기본 gnb) |
| `./dgist.sh ue [이름]` | UE attach 단계표 (NG Setup → RA → Msg3 → RRCSetupComplete → Reconfiguration → PDU session, 실패 신호) |
| `./dgist.sh test [모델] [req/s] [초]` | xApp 스모크 테스트: dgist-l1 안의 `ra_stub.py`가 4K JPEG을 agent에 보냄 (기본 26x 2 15) |
| `./dgist.sh core up\|down\|status` | Open5GS + UE NAT (sudo) |
| `./core.sh up\|down\|status` | 같은 것 (코어 on/off 전용 스크립트; nvidia 계정은 sudo 비밀번호를 묻는다) |
| `./va.sh start\|stop\|status\|logs` | VA 서비스 on/off: `va_service/yolo_env` 활성화 → `uvicorn app:app --host 0.0.0.0 --port 8000 --ssl-*` 백그라운드. 전화기는 `https://10.45.0.1:8000/`. 설정 `.env`의 `VA_*` (4K, 26x 고정, UE별 5 req/s, QoS 200 ms) |
| `./va.sh test [ue] [n] [req/s]` / `test2` | 전화기 대역(`ue_sim.py`)으로 4K 프레임을 보내 RUN→reply 경로와 CSV를 확인 (`test2` = ue1+ue2 동시) |
| `./va.sh bg start\|stop\|status` | 서비스 안의 가상 UE(hsw bg_yolo)로 dApp 경로에 부하 추가 |
| `./dgist.sh va ...` | `./va.sh ...`와 같음 |
| `./nonva.sh start\|stop\|status\|logs` | non-VA 서비스 on/off: 다른 전화기가 `https://10.45.0.1:8001/`에서 720p·20 fps 프레임을 올리고, 서버가 `https://10.45.0.1:8001/viewer/ue2`로 라이브 중계. 추론 없음, UL 지연 CSV, 텔레메트리 `nonva` (hsw PPO xApp 입력) |
| `./nonva.sh test [ue] [n] [fps]` | 전화기 대역(`nonva_sim.py`)으로 업로드 경로와 CSV 확인 |
| `./dgist.sh nonva ...` | `./nonva.sh ...`와 같음 |
| `./ric.sh start\|stop\|status\|build` | FlexRIC nearRT-RIC(우리 빌드 `build_dgist`, 127.0.0.2). **gNB보다 먼저** 올리고 gNB가 도는 동안 끄지 말 것(OAI E2 agent가 RIC 소실 시 gNB를 abort) — `stop`이 그 경우 거부(`--force`) |
| `./xapp.sh rntis\|start\|stop\|status\|trace\|dapp` | hsw GPU-aware PPO 슬라이싱 xApp + dApp 피드 (`xapp_ppo/README.md`) |
| `./all.sh up\|down\|status` | 전체를 순서대로 |
| `./logs.sh <대상>` | 색·필터가 들어간 실시간 로그 (`--raw`로 원문) |
| `./dash.sh start\|stop\|status\|logs\|list` | 실험 대시보드 on/off: PC에서 `http://10.150.21.161:8010/` → "측정 시작"(이름) / "측정 종료"(요약), 기록 `runs/dash/`, 같은 페이지에서 Replay. 스택과 따로 돈다 (`dash/README.md`) |
| `./dash.sh sim [ours\|baseline\|stop]` | 연습용 인스턴스(포트 8011): 가짜 숫자로 같은 화면·측정·Replay |
| `./core.sh config\|diff\|install-config\|build\|subs` | 코어 설정 복사본·소스 빌드·가입자 목록 (sudo) |
| `./dgist.sh build-l1 [타겟]` | 임시 Aerial 컨테이너에서 `ninja` 증분 빌드 (`FULL=1` 재구성 약 1시간, `ALL_CPUS=1` 전체 코어; 기본은 `BUILD_CPUS`=0-3,65-71) |
| `./dgist.sh build-oai` | `openairinterface5g/`로 gNB 이미지 재빌드 (11코어 약 8분; `ALL_CPUS=1` 가능) |

## 5. 표준 절차

```bash
cd /home/nvidia/_dgist
./dgist.sh core status            # Open5GS: 18/18 units active, ogstun 10.45.0.1  (아니면 ./dgist.sh core up)
./dgist.sh check                  # == ready
./dgist.sh up demo1               # L1 is ready → dApp tenant ready → gNB → cell 0 CONFIG/START done → NG Setup with the AMF ok
./va.sh start                     # VA 서비스 (전화기 → 4K 프레임 → dApp RUN → reply): agent connected 가 보이면 정상
./nonva.sh start                  # non-VA 서비스 (다른 전화기의 라이브 스트리밍, 추론 없음) - hsw 두-슬라이스 실험 구도(ue1 VA, ue2 non-VA)
#   전화기를 켜서 붙인다. 확인: ./dgist.sh ue   (RRCSetupComplete, PDU session 카운트)
#   전화기 브라우저 https://10.45.0.1:8000/ → UE1 선택 → Start Camera 또는 Video File(폰에 저장된 동영상) → Start VA (Stop VA에서 CSV 저장: runs/va_service_log/)
./va.sh test ue1 25 5             # 전화기 없이 경로 확인: 25/25 success, QoS met 100 %, dApp sm 80 / inf 약 33 ms 이면 정상
./dgist.sh test 26x 2 15          # dApp 자체: sm 80, 서버 약 38 ms 이면 정상
./dgist.sh status                 # VA 서비스 줄 포함; 자세히는 ./va.sh status
./va.sh stop; ./nonva.sh stop; ./dgist.sh down     # runs/demo1/ 에 로그
./core.sh down                    # 코어까지 내릴 때 (보통은 켜 둔다)
```
FlexRIC xApp까지 쓰려면 nearRT-RIC → `up` → xApp 순서 (`aerial-cuda-accelerated-ran/cuPHY-CP/dapp_hook/xapp_flexric/README.md`).

## 6. 로그 (`runs/<이름>/`)

`entrypoint.log`(기동/정지 단계), `l1_console.log`(cuphycontroller 콘솔), `phy_warn.log`(WRN/ERR/FATAL·Err.ind·CONFIG/START·RACH 발췌), `l1_persec.log`(cell 0 초당 DL/UL 줄),
`phy_tail.log`(nvlog 마지막 200만 줄), `dapp_tenant.log`·`dapp_tenant_slots.csv`(테넌트·슬롯별 SM 기록: YOLO에 제공된 틈 `gap_P/gap_N`, 추론 중 점유 SM `run_cls`, 큐, 벽시계; `./dgist.sh slotmap <name>`으로 GPU Slot Map 영상), `oai.log`(gNB 전체 로그), `l1_env.txt`, `compose_up.log`.
정상: Err.ind 0, FATAL 0. 시작 직후 WRN 2건 "Incorrect msg length"(OAI CONFIG/START.request가 8바이트 긴 것)와 "YAML invalid key: ul_*" 5건은 무해하다.

## 7. 빌드

- **L1**: 트리는 컨테이너 안 `/opt/nvidia/cuBB`에 마운트되고 빌드 디렉터리 `build.aarch64_f1002`의 경로가 그대로 맞아 `ninja`가 증분으로 동작한다
  (2026-11-05 복사 직후 `ninja -n` → "no work to do"). 플래그: `SCF_FAPI_10_04=OFF ENABLE_CONFORMANCE_TM_PDSCH_PDCCH=OFF SCF_FAPI_10_04_SRS=ON ENABLE_DAPP_HOOK=ON`
  — `ENABLE_CONFORMANCE_TM_PDSCH_PDCCH`가 ON이면 OAI의 DL_TTI.request가 잘못 해석되어 SSB/SIB1이 나가지 않는다. 빌드물 소유자는 스크립트가 nvidia로 돌려 놓는다.
- **OAI gNB 이미지**: `openairinterface5g/docker/Dockerfile.gNB.aerial.ubuntu.dgist` = 트리의 `Dockerfile.gNB.aerial.ubuntu`(ran-base → ran-build(nvipc + build_oai -w AERIAL --gNB --ninja) → ubuntu:noble 런타임)에 **`--build-e2`(E2 agent, E2AP_V2/KPM_V2_03)**만 더한 것. 원본 Dockerfile은 E2 agent를 빌드하지 않는다 — 연구실 이미지들은 호스트에서 `--build-e2`로 빌드한 `cmake_targets/ran_build/build/nr-softmodem`을 `Dockerfile.gNB.aerial.ubuntu.oai_gnb`로 포장한 것이고, 이 변형은 같은 바이너리(크기 158,725,912 B, `hybrid-uci`와 동일)를 docker build 한 번으로 만든다.
  legacy builder(`DOCKER_BUILDKIT=0`)를 쓰는 이유는 `--cpuset-cpus`로 컴파일을 격리 코어(4-64, 실시간 스레드) 밖에 묶기 위함.
- **TensorRT 엔진**: `cuPHY-CP/dapp_hook/models/full26*/seg*_sm*.engine`은 이 GPU(GH200)·이 컨테이너 이미지의 TensorRT로 만든 것. 이미지가 바뀌면 `models/build_engines.sh`로 재생성.
- **xApp 예제**: `cuPHY-CP/dapp_hook/xapp_flexric/` (`cmake -S . -B build`, 연구실 FlexRIC 트리에 링크; 빌드물 포함).

## 8. 두 연구실 변경점

`docs/CHANGES_HSW_OAI.md`(hsw: MAC 스케줄러·E2 agent·FlexRIC), `docs/CHANGES_DGIST_AERIAL.md`(DGIST: 기능 → 파일), 패치 원본 `docs/patches/`.

## 9. 검증 기록

- 2026-11-05 15:05-15:31 (troy가 nvidia 계정 경로에 설치): Aerial 트리 12 GB 복사 후 임시 컨테이너에서 `ninja -n` → "no work to do"(빌드물 유효);
  OAI 트리 복사 + UCI hunk → `git diff`가 검증된 `oai-gnb-aerial_hsw:hybrid-uci` 소스와 동일; 이미지 `dgist-oai-gnb-aerial:latest`(715 MB) 빌드 —
  첫 빌드는 원본 Dockerfile이라 E2 agent가 빠져 있었고(`e2_agent` 문자열 0), `.dgist` 변형으로 재빌드해 E2 포함 확인(`e2_agent` 17, `E2_SLICE_ACCEPT` 1,
  `NR_UL_PRB_COUNT_V1` 2, UCI hunk 1, ldd 누락 0, nr-softmodem 158,725,912 B = hybrid-uci와 같은 크기). nvidia 계정으로 `docker compose config`,
  `./dgist.sh check` 동작 확인.
- **실행 기록 2026-11-05 15:41- (nvidia 계정, `./dgist.sh up test1`)**: 연구실 스택을 내린 뒤 `up` → L1 컨테이너 Healthy 26 s(L1 is ready, dApp 엔진·CUDA graph 60개
  로드, agent 9810) → gNB 컨테이너 기동 → cell 0 CONFIG/START 15:41:44 → NG Setup with the AMF ok (Open5GS 127.0.0.5). L1 Err.ind 0, FATAL 0, cell 0 DL 0.04 Mbps
  (SIB1 송출, baseline 값), WRN은 yaml 키 기본값 안내만. gNB 로그에 hsw MAC 변경 활성 `[UL_TOTAL_PRB_CAP] enabled cap=273 ... E2 count quotas supported`.
  `./dgist.sh test 26x 2 15` → 27/27 성공, 서버 38.3 ms(디코드 5.9, 추론 32.3), E2E 38.9 ms, sm 80.
  E2 경로: 연구실 nearRT-RIC(루프백) 기동 → gNB E2 agent 등록(nb_id 3584, PLMN 999/70, RAN functions 2 3 142 142 143-146 148) → nvidia 계정으로
  `xapp_flexric/build/xapp_dapp_run --model auto --count 5` → MAC SM 142 구독 ok, RUN 5/5 성공 서버 38.4 ms, 구독 삭제·정지 정상 → RIC 종료(gNB는 재시도만 하며 영향 없음).
  **UE attach (15:48:56, 전화기 켬)**: RA(Msg1) → Msg3/CBRA 성공 → RRCSetupComplete → RRCReconfigurationComplete(15:49:00) → PDU Session 5 추가.
  코어: AMF `imsi-999700000177998 Registration complete`, SMF/UPF 세션 `DNN internet IPv4 10.45.0.2`. 호스트에서 UE로 ping 5/5 성공(RTT 11-285 ms), UE UL 약 2 Mbps
  (gNB MAC RX +2.6 MB/10 s), UE 통계 RSRP -73 dBm, SNR 20 dB, UL BLER 0.2-1.2 %, ulsch_errors 4. UE가 붙은 상태에서 `test 26x 2 15` 다시 27/27, 서버 38.6 ms, L1 Err.ind 0.
  `./dgist.sh down` 15:52:51: gNB → L1 순서로 정지·제거, 로그 `runs/test1/`(nvidia 소유), 이후 RAN 프로세스 없음, GPU 39 MiB, MPS 없음.
- **VA 서비스 2026-11-05 17:35-17:42 (`./dgist.sh up va1` + `./va.sh start`, nvidia 계정)**: hsw `yolo_service`를 `va_service/`로 복사(venv 경로 수정)하고
  추론을 dApp RUN으로 바꾼 뒤 전화기 대역 `ue_sim.py`로 확인 — ue1 25프레임 @5 req/s: 25/25 success, QoS 200 ms 충족 100 %, E2E 49.5 ms(UL 7.3 + AI 39.4 +
  기타 0.5 + DL 2.3; 같은 호스트), dApp sm 80, 디코드 5.5, 추론 32.6, 검출 88. ue1+ue2 동시 50프레임씩 @5 req/s: 100/100 success, QoS 100 %, E2E 48.6/48.7 ms,
  큐 대기 0.1 ms. 가상 UE 5 fps를 더해도 ue2 20/20 success. L1 Err.ind 0, FATAL 0, 에이전트 ok 103 fail 0. CSV·텔레메트리 JSON 정상. 자세히 `va_service/README.md`.

## 10. 2026-11-05 밤: PPO xApp 포팅 (완료)

- FlexRIC 빌드(`build_dgist`), PPO 묶음 복사, dApp 에이전트 `sm_report.last` + `dapp_reader.py`, `ric.sh`/`xapp.sh`/`all.sh`/`logs.sh`/`ue_table.py`, Open5GS 소스·설정 복사.
- 중간에 gNB가 첫 PDU 세션에서 계속 죽은 원인은 OAI NGAP 핸들러의 NULL 참조(optional NAS-PDU; gNB만 재시작하면 UE가 Service Request로 재활성화)였고
  `docs/patches/oai_pdusession_nas_null.patch`로 고쳐 이미지를 재빌드했다. 자세한 경위·교훈은 `xapp_ppo/README.md`.
- 21:40 검증: RIC → gNB(E2 SETUP) → 4 UE attach → `xapp.sh start` READY → 100 ms 결정 → gNB `E2_SLICE_APPLIED` 확인.

## 11. 주의

- 연구실 트리 `/home/nvidia/hsw/…`는 읽기만 한다. 이 디렉터리의 `openairinterface5g/`가 수정·빌드용 복사본이다.
- 코어 배치: L1 4-11, dApp 43/45/46, gNB 56-62, 빌드 0-3,65-71. 연구실 스택이 쓰던 13-18은 비워 둔다.
- 컨테이너 안에서 root가 쓰는 파일(설정 복사본, 빌드물, 로그)은 스크립트가 트리 소유자(nvidia)로 돌려 놓는다.
- GPU 사용 시간은 연구실 규칙(평일 18:00 이후는 연구원 우선)을 따른다.
- VA 서비스의 4K q0.90 프레임은 약 2.3 MB라 UE당 5 req/s면 UL 약 92 Mbps다. 1셀 UL 용량을 넘으므로 실제 전화기에서는 UL 지연이 E2E를 지배하고
  QoS 200 ms를 넘길 수 있다(hsw 측정: 1080p 435 KB에서도 UL 213 ms). 해상도를 유지하며 줄이려면 `.env`의 `VA_JPEG_QUALITY`·`VA_TARGET_FPS`를 낮춘다.
- `core.sh`/`dgist.sh core`는 sudo가 필요해 nvidia 계정에서는 비밀번호를 묻는다. VA 서비스(`va.sh`)와 L1/gNB(`dgist.sh up/down`)는 sudo 없이 된다.
- `ue_table.py`/`xapp.sh rntis`의 IMSI·IP 매핑은 systemd 저널(AMF/SMF)을 읽는다. nvidia 계정은 저널 그룹이 아니어서 비어 나온다: `sudo usermod -aG systemd-journal nvidia` 한 번(관리자) 또는 RNTI를 직접 지정.
