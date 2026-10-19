"""Keep USB provisioning alive while a Jetson checks/updates its boot release."""
from pathlib import Path
import time
from openpilot.common.jetson_model import selected_release
from openpilot.selfdrive.modeld.jetlink.update_permission import Permission, bulk_scheduling, wait_for_permission

CAPABILITY = 'carrot_boot_update_v1'
MESSAGE = 0x4003
RELEASE = Path(__file__).with_name('host_release.json')


def wait_for_boot_update(client, peer, wifi, connected, publish, sleep=time.sleep):
  if peer.get('carrot_host') != 'jetson' or peer.get(CAPABILITY) is not True:
    return
  manifest = selected_release(RELEASE, required=True)
  if manifest.get('delivery') == 'usb' and manifest.get('format') == 2:
    from tools.jetlink.usb_release import SHA256_CAPABILITY, record  # noqa: TID251
    if peer.get(SHA256_CAPABILITY) is not True:
      # Old receivers cannot be changed by an unsupported unsigned packet.
      # Retain the existing signed package only when it identifies identical
      # source/model/bundle content. Never fall back after a SHA-capable reject.
      try:
        legacy = selected_release(RELEASE, required=True, prefer_sha256=False)
      except (OSError, ValueError) as exc:
        raise ValueError('Install the SHA-256 USB receiver on Jetson once; no matching legacy package') from exc
      if legacy.get('delivery') != 'usb' or legacy.get('format') != 1 or record(legacy) != record(manifest):
        raise ValueError('Install the SHA-256 USB receiver on Jetson once; no matching legacy package')
      manifest = legacy
  if manifest.get('delivery') == 'local' and peer.get('carrot_local_release_v1') is not True:
    raise ValueError('Install the PC-prepared package for this Jetson model on Jetson first')
  usb_change = False
  integrity = 'sha256' if manifest.get('format') == 2 else 'signature'
  if manifest.get('delivery') == 'usb':
    from tools.jetlink.usb_release import CAPABILITY as USB_CAPABILITY  # noqa: TID251 -- standalone updater package
    from tools.jetlink.usb_release_client import upload  # noqa: TID251
    if peer.get(USB_CAPABILITY) is not True:
      raise ValueError('Install the USB receiver and enroll your experiment public key on Jetson once')
    usb_change = peer.get('usb_source_commit') != manifest['source_commit']
    if usb_change:
      from openpilot.common.params import Params
      permission = Permission(Params(), allow_parked=(peer.get('engine_state') == 'none'
                                                     and 'loaded' in peer and peer['loaded'] is None))
      def allowed():
        return connected() and permission.allowed()
      with bulk_scheduling():
        # A newly opened subscriber initially has no messages. Keep the boot
        # gate alive and show the reason instead of resetting its USB session.
        def waiting():
          publish('updating', peer=peer, host_update={'state': 'waiting_park', 'fraction': None, 'integrity': integrity})
          if wifi is not None:
            wifi.send(client)
          client.state()
        wait_for_permission(permission, connected, waiting, sleep)
        upload(client, manifest, RELEASE.with_name('usb_host_public.pem'), Path('/data/jetson-usb-cache'), allowed,
               lambda state, fraction: publish('updating', peer=peer, host_update={'state': state, 'fraction': fraction,
                                                                                'integrity': integrity}))
  while connected():
    if usb_change and not allowed():
      raise ConnectionError('USB activation requires offroad or fresh Park/inactive state')
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
    publish('updating', peer=peer, host_update={**value, 'integrity': integrity})
    if value.get('state') == 'ready':
      break
    sleep(.5)
  # The gate has no model. A fresh connection must greet the real server.
  raise ConnectionError('Jetson boot gate ended; reconnecting to runtime')
