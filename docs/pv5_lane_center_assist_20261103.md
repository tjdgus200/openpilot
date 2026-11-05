# PV5 차로 중앙 보정 · Lane Center Assist

캐럿웹 **설정 → PV5 → 주행 보조 → 차로 중앙 보정 (실험)**에서 선택합니다. 기본값은 꺼짐입니다.
켜기와 끄기는 기어와 무관하게 반영됩니다. 제어기의 설정 확인 주기는 250ms입니다.
크루즈가 꺼져 있어도 AlwaysLateral 등 기존 조건에 따라 실제 조향 제어가 활성화되어 있으면 보정합니다.
조향 제어가 꺼져 있으면 보정하지 않습니다. 차선·BSD·운전자·입력 유효성 조건과 보정 상한은 유지합니다.
2026-11-05 PC 회귀 테스트 148개가 통과했습니다. 크루즈 OFF/ON 좌우 보정, 비활성화 시 기존 출력과의
일치, 조향 허용·오류·운전자 중지·재부팅 차단 및 기존 보정 거부 조건을 합성 입력으로 확인했습니다.
실제 차량과 EPS/CAN 안전 계층의 동작은 이 테스트 범위에 포함되지 않습니다.

In CarrotWeb, open **Settings → PV5 → Driving Assist → Lane Center Assist (Experimental)**.
It defaults to off. Both settings apply in any gear on the next settings check,
which runs every 250 ms.

The PV5 fork applies a small additive lateral acceleration request before the existing
curvature smoothing and limiting. It requires active lateral control (including
AlwaysLateral with cruise disabled), valid Drive
gear while moving forward, gentle curvature, two confident lane lines and agreement
between the lane center and the model path. It yields to existing lane-line
mode, driver steering or braking, turn signals, maneuver intent, blind spots,
driver monitoring alerts, nearby tracked leads, and stale input. The extra
request is capped at 0.08 m/s² and ramps in after 0.5 seconds of eligibility.
When eligibility is lost, the extra request becomes zero and the existing
curvature filter and rate limits govern the transition.

보정 전용 속도 제한은 없습니다. 저속에서는 곡률 환산에 사용하는 속도의 하한을 적용해 보정값이 커지는 것을 막습니다.
The assist follows the existing steering activation speed rules. Curvature
conversion uses a bounded denominator to limit correction at low speed.

Jetson HUD는 기존 ‘대기’를 제어 비활성, 정차, 입력 대기, 운전자 개입, 방향 전환,
앞차, 레이더, 차선 신뢰도, 경로 조건, 연속 조건 확인 및 보정 불필요로 구분합니다.
기존 레인모드와 BSD 표시는 유지합니다. 별도 크루즈 활성 조건 외의 보정 허용 조건과 보정량 계산은 유지합니다.
새 대기 사유는 기존 상태 필드와 별도로 전송되며, 이전 버전은 기존 ‘대기’를 표시합니다.

모델 신뢰도 바의 높이와 숫자는 콤마 신뢰도 표시와 같은
`(1 - 최대 브레이크 해제 예측) × (1 - 최대 조향 개입 예측)` 지표를 사용합니다.
숫자는 표시용 지표이며 실제 성공 확률은 아닙니다. 색은 기존 모델 등급을 사용합니다.

PV5에서는 콤마 본체에도 모델 신뢰도, 스티어, 중앙 보정이 표시됩니다.
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
