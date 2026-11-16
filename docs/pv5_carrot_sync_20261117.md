# PV5 daily selective sync — 2026-11-17

## 고정 범위와 출처

- Target: `36891cd0f111873a5efe51ba0e201c0e951e0690`
- Upstream: `a31a59ca2d5aa5e1d515ce9d5cd575a702b12124`
- Merge base: `682ceeb484f93b2017f08548afbd18e452c291d5`
- 기존 누적 applied 42개는 다시 적용하지 않는다. 11월 14일 선별 포트와
  11월 16일 PV5 선행차/SCC 보완도 현재 target의 일부로 보존한다.
- ASSESS의 전체 경로 비교와 커밋 목록을 대조했다. 일부 upstream Git 객체는
  로컬에 없어 이번에도 전체 본문 동등성을 입증하지 못했다. 해당 변경은 보류한다.

완전 반영한 upstream 커밋:

- `bfcf747adfce97d5d93c8c98f99e50e0f20b760f`: 준비 보류 문구와 사유,
  적재/요청 모델 식별 및 상태 전환 로그의 실제 오류 등급.
- `f636c0b5a22263575ed9ffaa0cc465fa1a8a0ad9`: 실제 SwagLogger 등급 판정을
  유지하면서 테스트의 장치 ZMQ sink만 격리.

현재 snapshot 인자, 읽은 뒤 monotonic 검사, TTL, 모델 오류 우선순위를 유지한다.
같은 상태/사유/식별은 반복 기록하지 않는다. 실제 실패만 ERROR이고 정상 전환은 INFO다.
기존 전환 검사도 오류 본문 `detail`의 300자 제한과 `error` 등급 플래그를 별도로 확인한다.

## 이번 직접 요청에 따른 비전 전환 범위

이번 사용자 요청은 11월 10일 공통 적용 기록보다 우선하여 PV5 한정 범위를 지정했다.
과거 문서의 공통 적용 설명을 이번 실행의 권한으로 사용하지 않는다.

- `enable_vision_transition=False`가 독립적인 기본값이다. `pv5_refinements`와
  혼동하지 않으며 실제 live 경계는 `hyundai/KIA_PV5`에서만 두 기능을 허용한다.
- PV5의 0.20초 카메라 시각 확인, 현재 실측 레이더 값, 연속성 해제와 정상 레이더
  즉시 전환, 최신 SCC/원거리 복귀 보완은 유지한다. OFF는 확인 이력을 남기지 않는다.
- Replay는 모든 프레임의 완전하고 일관된 PV5 식별이 필요하다. 뒤늦게 나타난
  carParams도 전체 로그에서 검토하여 충돌/빈 식별은 전체 프레임을 unknown으로 둔다.
  다른 차량의 CAN 디코더를 새로 활성화하지 않는다. 캐시 버전은 7에서 8로 올린다.
- upstream `44306cae4e62fb76fac683c911113cdb2a72d4dc`의 알고리즘은 PV5에서
  보존하지만 전체 차종 적용은 제외하므로 완전 반영 목록에 올리지 않는다.
- 기존 알고리즘 검사의 수치/판정은 유지하고 ON fixture에 명시적인 admission을 준다.
  기본 OFF, live 차종, mixed/late replay, 재진입과 구형 캐시 거부 검사를 추가한다.
- 제어 검사 fixture의 비PV5 CP에는 기존 production 초기화와 맞는
  `pv5_scc_supported=False`를 추가한다. 차량 제어 소스나 검사의 기대값은 바꾸지 않는다.

## 보존 확인 범위

