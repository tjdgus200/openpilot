import os
import struct
import sys
from types import SimpleNamespace as NS

import pytest

from openpilot.common import params_watch
from openpilot.common.params_watch import ParamsWatch

pytestmark = pytest.mark.skipif(not sys.platform.startswith('linux'), reason='real Linux inotify')


def watcher(path):
  result = ParamsWatch(NS(get_param_path=lambda: str(path)), ['Setting'])
  assert result.fd >= 0
  return result


def test_atomic_inplace_delete_and_unrelated_changes(tmp_path):
  watch = watcher(tmp_path)
  try:
    assert not watch.changed()
    (tmp_path / 'Other').write_text('1')
    assert not watch.changed()
    (tmp_path / 'temporary').write_text('2')
    os.replace(tmp_path / 'temporary', tmp_path / 'Setting')
    assert watch.changed() and not watch.changed()
    (tmp_path / 'Setting').write_text('3')
    assert watch.changed()
    (tmp_path / 'Setting').unlink()
    assert watch.changed()
  finally:
    watch.close()


def test_directory_symlink_swap_falls_back_to_every_read(tmp_path):
  a, b, link = tmp_path / 'a', tmp_path / 'b', tmp_path / 'd'
  a.mkdir()
  b.mkdir()
  link.symlink_to(a)
  watch = watcher(link)
  link.unlink()
  link.symlink_to(b)
  assert watch.changed() and watch.fd == -1
  assert watch.changed()


@pytest.mark.parametrize('data', [struct.pack('iIII', -1, 0x4000, 0, 0),
                                struct.pack('iIII', 1, 0x8000, 0, 0), b'', b'bad',
                                struct.pack('iIII', 1, 2, 0, 100)])
def test_overflow_lost_or_malformed_watch_never_hits_cache(tmp_path, monkeypatch, data):
  watch = watcher(tmp_path)
  monkeypatch.setattr(params_watch.os, 'read', lambda *_: data)
  assert watch.changed() and watch.fd == -1
  assert watch.changed()


def test_missing_directory_or_api_falls_back(tmp_path):
  for params in (object(), NS(get_param_path=lambda: str(tmp_path / 'missing'))):
    watch = ParamsWatch(params, ['Setting'])
    assert watch.changed() and watch.fd == -1
