# PV5 차로 중앙 보정 · Lane Center Assist

캐럿웹 **설정 → PV5 → 주행 보조 → 차로 중앙 보정 (실험)**에서 선택합니다.
기본 OFF와 기존 저장값을 유지하며, 켜기·끄기는 약 250ms 내 모델 처리에 반영됩니다.
PV5에서만 구성하며 다른 차종의 모델 출력·제어는 그대로입니다.

2026-11-10 최종 승인으로 [원본 Blendful 전체](pv5_blendful_original_20261110.md)를
modeld의 원시 파서 출력에 적용합니다. 이전9지점 근사와 별도0.08m/s² 상한,
앞차/BSD/DM/페달/기어/양쪽 차선 확률50% 차단 및 별도 진입 dwell/ramp는 제거했습니다.
원본의 차로 폭 학습·한쪽 차선 처리·회피 경로 판단·신뢰도 필터·증가/해제율과
18km/h 미만 감쇠를 사용합니다. 회피 판단은 모델의 경로 변화로 하며 장애물 존재의
정답 판정이나 충돌 회피 보장이 아닙니다.

기존 차량 조향 허용·운전자 개입 양보·각도/속도/물리/CAN 한계는 유지합니다.
중앙 보정은 모델 action에 한 번만 더합니다. Carrot 레인모드가 lateralPlan을
선택하면 그 기존 경로가 우선하며, 이때 화면은 ‘레인모드’ 대기를 표시합니다.
이는 원본 알고리즘을 추가로 제한하는 조건이 아니라 실제 적용 경로 표시입니다.

The PV5-only default-OFF setting uses the complete upstream Blendful algorithm
on raw parser outputs in modeld. Its learned width, single-line handling, planned
avoidance logic, confidence filters, slew and fading below18km/h are preserved.
The old custom0.08m/s² cap and lead/BSD/DM/pedal/gear/bilateral-confidence gates,
and custom dwell/ramp are removed. Existing vehicle steering admission, driver
override and angle/rate/physics/CAN limits remain. Correction is applied once,
before existing model smoothing; Carrot's existing lane-mode planner selection
remains unchanged. Physical vehicle safety remains unvalidated.

The gauge displays the original geometric push at the lookahead, sqrt-scaled
against0.2m as in ZoomPilot. This is display normalization, not a steering ceiling.
Missing/stale telemetry clears the gauge without changing model/control output.
Existing Jetson packages read the same controlsState fields; no host model update
is required for this comma-side post-processing change.

## 이전 표시·패키지 이력

아래 기록은 각 날짜의 표시 및 패키지 검증 이력입니다. 중앙 보정의 현행 정책은
위 원본 포팅 설명이 우선합니다.

2026-11-05 ‘모델 신뢰도’ 표시는 ‘차선 인식’으로 교체했습니다.
콤마와 Jetson HUD 모두 현재 차로의 `modelV2.laneLineProbs[1]`(왼쪽), `[2]`(오른쪽)를
각각 백분율로 표시하고, 공통 바는 둘 중 낮은 값으로 채웁니다. 각 숫자와 바의 색은
85% 이상 초록, 50% 이상 노랑, 그 미만 빨강입니다. 이 기준은 화면 표시 전용입니다.
한쪽 값이 없거나 유효하지 않으면 그쪽은 `--`, 공통 바는 비움으로 표시합니다.
모델 입력이 오래되면 양쪽 모두 `--`로 돌아갑니다. 이전 개입 예측 점수나 모델 등급으로 대체하지 않습니다.
기존 모델 메시지를 읽으므로 추가 추론이나 USB 전송 필드는 없으며, 제어 조건은 바꾸지 않습니다.
이 수치는 모델의 차선 존재 예측이며 실제 인식 정확도나 주행 안전 확률을 보장하지 않습니다.
관련 PC 테스트 276개가 통과했고, 콤마3X의 큰 게이지·카메라 카드·한쪽 미수신 화면을
실제 렌더러와 합성 입력으로 확인했습니다. 실제 차량 화면과 연결 상태 검증은 별도입니다.

PV5에서는 콤마 본체에도 좌우 차선 인식 확률, 스티어, 중앙 보정이 표시됩니다.
외부 HUD 연결로 카메라 표시가 꺼지면 화면 오른쪽 아래에 큰 세로 게이지 두 개와
중앙 보정 바를 표시합니다. 카메라 화면에서는 오른쪽에 표시하며, 주행 경고가
뜰 때는 세 항목을 숨깁니다. 스티어의 부호는 방향을, 크기는 조향 요구량을 뜻합니다.
중앙 보정은 작동 중인 방향이나 대기 사유를 보여줍니다. 오래된 데이터는 `--`로 표시합니다.
콤마3·3X에서는 큰 게이지와 글자를 30% 키우고 게이지 사이의 빈 간격을 약 20% 줄였습니다.
카메라 화면의 정보 카드도 확대했으며, 상단 조향 경고는 880×104px에 58px 글자로 표시합니다.

크리티컬 경고는 콤마 화면 전체에 빨간 배경과 큰 굵은 흰 글씨로 표시됩니다.
경고음은 최대 디지털 출력 레벨로 재생되며, 평상시 알림의 음량은 기존 설정을 따릅니다.

