"""Owner-enabled, keyless physical USB; keep signatures on every other path."""
import builtins
import copy
import json
from pathlib import Path
import sys
from types import SimpleNamespace as NS

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from test_usb_release import env, Wire, cache_assets, activate, transfer
import usb_release as usb
import install_usb_receiver as enroll
import prepare_usb_release as prepare
import update_host as update
from tools.jetlink import usb_release_client as sender
from openpilot.common import jetson_model
from openpilot.selfdrive.modeld.jetlink import startup
from jetlink import protocol as p


def manifest(env):
  value = copy.deepcopy(env.value)
  value.pop('signature')
  value.update(format=2, integrity='sha256')
  return value


def enable(env):
  enroll.install(env.root, sha256=True)
  (env.root / 'updater/usb-experiment-public.pem').unlink(missing_ok=True)


def deny_crypto(monkeypatch):
  original = builtins.__import__
  def imported(name, *args, **kwargs):
    if name.startswith('Crypto'):
      pytest.fail('SHA-only mode imported a signature/crypto dependency')
    return original(name, *args, **kwargs)
  monkeypatch.setattr(builtins, '__import__', imported)


def test_unsigned_usb_cannot_enable_its_own_policy(env):
  with pytest.raises(ValueError, match='local owner policy'):
    usb.Receiver(env.root).begin(manifest(env))
  assert not (env.root / 'usb-incoming').exists()
  assert not (env.root / 'updater'/usb.SHA256_POLICY).exists()


def test_local_policy_preflight_refuses_unowned_state_before_replacing_any_helper(env):
  policy=env.root/'updater'/usb.SHA256_POLICY
  policy.write_text(json.dumps(usb.SHA256_POLICY_VALUE)); policy.chmod(0o666)
  before={name:(env.root/'updater'/name).stat().st_mtime_ns for name in enroll.FILES}
  with pytest.raises(ValueError,match='root-owned'): enroll.install(env.root,sha256=True)
  assert before=={name:(env.root/'updater'/name).stat().st_mtime_ns for name in enroll.FILES}


@pytest.mark.parametrize('failure', ['unknown', 'boolean', 'permissions', 'symlink', 'owner'])
def test_bad_local_policy_is_never_a_capability_or_permission(env, failure):
  enable(env)
  path = env.root / 'updater'/usb.SHA256_POLICY
  if failure=='unknown': path.write_text('{"format":1,"integrity":"anything","transport":"usb"}')
  if failure=='boolean': path.write_text('{"format":true,"integrity":"sha256","transport":"usb"}')
  if failure=='permissions': path.chmod(0o666)
  if failure=='owner':
    import os
    os.chown(path,1000,1000)
  if failure=='symlink':
    other=path.with_suffix('.other'); path.rename(other); path.symlink_to(other)
  assert not usb.sha256_enabled(env.root)
  with pytest.raises(ValueError, match='local owner policy'):
    usb.Receiver(env.root).begin(manifest(env))


def test_keyless_enrollment_full_transfer_and_activation_need_no_crypto(env, monkeypatch):
  deny_crypto(monkeypatch)
  official=(env.root/'updater/release-signing-public.pem').read_bytes()
  enable(env)
  value=manifest(env)
  cache=env.tmp/'keyless-cache'; cache_assets(env,cache)
  monkeypatch.setattr(sender.urllib.request,'urlopen',lambda *a,**k:pytest.fail('cached USB attempted network'))
  wire=Wire()
  sender.upload(wire.client,value,None,cache,lambda:True,lambda *a:None)
  assert usb.registered(env.root,value)
  assert (env.root/'current').resolve()==env.previous
  assert activate(value).done
  assert (env.root/'current/SOURCE_COMMIT').read_text()=='b'*40
  assert (env.root/'updater/release-signing-public.pem').read_bytes()==official
  wire.session.close()


