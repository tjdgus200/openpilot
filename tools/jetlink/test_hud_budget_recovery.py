"""Transient slow HUD work must not leave the admission estimator stuck forever."""
from pathlib import Path
import sys
from types import SimpleNamespace as NS

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from hud_rate import IdleBudget


@pytest.mark.parametrize('read_ms,write_ms', [(20., 2.), (2., 20.), (20., 20.)])
def test_single_slow_sample_recovers_without_model_or_HUD_process_restart(monkeypatch, read_ms, write_ms):
  from openpilot.selfdrive.modeld.jetlink import daemon
  now, sent = [100.], []
  monkeypatch.setattr(daemon.time, 'monotonic', lambda: now[0])
  monkeypatch.setattr(daemon, 'request_ready', lambda *a: False)
  budget = IdleBudget()
  budget.model_started(100.)
  budget.observe(100.04, {'display_snapshot_read_ms':read_ms, 'display_hud_send_ms':write_ms, 'display_hud_bytes':100})

  def packet():
    now[0] += .0002
    return b'fresh snapshot'

  def send(*args):
    # Every retry still fits the original nominal frame/4ms reserve in this
    # synthetic fast recovery case; actual USB timing is not guaranteed here.
    assert now[0] + .001 < budget.last_model + budget.period - .004
    sent.append(now[0])
    now[0] += .001

  client = NS(t=NS(send=send), _next_seq=lambda:len(sent)+1)
  for i in range(1,241):
    started = 100. + i*.05
    budget.model_started(started)
    now[0] = started + .030
    budget.completed(now[0], .030)
    daemon.publish_idle_hud(client, NS(packet=packet), budget, object())
  assert sent, 'one transient slow estimate caused permanent HUD starvation'
  assert sent[0] > 102.0  # No immediate blind retry of expensive work.


def test_no_snapshot_or_send_is_admitted_while_next_model_is_queued(monkeypatch):
  from openpilot.selfdrive.modeld.jetlink import daemon
  now, touched = [100.], []
  monkeypatch.setattr(daemon.time, 'monotonic', lambda:now[0])
  monkeypatch.setattr(daemon, 'request_ready', lambda *a:True)
  budget = IdleBudget()
  budget.observe(100., {'display_snapshot_read_ms':20.,'display_hud_send_ms':20.,'display_hud_bytes':100})
  for i in range(1,501):
    started = 100. + i*.05
    budget.model_started(started)
    now[0] = started + .03
    daemon.publish_idle_hud(NS(t=NS(send=lambda *a:touched.append('send'))),
      NS(packet=lambda:touched.append('read')),budget,object())
  assert touched == []


def test_heavy_inference_never_retries_display_despite_cost_aging(monkeypatch):
  from openpilot.selfdrive.modeld.jetlink import daemon
  now, touched = [100.], []
  monkeypatch.setattr(daemon.time, 'monotonic', lambda:now[0])
  monkeypatch.setattr(daemon, 'request_ready', lambda *a:False)
  budget = IdleBudget()
  budget.observe(100., {'display_snapshot_read_ms':20.,'display_hud_send_ms':20.,'display_hud_bytes':100})
  for i in range(1,501):
    started = 100. + i*.05
    budget.model_started(started)
    now[0] = started + .048
    budget.completed(now[0], .048)
    daemon.publish_idle_hud(NS(t=NS(send=lambda *a:touched.append('send'))),
      NS(packet=lambda:touched.append('read')),budget,object())
  assert touched == []


def test_persistently_slow_reads_back_off_and_never_send_a_late_snapshot(monkeypatch):
  from openpilot.selfdrive.modeld.jetlink import daemon
  now, reads, sent = [100.], [], []
  monkeypatch.setattr(daemon.time, 'monotonic', lambda:now[0])
  monkeypatch.setattr(daemon, 'request_ready', lambda *a:False)
  budget = IdleBudget()

  def packet():
    reads.append(now[0])
    now[0] += .02
    return b'late'

  for i in range(1,601):
    started = 100. + i*.05
    budget.model_started(started)
    now[0] = started + .03
    daemon.publish_idle_hud(NS(t=NS(send=lambda *a:sent.append('send'))),
      NS(packet=packet),budget,object())
  assert sent == [] and 1 < len(reads) <= 16
  assert all(b-a >= 2. for a,b in zip(reads,reads[1:],strict=False))