PV5 조향 제어가 활성화된 동안 조향 요구량 절댓값이 80% 이상이면 콤마와 외부 HUD
상단에 주황 배너를 띄웁니다. 90% 이상에서 활성 DM 시각 정책이 얼굴을 추적하면서
주의 이탈을 감지하면 배너가 빨갛게 바뀌고 콤마가 진입 시 한 번 경고음을 재생합니다.
기존 주행 경고가 우선합니다. 조향 요구량 100%는 표시 지표의 상한이며, 실제 핸들
각도나 물리적 한계를 나타내지 않습니다. Jetson에는 경고 단계만 전달합니다.

조향 경고를 포함한 Jetson 패키지 4종은 PC에서 빨간 배너를 렌더링하고
게시된 다운로드의 SHA-256을 확인했습니다. [N16](https://github.com/tjdgus200/openpilot/releases/tag/jetson-pv5-steering-advisory-n16-20261105-3ddf85da), [Flip Flop](https://github.com/tjdgus200/openpilot/releases/tag/jetson-pv5-steering-advisory-flip-flop-20261105-32c70f38), [CTV3M](https://github.com/tjdgus200/openpilot/releases/tag/jetson-pv5-steering-advisory-ctv3m-20261105-75ee44ff), [Light speed](https://github.com/tjdgus200/openpilot/releases/tag/jetson-pv5-steering-advisory-light-speed-20261105-61bcd321)

합성 입력으로 기존 조향 루프를 검증합니다. 공사 차선과 인식하지 못한
장애물은 보정 허용 조건을 통과할 수 있습니다. 이때 의도하지 않은 조향의
결과는 심각할 수 있으며, 실차 위험 발생률과 안전성은 확인되지 않았습니다.
실차 검증 전에는 일반 도로에서 끈 상태를 유지합니다.

Synthetic inputs exercise the existing steering loop. Construction markings
and unrecognized obstacles can pass the eligibility checks. Unintended steering
can have serious consequences, and physical vehicle safety and occurrence rate
are unverified. Keep the feature off on public roads pending vehicle validation.

## Jetson HUD packages

Focused PC tests passed on Windows (713) and Linux (318). The four isolated bundles passed import, HUD, schema and model queue checks. All 16 downloaded release assets matched the local SHA-256 digests. Physical hardware verification is pending.

- [n16](https://github.com/tjdgus200/openpilot/releases/tag/jetson-pv5-hud-feedback-n16-20261104-2294231d), source `2294231d`.
- [flip-flop](https://github.com/tjdgus200/openpilot/releases/tag/jetson-pv5-hud-feedback-flip-flop-20261104-524b908d), source `524b908d`.
- [ctv3m](https://github.com/tjdgus200/openpilot/releases/tag/jetson-pv5-hud-feedback-ctv3m-20261104-a9786765), source `a9786765`.
- [light-speed](https://github.com/tjdgus200/openpilot/releases/tag/jetson-pv5-hud-feedback-light-speed-20261104-313d94bd), source `313d94bd`.

## Jetson HUD startup hotfix

The next package revision fixes a pyray CFFI glyph pointer that aborted HUD startup before its first frame. All four archives passed native PC renderer startup and first-frame checks, and all 16 public assets matched their SHA-256 digests. Physical HUD verification is pending.

- [n16](https://github.com/tjdgus200/openpilot/releases/tag/jetson-pv5-hud-startup-n16-20261105-d98223c7), source `d98223c7`.
- [flip-flop](https://github.com/tjdgus200/openpilot/releases/tag/jetson-pv5-hud-startup-flip-flop-20261105-ca85307b), source `ca85307b`.
- [ctv3m](https://github.com/tjdgus200/openpilot/releases/tag/jetson-pv5-hud-startup-ctv3m-20261105-03a8375f), source `03a8375f`.
- [light-speed](https://github.com/tjdgus200/openpilot/releases/tag/jetson-pv5-hud-startup-light-speed-20261105-1c63c3e0), source `1c63c3e0`.

## 2026-11-05 차선 인식 표시 패키지

N16·Flip Flop·CTV3M·Light speed 모두 좌우 차선 확률 표시를 포함합니다. 각 패키지의 독립 실행·네이티브 HUD 첫 프레임·모델 입력 큐 계약·기존 HUD20 설정 검사를 통과했습니다. 공개 다운로드 16개를 다시 받아 로컬 SHA-256과 일치함을 확인했습니다. 기존 모델 파일, 부팅 시 모델 선택, 업데이트 허용 조건과 롤백은 유지합니다. 실기기 검증은 별도입니다.
패키지 참조 변경 후 모델 선택·번들·부팅 업데이트 회귀 테스트 67개도 통과했습니다.

- [n16](https://github.com/tjdgus200/openpilot/releases/tag/jetson-pv5-lane-probability-n16-20261105-b9664f8c), source `b9664f8c`.
- [flip-flop](https://github.com/tjdgus200/openpilot/releases/tag/jetson-pv5-lane-probability-flip-flop-20261105-4c8d5ab0), source `4c8d5ab0`.
- [ctv3m](https://github.com/tjdgus200/openpilot/releases/tag/jetson-pv5-lane-probability-ctv3m-20261105-3450c51b), source `3450c51b`.
- [light-speed](https://github.com/tjdgus200/openpilot/releases/tag/jetson-pv5-lane-probability-light-speed-20261105-0b4fc4f2), source `0b4fc4f2`.
