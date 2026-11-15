# SETUP.md — 다른 GH200 서버에 같은 스택 만들기 (2026-11-15 개정, 브랜치 `demo-slot-level`)

저장소: **https://github.com/MySprtlty/dgist-ai-ran** (브랜치 `demo`, `demo-slot-level`). 운용(켜기 / 끄기 / 모드 전환)은 `MANUAL.md`.
이 문서는 **빈 서버에서 클론 → 빌드 → 실행**까지의 순서와, 저장소에 들어 있지 않은 것(생성물)을 다시 만드는 방법을 적는다.

- 이 저장소 하나와 그 서브모듈만 있으면 된다. 다른 서버의 디렉터리(연구실 트리, 개발용 트리)를 읽거나 마운트하지 않는다.
- 클론하는 **디렉터리와 계정은 자유**다. 아래에서 `<패키지>` = 클론한 디렉터리. 스크립트는 자기 위치를 기준으로 동작하고, `.env`의 경로 값은 `<패키지>` 기준 상대 경로다.
- 스크립트는 `<패키지>`를 가진 계정으로 실행한다(root / sudo로 실행하면 거부한다). sudo가 필요한 것은 `core.sh`(Open5GS 서비스)와 `scripts/mig.sh`(GPU 분할) 둘뿐이다.

| 브랜치 | 내용 |
|---|---|
| `demo` | 2026-11-14에 돌린 구성: Ours / Baseline 모드 전환, VA · non-VA 서비스, 뷰어, 대시보드, 코어, RIC · xApp, 5셀 트래픽 생성기. YOLO는 "추론 한 번에 SM 등급 하나" |
| `demo-slot-level` (이 문서) | `demo` + 슬롯 단위 SM 전환(추론 하나의 조각마다 SM 등급을 바꾼다. `MANUAL.md` 1.2절 7번). 그 밖은 `demo`와 같다 |

두 브랜치는 같은 이름의 Aerial 포크 브랜치(`MySprtlty/aerial-cuda-accelerated-ran`의 `demo`, `demo-slot-level`)를 서브모듈로 가리킨다.

## 0. 무엇이 추적되고 무엇이 생성되는가

| 구성요소 | 저장소에서 | 호스트에서 만드는 법 |
|---|---|---|
| 운용 스크립트 · 설정 (`*.sh`, `scripts/`, `.env`, `docker-compose*.yaml`, `config/`, `core/etc/*.yaml`, `docs/`, `MANUAL.md`) | 추적 | — |
| VA / non-VA 서비스, 뷰어 (`va_service/`, `nonva_service/`) | 추적 | venv: `scripts/setup_venvs.sh`, 인증서: `scripts/make_certs.sh` |
| 대시보드 (`dash/`, `dash.sh`) | 추적 | VA 서비스의 venv와 인증서를 같이 쓴다 |
| PPO 슬라이싱 xApp Python 번들 (`xapp_ppo/*`, 학습된 `actor.pt` 포함) | 추적 | venv: `scripts/setup_venvs.sh` |
| Aerial cuPHY (L1, YOLO 테넌트, RU 에뮬레이터, testMAC) `aerial-cuda-accelerated-ran/` | **서브모듈** = 포크의 같은 이름 브랜치 | 빌드: `./dgist.sh build-l1` (컨테이너 안 Ninja, `build.aarch64_f1002/`). Ours와 Baseline이 **같은 빌드**를 쓴다 |
| OAI gNB + FlexRIC (MAC 쿼터 스케줄러 · E2 agent · xApp) `openairinterface5g/` | **패치 묶음** `oai/` (업스트림 커밋 고정 + 변경분) | `./oai/fetch_oai.sh` → 이미지: `./dgist.sh build-oai`, RIC / xApp: `./ric.sh build` |
| Open5GS 소스 `open5gs/` (v2.8.0) | **서브모듈** (업스트림) | 실행은 apt 패키지. 소스 빌드는 선택(`./core.sh build`) |
| YOLO 가중치 · ONNX · TensorRT 엔진 (슬롯 단위 전환용 `models/fhd132_26*/`, 등급별 `models/fhd26*/` · `models/full26*/`, Baseline용 `models/migfhd26*/`) | 없음 | `scripts/build_yolo_engines.sh fhd132`(등급별은 `fhd` · `4k`), `scripts/build_yolo_engines_mig.sh` (3절, 4절) |
| 트래픽 생성기의 테스트 벡터 (187개 파일, 0.85 GB) | 없음. 파일별 sha256 목록만 추적(`config/tgen/testvectors.sha256`) | `scripts/fetch_testvectors.sh` (5절) → `tgen/testVectors/` |
| Docker 이미지 (`nvcr.io/nvidia/aerial/aerial-cuda-accelerated-ran:25-3-cubb`, `ran-base:latest`, `dgist-oai-gnb-aerial:latest`) | 없음 | NGC pull / `docker build` (3절) |
| 실행 로그 `runs/`, `log/`, 생성기 전체 로그 `tgen/log/`, E2 SM 라이브러리 `flexric_sm/`, 개인키 `*/certs/`, `core/etc/tls/*.key` | 없음 (.gitignore) | 실행 · 빌드 때 생성 |
| 전화기 SIM 가입자(K / OPc) | 없음 | Open5GS WebUI(:9999)에 등록. IMSI는 `.env VA_IMSIS / NONVA_IMSIS` |

