"""Execute the real CAN application boundary with captured, hardware-free I/O."""
import ast
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from openpilot.selfdrive.car.card_diagnostics import should_log_card_diagnostics


@pytest.mark.parametrize('profiling', [False, True])
@pytest.mark.parametrize('fault', ['none', 'loop', 'process', 'can_timeout'])
def test_optional_stage_profiling_preserves_sends_and_fault_warning(profiling, fault):
  path = Path(__file__).parents[1] / 'card.py'
  tree = ast.parse(path.read_text(encoding='utf-8'))
  method = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == 'controls_update')
  messages, warnings, clock_calls = [], [], []
  stamp = 100_000_000

  def clock():
    clock_calls.append(1)
    return stamp

  namespace = {'car': NS(CarState=object, CarControl=object), 'PERF_DIAGNOSTICS': profiling,
               'REPLAY': False, 'time': NS(monotonic=lambda: .1, monotonic_ns=clock),
               'can_list_to_can_capnp': lambda frames, **kw: (frames, kw),
               'should_log_card_diagnostics': should_log_card_diagnostics,
               'print': lambda *args: warnings.append(args)}
  exec(compile(ast.Module(body=[method], type_ignores=[]), str(path), 'exec'), namespace)

  class Messages(dict):
    def all_alive(self, services):
      return True

  sm = Messages(modelV2=object(), radarState=object())
  sm.valid = sm.alive = dict.fromkeys(sm, True)
  command = NS(latActive=False)
  gates = []
  applies = []
  frame = (123, b'unchanged', 0)
  def apply(cc, now, model, radar):
    applies.append((cc, now, model, radar))
    return 'actuators', [frame]
  stages = ('decode', 'ci_update', 'sm_update', 'vision', 'state_tail',
            'state_total', 'publish', 'apply', 'sendcan', 'total')
  owner = NS(spas_signals=NS(enabled=False), initialized_prev=True, sm=sm,
             params=NS(get_bool=lambda _: False), CI=NS(apply=apply),
             lateral_startup=NS(update=lambda *args: gates.append(args) or True),
             tesla_signals=NS(configured=False), pm=NS(send=lambda *args: messages.append(args)),
             card_diag_recv_ns=stamp - (21_000_000 if fault == 'process' else 1_000_000),
             card_diag_stage_names=stages, card_diag_stage_current=dict.fromkeys(stages, 0),
             card_diag_stage_sum_us=dict.fromkeys(stages, 0), card_diag_stage_max_us=dict.fromkeys(stages, 0),
             card_diag_loop_max_us=51_000 if fault == 'loop' else 10_000,
             card_diag_process_max_us=0, card_diag_slow_loop=0, card_diag_slow_process=0,
             card_diag_can_timeouts=int(fault == 'can_timeout'), card_diag_frames=99)
  namespace['controls_update'](owner, NS(canValid=True), command)
  assert applies == [(command, stamp, sm['modelV2'], sm['radarState'])]
  assert len(gates) == 1 and owner.CC_prev is command and owner.last_actuators_output == 'actuators'
  assert messages == [('sendcan', ([frame], {'msgtype': 'sendcan', 'valid': True}))]
  assert len(clock_calls) == (3 if profiling else 1)
  assert bool(warnings) == (fault != 'none')
  if not profiling:
    assert len(warnings) == int(fault != 'none')
    assert owner.card_diag_stage_current == dict.fromkeys(stages, 0)
  assert owner.card_diag_frames == owner.card_diag_can_timeouts == 0
