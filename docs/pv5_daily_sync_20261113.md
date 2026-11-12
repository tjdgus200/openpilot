# 2026-11-13 일일 동기화: 출처와 보류 기록

## 고정 출처와 적용 범위

- target: `44f30935bce32f3d80477667bce61b9adf9a53db`
- upstream ajouatom/openpilot carrot-wip: `162da468cc4157f9b18daa4f6ac85438f51719ef`
- merge-base: `682ceeb484f93b2017f08548afbd18e452c291d5`
- 직전 검토 upstream: `bc8eb4c2e1aac3023c99f3914e657ed9638beab7`
- 실행 식별자: `20261113-070002-eb5642`

사용자가 전달한 ASSESS/Discord 사전 보고 계획에 따라 이번 APPLY는 이 문서만
추가한다. target을 유일한 부모로 커밋하며 upstream merge는 만들지 않는다.
새로 완전히 반영한 upstream 커밋은 없으므로 이번 `applied`는 빈 목록이다.
`previous.applied`의 누적 42개 이력은 보존하며 재적용하지 않는다.

APPLY에서 Git의 lazy fetch를 끄고 재확인한 신규 커밋은 14개이며 누적 이력과
중복되지 않는다. 신규 변경 41개 경로의 upstream 최신 blob은 모두 로컬에 없다.
ASSESS의 GitHub 연결도 실패했다. 커밋 제목·트리 경로 검토를 구현 본문 검토나
전체 기능 동등성 검증으로 취급하지 않는다. merge-base부터의 전체 변경 목록과
target 대 upstream 전체 트리 차이도 검토 대상이며, 선별 포트를 통째 덮어쓰지 않는다.
상세 출처·패키지·누적 SHA 목록은 실행 폴더의 `apply-source-audit.json`에 기록한다.

## 신규 변경의 처리

| upstream SHA | 처리와 남은 검토 |
| --- | --- |
| `8ca67794ca481a686a932185f6c1456435c8f864`, `5a2784cc2dc920cf54d2e40e7e4d9dad1c56b955` | 자동 Super 선택과 GPU 검증 순서는 target에 선별 포트되어 있다. observer 제외 및 본문 전체 동등성 미확인으로 applied 승격·중복 적용하지 않는다. |
| `6fabc6ac459fe07a8d4db43ccacdfb87ae69c6ba`, `d37a6d30b45ec137da36ade74c77026f4328a473`, `7689e12f0276b59e377bd985ce8bd89c666c390e` | 공식 N16 핀으로 PV5 패키지를 대체하지 않는다. 혼합된 진단·설치 도구는 본문과 다섯 패키지 호환성 검토 전 보류한다. |
| `0cb67d46cc4ac448326cc9b272ebfebdc6fde797` | 기존 power_setup/health와 중복 여부, 진단 OFF, 이미지 범위 확인 전 보류한다. |
| `a4a2c898d55708acb3a3d8608e62c45a6780796a`, `2b87210c0999bd1e142bf3b361b820860c3a44fe` | 자동 펌웨어 프로비저닝·EFI 상태 변경은 현재 펌웨어 쓰기 금지와 충돌하므로 제외한다. |
| `393b98d7335f71c795a7c57bcccbcd8c39a7bef0` | 최초 부팅 ONNX admission, protected storage, 이미지 진입점의 SHA/모델·immutable OS·bootstrap/probe/rollback 의존성 검토 전 보류한다. |
| `bc65f431c20d52fbc559925ce912ec7a0fcc71bc`, `f90a8ddb585fb70184df1c38c9b9b7fc40bce36a`, `162da468cc4157f9b18daa4f6ac85438f51719ef` | 본문 미확보 SD 이미지 배포·검증 기록을 PV5 검증이나 사용자 승인으로 전용하지 않는다. |
| `189c2faf39057c58a40099b0c874897ba81aba54`, `ffa5723eda259de6c3ef3962f687c5c8579c23ee` | Tesla 전용 속도 휠·웹 설정 및 병합은 제외한다. |

## 기존 미해결 범위

`44306cae4e62fb76fac683c911113cdb2a72d4dc`: 이번 직접 요청은 비전 전환 기본 OFF와
hyundai/KIA_PV5 live/replay 한정을 요구하지만 현재 `radar_motion/controller.py`는
차종 분기 없이 `_confirm_vision_transition`을 호출한다. 기본 OFF인
`pv5_refinements`는 별도의 동일 객체/정지물체 개선 스위치이며 비전 전환 게이트가 아니다.
[과거 PV5 한정 문서](pv5_vision_transition_20261109.md)와
[공통 정책 변경 기록](carrot_vision_transition_20261110.md)을 확인했다.
최신 직접 요청이 우선하나 승인된 이번 계획은 불일치를 기록하고 수정은 보류하는 범위다.
live/replay admission, 기록 차량 식별·캐시 무효화, PV5 알고리즘 동등성과 비PV5 복원
기준을 함께 검토해야 한다. 현재 실측값·연속성 해제·정상 레이더 즉시 전환도 보존해야 한다.
기존 검사 통과를 이 범위 불일치의 해결로 보고하지 않는다.

- `df90d30833d5f0a180aaec61db94a3b4dd0047e7`: 본문 확보 후 PV5 최종 SCC 조건,
  실제 앞차 우선·해제·비PV5 패킷 검토 필요.
