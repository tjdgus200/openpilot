# 2026-11-12 일일 동기화: 신규 기능 보류

## 고정 출처와 적용 범위

- target: `587412f312a2d60986c6437bc901cb6ba46828fd`
- upstream carrot-wip: `bc8eb4c2e1aac3023c99f3914e657ed9638beab7`
- merge-base: `682ceeb484f93b2017f08548afbd18e452c291d5`
- 직전 검토 upstream: `1032f8641bca0a85c0cd3662fbd3d1eddc783684`
- 실행 식별자: `20261112-070002-3fff04`

사용자가 전달한 ASSESS/Discord 사전 보고 계획에 따라 이번 APPLY는 이 보류
기록만 추가한다. 런타임·설정·검사·패키지·운영 정책은 변경하지 않는다.
새로 완전히 반영한 upstream 커밋은 없으므로 이번 `applied`는 빈 목록이다.
직전 누적 `previous.applied` 42개는 재적용하지 않으며 이 기록으로 지우지 않는다.
이 커밋은 target을 유일한 부모로 하는 문서 커밋이며 upstream merge를 만들지 않는다.

신규 12개 커밋/29개 경로와 누적 이력의 중복은 없다. 이름 변경 탐지를 끈
트리 비교에서 merge-base→upstream은 621개, target→upstream은 707개 경로다.
APPLY에서도 lazy fetch를 끈 객체 조회로 신규 29개 경로의 upstream blob이
모두 로컬에 없음을 확인했다. ASSESS의 네트워크 조회도 실패했다. 따라서 제목과
경로 검토를 구현 본문 검토나 기능 동등성 확인으로 취급하지 않는다.

## 신규 변경의 보류·제외 사유

| upstream SHA | 처리 및 남은 검토 |
| --- | --- |
| `df90d30833d5f0a180aaec61db94a3b4dd0047e7` | 전체 보류. 기존 PV5 신호정차 송신 객체와 겹친다. 소스 확보 후 PV5 범위, 최종 SCC 조건, 실제 앞차 우선, 해제, 비PV5 패킷 회귀 검토 필요. |
| `a041875117457d5b887d8a1d8afec3916e89637a` | 진단 의존 묶음 보류. 전력 모드/클럭 관측의 기본 OFF, 수집 비용, 기존 표시와 중복, 모델 우선권 검토 필요. |
| `e63b6f360945747ca4dc39fd237f3ee40204c0ec` | observer/bootstrap 검사를 선행 구현 및 기존 부팅·업데이트 계약과 함께 검토해야 한다. |
| `e56e71473c66d32093bc4c2ff05dcfd37c7ccb3f` | 스케줄러 진단의 주기·수명·미지원 처리·OFF 동작 확인 필요. |
| `e12057a89b3602c3cbc986fa5ce386ac8b4453b5` | 구형 런타임 롤백과 observer 서비스 종료/복구 검토 필요. |
| `f07617011dc02856d1e0ae5fa649908f1d11387b` | production inference bridge 타이밍/응답 계약과 진단 의존성 검토 필요. |
| `9945cdb9d2f413f40cda3698dc14645bd7801f5a` | 공식 N16 배포 핀은 보호된 PV5 전용 패키지를 대체할 수 없다. 다섯 모델의 호환 패키지 검증 없이 새 호스트 기능을 활성화하지 않는다. |
| `bc8eb4c2e1aac3023c99f3914e657ed9638beab7` | Ray 전용 PID/웹 설정으로 제외. |
| `ed157919a43e7f0c6a2ab7b48e1d5c10a48f7422` | Ray 전용 조사 기록으로 제외. |
| `ecdd04393430fd11f649e19b57774aca1cc20f9c` | Casper 전용 조사 기록으로 제외. |
| `19549c7c77b27a76ff71069c1fc9ed2b3e812532` | Casper 전용 조사 기록으로 제외. |
| `7d318b6eda555063919d2c0b669b6abea4c125ef` | Casper 전용 조사 기록으로 제외. 다른 차량 실험을 PV5 증거로 사용하지 않는다. |

## 기존 미해결 범위

`44306cae4e62fb76fac683c911113cdb2a72d4dc`의 현재 target 구현은 공통
0.20초 비전 전환이다. `DPathRadarController`의 기본 OFF인 `pv5_refinements`는
별도의 PV5 동일 객체/정지물체 개선을 제어하며, 공통 `_confirm_vision_transition`
호출을 차종 한정하지 않는다. live/replay 정책과 아래 기록은 이번 직접 요청의
비전 전환 기본 OFF·PV5 한정 조건과 불일치한다.

- [과거 PV5 한정 기록](pv5_vision_transition_20261109.md)
- [공통 전환 변경 기록](carrot_vision_transition_20261110.md)
- [필수 보존 체크리스트](pv5_model_preservation_checklist.md)

현재 사용자 요청이 과거 문서보다 우선한다. 다만 승인된 이번 계획은 이 불일치를
명시하고 런타임을 자동 되돌리지 않는 범위다. 비PV5 현재 출력도 바뀌는 범위 조정과
live/replay/캐시/차종 회귀 검토는 남아 있다. 현재 검사가 통과하더라도 PV5 한정
요청까지 충족했다고 보고하지 않는다.

