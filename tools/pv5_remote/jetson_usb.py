"""Fixed USB SSH stdio endpoint, invoked inside the owner's guarded comma session."""

import json
import os
from pathlib import Path
import re
import socket
import sys
import threading


GADGET = Path('/sys/kernel/config/usb_gadget/jetlink')
STATE = Path('/dev/shm/jetlink-mobile')
NET = Path('/sys/class/net')
LOCAL = '192.168.60.1'
TARGET = ('192.168.60.254', 2222)


def require_guard(cgroup, profile, uid):
  if (uid != 0 or not isinstance(profile, dict) or not any(line.startswith('0::/') and line.endswith('/pv5-conditional-remote.service')
                          for line in cgroup.splitlines()) or
      profile.get('resource_policy') != 'parked_vehicle_capped_v1' or
      profile.get('qualified_modes') != ['parked_vehicle']):
    raise ValueError('USB management requires the guarded Park session')


def owned_interface(gadget=GADGET, state=STATE, net=NET):
  function = gadget / 'functions/ncm.jetlink'
  link = gadget / 'configs/c.1/ncm.jetlink'
  if (not (gadget / 'UDC').read_text().strip() or not link.is_symlink() or
      link.resolve(strict=True) != function.resolve(strict=True) or
      (state / 'ncm-owned').read_text().strip() != 'ncm.jetlink'):
    raise ValueError('Jetlink NCM is unavailable')
  name = (function / 'ifname').read_text().strip()
  if not re.fullmatch(r'[A-Za-z0-9_.:-]{1,15}', name) or name in ('.', '..', 'lo'):
    raise ValueError('Invalid USB interface')
  index = (net / name / 'ifindex').read_text().strip()
  if not re.fullmatch(r'[1-9][0-9]*', index) or (state / 'interface').read_text().strip() != f'{name} {index}':
    raise ValueError('USB interface ownership changed')
  return name, index


def connect_usb(owned=owned_interface, create=socket.socket):
  name, index = owned()
  stream = create(socket.AF_INET, socket.SOCK_STREAM)
  try:
    stream.settimeout(5)
    stream.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, name.encode('ascii') + b'\x00')
    stream.bind((LOCAL, 0))
    stream.connect(TARGET)
    if owned() != (name, index):
      raise ValueError('USB interface changed during connection')
    stream.settimeout(None)
    return stream
  except BaseException:
    stream.close()
    raise


def relay(stream, reader, writer):
  def upload():
    try:
      while data := reader.read(65536):
        stream.sendall(data)
    except OSError:
      pass  # A revoked outer session or unplugged cable closes this direction.
    finally:
      try:
        stream.shutdown(socket.SHUT_WR)
      except OSError:
        pass

  # stdin and the socket can become readable independently. The outer sshd's
  # controlled cgroup owns this foreground process and both halves of the stream.
  threading.Thread(target=upload, daemon=True).start()
  while data := stream.recv(65536):
    view = memoryview(data)
    while view:
      count = writer.write(view)
      if not count:
        raise OSError('USB relay output closed')
      view = view[count:]


def main():
  if len(sys.argv) != 1:
    sys.exit('This USB endpoint takes no target or command arguments')
  try:
    profile = json.loads(Path('/data/pv5-remote-access/conditional_modes.json').read_text())
    require_guard(Path('/proc/self/cgroup').read_text(), profile, os.geteuid())
    with connect_usb() as stream, open(0, 'rb', buffering=0, closefd=False) as reader, open(1, 'wb', buffering=0, closefd=False) as writer:
      # Unbuffered reads are required for short interactive SSH packets.
      relay(stream, reader, writer)
  except (OSError, ValueError) as exc:
    sys.exit(f'Jetson USB management unavailable: {exc}')


if __name__ == '__main__':
  main()