## 1. 호스트 사전 조건 (한 번)

1. **GH200 + Aerial 25-3 호스트 설정**: NVIDIA *Aerial CUDA-Accelerated RAN 25-3 Installation Guide*대로 — 커널 부트 매개변수(코어 격리) · hugepages, DOCA / OFED, ConnectX-7 프런트홀 인터페이스, PTP(`ptp4l` / `phc2sys`, RU와 동기), NVIDIA 드라이버(open kernel modules), Docker + nvidia-container-toolkit.
2. **NGC**: `docker login nvcr.io` (NGC API key) 후 `docker pull nvcr.io/nvidia/aerial/aerial-cuda-accelerated-ran:25-3-cubb`.
3. **Open5GS 2.8.0** (apt: open5gs PPA) + MongoDB + WebUI(`http://<서버 IP>:9999`). 설정은 이 저장소의 `core/etc/*.yaml`을 `./core.sh install-config <nf>`로 설치한다(PLMN 999/70, UE 서브넷 10.45.0.0/16 = `ogstun 10.45.0.1`. AMF / UPF 주소는 yaml에서 확인).
4. **도구**: `git git-lfs cmake ninja-build build-essential python3.10 python3.10-venv openssl curl xz-utils`. FlexRIC: `libsctp-dev libpcre2-dev flex bison python3-dev`(FlexRIC README 참고. sqlite3는 번들). Open5GS 소스 빌드(선택)는 `./core.sh build`가 필요한 패키지를 알려 준다.
5. **계정**: 스택을 돌릴 계정 하나를 정한다. `docker` 그룹에 넣고(`sudo usermod -aG docker <계정>`), 저널을 읽을 수 있게 한다(`sudo usermod -aG systemd-journal <계정>`. `ue_table.py`가 Open5GS 저널에서 전화기를 찾는다).
   그 계정에 sudo가 없어도 된다. 그 경우 `core.sh`와 `scripts/mig.sh`만 sudo가 있는 계정에서 실행한다.
6. **디스크**: 처음 서버의 실측으로 엔진 · ONNX 12 GB(4K · FHD · MIG 엔진을 모두 만든 상태. 슬롯 단위 전환용 엔진 다섯 개는 0.24 GB), Python venv 8.4 GB, OAI 소스 · 빌드 2.8 GB, L1 빌드 0.6 GB, 테스트 벡터 0.85 GB. 여기에 Docker 이미지가 더해진다.
   트래픽 생성기를 켠 실행은 전체 로그가 **시간당 26~29 GB**(측정) 쌓인다(5절).

## 2. 클론

```bash
git lfs install                                   # Aerial의 testVectors/*.h5 는 git-lfs 객체다
git clone --recursive -b demo-slot-level https://github.com/MySprtlty/dgist-ai-ran.git <패키지>      # 슬롯 단위 전환이 없는 버전은 -b demo
cd <패키지>
./oai/fetch_oai.sh            # OAI 87c016b + FlexRIC ef6d722f 얕은 클론 + 패치 → ./openairinterface5g (약 1분)
vi .env                       # 서버에 맞게 고친다 (7절)
```

서브모듈이 비어 있으면 `git submodule update --init --recursive`. 다른 브랜치로 옮길 때는 `git checkout <브랜치> && git submodule update --init --recursive` 뒤에 `./dgist.sh build-l1`을 다시 한다(두 브랜치의 L1 소스가 다르다).

