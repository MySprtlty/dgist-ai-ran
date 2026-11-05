6G SM-aware20 테스트베드 패키지

입력20개:4UE별 버퍼/SNR/UL TP/사용PRB16개 + 현재VA/NonVA 예산2개 + YOLO모델번호/4 + SM/80.
외부 YOLO모델은26n/26s/26m/26l/26x 중 하나. 그 모델의SM개수와 동일 시점 한 쌍. timestamp_ms는 메타데이터이며 입력차원이 아님.
3개 학습시드의20차원Actor 확률 평균 후 정수예산 선택. CPU 추론. 별도 GPU-util/NVML 입력 없음.
6G 학습 결과: v24; 모든 학습/평가 및 선택 근거는 ../training_6g_sm_allocation/runs/sm_release_v26/에 보존.
새 후보 v26은 동일 보상 예산비교 데이터로 초기화 후 Basic18/SM20 모두200에피소드 PPO 추가학습. 선정하지 않았다면v24를 유지.
실제 무선 테스트베드 성능은 아직 검증하지 않음. 파일/추론/브리지 검증 완료와 RF실험 완료는 구분.

외부 계산 정보 파일(UTF-8 JSON, 임시파일 작성 후 atomic replace):
{"model_id":"26x","allocated_sm":80,"timestamp_ms":<실제 Unix 밀리초>}
PPO_SM_INPUT_FILE 환경변수로 파일 경로 지정. 모델·SM변경 시 한 묶음 갱신; 변경 없어도 실제 관측 heartbeat 필요.
허용SM24..80; 계산정보2초, 무선정보200ms 이상 오래되면ERR로 새예산 거절.2초는 현재 인터페이스 설정값.
write_compute.py --output /path/compute_state.json --model26x --sm80 형태가 아니라 아래처럼 공백을 둔다:
python write_compute.py --output /path/compute_state.json --model 26x --sm 80
이 명령은1회 기록 테스트용이며 외부 송신자가 실제 상태로 계속 갱신해야 한다. 오래된 상태에 현재시각만 덧씌우지 않는다.
현재 네트워크 수신은 상대 연구실 방식이 미정이므로 파일 어댑터 제공. 실제 연구실 간 송수신 연동 완료를 의미하지 않음.

테스트베드 설치: 이 폴더의 활성 파일 전체를 /home/nvidia/hsw/25_3_hsw/xapp_gpu_aware_no_lending_6G/로 복사.
모델과runtime/manifest를 반드시 같이 복사. 기존 졸업논문 폴더에 덮어쓰지 않는다.
검증:
/home/nvidia/hsw/25_3_hsw/xapp_basic_no_lending/.venv/bin/python /home/nvidia/hsw/25_3_hsw/xapp_gpu_aware_no_lending_6G/verify_6g.py

C xApp 실행(이미PPO4R 지원 빌드라면 C변경 없음):
cd /home/nvidia/hsw/25_3_hsw/openairinterface5g/openair2/E2AP/flexric/build/examples/xApp/c/slice
unset PPO_GPU_INPUT_FIXED
export PPO_RADIO_INPUT=1
export PPO_MIN_PRBS=5
export PPO_BUNDLE=/home/nvidia/hsw/25_3_hsw/xapp_gpu_aware_no_lending_6G
export PPO_PYTHON=/home/nvidia/hsw/25_3_hsw/xapp_basic_no_lending/.venv/bin/python
export PPO_SM_INPUT_FILE=/absolute/path/compute_state.json
export VA_RNTIS=VA1_RNTI,VA2_RNTI
export NONVA_RNTIS=NV1_RNTI,NV2_RNTI
./xapp_slice_ppo_ctrl
RNTI와 파일 경로는 실제 값으로 교체. C xApp이 Python을 자동 실행하므로 별도서버를 중복 실행하지 않는다.
서비스예산 합273, 최소5, lending없음. 반환값은제안예산이며 실제적용확인은C/E2측 담당.

독립 시뮬레이션 최종 평가:80SM약90%,YOLO5종균등,무선12조건,10초예열+30초측정. 완료프레임 기준 만족률.
정책 | VA PRB | VA UL | VA E2E | VA만족률 | NonVA E2E | NonVA만족률
PF | 제한없음 | 231.54 | 272.64 | 42.36 | 32.07 | 98.24
Basic18 ensemble | 198.59 | 145.21 | 186.30 | 76.96 | 205.37 | 87.10
SM20 ensemble | 198.71 | 144.66 | 185.76 | 77.67 | 205.37 | 86.89

평균85%는 개별조건보장이 아님. SM-aware VA개선량=0.72%p
검증: {"valid_model_sm_cases": 40, "invalid_cases": 11, "bridge_contract": "PASS", "cpu_predict_median_ms": 0.1689, "cpu_predict_p95_ms": 0.1962, "scope": "local CPU only; not Linux socket or E2/RF integration", "train_export_parity_cases": 400, "protected_unchanged": true}
이전6G폴더 백업: C:\25_3_hsw\training_6g_sm_allocation\runs\sm_release_v26\backup_6G_before20_1793880771593345000