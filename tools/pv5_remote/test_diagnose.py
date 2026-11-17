import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from tools.pv5_remote import diagnose as d


class DiagnosticChecks(unittest.TestCase):
  def test_root_never_opens_ipc(self):
    with patch.object(d.os, 'geteuid', return_value=0, create=True), patch.object(d.runpy, 'run_path') as load:
      self.assertEqual(d.source_snapshot(), {'source_error': 'run_as_normal_terminal_user'})
      load.assert_not_called()

  def test_errors_do_not_expose_exception_text(self):
    for error in (PermissionError('SECRET_PRIVATE_PATH'), RuntimeError('SECRET_KEY'), ValueError('RAW_LOG')):
      self.assertNotIn('SECRET', d.error_name(error))
      self.assertNotIn('RAW_LOG', d.error_name(error))

  def test_unit_output_is_allowlisted_even_on_partial_configuration_failure(self):
    with tempfile.TemporaryDirectory() as folder, patch.object(d, 'ROOT', Path(folder)):
      with patch.object(d, 'query', return_value=b'ActiveState=inactive\nResult=SECRET_KEY\nNRestarts=12\nSecret=RAW_LOG\n'):
        result = d.protected_snapshot()
      self.assertEqual(result['unit'], {'ActiveState': 'inactive', 'Result': 'other', 'NRestarts': '12'})
      self.assertEqual(result['protected_error'], 'FileNotFoundError')
      self.assertNotIn('SECRET', json.dumps(result))

  def test_source_failure_is_short_and_sanitized(self):
    with patch.object(d.os, 'geteuid', return_value=1000, create=True):
      with patch.object(d.runpy, 'run_path', side_effect=PermissionError('SECRET')):
        self.assertEqual(d.source_snapshot(), {'source_error': 'PermissionError'})

  def test_main_only_protected_child_is_elevated_and_no_service_mutation(self):
    with patch.object(d.sys, 'argv', ['diagnose.py']), patch.object(d.os, 'geteuid', return_value=1000, create=True):
      with patch.object(d, 'source_snapshot', return_value={'started': False}), patch.object(d, 'query', return_value=b'{"backend":"Running"}') as query:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
          d.main()
        args = query.call_args.args[0]
        self.assertEqual(args[:2], ['sudo', '-n'])
        self.assertEqual(args[-1], '--protected')
        self.assertIn('PV5_REMOTE_DIAG ', out.getvalue())
        self.assertNotIn('restart', ' '.join(args))

  def test_protected_child_does_not_read_ipc(self):
    with patch.object(d.sys, 'argv', ['diagnose.py', '--protected']), patch.object(d, 'source_snapshot') as source:
      with patch.object(d, 'protected_snapshot', return_value={'unit': {'ActiveState': 'active'}}):
        with contextlib.redirect_stdout(io.StringIO()) as out:
          d.main()
        self.assertEqual(json.loads(out.getvalue()), {'unit': {'ActiveState': 'active'}})
        source.assert_not_called()

  def test_command_budget_and_stderr_never_escape(self):
    from types import SimpleNamespace
    for result in (SimpleNamespace(returncode=1, stdout=b'', stderr=b'SECRET'),
                   SimpleNamespace(returncode=0, stdout=b'x' * 65537, stderr=b'')):
      with patch.object(d.subprocess, 'run', return_value=result):
        with self.assertRaisesRegex(ValueError, '^query_failed$'):
          d.query(['fixture'])


if __name__ == '__main__':
  unittest.main()
