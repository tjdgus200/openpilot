# 팝업 점 변형·약한 반복 — 2026-11-11

기존 `babd8945`의480ms 단발 점 변형을1초 smoothstep 변형으로 바꾼다.
이후 같은 상황이 표시되는 동안2.5초 주기로 아이콘 크기를 최대8%만 부드럽게 변화시킨다.
콤마3X·Jetson HUD는 같은 `DrivingPopupSlide.motion_dots`를 사용한다.
문구·위치·12개 점·현재 화면 FPS·깜빡이 방향·220ms 진입/퇴장·정착 후 최소1초 유지·
상황 종료·차량/운전자/긴급 경고 우선순위는 유지한다. 부주의 삼각형은 변경하지 않는다.

## 비용을 제한하는 구현

- 표시에서 처음 필요할 때만 두 종류까지 좌표 표를 준비한다. 모듈 import나 생성자는 표를 만들지 않는다.
- 진입20Hz·반복10Hz의 불변12점 표를 기존 렌더 호출에서 시간으로 선택한다. 화면 FPS를 올리지 않는다.
- 매 프레임 삼각함수·점 좌표 tuple 생성이 없다. 새 추론·타이머·스레드·IPC·로깅·OBD 요청이 없다.
- 시간 이상은 정지 모양으로 복귀/다음 정상 호출에서 재시작하며, 종료 중에는 정지 모양을 사용한다.
- 모델/FOV/제어/OBD/색상/기존 두 렌더러 등19개 보호 파일이 `babd8945`와 동일하다.
- 기존 최상위 판단 함수와 팝업 update/top의 AST가 동일하다. 바뀌는 것은 UI 점 좌표 선택이다.

## PC 비교

동일 PC Python에서9회 교대 측정의 중앙값이다. GPU/모델 추론 성능 측정이 아니다.

| 좌표 선택 함수 | 기존 µs/회 | 변경 µs/회 |
| --- | ---: | ---: |
| 첫1초 | 0.554 | 0.162 |
| 상황 유지 | 0.055 | 0.183 |

유지 중 증가는0.128µs/회이며,20fps 가정 환산은 초당 약
2.56µs다. 두 종류 캐시의 PC 보관 메모리는
120.6KiB, 첫 종류 준비는 중앙값
30.9µs였다.
이 수치로 실제 콤마3X/Orin 추론 간섭이0이라고 보장하지 않는다.

| 검증 | 결과 |
| --- | --- |
| 팝업/경고/방향/시간 오류/캐시 검사 | 73개 통과 |
| 필수6그룹·5모델 검사 | 2,533개 통과, 실패/스킵0 |
| 실제 네이티브 전후 비교 | 20fps·6.2초·총500프레임 |
| 두 화면의 반복 픽셀·2.5초 동일 위상 | 통과 |
| 정확한5종 아카이브 반복·경고/온도/DROP 렌더 | 5/5 통과 |
| 공개 파일 크기/SHA256 | 20개 일치 |

검사 수는 중복되므로 합산하지 않는다. 최초 비교 렌더의 구버전/새 테스트 import
호환 오류는 공통 메시지 fixture만 분리해 해결한 뒤 네 화면을 다시 실행했다.
GIF는 합성 입력으로 실제 렌더러를 실행한 것이며 실차/실기기 검증이 아니다.
차량 설치·재부팅은 미실행이다. 기존 허용 업데이트 절차로 새 Jetson 패키지가 필요하다.

## 모델별 공개 패키지

| 모델 | 패키지 소스 / 릴리스 | 공개 파일 |
| --- | --- | --- |
| n16 | [e659275a](https://github.com/tjdgus200/openpilot/releases/tag/jetson-pv5-hud-motion-loop-n16-20261111-e659275a) | 4개 일치 |
| flip-flop | [9234118d](https://github.com/tjdgus200/openpilot/releases/tag/jetson-pv5-hud-motion-loop-flip-flop-20261111-9234118d) | 4개 일치 |
| ctv3m | [9a888abf](https://github.com/tjdgus200/openpilot/releases/tag/jetson-pv5-hud-motion-loop-ctv3m-20261111-9a888abf) | 4개 일치 |
| light-speed | [54a4c976](https://github.com/tjdgus200/openpilot/releases/tag/jetson-pv5-hud-motion-loop-light-speed-20261111-54a4c976) | 4개 일치 |
| light-speed-2 | [966519f1](https://github.com/tjdgus200/openpilot/releases/tag/jetson-pv5-hud-motion-loop-light-speed-2-20261111-966519f1) | 4개 일치 |
