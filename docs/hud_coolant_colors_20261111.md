# 냉각수 온도 표시 색상 — 2026-11-11

배터리 냉각수 입구 온도에 최고·최저 온도와 동일한 표시 함수를 사용한다.
화면 정수 기준 <5°C 파랑,5~9°C 하늘색,10~30°C 흰색,31~34°C 노랑,>=35°C 빨강이다.
미수신·무효·유효시간 만료는 회색 `--°C`이며 기존26px·오른쪽 정렬을 유지한다.
밝은 테마에는 숫자 뒤에 어두운 바탕을 추가해 흰색·하늘색의 대비를 유지한다.

사용자가 정한 표시 기준이며 냉각수의 정상 작동·고장·보호 온도 기준이 아니다.
센서 수집·유효시간·열관리·BMS/CAN 경고·모델 입출력·FOV·차량 제어는 변경하지 않는다.
새 추론·계측·요청·폴링·파일 기록은 추가하지 않는다. 앞선 온도차·사용% 위치를 보존한다.

## 검증

| 항목 | PC 결과 |
| --- | --- |
| 배터리·HUD 집중 검사 | 210개 통과 |
| 필수6그룹·5모델 소스 검사 | 2,522개 통과, 실패/스킵0 |
| 정확한5종 아카이브의 네이티브 HUD 렌더 | 5/5 통과 |
| 한/영·좌우 배치·5색·0/음수/범위끝/미수신·밝은 테마 | 통과 |
| 공개 파일 재다운로드 크기/SHA256 | 20개 일치 |
| 모델/제어/OBD/색상·집계 보호 소스15개 | babf8eef와 동일 |

집중 검사와 전체 검사는 중복되므로 합산하지 않는다.
합성 입력으로 실제 패키지 렌더러를 실행한 PC 검사이며 실차/Orin 성능·안전 검증이 아니다.
차량 설치·재부팅은 미실행이다. 기존 허용 절차를 통한 새 Jetson 패키지 적용이 필요하다.

## 모델별 공개 패키지

| 모델 | 패키지 소스 / 릴리스 | 공개 파일 |
| --- | --- | --- |
| n16 | [0478b926](https://github.com/tjdgus200/openpilot/releases/tag/jetson-pv5-hud-coolant-n16-20261111-0478b926) | 4개 일치 |
| flip-flop | [c8ac3749](https://github.com/tjdgus200/openpilot/releases/tag/jetson-pv5-hud-coolant-flip-flop-20261111-c8ac3749) | 4개 일치 |
| ctv3m | [b0ab5283](https://github.com/tjdgus200/openpilot/releases/tag/jetson-pv5-hud-coolant-ctv3m-20261111-b0ab5283) | 4개 일치 |
| light-speed | [384367e5](https://github.com/tjdgus200/openpilot/releases/tag/jetson-pv5-hud-coolant-light-speed-20261111-384367e5) | 4개 일치 |
| light-speed-2 | [8f35cd73](https://github.com/tjdgus200/openpilot/releases/tag/jetson-pv5-hud-coolant-light-speed-2-20261111-8f35cd73) | 4개 일치 |
