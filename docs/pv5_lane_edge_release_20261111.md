# PV5 저속 차선 접근 보정·Jetson HUD 패키지 — 2026-11-11

사용자가 추가18km/h 제한 삭제와 새 HUD 문구 패키지 게시·푸시를 요청했다.
보정 자체의 문턱을 제거하고 기존 차량 횡제어 허용·정차·운전자/깜빡이 양보와
조향/CAN 한계를 유지했다. 정규화용 max(v,1)²는 활성 문턱이 아니다.

## 공개 패키지

다섯 ONNX의 URL·SHA256·크기, 런타임·저장 형식은 바꾸지 않았다.
각 실제 압축파일을 격리 실행하고 새 `차선 보정` 문구와 경고·연결 공백·자원
표시를 네이티브 렌더링했다. 모델별 source commit은 다르지만 소스 트리는 같다.

| 모델 | 패키지 소스 | 실제 렌더·새 문구 | 공개 파일 검증 |
| --- | --- | --- | --- |
| n16 | [9d31060f](https://github.com/tjdgus200/openpilot/releases/tag/jetson-pv5-lane-edge-n16-20261111-9d31060f) | 통과 | 4개 재다운로드·크기/SHA256 일치 |
| flip-flop | [bdc30b43](https://github.com/tjdgus200/openpilot/releases/tag/jetson-pv5-lane-edge-flip-flop-20261111-bdc30b43) | 통과 | 4개 재다운로드·크기/SHA256 일치 |
| ctv3m | [b79d8070](https://github.com/tjdgus200/openpilot/releases/tag/jetson-pv5-lane-edge-ctv3m-20261111-b79d8070) | 통과 | 4개 재다운로드·크기/SHA256 일치 |
| light-speed | [0bfcfd43](https://github.com/tjdgus200/openpilot/releases/tag/jetson-pv5-lane-edge-light-speed-20261111-0bfcfd43) | 통과 | 4개 재다운로드·크기/SHA256 일치 |
| light-speed-2 | [406886df](https://github.com/tjdgus200/openpilot/releases/tag/jetson-pv5-lane-edge-light-speed-2-20261111-406886df) | 통과 | 4개 재다운로드·크기/SHA256 일치 |

기존 공개 릴리스는 삭제하지 않았다. 다섯 선택기의 배포 핀과 N16 호환 핀을
검증한 주소·해시·크기로 갱신했다. 새 키의 기본OFF와 저장 값은 유지한다.

## PC 검증 범위

- 저속/극소 속도/0/변화·제어·다섯 모델 전달403개 통과.
- 직전4f416187 대비18km/h 이상10조건의 명령 곡률이 정확히 일치.
- 실제 앵글 제어/CAN 생성기의 저속 양방향12조건 통과. EPS 피드백은 시험 fixture다.
- 필수 다섯 모델·여섯 그룹2,358개 통과, 실패/스킵0. 중복 집합은 합산하지 않는다.
- 신경망/modeld·FOV·조향 복귀/한계 등9개 보호 소스는 직전과 동일.
- 정확한 패키지5종의 고립 import·네이티브 렌더링 통과, 공개 자산20개 검증.
- 초기 전체검사는 sparse-checkout의 dm_alerts 누락으로 수집 실패했다.
  HEAD 원본의 단일 파일만 펼쳤고 LF/CRLF 상태도 HEAD와 맞춰 재검사했다.
  단위 시험의0.3m/s 경계는 Float32 반올림을 고려해 기존 판정과 직접 비교했다.
  초기 실패·dirty 실행 보고서는 보관하며 최종 통과로 집계하지 않는다.

## 적용과 한계

콤마는 git pull 후 전체 재부팅이 필요하다. 선택 모델에 대응하는 Jetson 새 패키지는
기존 업데이트 허용 절차로 적용한다. 업데이트 조건·주행 중 재부팅 권한은 변경하지 않았다.
이 작업은 차량 설치·실차/Orin 추론시간·노면/야간 인식·이탈 방지 안전성을 검증하지 않았다.
기존 가정48조건에서22조건 이탈이 남았던 한계도 해소됐다고 주장하지 않는다.
