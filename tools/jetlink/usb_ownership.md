# PV5 USB 포트 전환 준비

기준 소스는 `carrot-nav-pv5`의 `b333436a1`과 AGNOS `19.8-carrot-bt1`의 `f5b3f77`이다. `usb_owner_policy.py`는 PC에서 실행 가능한 판정 후보이며, 실제 USB 포트에는 연결하지 않았다.

| 상태 | USB 포트에 허용할 용도 |
| --- | --- |
| 차량 분리 확인, 기존 ADB 옵션 ON | PC 정비용 USB ADB와 키 인증 SSH |
| 차량 연결, P단·시동 OFF 포함 | Jetlink |
| 분리 상태 미확인, 부팅 중, 판다 메시지 끊김 | Jetlink |
| 차량 분리 확인, ADB 옵션 OFF | Jetlink |

분리 확인은 유효한 `pandaStates`에 하니스 감지 회로가 있는 Panda 보드가 1~8개 있고, 모두 하니스 분리와 두 점화 신호 OFF를 연속 보고할 때 성립한다. White·Grey나 기종을 확인할 수 없는 보드는 분리 근거로 사용하지 않는다. 후보 시험값은 메시지 만료 250ms와 진입 확인 500ms다. 같은 메시지를 반복 읽어도 진입이 빨라지지 않으며 하니스 재연결, 메시지 오류·만료, ADB OFF를 판정하는 즉시 PC 선택을 취소한다. C++ 판다 메시지는 `CLOCK_BOOTTIME`을 사용하므로 소비 쪽도 같은 시계를 사용해야 한다. 판다 증거가 없는 기기는 Jetlink로 남는다.

## 운영 코드에서 연결해야 할 지점

1. AGNOS의 `set_adb.sh`가 `AdbEnabled`에 따라 기본 ADB/NCM을 바인딩한다. 기본 USB 사용 허가는 이 초기 바인딩과 런타임 토글에서 모두 검사해야 한다. 매니저가 뜨기 전에는 분리 증거가 없으므로 이 경로의 자동 바인딩을 보류해야 한다. 부팅 복구 경로에도 같은 정책이 적용되는지 확인한다.
2. 차량 상태를 읽는 쪽은 일반 프로세스 권한을 사용한다. 포트 전환 담당은 허가 만료와 소유자 종료를 처리하고, 기존 gadget의 해제가 확인된 다음에만 대상을 바인딩한다. AGNOS와 Jetlink가 각각 컨트롤러를 잡는 경쟁을 제거해야 한다. 네이티브 메시지 소켓을 root 프로세스가 먼저 생성하면 이전 부팅 회귀가 다시 발생할 수 있다.
3. Jetlink USB 네트워크에서는 PC에서 콤마로 들어오는 SSH를 IPv4와 IPv6 모두 확인한다. 기존 Jetson 업데이트와 콤마에서 젯슨으로 향하는 SSH는 별도로 대조한다. USB 케이블이 연결됐다는 정보만으로 PC와 젯슨을 구분할 수 없다. 기존 인증과 신뢰 설정을 유지한다.
4. 실물 인수 시험은 케이블을 꽂은 채 콜드 부팅, 분리 상태에서 PC SSH 성공, 차량 하니스 연결 시 PC 세션 종료, 연결된 P단·시동 OFF에서 Jetlink 복구, 판다 통신 중단과 전환 중 데몬 종료를 포함한다. 현재 사용 중인 콤마에서 분리 시 유효한 판다 메시지가 계속 나오는지도 확인한다.

소스 참고 [Carrot AGNOS 기본 ADB 설정](https://github.com/ajouatom/agnos-builder/blob/f5b3f77e59f1917f95e0752630ca08050463eede/userspace/usr/comma/set_adb.sh)

## PC 시험

`python -B tools/jetlink/test_usb_owner_policy.py -v`를 저장소 루트에서 실행한다. 시험은 포트 소유권의 판정만 검증한다. 실제 ADB/SSH 연결·종료나 Jetson 준비 시간은 장비에서 확인해야 한다.
