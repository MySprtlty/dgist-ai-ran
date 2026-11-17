# Network Slicing xApp ↔ Resource Allocator 인터페이스 (v1, 2026-11-11)

IITP 6G 통합 시연에서 Slicing xApp(최지웅 교수님 연구실, PPO)이 정한 서비스별 UL PRB 예산을 Resource Allocator(RA, 곽정호 교수님 연구실)가 받아 MAC에 건다. MAC에 제어를 보내는 xApp은 RA 하나다. Slicing xApp은 예산을 RA에 보내고, RA가 알려 주는 "MAC에 실제로 걸린 예산"을 다음 판단의 입력으로 쓴다. 이 정보는 지금까지 Slice SM(145) 보고에서 읽던 것이다.

```
Slicing xApp (PPO)  ──budget (100 ms, 바뀔 때)──▶  Resource Allocator  ──Slice SM 쿼터·ASSOC + RC Style 2 UE 하한──▶  OAI MAC
                    ◀──ra_state (10 ms)──────────                      ◀──MAC SM·Slice SM 보고 (10 ms)──────────────
                    ◀──ra_qos (100 ms)───────────
```

## 1. 전송

| 항목 | 값 |
|---|---|
| 방식 | UDP/IPv4, 데이터그램 하나 = JSON 객체 하나 (UTF-8, 끝의 `\n`은 있어도 됨), 1,400바이트 이하 |
| RA가 받는 주소 | `127.0.0.1:9820` (Slicing 쪽 `PPO_RA_ADDR`, RA 쪽 `RA_SLICING_LISTEN`) |
| Slicing이 받는 주소 | `127.0.0.1:9821` (Slicing 쪽 `PPO_RA_LISTEN`, RA 쪽 `RA_PPO_ADDR`) |
| 시각 | `t_ms` = 보낸 쪽 CLOCK_REALTIME ms. 신선도는 받은 쪽이 받은 시각으로 판단한다 |
| 공통 필드 | `type`, `v` (= 1), `t_ms`, `src` |
| RNTI | 16진 문자열 4자리 (`"4601"`) |

두 xApp은 같은 호스트(DGIST GH200)에서 돈다. 다른 호스트로 옮기면 주소만 바꾼다.

## 2. Slicing → RA

### 2.1 `budget`

PPO가 새 예산을 정했을 때 보낸다(시작 때 한 번 + 100 ms 판단 중 바뀐 때).

```json
{"type":"budget","v":1,"t_ms":1794000000123,"src":"ppo","run":1794000000001,"seq":812,
 "va_prb":150,"nonva_prb":123,"total":273,"min_prb":5,
 "va_rntis":["4601","7a3c"],"nonva_rntis":["b210","c0de"]}
```

| 필드 | 뜻 |
|---|---|
| `run` | Slicing xApp 프로세스를 구분하는 값(시작 시각 ms). 재시작하면 바뀐다 |
| `seq` | 한 `run` 안에서 예산마다 1씩 늘어나는 번호. 1부터 |
| `va_prb`, `nonva_prb` | 서비스 1(VA), 2(Non-VA)의 UL PRB 개수 쿼터. 지금 MAC 계약(`NR_UL_PRB_COUNT_V1`)과 같은 뜻: UL 슬롯마다 서비스 안의 UE들이 받는 PRB 합의 상한, 신규 전송과 재전송을 함께 셈, 서비스 사이 대여 없음 |
| `total` | 셀 UL PRB 합(273). `va_prb + nonva_prb == total` |
| `min_prb` | 서비스 하나의 최소 PRB(지금 5) |
| `va_rntis`, `nonva_rntis` | Slicing xApp이 아는 서비스별 UE와 그 순서(정책 입력 순서). RA는 이 소속으로 ASSOC한다 |

### 2.2 `bye`

Slicing xApp이 끝날 때 보낸다. RA는 마지막으로 건 예산을 그대로 둔다.

```json
{"type":"bye","v":1,"t_ms":1794000099000,"src":"ppo","run":1794000000001}
```

## 3. RA → Slicing

### 3.1 `ra_state` (10 ms마다, 그리고 예산을 처리한 직후)

지금 Slice SM 보고가 주던 내용(걸린 쿼터, UE 소속)에, 어느 예산까지 처리했는지를 더한 것이다.

```json
{"type":"ra_state","v":1,"t_ms":1794000000135,"src":"ra","seq":40211,"run":1794000000001,"budget_seq":812,
 "status":"applied","reason":"","applied":{"va":150,"nonva":123},
 "ues":[{"rnti":"4601","svc":1},{"rnti":"7a3c","svc":1},{"rnti":"b210","svc":2},{"rnti":"c0de","svc":2}]}
```

| 필드 | 뜻 |
|---|---|
| `seq` | RA가 보낸 `ra_state` 번호 |
| `run`, `budget_seq` | RA가 마지막으로 받은 예산의 `run`·`seq` (아직 없으면 0) |
| `status` | 그 예산의 처리 상태. `none` = 받은 예산 없음, `pending` = MAC에 보냈고 확인 대기(쿼터 축소 때 HARQ drain 등), `applied` = MAC이 그 예산을 걸었다고 보고함, `rejected` = 거절(`reason`에 이유) |
| `applied` | **지금 MAC에 걸려 있는** 서비스별 쿼터. `pending`·`rejected`이면 이전 값이다. 쿼터가 없으면 `{"va":0,"nonva":0}` |
| `ues` | MAC이 보고한 연결 UE 전체와 소속(`svc` 0 = 없음, 1 = VA, 2 = Non-VA) |

