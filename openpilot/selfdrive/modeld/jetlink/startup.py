"""Keep USB provisioning alive while a Jetson checks/updates its boot release."""
from pathlib import Path
import time
from openpilot.selfdrive.modeld.jetlink.update_permission import Permission, bulk_scheduling, wait_for_permission
from openpilot.common.jetson_model import selected_release

CAPABILITY = 'carrot_boot_update_v1'
MESSAGE = 0x4003
RELEASE = Path(__file__).with_name('host_release.json')


class RuntimeHandoff(ConnectionError):
  """The boot gate deliberately handed its transport to the selected runtime."""


class ConnectionStartup:
  """Bounded handshake grace for an attachment after confirmed offroad absence."""
  def __init__(self):
    self.started = None
    self.ready_once = False

  def absent(self, *, offroad, onroad):
    # jetlinkd survives ignition cycles. A previous drive's successful session
    # must not turn the next cold boot's unanswered HELLO into a red fault.
    # A dropout onroad (or contradictory/unknown road state) never rearms this.
    if offroad and not onroad:
      self.started = None
      self.ready_once = False

  def begin(self, now):
    if self.started is None:
      self.started = now

  def waiting(self, error, *, now, preparing):
    return (self.started is not None and
            initial_connection_wait(error, elapsed=now - self.started,
                                    ready_once=self.ready_once, preparing=preparing))


def initial_connection_wait(error, *, elapsed, ready_once, preparing):
  # Only an unanswered first handshake is ordinary startup. Engine/model
  # errors, unsafe cleanup and failures after a working session remain errors.
  from jetlink.transport.base import LinkError, LinkTimeout
  no_reader = (isinstance(error, LinkError)
               and str(error).startswith(('gadget write had no reader for ',
                                          'gadget write had no reader before deadline;')))
  return (not ready_once and not preparing and 0 <= elapsed < 120
          and (isinstance(error, (TimeoutError, LinkTimeout)) or no_reader))


def wait_for_boot_update(client, peer, wifi, connected, publish, sleep=time.sleep):
  if peer.get('carrot_host') != 'jetson' or peer.get(CAPABILITY) is not True:
    return
  manifest = selected_release(RELEASE, required=True)
  parked_gate = peer.get('carrot_park_update_gate_v1') is True
  if parked_gate:
    from openpilot.common.params import Params
    from openpilot.selfdrive.modeld.jetlink.park_update import GatePermission
    permission = GatePermission(Params())
    def waiting():
      # Explicit revocation also reaches a candidate probe already in progress.
      client.t.send_json(MESSAGE, client._next_seq(), {'pause': True})
      publish('updating', peer=peer, host_update={'state': 'waiting_park', 'fraction': None})
      if wifi is not None:
        wifi.send(client)
      client.state()
  if manifest.get('delivery') == 'local' and peer.get('carrot_local_release_v1') is not True:
    raise ValueError('Install the PC-prepared package for this Jetson model on Jetson first')
  usb_change = False
  if manifest.get('delivery') == 'usb':
    from tools.jetlink.usb_release import SHA256_CAPABILITY, CHECK_CAPABILITY  # noqa: TID251 -- shared standalone updater
    from tools.jetlink.usb_release_client import PermissionLost, available, upload  # noqa: TID251
    if peer.get(SHA256_CAPABILITY) is not True:
      raise ValueError('Install and locally enable the SHA-256 USB receiver on Jetson once')
    usb_change = peer.get('usb_source_commit') != manifest['source_commit']
    if not usb_change:
      # A server installed through NAS can have the same commit but no USB
      # catalog. Query without writes; legacy SHA receivers check offroad.
      usb_change = peer.get(CHECK_CAPABILITY) is not True or not available(client, manifest, connected)
    if usb_change:
      from openpilot.common.params import Params
      if not parked_gate:
        permission = Permission(Params(), allow_parked=(peer.get('engine_state') == 'none'
                                                       and 'loaded' in peer and peer['loaded'] is None))
      def allowed():
        return connected() and permission.allowed()
      def waiting():
        if parked_gate:
          client.t.send_json(MESSAGE, client._next_seq(), {'pause': True})
        publish('updating', peer=peer, host_update={'state': 'waiting_park', 'fraction': None, 'integrity': 'sha256',
                                                  'reason': getattr(permission, 'reason', 'vehicle_data_unavailable')})
        if wifi is not None:
          wifi.send(client)
        client.state()
      with bulk_scheduling():
        while True:
          wait_for_permission(permission, connected, waiting, sleep)
          try:
            upload(client, manifest, Path('/data/jetson-usb-cache'), allowed,
                   lambda state, fraction: publish('updating', peer=peer,
                                                  host_update={'state': state, 'fraction': fraction, 'integrity': 'sha256'}))
            break
          except PermissionLost:
            # Permission loss is not a reason to tear down/re-enumerate USB.
            # BEGIN restarts staging only after a new fresh approval; no install
            # or file-byte requests are sent while waiting. Real I/O errors propagate.
            if not connected():
              raise ConnectionError('Jetson disconnected during USB transfer') from None
  while connected():
    if usb_change or parked_gate:
      wait_for_permission(permission, connected, waiting, sleep)
    # The release selection is independent of NetworkManager profile reads.
    # Network provisioning stays on its private channel and out of status/logs.
    client.t.send_json(MESSAGE, client._next_seq(), manifest)
    if wifi is not None:
      wifi.send(client)
    value = client.state().get('carrot_update')
    if not isinstance(value, dict):
      # The gate may hand USB to the runtime before our final STATE_REQ.
      # Greet the new server instead of sending boot selections indefinitely.
      break
    publish('updating', peer=peer, host_update=value)
    if value.get('state') == 'ready':
      break
    sleep(.2 if parked_gate else .5)
  # The gate has no model. A fresh connection must greet the real server.
  if not connected():
    raise ConnectionError('Jetson disconnected during boot release check')
  raise RuntimeHandoff('Jetson boot gate ended; reconnecting to runtime')
