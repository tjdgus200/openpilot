"""PC audio regression subset: actual Soundd arbitration and generated PCM, no device."""
import ast
from pathlib import Path
from types import SimpleNamespace
import time

import numpy as np
from openpilot.cereal import car, log


def _checks():
  # Run the actual arbitration/mixer and its existing PCM tests. Device startup,
  # msgq and WAV loading are outside this PC-only harness.
  root = Path(__file__).resolve().parents[2]
  tree = ast.parse((root / 'openpilot/selfdrive/ui/soundd.py').read_text(encoding='utf-8'))
  cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'Soundd')
  methods = {'get_sound_data', 'update_alert', 'update_carrot_alert', 'get_audible_alert'}
  cls.body = [n for n in cls.body if isinstance(n, ast.Assign) or isinstance(n, ast.FunctionDef) and n.name in methods]
  functions = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in
               ('dm_warning_volume', 'check_selfdrive_timeout_alert')]
  sounds = next(n for n in tree.body if isinstance(n, ast.AnnAssign) and n.target.id == 'sound_list')
  entries = [(k, v) for k, v in zip(sounds.value.keys, sounds.value.values, strict=True)
             if k.attr in ('prompt', 'warningImmediate')]
  sounds.value.keys, sounds.value.values = map(list, zip(*entries, strict=True))
  namespace = dict(np=np, time=time, car=car, log=log, MAX_VOLUME=1., SELFDRIVE_STATE_TIMEOUT=5.,
                   AudibleAlert=car.CarControl.HUDControl.AudibleAlert, SimpleNamespace=SimpleNamespace,
                   cloudlog=SimpleNamespace(error=lambda *args: None))
  exec(compile(ast.fix_missing_locations(ast.Module(body=[sounds, *functions, cls], type_ignores=[])),
               'soundd-production-methods', 'exec'), namespace)
  tests = ast.parse((root / 'openpilot/selfdrive/ui/tests/test_soundd.py').read_text(encoding='utf-8'))
  selected = {'test_angle_limit_sound_without_dm_and_escalation_from_attention',
              'test_angle_limit_sound_yields_to_critical_and_cancels_stale_pending',
              'test_steering_attention_plays_once_and_yields_to_critical_alert',
              'test_timeout_warning_takes_priority_over_navigation_updates'}
  classes = [n for n in tests.body if isinstance(n, ast.ClassDef) and n.name in ('_SoundSubMaster', 'TestSoundd')]
  classes[-1].body = [n for n in classes[-1].body if isinstance(n, ast.FunctionDef) and n.name in selected]
  exec(compile(ast.Module(body=classes, type_ignores=[]), 'soundd-existing-tests', 'exec'), namespace)
  return namespace['TestSoundd']()


def test_angle_limit_audio_without_dm_and_escalation():
  _checks().test_angle_limit_sound_without_dm_and_escalation_from_attention()


def test_angle_limit_audio_critical_priority_and_stale_cancellation():
  _checks().test_angle_limit_sound_yields_to_critical_and_cancels_stale_pending()


def test_preserve_dm_advisory_episode_and_priority():
  _checks().test_steering_attention_plays_once_and_yields_to_critical_alert()


def test_preserve_timeout_priority():
  _checks().test_timeout_warning_takes_priority_over_navigation_updates()
