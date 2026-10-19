# Jetson 추론 최적화 분리 검토

기준: 공식 `ajouatom/openpilot`의 `carrot-nav` **74955c98**.
같은 기준의 Wi-Fi 검토 브랜치를 요구하지 않는 단일 커밋이다.
Wi-Fi·HUD·밝기·차량 제어·전원 처리·배포 매니페스트·개인 서명 키는 제외했다.
모델 ONNX/PKL과 기본 프로토콜도 바꾸지 않는다.

## 세 옵션 — 모두 기본 꺼짐

| 명시적 환경변수 =1 | 실행 위치 | 하는 일 |
| --- | --- | --- |
| CARROT_TRT_DEVICE_STATE | Jetson TensorRT | recurrent state의 GPU 내 피드백, 전체 finite 검사, 응답 이벤트 |
| CARROT_JETLINK_AIO_WRITE | comma jetlinkd | 기존 프레이밍을 유지하는 bounded FunctionFS 비동기 쓰기 |
| CARROT_TRT_INPUT_FASTPATH | Jetson TensorRT | uint8→FP16 네이티브 변환과 compact map의 고정 입력 버퍼 직접 복원 |

환경변수는 엔진/전송 생성 전에 명시해야 한다. 자동으로1을 넣는 개인 options 파일,
서버 시작 래퍼나 매니페스트는 포함하지 않았다. 일반 경로는 기존 방식으로 실행한다.
제작자는 원하면 한 옵션씩 골라 시험할 수 있다. 콤마 설치만으로 기존 Jetson
런타임이 바뀌지 않으며 제작자의 정상 서명·배포 절차가 필요하다.

### GPU 상태 전달

서로 다른 state 입력/출력 GPU 버퍼를 유지한다. 매 프레임의 state H2D/D2H 대신
GPU 피드백을 사용하며 모든 FP16/FP32 state의 유한성을 GPU에서 검사한다.
검사 상태와 driving 출력 확인 뒤 응답하고, 다음 호출 전 피드백은 같은 CUDA
스트림 순서로 보장한다. reset·close·실패 처리는 뒤따르는 GPU 작업까지 고려한다.
NaN/Inf 검사나 초기화·실패 거부를 제거하지 않는다. 지원하지 않는 dtype·누락된
NVRTC·CUDA 오류는 요청한 옵션을 거부한다. CUDA 그래프 capture 실패가 활성 옵션을
조용히 정상으로 간주하지 않게 한다. host binding 메모리는 여전히 유지한다.

### 비동기 USB 쓰기

기존 CarrotTransport의 프레이밍/버전/sequence를 유지하고 쓰기 원시 동작만 바꾼다.
32KiB 요청 최대32개, 즉1MiB로 제한한다. 완료 token·바이트수·시간 제한을 확인하고
짧은 쓰기·오류·기한 초과 시 연결을 실패 처리한다. 부분 전송 뒤 동기 경로로
재전송하지 않는다. C3 FunctionFS의 res2=바이트수와 신형의 res2=0을 엄격히 다룬다.
종료 시 커널이 아직 버퍼를 소유하면 정리/재바인딩을 거부한다. 상위 daemon은
AIO 정리 오류를 기존 UnsafeCleanupError로 전달한다. USB 전원 제어는 포함하지 않는다.

### 입력 준비

기존 NumPy FP16 변환과 같은 비트의 작은 C 조회표 변환이다. 컴파일/자가 검사는
엔진 준비 시 한 번 수행하고 프레임 중에는 하지 않는다. 협상된 정수 compact map을
중간 FP32 배열/bytes 재조립 없이 고정 입력 binding으로 복원한다. binding 객체·
주소·shape·dtype·stride·쓰기 가능 여부를 검증한다. raw v2/v3는 기존 검증을 유지하며
stateful TensorRT에 명시적으로 요청한 경우만 적용된다. ORT/eGPU 모델 경로는 그대로다.

## 수치와 해석

최신 공식 위 분리 코드의 Linux PC 회귀는 **258 통과, 선택적 참조 검사1 건너뜀**.
새 외부 Jetlink 참조 checkout이 없어서 건너뛴 protocol3 검사는 통과로 세지 않았다.
기존 pytest 설정 경고3개가 있다. 입력 비트 동등성·비정상 binding/입력·상태 reset·
deferred CUDA 순서·native finite·Linux AIO 실패/소유권·기존 세션/프로토콜을 확인했다.
실제 CUDA 장치 전용 검사는 이 PC 실행에 포함하지 않았다. 요청에 따라 추가 자동
테스트 파일과 임시 진단은 커밋에서 제외하고 재현 도구와 결과를 남겼다.

아래 값은 이전 통합 실험에서 얻은 기록이며 최신 공식 기준 분리 코드의 실차 측정이
아니다. FP16 전체 입력의 PC 준비 평균은0.357→0.101ms, p99 0.550→0.190ms였다.
실제 Orin 고정 pinned 입력의 별도 비교는 평균0.938→0.448ms, p99 1.030→0.478ms;
시험한 입력 binding 비트가 같고64프레임 A/B/A에서 driving 출력과 최종 state가 같았다.
약0.49ms 절감은 50ms 전체 예산의 약1%이며 20Hz/안전을 보장하지 않는다.

기존 통합 장치의 합성 크루즈 구간은 전체 호출 평균43.159ms이나 p99 54.362ms,
최대62.628ms였다. 클라이언트 왕복에서 서버 시간을 뺀 잔여는 평균11.614ms,
p99 21.848ms로 **물리 USB 시간만의 측정이 아니다**. GPU 상주 상태와 pinned/graph는
이미 활성화된 측정이며 이 수치를 세 옵션 전체의 개선율로 해석할 수 없다.
통신/IPC 대기 꼬리 개선은 입증되지 않았으므로 모든 옵션의 기본 활성화를 권하지 않는다.

## 제작자 시험 순서

1. 기본 OFF와 각 옵션 단독을 같은 모델/엔진/전원/카메라 조건으로 A/B/A 비교한다.
2. 입력·출력·전체 state 비트, reset·재연결·NaN/Inf·부분 쓰기·정리 실패를 확인한다.
3. 평균뿐 아니라 p99/최대/50ms 초과 수, 촬영→발행 나이, 새 frame ID·DM·pose
   유효성을 부하와 장시간 조건에서 확인한다. 모든 옵션 조합도 별도 검증한다.

`tools/jetlink/bench_input_staging.py`는 CPU 준비만 측정하며 GPU/USB/차량 지연을
제외한다. 기본 spec이 stateful nav가 아니면 해당 spec을 명시해야 한다.
`simulate_pipeline.py`는 가정한 시간의 이벤트 모의 실험으로 실제 모델·차량
안전 검증이 아니며 production pipeline을 바꾸지 않는다. 남는 CPU 코어에 준비 작업을
여러 개 분할한 이전 PC 시험은 더 느려서 채택하지 않았다.
