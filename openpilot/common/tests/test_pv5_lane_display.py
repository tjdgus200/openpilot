from types import SimpleNamespace

import pytest

from opendbc.car import structs
from openpilot.common.pv5_lane_display import pv5_lane_display_codes


class Source(dict):
  valid = {"carParams": True, "carState": True}


@pytest.fixture
def source():
  src = Source(
    carParams=SimpleNamespace(carFingerprint="KIA_PV5", notCar=False),
    carState=structs.CarState(canValid=True),
  )
  src.valid = Source.valid.copy()
  src.logMonoTime = {"carState": 1_000_000_000}
  marking = src["carState"].pv5LaneMarking
  marking.displayMonoTime = 900_000_000
  marking.leftDisplayCode = 21
  marking.rightDisplayCode = 0
  return src


@pytest.mark.parametrize("delay,expected", ((0, (21, 0)), (150_000_000, (21, 0)), (150_000_001, (-1, -1)), (-1, (-1, -1))))
def test_local_age_bound(source, delay, expected):
  assert pv5_lane_display_codes(source, now_ns=1_000_000_000 + delay) == expected


def test_jetson_uses_original_source_and_normalized_elapsed_clocks(source):
  source.logMonoTime["carState"] = 8_000_000_000
  assert pv5_lane_display_codes(source, source_event_mono_ns=1_000_000_000, now_ns=8_100_000_000) == (21, 0)
  assert pv5_lane_display_codes(source, source_event_mono_ns=1_000_000_000, now_ns=8_160_000_000) == (-1, -1)
  assert pv5_lane_display_codes(source, source_event_mono_ns=800_000_000, now_ns=8_100_000_000) == (-1, -1)


@pytest.mark.parametrize("problem", ("carParams", "carState", "canTimeout", "oldFields"))
def test_invalid_and_legacy_data_remain_unclassified(source, problem):
  if problem in ("carParams", "carState"):
    source.valid[problem] = False
  elif problem == "canTimeout":
    source["carState"].canTimeout = True
  else:
    source["carState"] = structs.CarState(canValid=True, leftLaneLine=0, rightLaneLine=21)
    source["carState"].pv5LaneMarking.monoTime = 900_000_000
    source["carState"].pv5LaneMarking.leftType = 0
  assert pv5_lane_display_codes(source, now_ns=1_000_000_000) == (-1, -1)


def test_display_code_wire_roundtrip_and_non_pv5_path(source):
  source["carState"].pv5LaneMarking.rightDisplayCode = 32
  with structs.CarState.from_bytes(source["carState"].to_bytes()) as restored:
    source["carState"] = restored
    assert pv5_lane_display_codes(source, now_ns=1_000_000_000) == (21, -1)
  source["carParams"].notCar = True
  assert pv5_lane_display_codes(source, now_ns=1_000_000_000) is None