@pytest.mark.parametrize('failure', ['hash','length','runtime','fields'])
def test_sha_mode_retains_content_and_runtime_guards(env, failure):
  enable(env); value=manifest(env)
  if failure=='runtime':
    value['runtime']['l4t']='99'
    with pytest.raises(ValueError): usb.Receiver(env.root).begin(value)
    return
  if failure=='fields':
    value['signature']=''
    with pytest.raises(ValueError): usb.Receiver(env.root).begin(value)
    return
  if failure=='hash': value['model']['sha256']='0'*64
  if failure=='length': value['model']['size']+=1
  receiver=usb.Receiver(env.root); receiver.begin(value)
  for i,path in enumerate((env.bundle,env.model)):
    data=path.read_bytes()
    for offset in range(0,len(data),usb.BLOCK):
      receiver.chunk(usb.HEADER.pack(i,offset)+data[offset:offset+usb.BLOCK])
  with pytest.raises(ValueError,match='checksum/length'): receiver.finish()
  assert (env.root/'current').resolve()==env.previous
  assert not (env.root/'local-releases'/value['profile']).exists()
  receiver.close()


def test_unsigned_registered_identity_reuses_identical_signed_files(env):
  transfer(env)
  enable(env)
  receiver=usb.Receiver(env.root)
  assert receiver.begin(manifest(env))=={'complete':True}
  assert receiver.finish()=={'complete':True}
  receiver.close()


def test_sha_transfer_aborts_on_loss_of_park_permission_before_registration(env, monkeypatch):
  enable(env); cache=env.tmp/'park-loss-cache'; cache_assets(env,cache)
  monkeypatch.setattr(sender.urllib.request,'urlopen',lambda *a,**k:pytest.fail('cached USB attempted network'))
  wire=Wire()
  with pytest.raises(ConnectionError,match='Park'):
    sender.upload(wire.client,manifest(env),None,cache,lambda:wire.types.count(usb.CHUNK)<2,lambda *a:None)
  assert usb.FINISH not in wire.types
  assert not (env.root/'local-releases'/env.value['profile']).exists()
  assert (env.root/'current').resolve()==env.previous
  wire.session.close()


def test_sha_policy_cannot_bypass_invalid_signed_usb_or_official_nas_signature(env, monkeypatch):
  enroll.install(env.root,sha256=True)
  bad=copy.deepcopy(env.value); bad['signature']=''
  with pytest.raises(ValueError): usb.Receiver(env.root).begin(bad)
  from test_update_host import manifest as official_manifest
  checked=[]
  def rejected(value):
    checked.append(value)
    raise ValueError('signature required')
  monkeypatch.setattr(update,'verify_signature',rejected)
  with pytest.raises(ValueError,match='signature required'): update.selected_commit(official_manifest())
  assert checked and checked[0].get('delivery')!='usb'


def test_keyless_builder_and_selection_pin_actual_assets(env, monkeypatch):
  deny_crypto(monkeypatch)
  value=prepare.prepare_sha256(env.bundle,env.model,env.value['profile'],env.value['bundle']['url'],
                               env.value['model']['url'],env.tmp/'public')
  assert 'signature' not in value and value==manifest(env)
  pin={'sha256':value['model']['sha256'],'nbytes':value['model']['size']}
  monkeypatch.setattr(jetson_model,'read_spec',lambda:pin)
  path=env.tmp/'public/host_release.json'
  assert jetson_model.selected_release(path,required=True)==value
  value['model']['sha256']='0'*64
  path.with_name('usb_host_sha256_release.json').write_text(json.dumps(value))
  with pytest.raises(ValueError,match='branch model'): jetson_model.selected_release(path,required=True)


