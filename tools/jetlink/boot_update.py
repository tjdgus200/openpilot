"""USB-only boot gate: verify the comma's release before starting model or HUD.

Runs as the existing root update service. No model/backend/renderer imports.
Network provisioning remains in the independent Wi-Fi service.
"""
import json
import re
from pathlib import Path
import sys
import subprocess
import threading
import time
import urllib.error

import update_host as update

CAPABILITY = 'carrot_boot_update_v1'
LOCAL_CAPABILITY = 'carrot_local_release_v1'
MESSAGE = 0x4003
READY = Path('/run/carrot-jetlink-boot-ready.json')
STATUS = Path('/run/carrot-jetlink-boot-update.json')
BOOT_ID = Path('/proc/sys/kernel/random/boot_id')
INTERNAL_CAPABILITY = 'carrot_internal_update_v1'


def internal_proof(value):
  return (isinstance(value, dict) and value.get('internal') is True
          and re.fullmatch('[0-9a-f]{32}', str(value.get('token', ''))) is not None
          and re.fullmatch('[0-9a-f-]{36}', str(value.get('boot', ''))) is not None
          and re.fullmatch('[0-9a-f]{40}', str(value.get('release', ''))) is not None)


def enabled():
  marker = update.ROOT / 'boot-update-required'
  if not marker.is_file():
    return False
  # Installing the new policy on a live device must not interrupt this boot.
  return marker.read_text().strip() != BOOT_ID.read_text().strip()


def runtime_ready():
  try:
    value = json.loads(READY.read_text())
    return (value['boot_id'] == BOOT_ID.read_text().strip() and
            value['source_commit'] == (update.ROOT / 'current/SOURCE_COMMIT').read_text().strip())
  except (OSError, ValueError, KeyError, TypeError):
    return False


def wait_for_runtime():
  while enabled() and not runtime_ready():
    time.sleep(.5)


def receive_wifi(payload):
  import pwd
  from wifi_protocol import receive
  owner = pwd.getpwnam('jetlink')
  # Retain the normal server's ability to replace these files after boot.
  # Root-owned files in sticky /dev/shm would block its later provisioning.
  receive(payload, owner=(owner.pw_uid, owner.pw_gid))


class Gate:
  def __init__(self, require_permission=False):
    self.require_permission = require_permission
    self.state = 'checking'
    self.fraction = None
    self.selected = None
    self.source_commit = None
    self.received = 0.
    self.next_try = 0.
    self.worker = None
    self.done = False
    self.error = ''
    self.failure_stage = None
    self.internal = None

  def report(self, state, fraction=None, *, failure_stage=None):
    self.state, self.fraction, self.failure_stage = state, fraction, failure_stage

  def telemetry(self):
    return {'state': self.state, 'fraction': self.fraction, 'error': self.error, 'failure_stage': self.failure_stage,
            'permission': 'internal_model' if self.internal is not None else 'park_or_offroad'}

  def select(self, manifest):
    # Validate before trusting equality, downloading or granting permission.
    commit = update.selected_commit(manifest)
    if update.PROTECTED_MARKER.exists():
      if json.loads(Path('/run/carrot-storage.json').read_text()).get('state') != 'protected':
        raise ValueError('Protected DATA storage unavailable')
    if manifest != self.selected:
      self.next_try = 0.
    self.selected = manifest
    self.source_commit = commit
    self.received = time.monotonic()

  def select_internal(self, value):
    if set(value) != {'manifest', 'internal'} or not internal_proof(value['internal']):
      raise ValueError('Native model ownership proof missing')
    self.select(value['manifest'])
    proof = value['internal']
    self.internal = proof
    self.require_permission = True
    wait = update.ROOT / 'updates/wait-comma-boot.json'
    if proof['release'] != self.source_commit:
      record = {'boot': proof['boot'], 'source_commit': self.source_commit}
      if not wait.exists() or json.loads(wait.read_text()) != record:
        update.atomic_json(wait, record)
    elif wait.exists():
      saved = json.loads(wait.read_text())
      if saved.get('boot') != proof['boot']:
        wait.unlink()

  def waiting_comma(self):
    # Durable across worker/Jetson restart. A daemon restart on the same comma
    # boot is not a device reboot and cannot release a paired-code barrier.
    return (update.ROOT / 'updates/wait-comma-boot.json').exists()

  def allowed(self):
    return (self.selected is not None and 0 <= time.monotonic() - self.received <
            (1. if self.require_permission else 5.))

  def _install(self, manifest):
    stage = 'verifying' if manifest.get('delivery') in ('local', 'usb') else 'downloading'
    try:
      self.report(stage, 0.)
      update.stage_manifest(manifest, progress=lambda fraction: self.report(stage, fraction))
      stage = 'installing'
      self.report(stage)
      # These services are still in wait_for_runtime(), but stop them before
      # swapping files so their next process imports the complete new release.
      subprocess.run(['systemctl', 'stop', 'carrot-jetlink', 'carrot-jetlink-hud'], check=True, timeout=45)
      if self.require_permission:
        update.activate(allowed=self.allowed)
      else:
        update.activate()
      if (update.ROOT / 'current/SOURCE_COMMIT').read_text().strip() != update.selected_commit(manifest):
        raise RuntimeError('Candidate validation failed; previous release retained')
      self.report('checking')
      self.error = ''
    except update.ActivationDeferred:
      self.report('waiting_park')
      self.error = ''
    except (urllib.error.URLError, TimeoutError, ConnectionError):
      if stage == 'downloading':
        self.report('waiting_internet')
        self.error = 'Download unavailable; retrying'
      else:
        self.report('failed', failure_stage=stage)
        self.error = 'Jetson update operation interrupted'
    except Exception as exc:
      self.report('failed', failure_stage=stage)
      self.error = str(exc)[:160]
    finally:
      self.next_try = time.monotonic() + (1 if self.state == 'waiting_park' else 30)

  def tick(self):
    if self.worker is not None and self.worker.is_alive():
      return
    if not self.allowed():
      if self.state != 'failed':
        self.report('waiting_park' if self.require_permission else 'checking')
      return
    manifest = self.selected
    if (update.ROOT / 'current/SOURCE_COMMIT').read_text().strip() == self.source_commit:
      if self.waiting_comma():
        self.report('waiting_comma_reboot')
        return
      update.atomic_json(READY, {'boot_id': BOOT_ID.read_text().strip(), 'source_commit': self.source_commit})
      READY.chmod(0o644)  # The unprivileged model/HUD guards must read this.
      self.report('ready')
      self.done = True
    elif time.monotonic() >= self.next_try:
      self.worker = threading.Thread(target=self._install, args=(manifest,), daemon=False)
      self.worker.start()


