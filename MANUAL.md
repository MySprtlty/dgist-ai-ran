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
```

## 4. 모듈별 상세

### core.sh — Open5GS 코어
`up | down | status` (sudo), `config`(설정 복사본 `core/etc/` 목록, 라이브와 차이), `diff [nf]`, `install-config <nf>`(복사본을 /etc/open5gs에 설치 + 유닛 재시작, 백업 남김), `build`(소스 `open5gs/` meson 빌드; dev 패키지 필요), `subs`(가입자 목록), `logs`. 가입자 추가는 WebUI `http://<host>:9999`.

### ric.sh — nearRT-RIC
`start | stop [--force] | restart | status | logs | install-sm | build`. 우리 빌드 `build_dgist`(E2AP_V2, KPM_V2_03), RIC IP 127.0.0.2(`config/flexric.conf`; 연구실의 옛 모니터 프로세스들이 127.0.0.1을 폴링하므로 분리). SM 라이브러리는 `flexric_sm/`에 두고 gNB 컨테이너에 마운트한다(`install-sm`; 빌드 뒤 한 번).

### dgist.sh — L1 + gNB
`check`(사전 점검) `up [이름]` `down` `status` `ue [이름]`(attach 단계) `logs [l1|gnb]` `test [모델] [req/s] [초]`(dApp RUN 스모크) `build-l1 [타겟]` `build-oai` `core|va|nonva|ric|xapp|all|logs …`(패스스루). 실행 로그는 `runs/<이름>/`.

### va.sh — VA 서비스
`start [local] | stop | restart | status | logs | test [ue] [n] [req/s] | test2 [n] | bg start --fps N|stop|status | make-frame`. 설정 `.env VA_*`(4K, 26x, 5 req/s, QoS 200 ms, JPEG 0.60). CSV `runs/va_service_log/`. 상세 `va_service/README.md`.

### nonva.sh — non-VA 서비스
`start | stop | restart | status | logs | test [ue] [n] [fps] | make-frame`. 설정 `.env NONVA_*`(720p, 20 fps, 0.80). 뷰어 `https://10.45.0.1:8001/viewer/ue1|ue2`. 상세 `nonva_service/README.md`.

### xapp.sh — PPO 슬라이싱 xApp
`rntis`(UE 표 + 매핑) `start [VA1,VA2 NV1,NV2]` `stop` `restart` `status` `trace [n]` `logs` `dapp`(dApp 피드 확인) `compute [restart]`(6G compute 파일) `guard`(내부용) `moni [start|stop|status|logs] [VA NV]`. 번들 선택 `.env PPO_BUNDLE_NAME`(기본 6G 20입력: YOLO 모델·SM을 우리 dApp에서 받음). 상세 `xapp_ppo/README.md`.

### logs.sh — 읽기 쉬운 로그
`./logs.sh <대상> [-n N] [--raw]` — 대상: `gnb mac l1 dapp va nonva ric xapp ppo moni core|amf|smf|upf ue all`. 빨강 = 오류/실패, 초록 = 성공/attach, 노랑 = 경고/HOLD.

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
| 빌드 로그 | `runs/build_*.log` |

## 6. 설정 (`.env`) 요약

이미지·빌드 디렉터리·코어 수(`BUILD_CPUS`), gNB cpuset, dApp(`DAPP_*`), VA(`VA_*`), non-VA(`NONVA_*`), FlexRIC(`FLEXRIC_BUILD`, `FLEXRIC_SM_DIR`, `RIC_IP`), PPO(`PPO_BUNDLE_NAME`, `PPO_GPU_SOURCE`, `PPO_DAPP`, `PPO_MIN_PRBS`), 전화기 SIM→서비스(`VA_IMSIS`, `NONVA_IMSIS`), guard(`XAPP_GUARD`, `XAPP_GUARD_S`), RU/코어(`RU_IP`, `FH_IF`, `UE_SUBNET`, `NO_CORE`). 바꾼 뒤 해당 모듈만 restart.