다음 이전 보류도 유지한다.

- `ec79cae36ec3e9c897093cc987842e50f3001543`,
  `1032f8641bca0a85c0cd3662fbd3d1eddc783684`: 취소/예측 방향지시,
  스키마, 타차종, 음성 의존 기능 전체 보류.
- `7d337b05495077d79ed52455776a2afa6edc1682`: 기존 PV5 신호정차 포트 유지,
  Panda/Casper 등 나머지 혼합 기능 보류.
- `f356434713cc10a39c64812e2d37b68dbb73b71d`: 기존 publish 신선도 수정 유지.
  전체 커밋 동등성 미확정이므로 applied로 승격하지 않는다.
- `b8f7f3b4057213e91d3db7efd4bd5e960134cb34`: crop 경계 표시,
  renderer/패키지 검토 보류.
- `fe2ed6ee73c645ad81716ce42eaa9b3322afafef` 및 나머지 기존 보류 유지.

## 보존 경계

13개 동작 영역은 target 대비 소스 변경 없이 유지한다. 아래 목록은 코드 불변
범위이며 실물 동작 검증 성공을 의미하지 않는다.

1. 차량 BSD OR fresh 로컬 ONNX, 점멸/최상단 경고/밝기 복귀.
2. 기존 저부하 Jetson 전력·온도·CPU/MEM/GPU/추론 통계와 미수신 표시.
3. PV5 배터리 화면, P/정확한 정지 dwell, 108전압/15온도 및 저장 설정.
4. 등록된 단일 OBD, strict UART/AT/framing, 전력 우선/adaptive pacing 및 정리 후 재접속.
5. 실제 관측 기준 전력5초/기타60초 TTL, fresh 배지, future/expired 거부.
6. Q92와 저장10/15/20/25/30FPS, 단일 USB 소유권, 모델 우선 및 기존 fallback.
7. AGNOS/Jetlink 준비 전 USB 입력전류0 강제·재확인.
8. 기존 업데이트 권한, 해시/모델/저장소/probe/rollback 및 실행 중 엔진 교체 금지.
9. reader/watchdog/endpoint/native/AIO/FD/UDC 종료 확인 후 복구.
10. GPU 재귀/CUDA graph/LUT/같은 프레임 준비 겹침/공유RAM 기본값과 정확한 N16 게이트.
11. DID 응답 경계, strict parser, 제한된 quarantine/ATI 복구 및 TTL·취소·정리 보호.
12. 모든 파서 finite 검사, 내부 모델 이후 join, 검증된 coherent warp 및 버퍼/fence 수명.
13. 연결 중 같은 모델 protocol2 업데이트 권한 재검사, 승인 갱신/철회 및 검은 부팅 진단.

최신 차선 경계 보조, 조향 복귀, AE/FOV, 광각 하향0..100, HUD 경고/아이콘/
배터리 표시와 정차 사고 보고 경로도 그대로 유지한다. 과거 Blendful runtime을
복원하지 않는다. 다섯 모델 패키지 및 bootstrap pin은 변경하지 않는다.
현재 package source 접두사는 N16 `3e60f150`, Flip Flop `07ee9cad`,
CTV3M `2594550c`, Light speed `1671addb`, v2 `7c2c5400`이다.
아카이브 다운로드·실행 및 공개 자산 검증은 이번 범위에서 수행하지 않는다.

## 실행할 검증과 결과 위치

기존 owner 검증기를 수정하지 않고, 이 문서 커밋을 대상으로 실행한다.
아래 `WORKTREE`, `RUN_DIR`는 실행 컨텍스트의 경로이고 `TARGET`은 위 고정 SHA다.
WSL 명령에는 해당 경로를 `/mnt/c/...`로 변환한다.

```text
WSL CarrotJetson:
/opt/carrot-daily-pc-venv/bin/python <owner>/validate.py WORKTREE TARGET RUN_DIR/validation.json
/opt/carrot-daily-pc-venv/bin/python tools/jetlink/check_pv5_models.py --report RUN_DIR/pv5-model-checks.json
Windows:
py -3.12 <owner>/validate_pc.py WORKTREE TARGET RUN_DIR/obd-validation.json
```

실행 결과의 기준은 RUN_DIR의 JSON과 개별 로그, 최종 APPLY 응답이다. 두
`preservation_passed`와 `pc_contracts_passed`가 모두 true이고 필수 실패·스킵·미수집이
없어야 검사 통과다. 이 문서는 실행 전 출처/범위를 고정하며 통과를 선결하지 않는다.
소스/보호 경로 검토는 `apply-source-audit.json`, 최종 부모·작업 트리·변경 범위는
`apply-final-audit.json`에 기록한다. 신규 CAN/모델/웹 설정 변경은 없다.

에이전트 push, 장치 접속·설치·재부팅, 새 스케줄/하위 에이전트는 없다.
PC 결과는 실차 안전, 20Hz, P99, BLE/USB/RF 복구시간 또는 장기 안정성의 증거가 아니다.
