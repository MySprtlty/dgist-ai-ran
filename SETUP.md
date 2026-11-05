# SETUP.md — 다른 GH200 서버에 같은 스택 만들기 (2026-11-06)

저장소: **https://github.com/MySprtlty/dgist-ai-ran** — `/home/nvidia/_dgist`의 git 작업 트리. 운용(켜기/끄기)은 `MANUAL.md`, 구조·명령·검증 기록은 `README.md`.
이 문서는 **빈 서버에서 클론 → 빌드 → 실행**까지의 순서와, 저장소에 들어 있지 않은 것(생성물)을 어떻게 다시 만드는지를 적는다.

## 0. 무엇이 추적되고 무엇이 생성되는가

| 구성요소 | 저장소에서 | 호스트에서 만드는 법 |
|---|---|---|
| 운용 스크립트·설정 (`*.sh`, `.env`, `docker-compose.yaml`, `config/`, `core/etc/*.yaml`, `docs/`, `MANUAL.md`) | 추적 | — |
| VA / non-VA 서비스 (`va_service/`, `nonva_service/` 코드, `requirements.txt`) | 추적 | venv: `scripts/setup_venvs.sh`, 인증서: `scripts/make_certs.sh` |
| PPO 슬라이싱 xApp Python 번들 (`xapp_ppo/*`, 학습된 `actor.pt` 포함) | 추적 | venv: `scripts/setup_venvs.sh` |
| Aerial cuPHY (FAPI-Aware Scheduler L1) `aerial-cuda-accelerated-ran/` | **서브모듈**(포크 `MySprtlty/aerial-cuda-accelerated-ran`, GitHub에 있는 커밋 `aerial/BASE_COMMIT`) **+ 패치 묶음 `aerial/`**(그 이후 우리 변경분; 포크에는 푸시하지 않음) | `./aerial/apply_patches.sh` → 빌드: `./dgist.sh build-l1` (컨테이너 안 Ninja, `build.aarch64_f1002/`) |
| OAI gNB + FlexRIC (연구실 MAC 쿼터 스케줄러·E2 agent·xApp) `openairinterface5g/` | **패치 묶음** `oai/` (업스트림 커밋 고정 + 변경분) | `./oai/fetch_oai.sh` → 이미지: `./dgist.sh build-oai`, RIC/xApp: `./ric.sh build` |
| Open5GS 소스 `open5gs/` (v2.8.0) | **서브모듈** (업스트림) | 실행은 apt 패키지; 소스 빌드는 선택(`./core.sh build`) |
| YOLO 가중치·ONNX·TensorRT 엔진 (`models/full26*/seg00_sm<N>.engine`) | 없음 | `scripts/build_yolo_engines.sh` (~3 h) |
| Docker 이미지 (`nvcr.io/.../aerial-cuda-accelerated-ran:25-3-cubb`, `ran-base:latest`, `dgist-oai-gnb-aerial:latest`) | 없음 | NGC pull / `docker build` (§3) |
| 실행 로그 `runs/`, `log/`, E2 SM 라이브러리 `flexric_sm/`, 개인키 `*/certs/`, `core/etc/tls/*.key` | 없음 (.gitignore) | 실행·빌드 때 생성 |
| 전화기 SIM 가입자(K/OPc) | 없음 | Open5GS WebUI(:9999)에 등록; IMSI는 `.env VA_IMSIS/NONVA_IMSIS` |

## 1. 호스트 사전 조건 (한 번)

1. **GH200 + Aerial 25-3 호스트 설정**: NVIDIA *Aerial CUDA-Accelerated RAN 25-3 Installation Guide*대로 — 커널 부트 매개변수·hugepages, DOCA/OFED, ConnectX-7 프런트홀 인터페이스(여기서는 `aerial00`, `.env FH_IF`), PTP(`ptp4l`/`phc2sys`, RU와 동기), NVIDIA 드라이버(open kernel modules), Docker + nvidia-container-toolkit. RU(Benetel)의 IP/MAC/VLAN은 `.env RU_IP`와 `config/cuphycontroller_dgist.yaml`.
2. **NGC**: `docker login nvcr.io` (NGC API key) 후 `docker pull nvcr.io/nvidia/aerial/aerial-cuda-accelerated-ran:25-3-cubb`.
3. **Open5GS 2.8.0** (apt: open5gs PPA) + MongoDB + WebUI(`http://<host>:9999`). 설정은 이 저장소의 `core/etc/*.yaml`을 `./core.sh install-config <nf>`로 설치(PLMN 999/70, UE 서브넷 10.45.0.0/16 = `ogstun 10.45.0.1`, AMF/UPF 주소는 yaml 확인).
4. **빌드 도구**: `git cmake ninja-build build-essential python3.10 python3.10-venv openssl`; FlexRIC: `libsctp-dev libpcre2-dev flex bison python3-dev` (FlexRIC README 참고; sqlite3는 번들). Open5GS 소스 빌드(선택)는 `./core.sh build`가 필요한 패키지를 출력한다.
5. **계정**: 모든 것은 **nvidia 계정**으로, `/home/nvidia/_dgist`에서 실행한다(스크립트가 root 실행을 거부함). `core.sh`만 sudo를 쓴다. 저널 읽기(`ue_table.py`)는 `sudo usermod -aG systemd-journal nvidia`.