### 3.2 `ra_qos` (100 ms마다)

RA가 아는 서비스 품질과 AI 상태. Slicing 정책 입력으로 쓸지는 Slicing 쪽이 정한다(지금 정책은 쓰지 않음).

```json
{"type":"ra_qos","v":1,"t_ms":1794000000200,"src":"ra","window_ms":100,
 "va":{"requests":12,"on_time":11,"deadline_ms":100,"e2e_p50_ms":82.1,"net_p50_ms":49.7,"proc_p50_ms":31.2},
 "nonva":{"requests":40,"on_time":40,"deadline_ms":0,"e2e_p50_ms":null,"net_p50_ms":21.0,"proc_p50_ms":null},
 "ues":[{"rnti":"4601","svc":1,"model":"26n","floor_prb":30,"sm":80},{"rnti":"7a3c","svc":1,"model":"26x","floor_prb":20,"sm":80}]}
```

- `requests` = 창 안에서 끝난 요청 수, `on_time` = 그중 기한 안에 끝난 수. 서비스 완료율 xₛ = `on_time / requests`(KPI 11 Jain 지수의 입력)
- `e2e/net/proc_p50_ms` = 창 안의 중앙값. 값이 없으면 `null`
- `ues[].model`·`sm` = 그 UE의 마지막 요청에 쓴 YOLO 모델과 SM, `floor_prb` = RA가 건 UL PRB 하한

## 4. 규칙

### 4.1 Slicing 쪽 (`xapp_slice_ppo_ctrl.c`의 `PPO_OUTPUT=ra` 모드, Slicing 연구실 패치, 이 저장소에는 아직 없음)

- 시작: `ra_state`가 오고 대상 UE 4대가 모두 `ues`에 보일 때까지 최대 10초 기다린다. 그다음 시작 예산 137:136(seq 1)을 보내고, 5초 안에 `budget_seq` = 1, `status` = `applied`, 쿼터 137:136, 네 UE가 순서대로 소속된 `ra_state`를 확인한다. 이전 Slice SM 모드의 "쿼터 → 확인 → ASSOC → 확인"을 한 번에 한다.
- 100 ms마다: 마지막 `ra_state`가 200 ms 안에 왔고, 쿼터 합이 273이고, 소속이 맞을 때만(healthy) 판단한다. 그렇지 않으면 HOLD. 모델 입력의 현재 쿼터는 `applied`이다(이전과 같이 "걸린 값만 입력").
- 새 예산을 보낸 뒤 80 ms 안에 `budget_seq`가 그 seq이고 `status` = `applied`이고 `applied`가 그 값인 `ra_state`가 오면 STORED, 아니면 NOT_CONFIRMED(다음 판단에서 다시 읽음).
- 끝날 때 `bye`를 보낸다. Slice SM 모드와 달리 쿼터를 지우지 않는다(RA가 관리).

### 4.2 RA 쪽

- `budget` 검사: `total` = 셀 PRB(273), `va_prb + nonva_prb == total`, 각각 `min_prb` 이상, RNTI 중복 없음. 어기면 `rejected`(`applied`는 그대로).
- 같은 `run`에서 이미 처리한 `seq` 이하는 무시하고 `ra_state`만 다시 보낸다. `run`이 바뀌면 그 run의 seq를 새로 받는다.
- 예산을 MAC에 거는 순서: 쿼터를 줄이는 서비스는 그 서비스 UE의 하한 합을 먼저 새 쿼터 안으로 내린 뒤 쿼터를 보낸다. 늘릴 때는 쿼터를 먼저 보낸다.
- 소속: 예산의 `va_rntis`·`nonva_rntis`로 ASSOC한다. 소속이 이미 같으면 다시 보내지 않는다.
- `applied`는 MAC Slice SM 보고에서 확인된 값만 쓴다(보낸 값이 아니라).
- Slicing xApp이 조용하거나 `bye`를 보내면 마지막 예산을 유지한다.
- `ra_state`는 10 ms마다 보낸다. 예산을 받으면 처리 결과를 바로 한 번 더 보낸다.

## 5. 구현

| 파일 | 내용 |
|---|---|
| `ra_slicing_link.h` | 헤더 전용 C 라이브러리(C11, POSIX, 의존성 없음). Slicing 쪽: `rsl_open`, `rsl_send_budget`, `rsl_send_bye`, `rsl_recv`(→ `rsl_state_t`, `rsl_qos_t`) |
| `ra_slicing_link.py` | Python 라이브러리. RA 쪽 `RaSide`(budget 받기, `ra_state`·`ra_qos` 보내기, `check_budget`), 시험용 Slicing 쪽 `SlicingSide` |
| `ra_stub.py` | RA 대역. MAC 없이 받은 예산을 정한 지연 뒤 `applied`로 보고. 모드: `normal`, `reject`, `silent`, `pending` |
| `test_slicing_side.c` | Slicing 쪽 논리(시작 확인, 100 ms 예산, 80 ms STORED)를 헤더로 흉내 낸 시험 프로그램 |
| `test_link.sh` | 대역 + 시험 프로그램으로 시나리오 4개를 돌림 |
