"""Exercise actual supervision methods with bounded, owned test children only."""
import ast
from pathlib import Path
from types import SimpleNamespace


def load_process(source=None, clock=None):
  import time
  path = Path(source) if source else Path(__file__).parents[1] / 'process.py'
  names = {'ManagerProcess', 'PythonProcess', 'NativeProcess', 'ensure_running', 'join_process'}
  definitions = [node for node in ast.parse(path.read_text(encoding='utf8')).body
                 if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name in names]
  reports = []
  namespace = {'Params': object, 'launcher': None, 'nativelauncher': None, 'BASEDIR': '.',
               'report_process_exit': lambda *args: reports.append(args), 'flush_crash_reports': lambda: None,
               'cloudlog': SimpleNamespace(info=lambda *_: None, error=lambda *_: None),
               'car': SimpleNamespace(CarParams=object),
               'log': SimpleNamespace(ManagerState=SimpleNamespace(ProcessState=SimpleNamespace(new_message=SimpleNamespace)))}
  exec('import os, signal\nfrom abc import ABC, abstractmethod\n'
       'from collections.abc import Callable, ValuesView\nfrom multiprocessing import Process, get_context', namespace)
  namespace['time'] = clock or time
  exec(compile(ast.Module(body=definitions, type_ignores=[]), str(path), 'exec'), namespace)
  namespace['reports'] = reports
  return namespace


def stubborn_worker(ready):
  import signal
  import time
  signal.signal(signal.SIGINT, signal.SIG_IGN)
  ready.set()
  while True:
    time.sleep(0.01)


def real_stop_probe(source=None):
  import json
  import multiprocessing
  import time
  ns = load_process(source=source)
  procs, children, calls = [], [], []
  try:
    for index in range(2):
      ready = multiprocessing.Event()
      child = multiprocessing.Process(target=stubborn_worker, args=(ready,))
      child.start()
      children.append(child)
      assert ready.wait(3), 'owned child readiness timeout'
      p = ns['PythonProcess'](f'owned{index}', 'unused', lambda *_: False)
      p.proc = child
      procs.append(p)
    started = time.monotonic()
    while any(p.proc is not None for p in procs):
      assert time.monotonic() - started < 18, 'owned children failed to exit'
      before = time.monotonic()
      ns['ensure_running'](procs, False)
      calls.append(time.monotonic() - before)
      for p in procs:
        if p.proc is not None:
          state = p.get_process_state_msg()
          assert not state.shouldBeRunning
      time.sleep(0.01)
    elapsed = time.monotonic() - started
    assert all(child.exitcode is not None for child in children)
    print(json.dumps({'max_supervision_s': max(calls), 'elapsed_s': elapsed, 'ticks': len(calls),
                      'owned_children': len(children), 'reaped': len(children),
                      'unexpected_crash_reports': len(ns['reports'])}))
  finally:
    for child in children:
      if child.is_alive():
        child.kill()
      child.join(2)
      child.close()


if __name__ == '__main__':
  import sys
  real_stop_probe(sys.argv[1] if len(sys.argv) > 1 else None)