def boot_client(env, monkeypatch, hash_peer=True, policy=True):
  if policy: enable(env)
  value=manifest(env)
  selected=[]
  def selection(*a,**kw):
    preferred=kw.get('prefer_sha256',True)
    selected.append(preferred)
    return value if preferred else env.value
  monkeypatch.setattr(startup,'selected_release',selection)
  monkeypatch.setattr(startup,'RELEASE',env.tmp/'host_release.json')
  (env.tmp/'usb_host_public.pem').write_bytes(env.public.read_bytes())
  monkeypatch.setattr(startup,'Permission',lambda *a,**k:NS(allowed=lambda:True))
  monkeypatch.setitem(sys.modules,'openpilot.common.params',NS(Params=lambda:NS()))
  cache=env.tmp/'boot-cache'; cache_assets(env,cache)
  original=sender.upload
  monkeypatch.setattr(sender,'upload',lambda c,v,k,ignored,allowed,progress:original(c,v,k,cache,allowed,progress))
  monkeypatch.setattr(sender.urllib.request,'urlopen',lambda *a,**k:pytest.fail('cached USB attempted network'))
  wire=Wire(); sent=[]; original_send=wire.send
  def send(kind,seq,parts,timeout=None):
    if kind==usb.BEGIN: sent.append(json.loads(b''.join(parts)))
    original_send(kind,seq,parts,timeout)
  wire.send=send
  wire.send_json=lambda kind,seq,v:send(kind,seq,[json.dumps(v).encode()])
  wire.client.state=lambda:{'carrot_update':{'state':'ready'}}
  peer={'carrot_host':'jetson',startup.CAPABILITY:True,usb.CAPABILITY:True,
        usb.SHA256_CAPABILITY:hash_peer,'usb_source_commit':'a'*40,'engine_state':'none','loaded':None}
  return wire,peer,selected,sent


@pytest.mark.parametrize('hash_peer', [False,True])
def test_boot_capability_selects_sha_or_identical_legacy_without_remote_policy_change(env, monkeypatch, hash_peer):
  wire,peer,selected,sent=boot_client(env,monkeypatch,hash_peer=hash_peer,policy=hash_peer)
  with pytest.raises(ConnectionError,match='reconnecting'):
    startup.wait_for_boot_update(wire.client,peer,None,lambda:True,lambda *a,**k:None)
  assert selected==([True] if hash_peer else [True,False])
  assert len(sent)==1 and sent[0]['format']==(2 if hash_peer else 1)
  assert usb.registered(env.root,manifest(env))
  assert usb.sha256_enabled(env.root) is hash_peer
  wire.session.close()


def test_sha_capable_rejection_never_silently_retries_legacy_signature(env, monkeypatch):
  wire,peer,selected,sent=boot_client(env,monkeypatch,hash_peer=True,policy=False)
  with pytest.raises(ValueError,match=r'Jetson rejected USB update \[begin\]'):
    startup.wait_for_boot_update(wire.client,peer,None,lambda:True,lambda *a,**k:None)
  assert selected==[True] and len(sent)==1 and sent[0]['format']==2
  assert not usb.sha256_enabled(env.root)
  wire.session.close()


@pytest.mark.parametrize('field', ['source_commit', 'bundle'])
def test_old_receiver_cannot_silently_receive_a_different_signed_release(env, monkeypatch, field):
  wire,peer,selected,sent=boot_client(env,monkeypatch,hash_peer=False,policy=False)
  if field=='source_commit': env.value[field]='c'*40
  else: env.value['bundle']['sha256']='0'*64
  with pytest.raises(ValueError,match='no matching legacy package'):
    startup.wait_for_boot_update(wire.client,peer,None,lambda:True,lambda *a,**k:None)
  assert not sent and not wire.types
  wire.session.close()


def test_active_boot_installation_cannot_receive_another_sha_package(env):
  enable(env); wire=Wire(); wire.session.gate.worker=NS(is_alive=lambda:True)
  wire.send(usb.BEGIN,1,[json.dumps(manifest(env)).encode()])
  assert json.loads(bytes(wire.recv().payload))['ok'] is False
  assert not (env.root/'usb-incoming').exists()
  wire.session.close()


def test_boot_advertises_hash_mode_only_after_local_installation(env):
  wire=Wire()
  for seq,enabled in ((1,False),(2,True)):
    if enabled: enable(env)
    wire.send(p.Msg.HELLO_REQ,seq,[b'{}'])
    reply=json.loads(bytes(wire.recv().payload))
    assert reply[usb.SHA256_CAPABILITY] is enabled
    assert reply[usb.CAPABILITY] is True
  wire.session.close()
