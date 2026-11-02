import pytest

from openpilot.selfdrive.ui import render_diagnostics as diagnostics


def test_elapsed_and_cpu_time_are_separate_and_repeated_sections_accumulate(monkeypatch):
  wall, cpu = [0], [0]
  monkeypatch.setattr(diagnostics.time, 'monotonic_ns', lambda: wall[0])
  monkeypatch.setattr(diagnostics.time, 'thread_time_ns', lambda: cpu[0])
  timing = diagnostics.RenderDiagnostics('ui', emit=lambda *a, **kw: None, enabled=True)
  captured = []
  monkeypatch.setattr(timing.runtime, 'record', lambda **values: captured.append(values))
  def render():
    wall[0] += 30_000_000
    cpu[0] += 4_000_000
    return 42
  timing.start()
  assert timing.call('lanes', render) == 42
  timing.call('lanes', render)
  timing.finish()
  assert captured == [{'work_ms': 60., 'thread_cpu_ms': 8., 'lanes_ms': 60., 'lanes_cpu_ms': 8.}]
  timing.start()
  timing.finish()
  assert captured[-1] == {'work_ms': 0., 'thread_cpu_ms': 0.}


def test_render_errors_are_not_hidden_by_diagnostics():
  timing = diagnostics.RenderDiagnostics('ui', emit=lambda *a, **kw: None, enabled=True)
  def render():
    raise ValueError('render failed')
  timing.start()
  with pytest.raises(ValueError, match='render failed'):
    timing.call('lanes', render)


def test_disabled_render_calls_callbacks_without_timers_or_samples(monkeypatch):
  def forbidden():
    pytest.fail('disabled profiler read a clock')
  monkeypatch.setattr(diagnostics.time, 'monotonic_ns', forbidden)
  monkeypatch.setattr(diagnostics.time, 'thread_time_ns', forbidden)
  timing = diagnostics.RenderDiagnostics('ui', enabled=False)
  monkeypatch.setattr(timing.runtime, 'record', lambda **kw: pytest.fail('disabled profiler recorded'))
  calls = []
  for _ in range(100):
    timing.start()
    assert timing.call('hud', lambda value: calls.append(value) or value, 42) == 42
    timing.finish()
  assert calls == [42] * 100
  assert timing.values == {} and timing.started == timing.cpu_started == 0
  with pytest.raises(ValueError, match='render failed'):
    timing.call('alert', lambda: (_ for _ in ()).throw(ValueError('render failed')))