- `a041875117457d5b887d8a1d8afec3916e89637a`, `e63b6f360945747ca4dc39fd237f3ee40204c0ec`,
  `e56e71473c66d32093bc4c2ff05dcfd37c7ccb3f`, `e12057a89b3602c3cbc986fa5ce386ac8b4453b5`,
  `f07617011dc02856d1e0ae5fa649908f1d11387b`, `9945cdb9d2f413f40cda3698dc14645bd7801f5a`:
  진단·observer·공식 N16 패키지 보류 유지. 기본 OFF·비용·구형 런타임·롤백 검토 필요.
- `ec79cae36ec3e9c897093cc987842e50f3001543`, `1032f8641bca0a85c0cd3662fbd3d1eddc783684`:
  이전 전체 보류 유지.
- `7d337b05495077d79ed52455776a2afa6edc1682`: 기존 PV5 포트는 보존하되 Panda/Casper 등
  혼합 기능은 보류. `f356434713cc10a39c64812e2d37b68dbb73b71d`의 기존 publish 수정도
  보존하나 전체 동등성 미확정으로 applied에 넣지 않는다.
- `b8f7f3b4057213e91d3db7efd4bd5e960134cb34`, `fe2ed6ee73c645ad81716ce42eaa9b3322afafef` 및
  이전 보류·타차종 제외 내역은 그대로 유지한다.

## 보존 경계

다음 13개 영역의 소스·검사·설정·패키지를 target 대비 변경하지 않는다.
이는 코드 보존 범위이며 실장치 검증 결과가 아니다.

1. 차량 BSD OR fresh 로컬 ONNX, 200ms 점멸·경고 우선순위·밝기 복귀.
2. 기존 저부하 Jetson 전력·온도·CPU/MEM/GPU/추론 통계와 미수신 표시.
3. 배터리 화면8, P/정확한 정지 dwell, 108전압·15온도, 저장 설정.
4. 등록한 단일 OBD, UART/AT/framing, 전력 우선·adaptive pacing·정리 후 100ms 재접속.
5. 실제 관측 기준 전력5초/기타60초 TTL, fresh 배지, future/expired 거부.
6. TurboJPEG Q92와 저장 FPS, 단일 USB 소유권·모델 우선·기존 fallback.
7. AGNOS/Jetlink 준비 전 USB 입력전류0 강제·재확인.
8. 업데이트 권한·해시/크기/모델/저장소·probe/rollback, 실행 중 엔진 교체 금지.
9. reader/watchdog/endpoint/native/AIO/FD/UDC 종료 확인 후 복구.
10. GPU 재귀/CUDA graph/LUT/준비 겹침/공유RAM 기본값과 정확한 N16 전용 게이트.
11. strict DID 응답 경계, 제한된 quarantine/ATI 복구, TTL·취소·정리 보호.
12. 모든 파서 finite 검사, 내부 모델 이후 join, coherent warp 검증·버퍼/fence 수명.
13. 연결 중 같은 모델 protocol2 업데이트, 승인 갱신·철회·검은 부팅 진단과 panel 소유권.

최신 차선 경계 보조, 조향 복귀, AE/FOV·광각 하향, HUD 경고·아이콘·배터리·COMMA 표시도
보존한다. [필수 모델 체크리스트](pv5_model_preservation_checklist.md)와
[Super 선별 포트](pv5_jetson_auto_super_20261113.md)를 기준으로 한다.
진단 OFF와 기존 health 경로를 유지하며 observer·sampler를 추가하지 않는다.
다섯 PV5 패키지와 bootstrap pin은 불변이다. 서로 다른 source_commit 다섯 개와 동일
소스 트리를 로컬에서 확인했다. 기존 문서의 공개 자산 검증은 이번 다운로드 검증이 아니다.
이번 문서 변경에는 새 패키지가 필요하지 않다. 향후 호스트/renderer 계약 변경은
다섯 호환 패키지 검증과 함께 검토한다.

## 검사와 결과 위치

문서 커밋 후 기존 검증기를 수정 없이 실행한다. WORKTREE/TARGET/RUN_DIR는 위 실행
컨텍스트 값이며 WSL에서는 Windows 경로를 `/mnt/c/...`로 변환한다.

```text
WSL CarrotJetson:
/opt/carrot-daily-pc-venv/bin/python <owner>/validate.py WORKTREE TARGET RUN_DIR/validation.json
/opt/carrot-daily-pc-venv/bin/python tools/jetlink/check_pv5_models.py --report RUN_DIR/pv5-model-checks.json
Windows:
py -3.12 <owner>/validate_pc.py WORKTREE TARGET RUN_DIR/obd-validation.json
```

실제 결과는 RUN_DIR의 JSON·개별 로그와 최종 APPLY 응답에 기록한다. 이 문서는 실행 전
출처·범위를 고정하며 통과를 선결하지 않는다. 두 `preservation_passed`와 체크리스트의
`pc_contracts_passed`가 모두 true이고 필수 실패·스킵·미수집이 없어야 검사 통과다.
보호 파일 불변·단일 부모·target 후손·clean 상태는 `apply-final-audit.json`으로 확인한다.
CAN/모니터링/모델/웹 설정 변경은 없다. PC 검사는 실차 안전, 실제 Super 활성,
20Hz/P99, 전력·열·RF 복구시간의 증거가 아니다. 장치 접근·설치·재부팅·펌웨어 쓰기,
push·새 스케줄·하위 에이전트 생성은 수행하지 않는다.