| 항목 | 유지한 계약 |
| --- | --- |
| 1 BSD | OEM OR fresh ONNX, 양측 표시/2.5Hz/밝기/만료 |
| 2 통계 | 실제 전력·온도·CPU/MEM/GPU/추론과 unknown 표시 |
| 3 배터리 화면 | 화면8, P/zero dwell, 108/15 heatmap, 저장값 |
| 4 OBD | 등록한 한 어댑터, 순차 요청·adaptive pacing·bounded cleanup/100ms 재시도 |
| 5 데이터 TTL | 전력5초/나머지60초, 관측 시각·future/stale 거부 |
| 6 HUD | Q92/FPS 선택, 모델 우선/idle budget·backoff |
| 7 USB 전력 | 보조 입력전류0, 기존 데이터/VBUS 역할 |
| 8 초기 업데이트 | fresh 권한·hash/model/runtime 검증·rollback |
| 9 USB cleanup | 모든 기존 자원 종료 확인, 실패 차단 |
| 10 최적화 | GPU 상태/graph/LUT/overlap/shared RAM, 정확한 N16 전용 게이트 |
| 11 OBD 응답 | strict parser/count, quarantine/ATI 한도, 기존 TTL/정리 |
| 12 Jetlink 방어 | finite 출력, join 순서, coherent warp/fence/소유권 |
| 13 연결 중 업데이트 | protocol2·fresh P/zero·승인 갱신/철회·단일 updater·boot panel |

다섯 모델의 핀·서로 다른 source_commit·neural/runtime identity, 조향/차선/노출,
SCC 한계·기본 OFF, PV5 경고음/DM, 철회된 staged cut-in 상태를 유지한다.
보호 파일 및 바이너리·펌웨어는 수정하지 않는다. 새 호스트 계약/패키지는 필요 없다.

## 검사와 한계

커밋 전 집중 검사: 1,102 passed, 0 failed/skipped. live/replay/loader/구형 캐시,
레이더/MPC 입력 준비, PV5 보완과 Jetlink startup/status/logging을 포함한다.
추가 제어 루프 검사: 142 passed. 두 Params 읽기만 0으로 고정한 PC fixture이며,
native Params·IPC·MPC 최적화기·차량을 실행한 검사는 아니다.

독립 비교 소스의 전체 radar_motion 디렉터리를 사용한 합성 replay:

| 비교 | 프레임 | 전체 출력 차이 |
| --- | ---: | ---: |
| 기본 OFF vs 44306cae 부모 `4068a12695c5d3986dc8264345391d53b3d04147` | 7,200 | 0 |
| PV5 ON/최신 refinements vs 고정 target | 7,200 | 0 |

6개 radar mode, front/corner, 10개 합성 전환/누락/불연속 조건을 비교했다.
수치는 실차 replay나 제동거리·20Hz 성능·장기 안정성 증거가 아니다.

최종 커밋에서 owner `validate.py`는 WSL CarrotJetson의
`/opt/carrot-daily-pc-venv/bin/python`, `validate_pc.py`는 Windows Python 3.12로
실행한다. 별도로 `check_pv5_models.py` 전체 체크리스트를 실행한다.
각 보고서의 source/head와 실패·스킵·필수 suite 누락 여부를 확인한다.
최종 결과와 명령/원시 로그는 RUN_DIR의 `validation.json`, `obd-validation.json`,
`model-checklist.json`, `focused.json`, `control.json`, `paired-radar.json`에 기록한다.

`docs/pv5_scc_decel_20261114.md`의 기존 `test_parser_can_valid` 초기 유효성 실패는
수정/완화하지 않았다. 이번에는 CAN parser/sender를 변경하지 않아 그 검사를 재실행하지 않았다.
초기 검사 중 발생한 sparse 모듈 누락과 fixture 불일치는 해소 후 해당 검사를 재실행했다.

## 보류

승인된 ASSESS 및 previous.deferred를 승계한다. PR580/Kick/SPAS/레버 묶음,
USB resident-wait/900초 준비, 전역 cloud/modem, StopGo 제거, B1 펌웨어/소유권,
자동 Super 펌웨어/EFI/이미지, 공식 N16 핀 대체, observer, unrelated eGPU
`fe2ed6ee73c645ad81716ce42eaa9b3322afafef`는 통합하지 않는다.
이미 선별 반영한 11월 14일 변경도 전체 동등성 증명 없이 applied로 승격하지 않는다.
미확보 본문·의존성·PV5 전용 admission·모델별 검증 패키지가 필요한 부분은 보류한다.

작업 범위는 context worktree와 RUN_DIR뿐이다. push, 장치 접근/설치/재시작,
설정 저장, 운영 정책/검증기/스케줄 변경 및 하위 에이전트 생성은 하지 않는다.