class BootstrapSession:
  def __init__(self, transport, gate):
    self.gate = gate
    self.usb_receiver = None
    self.attach(transport)

  def attach(self, transport):
    self.transport = transport
    self.last_seq = -1
    self.usb_started = False

  def close(self, *, reconnect=False):
    self.usb_started = False
    if self.usb_receiver is not None:
      if reconnect and self.usb_receiver.resumable:
        return
      self.usb_receiver.close()
      self.usb_receiver = None

  def handle(self, msg):
    from jetlink import protocol as p
    from wifi_protocol import CAPABILITY as wifi_capability, MESSAGE as wifi_message
    if msg.msg_type == p.Msg.HELLO_REQ:
      self.last_seq = msg.seq
      self.usb_started = False
      # Optional dependency for older offline bootstrap payloads.
      try:
        import usb_release as usb
      except ImportError:
        usb = None
      sha256_usb = usb is not None and usb.sha256_enabled(update.ROOT)
      try:
        current = (update.ROOT / 'current/SOURCE_COMMIT').read_text().strip()
      except OSError:
        current = None
      self.transport.send_json(p.Msg.HELLO_RESP, msg.seq, {
        'protocol': p.VERSION, 'carrot_host': 'jetson', CAPABILITY: True, LOCAL_CAPABILITY: True,
        wifi_capability: True, 'engine_state': 'none', 'loaded': None, 'sleep_after': 0,
        'carrot_usb_sha256_v1': sha256_usb, 'carrot_usb_sha256_check_v1': sha256_usb,
        'carrot_usb_sha256_resume_v1': sha256_usb,
        'usb_source_commit': current,
        'carrot_park_update_gate_v1': self.gate.require_permission,
        INTERNAL_CAPABILITY: True,
      })
      return
    if msg.seq <= self.last_seq:
      return
    self.last_seq = msg.seq
    if msg.msg_type in (0x4010, 0x4011, 0x4012, 0x4014, 0x4017):
      try:
        import usb_release as usb
        if self.gate.done or (self.gate.worker is not None and self.gate.worker.is_alive()):
          raise ValueError('Boot activation is already running')
        if update.PROTECTED_MARKER.exists() and json.loads(Path('/run/carrot-storage.json').read_text()).get('state') != 'protected':
          raise ValueError('Protected storage unavailable')
        self.gate.selected = None
        if self.usb_receiver is None:
          self.usb_receiver = usb.Receiver(update.ROOT)
        if msg.msg_type in (usb.BEGIN, usb.CHECK, usb.BEGIN_RESUME):
          if not 0 < len(msg.payload) <= 65536:
            raise ValueError('USB manifest size invalid')
          value = json.loads(bytes(msg.payload))
          if msg.msg_type == usb.CHECK:
            usb.trusted(update.ROOT, value)
            result = {'complete': usb.registered(update.ROOT, value)}
          else:
            result = self.usb_receiver.begin(value, resume=msg.msg_type == usb.BEGIN_RESUME)
            self.usb_started = True
        elif not self.usb_started:
          raise ValueError('USB transfer must start with BEGIN')
        elif msg.msg_type == usb.CHUNK:
          result = self.usb_receiver.chunk(msg.payload)
        else:
          if bytes(msg.payload) != b'{}':
            raise ValueError('Invalid USB finish')
          result = self.usb_receiver.finish()
      except Exception:
        self.close()
        reply = {'ok': False, 'error': 'USB package rejected; current release retained'}
      else:
        reply = dict(ok=True, **result)
      # An ACK write failure is a link failure, so keep any accepted resume bytes.
      self.transport.send_json(0x4013, msg.seq, reply)
    elif msg.msg_type == wifi_message:
      receive_wifi(msg.payload)
    elif msg.msg_type == MESSAGE:
      try:
        if not 0 < len(msg.payload) <= 65536:
          raise ValueError('Release manifest size invalid')
        value = json.loads(bytes(msg.payload))
        if value == {'pause': True}:
          self.gate.received = 0.
        elif isinstance(value, dict) and 'internal' in value:
          self.gate.select_internal(value)
        else:
          self.gate.select(value)
      except Exception:
        # Do not report incoming data: provisioning credentials must never leak.
        self.gate.selected = None
        self.gate.report('failed', failure_stage='verifying')
        self.gate.error = 'Selected release verification failed'
    elif msg.msg_type == p.Msg.STATE_REQ:
      self.transport.send_json(p.Msg.STATE_RESP, msg.seq, {'carrot_update': self.gate.telemetry()})
    elif msg.msg_type == p.Msg.PING:
      self.transport.send(p.Msg.PONG, msg.seq)
    else:
      # This process cannot create an engine or accept inference/upload requests.
      self.transport.send_json(p.Msg.ERROR, msg.seq, {'error': 'update_required', 'detail': 'Boot release check pending'})