## 2. 클론

```bash
sudo -u nvidia -H bash
git lfs install                                                                           # Aerial의 testVectors/*.h5 는 git-lfs 객체
git clone --recursive https://github.com/MySprtlty/dgist-ai-ran.git /home/nvidia/_dgist   # 서브모듈: aerial(포크 커밋), open5gs(v2.8.0)
cd /home/nvidia/_dgist
./aerial/apply_patches.sh     # 포크 커밋 이후의 DGIST 변경분(aerial/local.patch + files/) 적용 → 이 서버의 Aerial 트리와 동일
./oai/fetch_oai.sh            # OAI 87c016b + FlexRIC ef6d722f 얕은 클론 + 연구실/DGIST 패치 → ./openairinterface5g (약 1분)
vi .env                       # 호스트에 맞게: FH_IF, RU_IP, BUILD_CPUS와 CPU 코어 지정, VA_IMSIS/NONVA_IMSIS(전화기 SIM), SM 클래스(DAPP_TENANT_CLASSES)
```
서브모듈이 비어 있으면 `git submodule update --init --recursive`.

## 3. 빌드 (순서대로, 총 4~5시간 중 대부분은 엔진)

```bash
./dgist.sh check                                  # 사전 점검 (이미지·인터페이스·코어)
./dgist.sh build-l1                               # (1) cuphycontroller_scf + dapp_tenant_main 등, FULL=1 ./dgist.sh build-l1 로 재구성  ~40분
(cd openairinterface5g && docker build -f docker/Dockerfile.base.ubuntu -t ran-base:latest .)   # (2a) OAI 베이스 이미지 (한 번)
./dgist.sh build-oai                              # (2b) dgist-oai-gnb-aerial:latest (E2 agent 포함, nvipc_src 포함)  ~40분
./ric.sh build                                    # (3) FlexRIC nearRT-RIC + xApps (build_dgist) + E2 SM 라이브러리 → flexric_sm/ (install-sm 포함)
./scripts/setup_venvs.sh                          # (4) va_service/yolo_env, xapp_ppo venv
./scripts/make_certs.sh                           # (5) HTTPS 인증서 CN=10.45.0.1 (전화기에 ca.crt 설치 또는 경고 수락)
./scripts/build_yolo_engines.sh                   # (6) YOLO26 n/s/m/l/x × SM 클래스(.env DAPP_TENANT_CLASSES) 엔진, GPU 비어 있어야 함  ~3시간
for nf in amf smf upf nrf ausf udm udr pcf nssf bsf scp; do ./core.sh install-config $nf; done   # (7) 코어 설정 (라이브 /etc/open5gs 백업 후 교체)
```
가입자: WebUI에 IMSI(`.env`의 네 개)·K·OPc 등록. 기존 서버에서 옮길 때는 그 서버의 MongoDB에서 내보낸다(이 저장소에는 없음).

## 4. 실행·검증

```bash
./all.sh up demo1          # 코어 → RIC → L1+gNB → VA → non-VA (MANUAL.md §3)
./all.sh status ; ./dgist.sh ue ; ./logs.sh gnb
./dgist.sh test 26x 4 20   # 전화기 없이 dApp RUN 스모크(4 req/s, 20 s)
./xapp.sh start ; ./xapp.sh moni start   # 전화기 4대가 붙은 뒤
./all.sh down
```
주의: RIC는 gNB보다 먼저, gNB가 도는 동안 RIC를 내리지 않는다(`MANUAL.md §2`). root로 스크립트를 실행하지 않는다.

## 5. 개발 흐름 (어디를 고치고 어떻게 커밋하나)

| 고치는 것 | 위치 | 빌드 | 커밋 |
|---|---|---|---|
| 스크립트·설정·서비스·xApp Python | `_dgist` 최상위 | — / `./va.sh restart` 등 | 이 저장소에 바로 `git commit && git push` |
| cuPHY / FAPI-Aware Scheduler / dApp 테넌트 | `aerial-cuda-accelerated-ran/` | `./dgist.sh build-l1` | `./aerial/update_patches.sh` → `git add aerial && git commit` (포크에는 푸시하지 않음; 포크를 올리는 날에는 `aerial/BASE_COMMIT`을 그 커밋으로 바꾸고 패치를 재생성) |
| OAI gNB / FlexRIC xApp | `openairinterface5g/` | `./dgist.sh build-oai`, `./ric.sh build` | `./oai/update_patches.sh` → `git add oai && git commit` |
| Open5GS 소스 | `open5gs/` | `./core.sh build` | 수정 시 포크를 만들어 서브모듈 URL 변경 |
| Open5GS 설정 | `core/etc/*.yaml` | `./core.sh install-config <nf>` | 이 저장소 |

버전 기록: Aerial 25-3(커밋 654c249) 기반 포크 `hybrid` 커밋 `aerial/BASE_COMMIT` + `aerial/local.patch`; OAI `87c016b5` + FlexRIC `ef6d722f`(`oai/README.md`); Open5GS v2.8.0; 변경 요약 `docs/CHANGES_DGIST_AERIAL.md`, `docs/CHANGES_HSW_OAI.md`, 개별 패치 `docs/patches/`.
