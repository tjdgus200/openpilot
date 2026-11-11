# 배터리 온도차·사용 % 위치 조정 — 2026-11-11

- 온도차: 최저온도의 왼쪽 아래, 두 온도 사이 구분선에서16px 오른쪽에 왼쪽 정렬. 기존20px 유지.
- 사용 %: 잔량 쪽으로 이동해 정보 패널 왼쪽부터 폭의30% 지점에 오른쪽 정렬. 기존28px 유지.
- 최고·최저66px, 기존 온도5색·DROP 글자/숫자 색상과1Hz, SOC 기준값·계산을 보존한다.
- 위치와 줄임 폭만 바꾸며 추가 데이터 수집·추론·타이머·기록은 없다.

## PC 검증

| 항목 | 결과 |
| --- | --- |
| 배터리/사용량/온도 및 기존 HUD 집중 검사 | 210개 통과 |
| 최종 소스 필수6그룹·5모델 보존 검사 | 2,522개 통과, 실패/스킵0 |
| 정확한5종 압축파일 네이티브 렌더 | 5/5 통과 |
| 실제 폰트로 사용% 끝과 최고온도 시작 간격24px 초과 | 통과 |
| 온도차가 최저온도 왼쪽 아래, 사용%와40px 이상 간격 | 통과 |
| 좌우 배치·한국어/영어·부호/3자리/0/미수신·밝은 테마/경고 | 통과 |
| 공개 파일 재다운로드 크기/SHA256 | 20개 일치 |

검사 집합은 중복되므로 합산하지 않는다. 모델/FOV/제어·배터리 수집·온도색상·
프레임 집계의 보호 소스15개는 직전6d648f8c와 같다. ONNX·런타임·저장 형식과
기존 릴리스는 유지하고 선택기5종과 N16 호환 핀을 새 검증본으로 연결한다.

## 공개 패키지

| 모델 | 패키지 소스/릴리스 | 네이티브 검사 | 공개 파일 |
| --- | --- | --- | --- |
| n16 | [2fe6f7e3](https://github.com/tjdgus200/openpilot/releases/tag/jetson-pv5-hud-layout-n16-20261111-2fe6f7e3) | 통과 | 4개 일치 |
| flip-flop | [fc18cb15](https://github.com/tjdgus200/openpilot/releases/tag/jetson-pv5-hud-layout-flip-flop-20261111-fc18cb15) | 통과 | 4개 일치 |
| ctv3m | [8ffb8d6d](https://github.com/tjdgus200/openpilot/releases/tag/jetson-pv5-hud-layout-ctv3m-20261111-8ffb8d6d) | 통과 | 4개 일치 |
| light-speed | [3361b46a](https://github.com/tjdgus200/openpilot/releases/tag/jetson-pv5-hud-layout-light-speed-20261111-3361b46a) | 통과 | 4개 일치 |
| light-speed-2 | [6d707608](https://github.com/tjdgus200/openpilot/releases/tag/jetson-pv5-hud-layout-light-speed-2-20261111-6d707608) | 통과 | 4개 일치 |

콤마 Git Pull/재부팅 및 기존 허용 절차의 Jetson 패키지 업데이트가 필요하다.
차량 설치·재부팅·실차/Orin 성능 검증은 미실행이다. 전달 화면은 합성 데이터를
실제 패키지 렌더러로 그린 PC 결과이며 실차 사진이 아니다.
