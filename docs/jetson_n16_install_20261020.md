# N16 모델을 사용하는 Jetson Orin 실험 브랜치

- 콤마 저장소: https://github.com/tjdgus200/openpilot
- 브랜치: `codex/jetson-nav-runtime-port`
- N16 배포 설정 커밋: `cf036fa5783793cfa39d9efd2869039a35337d42`
- Jetson 소스 커밋: `a1d723a6b4db051070c0eab5ffceb59c982998e4`
- Jetson 패키지: https://github.com/tjdgus200/openpilot/releases/tag/jetson-n16-20261020-a1d723a6
- 원본 모델: https://huggingface.co/commaai/openpilot_driving_models/tree/eed9e515dccd5aa9be969a2d6c57eb469174a97d/96244b1f-6c4e-4adc-848d-eccd0b44b182/12800

## 필요한 기존 환경

이 패키지는 이미 Carrot Jetson 보호 DATA 런타임을 설치한 Orin용입니다.
확인한 장치는 Orin Nano Super, L4T 36.4.7, TensorRT 10.3.0입니다.
다른 Jetson·OS·TensorRT 조합까지 검증한 배포본이 아닙니다.
이 파일로 QSPI·JetPack·OS를 플래시하지 않습니다.

Jetson에는 SHA-256 USB 수신 정책이 한 번 설치돼 있어야 합니다.
없다면 다음 자료의 지원 이미지·설치 조건을 먼저 확인해 적용합니다:
https://github.com/tjdgus200/openpilot/releases/tag/jetson-usb-sha256-bootstrap-20261020-52a8ffee
기존 소유자 서명 수신기에는 동일 내용의 서명 패키지도 유지하지만,
이 저장소의 소유자 키가 등록되지 않은 장치를 대신 인증해 주지는 않습니다.
공식 서버의 서명키와 공식 복구 경로는 교체하지 않습니다.

## 콤마 설치

차량에서 분리한 탁상 또는 시동이 꺼진 안전한 설치 상태에서 진행합니다.
기존 Carrot 설치를 이 포크로 바꾸는 예시이며 기존 코드 폴더를 보관합니다.
`openpilot-n16` 폴더가 이미 있거나 clone이 실패하면 다음 단계로 진행되지 않습니다.
설정은 기존 `/data/params`를 유지합니다.

```sh
cd /data &&
git clone --depth 1 --single-branch -b codex/jetson-nav-runtime-port \
  https://github.com/tjdgus200/openpilot.git openpilot-n16 &&
mv openpilot "openpilot-before-n16-$(date +%Y%m%d-%H%M%S)" &&
mv openpilot-n16 openpilot &&
sudo reboot
```

단순히 Jetlink 프로세스만 재시작하면 부모 프로세스의 이전 모델 설정이
남을 수 있으므로 콤마를 정상 재부팅합니다.

1. 콤마 인터넷 연결을 유지해 배포 번들과 원본 ONNX를 받게 합니다.
2. 콤마와 Jetson을 데이터 지원 USB로 연결합니다.
3. Jetson 복구 점퍼를 제거하고 정상 모드로 전원을 다시 넣습니다.
4. 부팅 게이트가 모델·소스 선택을 확인하고 콤마가 USB로 패키지를 전송합니다.
   Jetson 자체 Wi-Fi 인터넷 연결은 이 USB 파일 전송에 필요하지 않습니다.
5. 파일 크기·SHA, 런타임 계약과 실행 검사를 통과해야 활성화됩니다.
   거부되면 검사를 끄지 말고 이전 릴리스 유지 및 공개 오류 상태를 확인합니다.

## 확인과 복귀

표시 모델은 `N16 Nav (96244b1f)`이며 ONNX SHA-256은
`01effc679eb46d6036ce0302673a87fe2dcfcc95a679daeeb929679e53c3647a`입니다.

공식으로 복귀하려면 공식 `carrot-nav`를 정상 설치하고 콤마와 Jetson을
재부팅합니다. 공식 브랜치의 모델·서버 선택 및 검증 경로는 유지됩니다.
필요한 공식 파일이 캐시에 없다면 인터넷으로 내려받을 수 있어야 합니다.

## 현재 검증 범위

PC 회귀 665개, 기존 격리 Orin 2,400프레임 실행과 GPU 상태 유지 비교는
통과했습니다. 이후 실제 카메라·USB 왕복·맵·크루즈 모의 입력을 포함한
탁상 시험도 끝났습니다. 5분 구간 20.003786Hz, 프레임 누락·관측한 무효
모델/위치 입력/플래너 출력은 0건입니다.

전체 추론 호출은 평균 43.10ms, P99 53.49ms였고 6,001회 중 138회가
50ms를 초과했습니다. 매 프레임 50ms 마감이나 실주행 안전성을 보장한
릴리스로 해석하면 안 됩니다. CPU와 TensorRT 간 수치 차이의 허용 기준,
실제 주행·지도 타일 렌더링·전체 차량 제어 부하는 별도 검증 대상입니다.
자세한 조건과 수치는 [실측 보고서](jetson_n16_bench_20261020.md)를 확인합니다.