## 3. 빌드 (순서대로. 2~3시간. 등급별 엔진 (6')까지 만들면 3시간쯤 더 든다)

```bash
./dgist.sh build-l1                               # (1) cuphycontroller_scf, dapp_tenant_main, dapp_agent_test, ru_emulator, test_mac. 재구성은 FULL=1 ./dgist.sh build-l1   ~40분
(cd openairinterface5g && docker build -f docker/Dockerfile.base.ubuntu -t ran-base:latest .)   # (2a) OAI 베이스 이미지 (한 번)
./dgist.sh build-oai                              # (2b) dgist-oai-gnb-aerial:latest (E2 agent, nvipc_src 포함)  ~40분
./ric.sh build                                    # (3) FlexRIC nearRT-RIC + xApp (build_dgist) + E2 SM 라이브러리 → flexric_sm/
./scripts/setup_venvs.sh                          # (4) va_service/yolo_env (서비스 · 대시보드 · 엔진 내보내기), xapp_ppo venv
./scripts/make_certs.sh                           # (5) HTTPS 인증서 CN=10.45.0.1 (전화기에 ca.crt를 설치하거나 경고를 넘긴다)
./scripts/build_yolo_engines.sh fhd132            # (6) 슬롯 단위 전환용 엔진: YOLO26 n/s/m/l/x 모델마다 하나(132 SM). GPU가 비어 있어야 한다  엔진 하나에 3~8분
./scripts/build_yolo_engines.sh fhd               # (6') 선택: 등급별 엔진(.env DAPP_TENANT_CLASSES_FHD). .env DAPP_TENANT_ENGINE_SM=0 으로 돌릴 때만 필요하다  ~3시간
for nf in amf smf upf nrf ausf udm udr pcf nssf bsf scp; do ./core.sh install-config $nf; done   # (7) 코어 설정 (기존 /etc/open5gs를 백업한 뒤 바꾼다)
./dgist.sh check                                  # (8) 사전 점검: 이미지 · 엔진 · 인터페이스 · PTP · 코어 · 테스트 벡터 · 디스크
```

- (6)은 가중치 `yolo26{n,s,m,l,x}.pt`를 ultralytics로 내려받아(인터넷 필요) ONNX로 내보내고, Aerial 컨테이너 안에서 엔진을 만든다. `fhd132`는 MPS 없이
  GPU의 132 SM이 모두 보이는 상태에서 모델마다 엔진 하나를 만든다(`models/fhd132_26<m>/seg00_sm132.engine`). 숫자는 GPU의 SM 수와 같아야 하고(GH200 = 132),
  다르면 스크립트가 멈춘다. `.env DAPP_TENANT_ENGINE_SM`도 같은 숫자여야 한다.
  **이 프로파일은 이 패키지의 스크립트로 아직 실행해 본 적이 없다**(측정에 쓴 엔진은 같은 `trtexec` 명령을 손으로 실행해 만들었다).
