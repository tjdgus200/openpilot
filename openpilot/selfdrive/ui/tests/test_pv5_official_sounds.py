"""Actual PCM loader/playback/profile methods on PC, without speaker/device I/O."""

import ast
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace as NS
import wave

import numpy as np
import pytest

from openpilot.cereal import car, log
from openpilot.selfdrive.ui.pv5_sound_assets import OFFICIAL_SOUND_FILES, load_official_sound_pack

ROOT = Path(__file__).resolve().parents[4]
ASSETS = ROOT / 'openpilot/selfdrive/assets/sounds_pv5_official'
A = car.CarControl.HUDControl.AudibleAlert


def production_class(text=None):
  source = ast.parse(text or (ROOT / 'openpilot/selfdrive/ui/soundd.py').read_text(encoding='utf-8'))
  scope = {
    'np': np,
    'os': os,
    'wave': wave,
    'BASEDIR': str(ROOT),
    'SAMPLE_RATE': 48000,
    'MAX_VOLUME': 1.0,
    'AudibleAlert': A,
    'log': log,
    'SELFDRIVE_STATE_TIMEOUT': 5.0,
    'time': NS(monotonic=lambda: 100.0),
    'Params': lambda: NS(get_int=lambda *_: 100),
    'cloudlog': NS(error=lambda *_: None, exception=lambda *_: None),
    'load_official_sound_pack': load_official_sound_pack,
  }
  functions = [n for n in source.body if isinstance(n, ast.FunctionDef)]
  exec(compile(ast.Module(body=functions, type_ignores=[]), 'actual-sound-helpers', 'exec'), scope)
  mapping = next(n.value for n in source.body if isinstance(n, ast.AnnAssign) and getattr(n.target, 'id', '') == 'sound_list')
  scope['sound_list'] = eval(compile(ast.Expression(mapping), 'actual-sound-map', 'eval'), scope)
  cls = next(n for n in source.body if isinstance(n, ast.ClassDef) and n.name == 'Soundd')
  methods = {'load_sounds', 'update_sound_profile', 'get_sound_data', 'update_alert', 'get_audible_alert', 'update_carrot_alert'}
  cls.body = [n for n in cls.body if isinstance(n, ast.Assign) or isinstance(n, ast.FunctionDef) and n.name in methods]
  exec(compile(ast.Module(body=[cls], type_ignores=[]), 'actual-sound-methods', 'exec'), scope)
  return scope['Soundd']


def engine(language='ko'):
  cls = production_class()
  sound = cls()
  sound.lang = language
  sound.current_alert, sound.current_alert_type = A.none, ''
  sound.current_sound_frame, sound.current_volume, sound.carrot_count_down = 0, 1.0, 100
  sound.load_sounds()
  return sound


class ProfileSM:
  def __init__(self, fingerprint='KIA_PV5', valid=True, not_car=False):
    self.valid = {'carParams': valid}
    self.cp = NS(carFingerprint=fingerprint, notCar=not_car)

  def __getitem__(self, key):
    assert key == 'carParams'
    return self.cp


@pytest.mark.parametrize('name', sorted(set(OFFICIAL_SOUND_FILES.values())))
def test_actual_official_assets_match_pinned_manifest_and_pcm_format(name):
  manifest = json.loads((ASSETS / 'manifest.json').read_text())
  data = (ASSETS / name).read_bytes()
  assert hashlib.sha256(data).hexdigest() == manifest['files'][name]['sha256']
  assert manifest['source_commit'] == '6939d172e85e3b91f16d90cc8de72e8f938cc196'
  with wave.open(str(ASSETS / name)) as wav:
    assert (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) == (1, 2, 48000)
    assert 0 < wav.getnframes() <= 96000


@pytest.mark.parametrize('language', ['ko', 'en', 'zh-CHS'])
@pytest.mark.parametrize(
  'fingerprint,valid,not_car,expected',
  [('KIA_PV5', True, False, True), ('KIA_PV5', False, False, False), ('KIA_PV5', True, True, False), ('OTHER', True, False, False), ('', True, False, False)],
)
def test_actual_profile_selection_and_legacy_fallback(language, fingerprint, valid, not_car, expected):
  sound = engine(language)
  sound.update_sound_profile(ProfileSM(fingerprint, valid, not_car))
  assert sound.pv5_profile == expected
  assert sound.loaded_sounds is (sound.pv5_sounds if expected else sound.legacy_sounds)
  assert sound.critical_gains is (sound.pv5_gains if expected else sound.legacy_gains)
  # Navigation/voice, readiness and FCW's Carrot stopStop cue stay unchanged.
  for name in ('stopStop', 'systemReady', 'radarCutin', 'audio5', 'trafficSignGreen'):
    alert = getattr(A, name)
    assert sound.pv5_sounds[alert] is sound.legacy_sounds[alert]


