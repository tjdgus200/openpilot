# PV5 신호 정차용 가상3m SCC 객체 — 2026-11-10

사용자는 레이더 전체 경로 비교 후 별도로 신호 정차용 가상3m 객체 도입을
명시했다. 기존 신규 차량 동작의 PV5 한정을 유지하며 다른 차종에 적용하지 않는다.
출처는 carrot-wip `c8454993`의 `stop_object.py`, `7d337b05`의 송신 객체 연결과
`f3564347`의 전체 publish 경로 신선도 수정이다. 혼합된 Panda/Casper 고장
감시·Tesla·음성 제어·다른 차종 기능은 가져오지 않는다.

## 요청부터 송신까지

- `signal_stop_object_requested`는 KIA_PV5·Hyundai·CANFD+CAMERA_SCC·실차·
  openpilot 종방향 제어에 한정한다. 제어 enabled/longActive, D단, 유효 CAN과
  timeout 없음, 페달/hold/주차 브레이크 없음, shouldStop·상태3/5, 계획/실측
  앞차 없음이 필요하다. 상태3/5는 기존 e2eStop/e2eStopped다.
- 실제 `Controls.publish`에서 longitudinalPlan과 radarState의 유효성 및
  수신 프레임 나이0~0.20초를 각각 확인한다. 다른 함수의 지역변수를 참조하지 않는다.
  `CarControl.HUDControl.signalStopObject @24`는 최종 요청일 뿐이다.
- `create_acc_control_scc2`는 기존 SCC/StopReq/interlock 처리 뒤 송신 객체를
  추가한다. 최종 단계도 CS.CP로 PV5 범위를 재확인하며 ACCMode1·StopReq1,
  현재 CAN·기어·페달/hold·실제 앞차, 유한한 모든 raw/휠 속도≤0.7m/s를 검사한다.
- 송신에만 `ACC_ObjDist=3m`, 횡위치0, 상대속도`-vEgo`, 기존 객체 상태2를 넣는다.
  정지 물체의 상대속도를0으로 잘못 보내지 않는다. 기존 가감속 요청·jerk·
  제동 한계를 바꾸지 않고 앞차가 실제 나타나면 그 실측값을 우선한다.
- 수신 SCC 사본만 편집하며 RX·radarState·planner·모델에는 객체를 넣지 않는다.
  다음 송신마다 적용 상태를 비우고 조건을 다시 확인하므로 요청/조건 해제에
  따라 복귀한다. SCC 템플릿 소실은 기존 reset으로 적용 상태를 지운다.
- `CarControl.Actuators.stopObjectActive @11`은 마지막 실제 생성 SCC의 적용
  여부다. 요청과 실제 송신 결과를 혼동하지 않는다. 두 필드의 기본값은False이며
  원본 wire tag를 유지한다. 새 설정·워커·상시 진단·Jetson 패키지 변경은 없다.

## 검사 범위

원본 요청/패킷 검사를 PV5로 한정해 포팅하고 다른 차종 강제 요청도 거부한다.
전체 publish 함수·실제 Cap'n Proto 메시지·실제 CAN 패커/파서로0.20초 경계,
역행 시각·만료·활성 해제·기어·페달/hold·실제 앞차·속도/NaN/Inf·템플릿 소실과
wire tag를 확인한다. publish에서 생성한 요청이 실제 SCC로 전달되고 만료/실제
앞차가 이를 해제하는 경로도 검사한다. 영구 모델 체크리스트에 이 검사를 추가한다.

비대상 차량과 요청 OFF의 기존 CAN 출력 동일성, 기존 조향·신호 정지·레이더·
모델/HUD 검사를 함께 실행한다. PC 데이터와 기록용 solver는 실제 ECU·ESC의
수용이나 정지 성공·폐루프 안전성의 증거가 아니다. 이번 작업에서 차량 접속·
설정 쓰기·실기기 재부팅은 하지 않는다. 콤마 업데이트·재부팅은 별도이며
모델/Jetson 패키지 교체나 Panda 펌웨어 변경은 필요하지 않다.

## 완료한 PC 검증

- 전체 publish·차종/신선도·가상 객체 요청/송신/해제·정차와 기존 조향 컨트롤러:
  **332 passed,0 failed,0 skipped**. 실제 carControl→SCC 경로를 포함한다.
- 기존37개 파일의 레이더·앞차·끼어들기·정차·SCC 회귀:
  **2,724 passed,0 failed,0 skipped**.
- 동일한 실제 패커 카운터로 PV5768개 조건은 원본과, 비대상 EV6의 강제 요청을
  포함한768개 조건은 도입 전과 비교했다. 총1,536개 송신 바이트·가속도 출력
  차이0건. 이 수치는 물리 차량 응답의 동일성 검사가 아니다.
- 다섯 모델/여섯 그룹 영구 검사: **1,781 passed,0 failed,0 skipped**.
  순수 publish/범위/신선도 검사는 자동 검사에 포함하고, CAN/Params 준비가
  필요한 패킷·정차 검사는 별도 필수 release check로 명시했다.
- 실제 state_control의 중앙 보정·크루즈/횡제어 준비 회귀 **148 passed**.
  위 검사 집합에는 중복이 있으므로 전체 건수로 더하지 않는다.
- 변경 전과 비교한 Ruff 신규 진단0건, Python 구문과 사용자 문서 검사 통과.
  기존 파일의 진단은 이번 기능과 무관해 유지했다. 문서 검사에서 controlsd의
  광범위한 DM 매핑은 DM 동작 불변의 명시적 사유로 제외했으며, 관련 KO/EN
  속도·감속 가이드는 실제로 갱신했다.

기존 Linux PC 래퍼는 스키마를 ASCII 임시 경로에 복사하고 실제 CAN 파서/패커를
사용한다. 집중 CAN/컨트롤러 검사에서는 네이티브 Params 의존성만 명시적 fixture로
대체하며 예상하지 않은 저장소 호출은 실패시킨다. 영구 검사에서는 이 fixture를
전역으로 넣지 않는다. 초기 PC 의존성 수집 실패는 수정 후 재실행했으며 통과로
집계하지 않았다. 소스/모델에 연결된 결과와 짝지은 패킷 비교는 로컬
`workspace/pv5-signal-stop-object-20261110`에 보관한다.
