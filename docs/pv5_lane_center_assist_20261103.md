# PV5 차로 중앙 보정 · Lane Center Assist

캐럿웹 **설정 → PV5 → 주행 보조 → 차로 중앙 보정 (실험)**에서 선택합니다. 기본값은 꺼짐입니다.
제어를 해제하고 차량을 P단에 정차하면 켜짐이 반영됩니다. 끄기는 기어와 무관하게 반영되며 설정 확인 주기는 250ms입니다.

In CarrotWeb, open **Settings → PV5 → Driving Assist → Lane Center Assist (Experimental)**.
It defaults to off. The selected setting arms when stopped in Park with control disengaged.
Off applies in any gear on the next settings check, which runs every 250 ms.

The PV5 fork applies a small additive lateral acceleration request before the existing
curvature smoothing and limiting. It requires active driving control, valid Drive
gear at 45–100 km/h, gentle curvature, two confident lane lines and agreement
between the lane center and the model path. It yields to existing lane-line
mode, driver steering or braking, turn signals, maneuver intent, blind spots,
driver monitoring alerts, nearby tracked leads, and stale input. The extra
request is capped at 0.08 m/s² and ramps in after 0.5 seconds of eligibility.
When eligibility is lost, the extra request becomes zero and the existing
curvature filter and rate limits govern the transition.

합성 입력으로 기존 조향 루프를 검증합니다. 공사 차선과 인식하지 못한
장애물은 보정 허용 조건을 통과할 수 있습니다. 이때 의도하지 않은 조향의
결과는 심각할 수 있으며, 실차 위험 발생률과 안전성은 확인되지 않았습니다.
실차 검증 전에는 일반 도로에서 끈 상태를 유지합니다.

Synthetic inputs exercise the existing steering loop. Construction markings
and unrecognized obstacles can pass the eligibility checks. Unintended steering
can have serious consequences, and physical vehicle safety and occurrence rate
are unverified. Keep the feature off on public roads pending vehicle validation.