- (6')의 등급별 엔진은 `.env DAPP_TENANT_ENGINE_SM=0`으로 브랜치 `demo`의 규칙(추론 한 번에 SM 등급 하나)을 쓸 때만 필요하다. 4K 입력(`.env DAPP_INPUT=4k`)에는
  슬롯 단위 전환이 없어서 `./scripts/build_yolo_engines.sh 4k`의 등급별 엔진을 쓴다.
- `./dgist.sh check`는 `.env DAPP_TENANT_ENGINE_SM`이 132이면 `fhd132` 엔진 다섯 개를, 0이면 등급별 엔진을 찾는다.
- 가입자: WebUI에 전화기 네 대의 IMSI(`.env`의 값) · K · OPc를 등록한다. 기존 서버에서 옮길 때는 그 서버의 MongoDB에서 내보낸다(이 저장소에는 없다).

## 4. Baseline 모드 준비 (GPU를 MIG로 나눠 쓰는 비교 대상)

Baseline은 GPU를 MIG 4g(64 SM, L1) + 3g(60 SM, YOLO)로 고정 분할한다. L1은 Ours와 같은 빌드이고 dApp 기능만 끈다. YOLO 엔진만 3g 조각 안에서 따로 만든다.

```bash
./all.sh down                                     # 스택과 GPU가 비어 있어야 한다
sudo <패키지>/scripts/mig.sh on                   # [sudo] MIG를 켜고 4g + 3g 조각을 만든다 (runs/mig.env에 조각 UUID 기록)
./scripts/build_yolo_engines_mig.sh n s m l x     # models/migfhd26<m>/seg00_sm60.engine. 모델 하나에 7~15분. (6)의 ONNX가 있어야 한다
sudo <패키지>/scripts/mig.sh off                  # Ours로 돌아갈 때
```

모드 전환 절차는 `MANUAL.md` 2절. 모델 선택 규칙(`.env VA_MODEL_SELECT=1`)을 켠 채 Baseline을 쓰면 가중치가 0이 아닌 모델마다 MIG 엔진이 있어야 하고, 없는 모델의 요청은 `VA_MODEL`로 돈다.

## 5. 트래픽 생성기 준비 (가상 5셀)

`.env TRAFFIC_GEN=1`(기본)이면 L1 컨테이너가 RU 에뮬레이터와 testMAC도 띄워 가상 셀 5개(L1 셀 0~4)를 전화기 셀(L1 셀 5) 옆에서 돌린다. 끄려면 `./tgen.sh off`(전화기 셀만, 테스트 벡터 불필요).

1. **NIC 루프**: 두 번째 NIC의 두 포트(`aerial02` ↔ `aerial03`)를 케이블로 직접 잇는다. 한쪽은 L1, 다른 쪽은 RU 에뮬레이터가 쓴다.
2. **테스트 벡터**: git에 넣기에는 크므로(0.85 GB) 아카이브 하나(`dgist_tgen_testvectors_20261113.tar.xz`, 354 MB)로 옮긴다.

   ```bash
   ./scripts/fetch_testvectors.sh                    # 이 저장소의 릴리스 자산(tgen-testvectors-20261113)에서 내려받아 tgen/testVectors/에 설치
   ./scripts/fetch_testvectors.sh <아카이브 파일>     # 내려받을 수 없으면: 기존 서버에서 파일을 옮겨 와서 설치
   ./scripts/fetch_testvectors.sh verify             # 설치된 187개 파일을 config/tgen/testvectors.sha256과 대조
   ./scripts/fetch_testvectors.sh pack [파일]        # (기존 서버에서) 설치된 세트로 아카이브를 다시 만든다
   ```

   어느 경로로 설치하든 마지막에 파일마다 sha256을 대조한다. 내려받기가 404로 실패하면 릴리스 자산이 아직 올라가지 않은 것이다 — 두 번째 줄의 방법을 쓴다.
3. **로그 공간**: 전체 로그는 `tgen/log/<실행 이름>/`에 쌓인다. 여유가 100 GB보다 적으면 `./dgist.sh check`가 알린다.
   큰 디스크를 쓰려면 클론 직후에 `tgen`을 링크로 만들거나(`mkdir -p /큰디스크/tgen && ln -s /큰디스크/tgen tgen`) `.env TRAFFIC_GEN_TV_DIR / TRAFFIC_GEN_LOG_DIR`에 절대 경로를 적는다.
4. **Baseline + 생성기**는 Benetel 쪽 NIC의 MTU를 8192로 쓴다(`config/tgen/cuphycontroller_dgist_tgbase.yaml`). MIG 4g 조각의 메모리에 프런트홀 버퍼를 맞추기 위한 값이고 설정 파일에 이미 들어 있다.

## 6. 실행 · 검증

```bash
./all.sh up demo1          # 코어 → RIC → L1 + gNB → VA → non-VA  (끝의 "FAILED components: ric"는 타이밍 오경보다. MANUAL.md)
./all.sh status ; ./dgist.sh status
./dgist.sh test 26x 4 20   # 전화기 없이 YOLO 요청 스모크 (4 req/s, 20 s)
./dash.sh start            # 대시보드 http://<서버 IP>:8010/ , https://<서버 IP>:8012/
./xapp.sh start ; ./xapp.sh moni start   # 전화기 4대가 붙은 뒤
./all.sh down
```

- 실행 이름(`demo1`)은 다시 쓰지 않는다. RIC는 gNB보다 먼저 켜고, gNB가 도는 동안 RIC를 내리지 않는다.
- 가상 카메라(`./vcam.sh`)의 프레임은 저장소에 없다. `./vcam.sh make-frames <그림 디렉터리 | 동영상>`으로 만들고, 없으면 `va_service/test_frames/frame_1920x1080_q90.jpg` 한 장을 되풀이해 보낸다.

## 7. 서버마다 고쳐야 하는 값

이 저장소의 값은 처음 만든 서버(GH200 한 대, 132 SM, 72코어, Benetel RU) 기준이다. 변수로 빼지 않은 것들이라 새 서버에서는 직접 고친다.

| 무엇 | 어디 | 처음 서버의 값 |
|---|---|---|
| 프런트홀 인터페이스 이름, RU 주소 | `.env FH_IF`, `RU_IP` | `aerial00`, `10.10.0.100` |
| 프런트홀 NIC의 PCI 주소, RU의 MAC · VLAN | `config/cuphycontroller_dgist.yaml`, `config/cuphycontroller_dgist_base.yaml`, `config/tgen/cuphycontroller_dgist_tg*.yaml`의 `nic`, `dst_mac_addr`, `vlan` | `0002:01:00.0`, Benetel 550 |
| 생성기 루프 NIC의 PCI 주소와 MAC | `config/tgen/cuphycontroller_dgist_tg*.yaml`의 `nic` · `src_mac_addr`(L1 쪽 포트), `config/tgen/ru_dgist_tg.yaml`의 `nic_interface` · `peerethaddr`(에뮬레이터 쪽) | `0000:01:00.0` / `0000:01:00.1` |
| 루프 인터페이스 이름 | `dgist.sh`의 사전 점검(`aerial02`, `aerial03`) | — |
| CPU 코어 배정 | `.env BUILD_CPUS`(빌드, 격리되지 않은 코어), `OAI_CPUSET`, `DAPP_TENANT_CPU`, `DAPP_AGENT_CPU`. L1 · RU 에뮬레이터 · testMAC의 워커 코어는 `config/` 아래 yaml. `dgist.sh test`는 코어 65-71을 쓴다 | 격리 코어 4-64 |
| 전화기 | `.env VA_IMSIS`, `NONVA_IMSIS`(각 2대), WebUI의 가입자 | — |
| gNB | `config/gnb.conf`(AMF 주소, PLMN, 주파수, 전력 제어 목표) | — |
| GPU | SM 등급 목록(`.env DAPP_TENANT_CLASSES*`, `DAPP_PUSCH_PROFILES`, `DAPP_PDSCH_PROFILES`)과 `DAPP_TENANT_ENGINE_SM`(GPU의 SM 수)은 GH200(132 SM), MIG 4g = 64 SM / 3g = 60 SM 기준 | — |

## 8. 개발 흐름 (어디를 고치고 어떻게 커밋하나)

| 고치는 것 | 위치 | 빌드 | 커밋 |
|---|---|---|---|
| 스크립트 · 설정 · 서비스 · 대시보드 · xApp Python | `<패키지>` 최상위 | — / `./va.sh restart` 등 | 이 저장소 |
| cuPHY / L1 / YOLO 테넌트 | `aerial-cuda-accelerated-ran/` (포크의 같은 이름 브랜치) | `./dgist.sh build-l1` | 서브모듈 안에서 commit → 포크에 push → 상위에서 `git add aerial-cuda-accelerated-ran && git commit` |
| OAI gNB / FlexRIC xApp | `openairinterface5g/` | `./dgist.sh build-oai`, `./ric.sh build` | `./oai/update_patches.sh` → `git add oai && git commit` |
| Open5GS 설정 | `core/etc/*.yaml` | `./core.sh install-config <nf>` | 이 저장소 |

- 공통 수정은 `demo`에 넣고 `demo-slot-level`로 병합한다(`git checkout demo-slot-level && git merge demo`). 슬롯 단위 전환에만 해당하는 것은 `demo-slot-level`에만 넣는다. 두 저장소 모두 같은 규칙이다.
- `.env`는 추적되는 파일이다. 비밀 값(SIM 키, 토큰)은 넣지 않는다.

버전: Aerial 25-3(커밋 654c249) 기반 포크. OAI `87c016b5` + FlexRIC `ef6d722f`(`oai/README.md`). Open5GS v2.8.0. 변경 요약 `docs/CHANGES_DGIST_AERIAL.md`, `docs/CHANGES_HSW_OAI.md`, 개별 패치 `docs/patches/`.