def boot(parked=False):
  if not parked and runtime_ready():
    subprocess.run(['systemctl', 'start', 'carrot-jetlink', 'carrot-jetlink-hud'], check=True, timeout=45)
    return
  # Recover an interrupted transaction before making any release comparison.
  # An unrelated pending download is not activated before the current pin arrives.
  if (update.ROOT / 'updates/transaction.json').exists():
    subprocess.run(['systemctl', 'stop', 'carrot-jetlink', 'carrot-jetlink-hud'], check=True, timeout=45)
    update.activate()
  READY.unlink(missing_ok=True)
  sys.path.insert(0, str(update.ROOT / 'current/third_party/jetlink'))
  from jetlink.transport.usbbulk import UsbBulkTransport
  from jetlink.transport.base import LinkTimeout
  gate = Gate(require_permission=parked)
  transport = session = None
  while not gate.done:
    try:
      if transport is None:
        transport = UsbBulkTransport.open(timeout_ms=1000)
        if session is None:
          session = BootstrapSession(transport, gate)
        else:
          session.attach(transport)
      try:
        message = transport.recv(timeout=.25)
      except LinkTimeout:
        pass
      else:
        session.handle(message)
      gate.tick()
      update.atomic_json(STATUS, dict(gate.telemetry(), updated=time.monotonic()))
      STATUS.chmod(0o644)  # Park worker's private umask must not hide diagnostics.
    except Exception:
      if session is not None:
        session.close(reconnect=True)
      if transport is not None:
        transport.close()
      transport = None
      # A disconnected USB link cannot approve starting on a remembered pin.
      gate.received = 0.
      time.sleep(1)
  if transport is not None:
    transport.close()
  if session is not None:
    session.close()
  subprocess.run(['systemctl', 'start', 'carrot-jetlink', 'carrot-jetlink-hud'], check=True, timeout=45)
