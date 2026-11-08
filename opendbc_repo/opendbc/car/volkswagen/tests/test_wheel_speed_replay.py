import pytest

from opendbc.can import CANPacker
from opendbc.car import Bus
from opendbc.car.structs import CarParams
from opendbc.car.volkswagen.carstate import CarState
from opendbc.car.volkswagen.values import CAR, DBC


@pytest.mark.parametrize('candidate,message,signals', [
  (CAR.VOLKSWAGEN_GOLF_MK7, 'ESP_19',
   ('ESP_VL_Radgeschw_02', 'ESP_VR_Radgeschw_02', 'ESP_HL_Radgeschw_02', 'ESP_HR_Radgeschw_02')),
  (CAR.VOLKSWAGEN_ID4_MK1, 'ESC_51', ('VL_Radgeschw', 'VR_Radgeschw', 'HL_Radgeschw', 'HR_Radgeschw')),
  (CAR.VOLKSWAGEN_PASSAT_NMS, 'Bremse_3',
   ('Radgeschw__VL_4_1', 'Radgeschw__VR_4_1', 'Radgeschw__HL_4_1', 'Radgeschw__HR_4_1')),
])
@pytest.mark.parametrize('factor', [1.0, 1.05])
def test_packed_speed_and_stop_replay(candidate, message, signals, factor):
  cp = CarParams(carFingerprint=candidate, flags=int(candidate.config.flags),
                 transmissionType='automatic', wheelSpeedFactor=factor)
  state = CarState(cp)
  parsers = state.get_can_parsers(cp)
  packer = CANPacker(DBC[candidate][Bus.pt])
  pq = candidate == CAR.VOLKSWAGEN_PASSAT_NMS

  # Use packed CAN bytes and the production decoder, including stop after motion.
  for frame, speeds in enumerate(((0, 0, 0, 0), (36, 54, 72, 90), (0, 0, 0, 0)), 1):
    messages = [packer.make_can_msg(message, 0, dict(zip(signals, speeds, strict=True)))]
    if pq:
      # PQ uses its independent ABS vehicle-speed signal, not the wheel mean.
      messages.append(packer.make_can_msg('Bremse_1', 0, {'Geschwindigkeit_neu__Bremse_1_': 72 if any(speeds) else 0}))
    parsers[Bus.pt].update([frame * 10_000_000, messages])
    result = state.update(parsers)
    for wheel, speed in zip(('fl', 'fr', 'rl', 'rr'), speeds, strict=True):
      assert getattr(result.wheelSpeeds, wheel) == pytest.approx(speed / 3.6 * factor)
    expected = (20 if any(speeds) else 0) if pq else sum(speeds) / 4 / 3.6 * factor
    assert result.vEgoRaw == pytest.approx(expected)
    assert result.standstill == (not any(speeds))
