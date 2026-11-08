"""PC scheduling/backpressure checks; no physical USB latency guarantee."""
import socket
import sys
from collections import deque
from types import SimpleNamespace as NS

import pytest

from hud_rate import IdleBudget


@pytest.fixture
def display(monkeypatch):
  if sys.platform == 'win32':
    monkeypatch.setitem(sys.modules, 'fcntl', NS())
  from openpilot.selfdrive.modeld.jetlink import daemon
  now = [10.02]
  monkeypatch.setattr(daemon.time, 'monotonic', lambda: now[0])
  a, b = socket.socketpair()
  budget = IdleBudget()
  budget.model_started(10.)
  sent, consumed = [], []
  media = deque([b'fragment1', b'fragment2', b'fragment3'])
  def snapshot():
    consumed.append('snapshot')
    return b'latest'
  def fragment():
    consumed.append('media')
    return media.popleft() if media else None
  def send(kind, seq, parts):
    sent.append((kind, seq, parts))
    now[0] += .0002
  seq = iter(range(1, 50))
  client = NS(t=NS(send=send), _next_seq=lambda: next(seq))
  publisher = NS(packet=snapshot, media_packet=fragment)
  yield NS(daemon=daemon, now=now, connection=a, model=b, budget=budget,
           client=client, publisher=publisher, sent=sent, consumed=consumed, media=media)
  a.close()
  b.close()


def pump(d):
  return d.daemon.publish_idle_hud(d.client, d.publisher, d.budget, d.connection,
                                   navigation=True, fast_receiver=True)


def test_queued_model_even_partial_header_preempts_all_display(display):
  d = display
  d.model.sendall(b'\x10')
  assert pump(d) == {}
  assert d.sent == d.consumed == []
  assert d.connection.recv(1) == b'\x10'  # Scheduling never consumes IPC.


@pytest.mark.parametrize('now', [10.045, 10.049, 10.05, 10.08])
def test_no_display_near_or_after_next_expected_model_deadline(display, now):
  d = display
  d.now[0] = now
  assert pump(d) == {}
  assert d.sent == d.consumed == []


def test_ready_request_after_snapshot_read_drops_snapshot_and_keeps_media(display):
  d = display
  def snapshot():
    d.model.sendall(b'x')
    return b'obsolete'
  d.publisher.packet = snapshot
  pump(d)
  assert d.sent == []
  assert list(d.media) == [b'fragment1', b'fragment2', b'fragment3']


def test_model_arriving_during_first_write_prevents_navigation_burst(display):
  d = display
  def send(kind, seq, parts):
    d.sent.append(kind)
    d.model.sendall(b'x')
  d.client.t.send = send
  pump(d)
  assert d.sent == [d.daemon.HUD_MESSAGE]
  assert list(d.media) == [b'fragment1', b'fragment2', b'fragment3']


@pytest.mark.parametrize('delay', [.003, .020, .100])
def test_slow_display_write_stops_media_and_future_idle_admission(display, delay):
  d = display
  def send(kind, seq, parts):
    d.sent.append(kind)
    d.now[0] += delay
  d.client.t.send = send
  pump(d)
  expected = [d.daemon.HUD_MESSAGE] + ([d.daemon.NAVI_MESSAGE] if delay == .003 else [])
  assert d.sent == expected
  assert len(d.media) == (2 if delay == .003 else 3)
  d.now[0] += .05
  assert pump(d) == {}
  assert d.sent == expected


def test_free_idle_window_preserves_fragment_order_and_sequence(display):
  d = display
  pump(d)
  assert [s[0] for s in d.sent] == [d.daemon.HUD_MESSAGE] + [d.daemon.NAVI_MESSAGE] * 3
  assert [s[1] for s in d.sent] == [1, 2, 3, 4]
  assert [s[2][0] for s in d.sent[1:]] == [b'fragment1', b'fragment2', b'fragment3']


def test_request_arriving_between_media_fragments_stops_tail_without_losing_next(display):
  d = display
  def send(kind, seq, parts):
    d.sent.append(kind)
    if kind == d.daemon.NAVI_MESSAGE:
      d.model.sendall(b'x')
  d.client.t.send = send
  pump(d)
  assert d.sent == [d.daemon.HUD_MESSAGE, d.daemon.NAVI_MESSAGE]
  assert list(d.media) == [b'fragment2', b'fragment3']


def test_model_request_during_media_read_defers_one_fragment_without_reordering(display):
  d = display
  original = d.publisher.media_packet
  def media():
    d.model.sendall(b'x')
    return original()
  d.publisher.media_packet = media
  pump(d)
  assert [s[0] for s in d.sent] == [d.daemon.HUD_MESSAGE]
  assert d.publisher._idle_media_pending[1] == b'fragment1'
  assert d.connection.recv(1) == b'x'
  d.publisher.media_packet = original
  pump(d)
  assert [s[2][0] for s in d.sent if s[0] == d.daemon.NAVI_MESSAGE] == [b'fragment1', b'fragment2', b'fragment3']
  assert d.publisher._idle_media_pending is None


def test_slow_media_read_defers_fragment_without_starving_hud(display):
  d = display
  original = d.publisher.media_packet
  def media():
    d.now[0] += .01
    return original()
  d.publisher.media_packet = media
  pump(d)
  assert [s[0] for s in d.sent] == [d.daemon.HUD_MESSAGE]
  assert d.publisher._idle_media_pending[1] == b'fragment1'
  assert pump(d)['display_hud_bytes'] > 0
  assert [s[0] for s in d.sent] == [d.daemon.HUD_MESSAGE, d.daemon.HUD_MESSAGE]
  assert d.publisher._idle_media_pending[1] == b'fragment1'


def test_expired_unsent_media_is_not_replayed_or_used_to_date_new_fragment(display):
  d = display
  d.publisher._idle_media_pending = (0., b'stale')
  original = d.publisher.media_packet
  def media():
    d.model.sendall(b'x')
    return original()
  d.publisher.media_packet = media
  pump(d)
  stamp, packet = d.publisher._idle_media_pending
  assert stamp > 0 and packet == b'fragment1'
  assert not any(s[2][0] == b'stale' for s in d.sent)


@pytest.mark.parametrize('duration,delivered', [(.003, True), (.010, True), (.030, False)])
def test_snapshot_read_uses_actual_headroom_and_rechecks_before_write(display, duration, delivered):
  d = display
  def snapshot():
    d.now[0] += duration
    return b'expensive'
  d.publisher.packet = snapshot
  pump(d)
  assert any(s[0] == d.daemon.HUD_MESSAGE for s in d.sent) is delivered
  if not delivered:
    assert len(d.media) == 3


def test_snapshot_failure_is_display_only(display):
  d = display
  d.publisher.packet = lambda: (_ for _ in ()).throw(OSError('publisher unavailable'))
  pump(d)
  assert not any(s[0] == d.daemon.HUD_MESSAGE for s in d.sent)


def test_partial_usb_write_failure_is_not_silently_ignored(display):
  d = display
  d.client.t.send = lambda *a: (_ for _ in ()).throw(ConnectionError('framing failed'))
  with pytest.raises(ConnectionError, match='framing failed'):
    pump(d)
  assert len(d.media) == 3