def test_preloaded_arrays_share_aliases_and_switch_only_between_tones(monkeypatch):
  sound = engine()
  assert sound.pv5_sounds[A.engage] is sound.pv5_sounds[A.engage2] is sound.pv5_sounds[A.longEngaged]
  assert sound.pv5_sounds[A.prompt] is sound.pv5_sounds[A.promptRepeat]
  sound.update_alert(A.engage)
  original = sound.loaded_sounds
  sound.update_sound_profile(ProfileSM())
  assert sound.loaded_sounds is original
  sound.get_sound_data(len(sound.loaded_sounds[A.engage]))
  sound.update_alert(A.none)
  assert sound.current_alert == A.none
  monkeypatch.setattr(wave, 'open', lambda *_a, **_k: (_ for _ in ()).throw(AssertionError('No profile-path I/O')))
  for _ in range(1000):
    sound.update_sound_profile(ProfileSM())
  assert sound.pv5_profile and sound.loaded_sounds is sound.pv5_sounds
  sound.update_alert(A.disengage)
  sound.update_sound_profile(ProfileSM('OTHER'))
  assert sound.pv5_profile
  sound.get_sound_data(len(sound.loaded_sounds[A.disengage]))
  sound.update_alert(A.none)
  sound.update_sound_profile(ProfileSM('OTHER'))
  assert not sound.pv5_profile


@pytest.mark.parametrize('name', ['engage', 'disengage', 'refuse', 'prompt'])
def test_actual_new_one_shot_finishes_once(name):
  sound = engine()
  sound.update_sound_profile(ProfileSM())
  alert = getattr(A, name)
  sound.update_alert(alert)
  length = len(sound.loaded_sounds[alert])
  assert np.any(sound.get_sound_data(length))
  assert not np.any(sound.get_sound_data(length))
  sound.update_alert(A.none)
  assert sound.current_alert == A.none


@pytest.mark.parametrize('name', ['promptRepeat', 'promptDistracted', 'warningSoft', 'warningImmediate'])
def test_actual_new_repeating_pcm_and_critical_gain(name):
  sound = engine()
  sound.update_sound_profile(ProfileSM())
  alert = getattr(A, name)
  sound.update_alert(alert, 'driverDistracted3/permanent' if name == 'warningImmediate' else '', critical=True)
  sound.current_volume = 0.0
  length = len(sound.loaded_sounds[alert])
  first = sound.get_sound_data(length)
  np.testing.assert_array_equal(first, sound.get_sound_data(length))
  assert np.max(np.abs(first)) == pytest.approx(1.0)


@pytest.mark.parametrize('failure', [OSError, EOFError, ValueError, MemoryError, wave.Error])
def test_new_pack_failure_keeps_existing_warning_pack(failure):
  sound = engine()
  scope = sound.load_sounds.__globals__

  def fail(*_):
    raise failure('injected pack failure')

  scope['load_official_sound_pack'] = fail
  sound.load_sounds()
  sound.update_sound_profile(ProfileSM())
  assert not sound.pv5_pack_ready and not sound.pv5_profile
  assert sound.loaded_sounds is sound.legacy_sounds
  sound.update_alert(A.warningImmediate, 'driverUnresponsive3/permanent', critical=True)
  assert np.any(sound.get_sound_data(len(sound.loaded_sounds[A.warningImmediate])))


def test_new_pcm_preserves_scc_one_shot_and_silent_red_priority():
  from openpilot.selfdrive.selfdrived.tests.test_pv5_scc_warning import sound_state

  sound = engine()
  sound.update_sound_profile(ProfileSM())
  pulse = sound_state('pv5SccDecel/warning', A.prompt)
  sound.get_audible_alert(pulse)
  length = len(sound.loaded_sounds[A.prompt])
  assert np.any(sound.get_sound_data(length))
  sound.get_audible_alert(pulse)
  assert not np.any(sound.get_sound_data(length))
  quiet = sound_state('pv5SccDecelSilent/warning')
  quiet['carrotMan'].leftSec = 10
  sound.get_audible_alert(quiet)
  assert not np.any(sound.get_sound_data(length))
  sound.get_audible_alert(pulse)
  assert np.any(sound.get_sound_data(length))


def test_callback_uses_matching_preloaded_pcm_and_gain_snapshot():
  sound = engine()
  sound.update_sound_profile(ProfileSM())
  sound.update_alert(A.warningImmediate, 'driverUnresponsive3/permanent', critical=True)
  pack = sound.active_sound_pack
  # Simulate legacy compatibility attributes being staged for the next pack.
  sound.loaded_sounds, sound.critical_gains = sound.legacy_sounds, sound.legacy_gains
  expected = pack[0][A.warningImmediate] * pack[1][A.warningImmediate]
  np.testing.assert_array_equal(sound.get_sound_data(len(expected)), expected)


@pytest.mark.parametrize('kind', ['header', 'stereo', 'empty', 'truncated'])
def test_real_malformed_wave_is_rejected(tmp_path, kind):
  file = tmp_path / 'engage.wav'
  if kind == 'header':
    file.write_bytes(b'invalid WAV')
  else:
    with wave.open(str(file), 'wb') as wav:
      wav.setnchannels(2 if kind == 'stereo' else 1)
      wav.setsampwidth(2)
      wav.setframerate(48000)
      wav.writeframes(b'' if kind == 'empty' else b'\x01\x00' * 20)
    if kind == 'truncated':
      file.write_bytes(file.read_bytes()[:-10])
  cls = production_class()
  with pytest.raises((ValueError, EOFError, wave.Error)):
    load_official_sound_pack(tmp_path, cls.load_sounds.__globals__['sound_list'], A, {}, {})
