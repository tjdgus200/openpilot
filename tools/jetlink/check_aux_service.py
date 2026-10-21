"""PC regression runner: real cereal/schema/service, import-only native IPC doubles."""
import importlib.util
import os
from pathlib import Path
import shutil
import sys
import tempfile
import types


def main():
  root = Path(__file__).resolve().parents[2]
  sys.path[:0] = [str(root), str(root / 'third_party/jetlink'), str(root / 'tools/jetlink')]
  os.environ['PYTEST_DISABLE_PLUGIN_AUTOLOAD'] = '1'
  if importlib.util.find_spec('msgq') is None:
    native = types.ModuleType('msgq')
    native.__path__ = []
    def unavailable(*args, **kwargs):
      raise RuntimeError('PC check cannot open native IPC')
    for name in ('fake_event_handle', 'drain_sock_raw', 'Context', 'Poller', 'SubSocket', 'PubSocket',
                 'SocketEventHandle', 'toggle_fake_events', 'set_fake_prefix', 'get_fake_prefix',
                 'delete_fake_prefix', 'wait_for_one_event', 'sub_sock', 'pub_sock'):
      setattr(native, name, unavailable)
    native.MultiplePublishersError = native.IpcError = RuntimeError
    sys.modules['msgq'] = native
  import openpilot
  import pytest
  with tempfile.TemporaryDirectory(prefix='pv5-aux-schema-') as directory:
    temp = Path(directory)
    for source in ('openpilot/cereal', 'opendbc_repo/opendbc/car'):
      shutil.copytree(root / source, temp / source, ignore=shutil.ignore_patterns('__pycache__'))
    openpilot.__path__.insert(0, str(temp / 'openpilot'))
    tests = sys.argv[1:] or [
      'openpilot/selfdrive/carrot/tests/test_xiaoge_vision.py',
      'openpilot/selfdrive/carrot/tests/test_xiaoge_inference.py',
      'openpilot/selfdrive/carrot/tests/test_xiaoge_nv12.py',
    ]
    return pytest.main(['-q', '-c', '/dev/null', '--noconftest', '--confcutdir=' + str(root), '-p', 'no:cacheprovider',  # noqa: TID251 -- isolated test child; plugins disabled
                        '--rootdir=' + str(root), '--tb=short', *tests])


if __name__ == '__main__':
  raise SystemExit(main())
