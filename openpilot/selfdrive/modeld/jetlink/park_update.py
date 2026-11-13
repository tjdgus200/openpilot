"""Check changed same-model runtime pins between frames, with fresh Park proof."""
import json
import time

from openpilot.selfdrive.modeld.jetlink.startup import RELEASE, RuntimeHandoff
from openpilot.selfdrive.modeld.jetlink.update_permission import Permission, parked_problem

CAPABILITY = 'carrot_park_update_v1'
REQUEST, RESPONSE = 0x4015, 0x4016


class ParkPermission(Permission):
  def allowed(self):
    try:
      if self.params.get_bool('IsOffroad') or not self.params.get_bool('IsOnroad'):
        return False  # Running runtime handoff is specifically a Park action.
      if self.sm is None:
        if self.factory is None:
          from openpilot.cereal import messaging
          self.sm = messaging.SubMaster(['carState', 'carControl', 'selfdriveState'])
        else:
          self.sm = self.factory()
      self.sm.update(0)
      now = self.clock()
      return (not parked_problem(self.sm, now)
              and all(self.sm.valid[n] and self.sm.alive[n] and 0 <= now - self.sm.recv_time[n] < .25
                      for n in ('carControl', 'selfdriveState'))
              and self.sm['carControl'].enabled is False and self.sm['selfdriveState'].active is False)
    except Exception:
      return False


class GatePermission(ParkPermission):
  def allowed(self):
    if self.allow_internal:
      from openpilot.common.jetlink_maintenance import lease
      if lease().allowed():
        self.reason = 'internal_model'
        return True
    if self.params.get_bool('IsOffroad') and not self.params.get_bool('IsOnroad'):
      return True
    return super().allowed()


class Watcher:
  def __init__(self, peer, params, clock=time.monotonic, permission=None):
    self.peer, self.clock = peer, clock
    self.enabled = peer.get('carrot_host') == 'jetson' and peer.get(CAPABILITY) is True
    self.permission = permission or ParkPermission(params, clock=clock)
    from openpilot.common.jetlink_maintenance import CAPABILITY as INTERNAL_CAPABILITY
    self.internal = peer.get(INTERNAL_CAPABILITY) is True
    self.next_poll = self.next_manifest = self.next_attempt = 0.

  def changed_manifest(self, spec):
    from openpilot.common.jetson_model import selected_release
    try:
      manifest = selected_release(RELEASE, required=True)
      if (manifest.get('delivery') != 'usb' or manifest['source_commit'] == self.peer.get('carrot_source_commit')
          or manifest['model']['sha256'] != spec.sha256 or manifest['model']['size'] != spec.nbytes):
        return None
      return manifest
    except (OSError, ValueError, KeyError, TypeError):
      return None  # A malformed/new pin must not interrupt the active model.

  def tick(self, client, *, spec=None):
    if not self.enabled or self.clock() < self.next_poll:
      return
    self.next_poll = self.clock() + .2
    if self.clock() < max(self.next_manifest, self.next_attempt):
      return
    self.next_manifest = self.clock() + 1.
    manifest = self.changed_manifest(client.spec if spec is None else spec)
    if manifest is None:
      return
    proof = None
    if self.internal:
      from openpilot.common.jetlink_maintenance import lease
      if lease().allowed():
        proof = lease().proof
    if proof is None and not self.permission.allowed():
      return
    self.next_attempt = self.clock() + 5.
    seq = client._next_seq()
    client.t.send_json(REQUEST, seq, {'manifest': manifest, **({'internal': proof} if proof else {'parked': True})})
    message = client._expect(RESPONSE, seq, 2.)
    if len(message.payload) > 128:
      raise ValueError('Oversized Park update acknowledgement')
    if json.loads(bytes(message.payload)) == {'accepted': True}:
      raise RuntimeHandoff('Park update requested; reconnecting to empty boot gate')


def wait_for_running_update(client, peer, params, connected, publish, *, wifi=None, sleep=time.sleep):
  """Reconcile a resident PV5 runtime before ENGINE_REQ or synthetic warmup.

  After only the comma reboots, HELLO can reach the old running server instead
  of the empty boot gate. Engine preparation must not hide the update path, nor
  submit probe inference that keeps the host's recent-inference guard closed.
  The expected spec only selects a pin; the host still checks its own model,
  package trust, ownership proof and service retirement before any install.
  """
  watcher = Watcher(peer, params)
  if not watcher.enabled:
    return
  from openpilot.selfdrive.modeld.jetlink.contracts import JETSON_SPEC
  manifest = watcher.changed_manifest(JETSON_SPEC)
  if manifest is None or not str(manifest.get('profile', '')).startswith('jetson-pv5-'):
    return  # Other peers and non-PV5 delivery keep their existing startup path.
  while connected():
    watcher.tick(client, spec=JETSON_SPEC)
    publish('updating', peer=peer, host_update={'state': 'waiting_park', 'fraction': None})
    if wifi is not None:
      wifi.send(client)
    client.state()
    sleep(.2)
  raise ConnectionError('Jetson disconnected before running-runtime update handoff')
