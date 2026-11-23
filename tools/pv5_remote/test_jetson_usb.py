"""Tests for the Park-gated comma-side Jetson USB relay."""

from contextlib import ExitStack
import io
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import threading
import uuid
from unittest.mock import Mock  # noqa: TID251 - test doubles for pytest

import pytest

from tools.pv5_remote import jetson_usb as comma  # noqa: TID251 - device helper lives outside the openpilot package


def write(path, data):
  path.parent.mkdir(parents=True, exist_ok=True)
  path.write_text(data)


def test_requires_owned_bound_usb_interface(tmp_path):
  gadget, state, net = (tmp_path / name for name in ('gadget', 'state', 'net'))
  function = gadget / 'functions/ncm.jetlink'
  link = gadget / 'configs/c.1/ncm.jetlink'
  write(function / 'ifname', 'usb9')
  write(gadget / 'UDC', 'controller')
  write(state / 'ncm-owned', 'ncm.jetlink')
  write(state / 'interface', 'usb9 8')
  write(net / 'usb9/ifindex', '8')
  link.parent.mkdir(parents=True)
  link.symlink_to(function, target_is_directory=True)
  assert comma.owned_interface(gadget, state, net) == ('usb9', '8')
  for path, wrong, correct in ((gadget / 'UDC', '', 'controller'), (state / 'interface', 'usb9 9', 'usb9 8'),
                               (state / 'ncm-owned', '', 'ncm.jetlink'), (function / 'ifname', '../usb9', 'usb9')):
    write(path, wrong)
    with pytest.raises(ValueError):
      comma.owned_interface(gadget, state, net)
    write(path, correct)
  link.unlink()
  with pytest.raises(ValueError):
    comma.owned_interface(gadget, state, net)


def test_requires_park_session_and_root():
  group = '0::/system.slice/pv5-conditional-remote.service'
  profile = {'resource_policy': 'parked_vehicle_capped_v1', 'qualified_modes': ['parked_vehicle']}
  comma.require_guard(group, profile, 0)
  for cgroup, policy, uid in (('/', profile, 0), (group + '/escape', profile, 0), (group, {}, 0), (group, [], 0),
                              (group, profile, 1000), (group, dict(profile, qualified_modes=['detached_home']), 0)):
    with pytest.raises(ValueError):
      comma.require_guard(cgroup, policy, uid)


@pytest.mark.skipif(not hasattr(socket, 'SO_BINDTODEVICE'), reason='Linux device binding')
def test_peer_reserved_and_usb_rebind_refused():
  stream = Mock()
  with pytest.raises(ValueError):
    comma.connect_usb(Mock(side_effect=[('usb1', '7'), ('usb1', '9')]), lambda *_: stream)
  stream.setsockopt.assert_called_once_with(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, b'usb1\x00')
  stream.bind.assert_called_once_with(('192.168.60.1', 0))
  stream.connect.assert_called_once_with(('192.168.60.254', 2222))
  stream.close.assert_called_once()
  setup = Path(__file__).resolve().parents[1] / 'jetlink/setup_mobile.sh'
  assert '--dhcp-range=192.168.60.2,192.168.60.253,' in setup.read_text()


def test_relay_interactive_and_half_close():
  left, right = socket.socketpair()
  with left, right:
    right.settimeout(3)
    output = io.BytesIO()
    task = threading.Thread(target=comma.relay, args=(left, io.BytesIO(b'a'), output), daemon=True)
    task.start()
    assert right.recv(2) == b'a'
    assert right.recv(2) == b''
    right.sendall(b'last reply')
    right.shutdown(socket.SHUT_WR)
    task.join(3)
    assert not task.is_alive()
    assert output.getvalue() == b'last reply'


@pytest.mark.skipif(not (sys.platform == 'linux' and shutil.which('ip') and os.geteuid() == 0),
                    reason='requires root and Linux network namespaces')
def test_native_connection_uses_only_cable_interface():
  def run(*args):
    return subprocess.run(args, capture_output=True, text=True, check=True, timeout=5)

  left, right = names = ['jl-test-' + uuid.uuid4().hex[:10] for _ in range(2)]
  with ExitStack() as cleanup:
    for ns in names:
      try:
        run('ip', 'netns', 'add', ns)
      except subprocess.CalledProcessError as exc:
        if 'not permitted' in exc.stderr.lower() or 'permission denied' in exc.stderr.lower():
          pytest.skip('isolated network namespaces unavailable to this Linux process')
        raise
      cleanup.callback(run, 'ip', 'netns', 'del', ns)
    run('ip', '-n', left, 'link', 'add', 'jlcable', 'type', 'veth', 'peer', 'name', 'jlpeer', 'netns', right)
    for ns, dev, address in ((left, 'jlcable', comma.LOCAL), (right, 'jlpeer', comma.TARGET[0])):
      run('ip', '-n', ns, 'addr', 'add', address + '/24', 'dev', dev)
      run('ip', '-n', ns, 'link', 'set', dev, 'up')
    server_code = """import socket
with socket.socket() as server:
  server.settimeout(5)
  server.bind(('192.168.60.254', 2222))
  server.listen(1)
  print('ready')
  connection, peer = server.accept()
  with connection:
    assert peer[0] == '192.168.60.1'
    connection.sendall(b'ok')
"""
    client_code = """from tools.pv5_remote.jetson_usb import connect_usb
with connect_usb(lambda: ('jlcable', '123')) as connection:
  print(connection.recv(2).decode())
"""
    server = subprocess.Popen(['ip', 'netns', 'exec', right, sys.executable, '-u', '-c', server_code],
                              stdout=subprocess.PIPE, text=True)
    try:
      assert server.stdout.readline().strip() == 'ready'
      assert run('ip', 'netns', 'exec', left, sys.executable, '-B', '-c', client_code).stdout.strip() == 'ok'
      assert server.wait(timeout=5) == 0
      assert comma.TARGET[0] not in run('ip', '-n', left, '-br', 'addr').stdout
    finally:
      if server.poll() is None:
        server.kill()
        server.wait(timeout=5)
      server.stdout.close()
