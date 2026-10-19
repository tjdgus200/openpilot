"""Real resolver/remote-service regression for a lost HUD snapshot."""
import base64
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import hud
import hud_protocol
from cluster_live import OpenpilotLiveSource
from main import DEFAULT_USB_BRIGHTNESS, resolved_usb_brightness


class Source:
  def __init__(self, value):
    self.value = value
    self.calls = 0

  def screen_brightness_percent(self):
    self.calls += 1
    return self.value


@pytest.mark.parametrize('last', [0, 1, 43, 65, 100])
def test_repeated_unavailable_input_preserves_last_brightness_then_recovers(last):
  policy = hud.StableBrightness(resolved_usb_brightness)
  source = Source(last)
  assert policy(0, source, auto_enabled=True) == last
  source.value = None
  # Covers short and sustained loss; no timeout promotes brightness to 80.
  assert {policy(0, source, auto_enabled=True) for _ in range(1200)} == {last}
  source.value = 0
  assert policy(0, source, auto_enabled=True) == 0
  source.value = 57
  assert policy(0, source, auto_enabled=True) == 57
  assert source.calls == 1203


def test_first_unavailable_input_retains_original_startup_fallback():
  policy = hud.StableBrightness(resolved_usb_brightness)
  assert policy(0, None, auto_enabled=True) == DEFAULT_USB_BRIGHTNESS
  assert policy(0, Source(None), auto_enabled=True) == DEFAULT_USB_BRIGHTNESS


def test_manual_changes_and_manual_to_auto_loss_follow_last_resolved_value():
  source = Source(None)
  policy = hud.StableBrightness(resolved_usb_brightness)
  assert policy(65, source, auto_enabled=True) == 65
  assert policy(0, source, auto_enabled=True) == 65
  assert policy(23, source, auto_enabled=True) == 23
  assert policy(0, source, auto_enabled=True) == 23
  source.value = 0
  assert policy(0, source, auto_enabled=True) == 0
  # Explicit CLI zero is manual-off, not unavailable automatic input.
  source.value = 90
  assert policy(0, source, auto_enabled=False) == 0


def test_brightness_memory_is_local_to_each_renderer():
  first = hud.StableBrightness(resolved_usb_brightness)
  second = hud.StableBrightness(resolved_usb_brightness)
  assert first(65, None, auto_enabled=True) == 65
  assert second(0, None, auto_enabled=True) == DEFAULT_USB_BRIGHTNESS


def test_expired_packet_keeps_only_brightness_not_vehicle_validity(tmp_path, monkeypatch):
  from openpilot.cereal import log
  clock = [10.]
  packet = tmp_path / 'hud.packet'
  monkeypatch.setattr(hud_protocol, 'HUD_PACKET', packet)
  monkeypatch.setattr(hud_protocol, '_next_snapshot_read', 0.)
  monkeypatch.setattr(hud_protocol, '_cached_snapshot', None)
  monkeypatch.setattr(hud_protocol.time, 'monotonic', lambda: clock[0])
  services = ['deviceState', 'wideRoadCameraState']
  remote = hud.RemoteSubMaster(services)
  source = object.__new__(OpenpilotLiveSource)
  source.sm = remote
  policy = hud.StableBrightness(resolved_usb_brightness)

  def fresh(started):
    clock[0] += .03  # Advance beyond read_snapshot's existing 20ms read cache.
    event = log.Event.new_message(logMonoTime=int(clock[0] * 1e9), valid=True,
                                  deviceState={'started': started, 'screenBrightnessPercent': 65})
    value = {'version': 1, 'sent': clock[0], 'events': {'deviceState': base64.b64encode(event.to_bytes()).decode()},
             'params': {}, 'alive': {'deviceState': True}, 'valid': {'deviceState': True},
             'received': {'deviceState': clock[0]}, 'mono': {'deviceState': int(clock[0] * 1e9)}}
    packet.write_bytes(hud_protocol.HEADER.pack(clock[0]) + json.dumps(value).encode())
    remote.update()

  fresh(started=False)
  assert resolved_usb_brightness(0, source, auto_enabled=True) == 0
  assert policy(0, source, auto_enabled=True) == 0
  clock[0] += .6  # Existing 0.5s freshness limit; it is intentionally unchanged.
  remote.update()
  assert hud_protocol.read_snapshot() is None
  assert not any(remote.alive.values()) and not any(remote.valid.values())
  assert not any(remote.updated.values())
  assert resolved_usb_brightness(0, source, auto_enabled=True) == DEFAULT_USB_BRIGHTNESS
  assert policy(0, source, auto_enabled=True) == 0
  fresh(started=True)
  assert remote.alive['deviceState'] and remote.valid['deviceState']
  assert policy(0, source, auto_enabled=True) == 65
  clock[0] += .6
  remote.update()
  assert not any(remote.alive.values()) and not any(remote.valid.values())
  assert policy(0, source, auto_enabled=True) == 65
  fresh(started=False)
  assert policy(0, source, auto_enabled=True) == 0
