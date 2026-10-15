"""Keep USB provisioning alive while a Jetson checks/updates its boot release."""
from pathlib import Path
import time
from openpilot.common.jetson_model import selected_release

CAPABILITY = 'carrot_boot_update_v1'
MESSAGE = 0x4003
RELEASE = Path(__file__).with_name('host_release.json')


def wait_for_boot_update(client, peer, wifi, connected, publish, sleep=time.sleep):
  if peer.get('carrot_host') != 'jetson' or peer.get(CAPABILITY) is not True:
    return
  manifest = selected_release(RELEASE, required=True)
  if manifest.get('delivery') == 'local' and peer.get('carrot_local_release_v1') is not True:
    raise ValueError('Install the PC-prepared package for this Jetson model on Jetson first')
  usb_change = False
  if manifest.get('delivery') == 'usb':
    from tools.jetlink.usb_release import CAPABILITY as USB_CAPABILITY  # noqa: TID251 -- standalone updater package
    from tools.jetlink.usb_release_client import upload  # noqa: TID251
    if peer.get(USB_CAPABILITY) is not True:
      raise ValueError('Install the USB receiver and enroll your experiment public key on Jetson once')
    usb_change = peer.get('usb_source_commit') != manifest['source_commit']
    if usb_change:
      from openpilot.common.params import Params
      params = Params()
      def allowed():
        return connected() and params.get_bool('IsOffroad') and not params.get_bool('IsOnroad')
      upload(client, manifest, RELEASE.with_name('usb_host_public.pem'), Path('/data/jetson-usb-cache'), allowed,
             lambda state, fraction: publish('updating', peer=peer, host_update={'state': state, 'fraction': fraction}))
  while connected():
    if usb_change and not allowed():
      raise ConnectionError('USB activation requires offroad state; retry parked')
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
    sleep(.5)
  # The gate has no model. A fresh connection must greet the real server.
  raise ConnectionError('Jetson boot gate ended; reconnecting to runtime')
