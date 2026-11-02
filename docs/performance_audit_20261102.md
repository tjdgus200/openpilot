# 정상 운전 경로 성능 점검 — 2026-11-02

사용자 요청: 남아 있는 시험·진단 부하는 줄이고, 원본/기능 코드의
성능 후보도 근거와 함께 구분한다. 실제 차량·Jetson에는 접속하거나 설치하지 않았다.

## 수정한 잔여 계측

| 실행 경로 | 확인한 불필요한 작업 | 변경 |
|---|---|---|
| C3/C4 UI | 꺼진 RenderDiagnostics 호출 전후에도 벽시계·스레드 CPU 시간을 읽고 섹션 딕셔너리 생성 | 꺼진 경우 콜백 직접 실행. uiDebug의 상태·녹화·plotMode 발행은 유지하고 선택적 시간 필드는0 |
| CAN card | 100Hz 경로에서 단계별 시계·10항목 딕셔너리·합계/최대 계산 | 상세 계측만 opt-in. CAN 수신 타임스탬프, 누적 수신 실패, 느린 루프/전송 경고 유지 |
| modeld/DM/plannerd/worker | record() 내부가 꺼져도 인자용 CPU 시계·문맥·통계 생성 | 호출부에서도 분기. 메시지의 기본 실행시간과 계획 주기·신선도·위상 제어는 유지 |
| 카메라 짝 맞추기 | 매 수신별 trace 리스트와 타이머, 매 프레임 상세 타이밍 | 정상 경로는 상세 trace 없이 동일한 짝 선택. 누락 시 최근 프레임 정보와 제한된 오류 경고 유지 |
| eGPU generic/local warp | 입력·warp·출력·복구 체크포인트마다 부가 시계/통계 | 부가 계측만 분기. 복사·출력·반복 상태·체크포인트 동기화는 동일 |
| Jetson HUD 메인 | 꺼진 렌더 프로파일러를 위해 시작 시계 수집 | 프로파일러가 켜진 경우에만 시작 시계 수집 |
| JPEG 비동기 전송 | 기본 상태에서도 인코딩/전송 타이머와 샘플 큐·수집 잠금 | 기본 수집 중단. 실제 인코딩·전송·큐 용량 대기·오류 전달 유지 |
| H.264 전송 | 기본 상태에서도 단계별 샘플과 native timing getter 조회 | 기본 수집·조회 중단. 별도 명시적 전송 진단의 타이머와 ACK/시간 제한은 독립 유지 |

명시적으로 켠 JPEG/H.264 프로파일러도 샘플을 최대256개만 보관한다.
일반 실행의 `CARROT_PERF_DIAGNOSTICS`는 unset/default-OFF이며 exact1만
개인 진단용으로 허용한다. 기존 명시적 `--profile-render` 경로는 유지한다.
전용 CLI 벤치마크/시험 파일은 보관하되 정상 manager 경로에서 자동 실행하지 않는다.
기존 웹 비교표 제거와 Jetson FPS/추론 footer 제거도 유지한다.

## 기능 코드에서 남은 성능 후보

아래는 소스에서 실행 비용이 확인되는 경로다. 실제 병목의 순서나 ms 이득을
측정한 결과는 아니다. 이미 있는 최적화·유효성 조건을 무시한 제거는 하지 않았다.

| 후보 | 소스 근거 | 다음 검증 / 보존 이유 |
|---|---|---|
| HUD 전체 화면 읽기·JPEG 인코딩 | `cluster/main.py`, `cluster_usb_pipeline.py`, `cluster_usb_display.py`에서 렌더 결과를 가져와 TurboJPEG로 인코딩 | FPS 증가 시 처리 횟수가 증가. 장치에서 읽기/변환/인코딩 시간을 분리해야 함. Q92와 사용자의 FPS 설정 유지 |
| HUD snapshot 반복 읽기·JSON/이벤트 해석 | `tools/jetlink/hud_protocol.py:read_snapshot`은20ms 캐시 후 유한 크기 파일 읽기와 json.loads, `tools/jetlink/hud.py:RemoteSubMaster`는 변경된 이벤트의 base64/Capnp 해석 | generation 기반 불변 캐시 후보. 데이터 신선도·재연결·타임스탬프·이벤트 소유권 검증이 선행되어야 함 |
| eGPU 복구 체크포인트 | `generic_model_runtime.py:save_recovery_state`에서 반복 상태 백업 후 Device.synchronize | 이 경로는 USB AMD/eGPU용. Jetson 경로와 혼동하지 말 것. 복구 시 정확한 상태 복원을 위해 유지 |
| DM 결과의 추가 소유 복사 | `dmonitoringmodeld.py:ModelState.run`의 numpy().flatten() | view로 바꾸기 전에 결과 버퍼의 수명·재사용을 검증해야 함. 현재 변경하지 않음 |
| 지도 렌더링·PNG preview | `navigation/mapsd.py`, `preview.py`에서 지도와 Web/USB 공유 preview 생성 | CPU 예산을 제한하는 render_interval과 지연/신선도 검사를 유지. preview 소비자·수요를 확인한 뒤 추가 캐시 검토 |

별도 USB 포트라고 해서 CPU·캐시·메모리 대역폭까지 독립이라는 뜻은 아니다.
계측 제거만으로 추론 시간이 크게 줄거나 영향이0이 된다고 주장하지 않는다.
온도·전력/저장소 건강 상태, 수신/송신 deadline, GPU 완료 대기, 출력 유한성·상태
동일성 검사, WorkerProgress 생존 감시, 경고/운전자 감시는 성능 시험용이 아니므로 유지한다.

## 검증 범위

- 콜백 결과·예외·순서, 카메라 짝 선택·누락 경고, CAN 적용 함수의 전송 내용과
  timeout/지연 경고를 계측 on/off로 비교한다.
- 입력·warp·모델 호출·반복 상태·출력을 합성 실행으로 비교한다. 복구 백업과
  Device.synchronize 호출도 유지되는지 검사한다.
- JPEG/H.264는 각1000회 합성 프레임/패킷으로 순서·바이트·ACK·계측 큐 상한과
  기본 상태의 시계/native getter 미호출을 검사한다. 실제 인코더/USB 시험은 아니다.
- FPS 설정, 모델 우선 입장, 큐, 입력 통합, 출력 검증의 기존 Jetlink 회귀도 실행한다.
- native CAN 라이브러리 및 loggerd SConscript가 없는 이 PC checkout에서는 그 의존
  시험을 완주할 수 없다. CAN 적용 경계는 별도 실제 함수+합성 I/O 시험으로 검증한다.
- 전체 파일 Ruff에 기존 위반이 있으므로 기준 커밋과 비교하여 새 위반이 없는지 검사한다.

실제 Jetson CPU 사용률·추론 P99·BLE RF 안정성·차량 동작은 이 검증으로 증명되지 않는다.
통합 PC 검증:593 passed, 5 skipped, 1 deselected, 3 subtests passed.
변경 파일 Ruff: 기존169개, 이후169개, 새 위반0개.
패키지 배포 기록은 네 모델의 체크포인트/런타임 조합을 보존하여 아래에 추가한다.
