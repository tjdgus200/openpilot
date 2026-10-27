"""Scanner-style stream/session fault injection; no hardware reliability claims."""
import asyncio
from types import SimpleNamespace as NS

import pytest

from openpilot.selfdrive.carrot import obd_battery as b
from test_obd_battery import DiscoveryLink, FakeLink, notification, response, payload


@pytest.mark.parametrize('tail', [b'\r', b'\n', b'\r\n', b' \t\r\n'])
def test_padding_in_same_or_later_notification_does_not_reconnect(tail):
  async def check():
    for chunks in ([response()+tail], [response(),tail]):
      link=FakeLink(chunks)
      assert b.parse_reply(await link.command('220101')) == payload()
      await link.command('220101')
      assert len(link.writes) == 2
      assert not link.command_active
  asyncio.run(check())


@pytest.mark.parametrize('tail', [b'x', b'7EC', b'>', b'\0', b'\xff', b'\r7ED123'])
def test_nonpadding_after_prompt_is_never_discarded_or_used_by_next_query(tail):
  async def check():
    for chunks in ([response()+tail], [response(),tail]):
      link=FakeLink(chunks)
      with pytest.raises(b.QueryError):
        await link.command('220101')
        await link.command('220105')
      assert len(link.writes) == 1
      assert not link.command_active
  asyncio.run(check())


def test_empty_notifications_and_fragmented_terminal_prompt():
  async def check():
    raw=response()
    link=FakeLink([b'',raw[:20],raw[20:-1],b'\r',b'>',b'\n'])
    assert b.parse_reply(await link.command('220101')) == payload()
    await link.command('220101')
    assert len(link.writes) == 2
  asyncio.run(check())


def test_reply_budget_starts_after_att_write_not_before(monkeypatch):
  clock=[100.]
  monkeypatch.setattr(b,'time',NS(monotonic=lambda:clock[0]))
  class Link(b.Link):
    async def call(self,*args,**kwargs):
      clock[0]+=1.8
      async def notify():
        await asyncio.sleep(0)
        clock[0]+=1.7
        self.queue.put_nowait(notification(response()))
      self.notifier=asyncio.create_task(notify())
      return ()
  async def check():
    link=Link(None,'00:11:22:33:44:55')
    assert b.parse_reply(await link.command('220101')) == payload()
    await link.notifier
    assert link.last_latency_ms == pytest.approx(3500.)
    assert link.response_timeout == 6.
  asyncio.run(check())


@pytest.mark.parametrize('interface,key', [(b.DEVICE,'Connected'),(b.GATT,'Notifying')])
def test_actual_disconnect_wakes_waiter_without_waiting_for_prompt_timeout(interface,key):
  class Link(b.Link):
    async def call(self,*args,**kwargs):
      self.queue.put_nowait(NS(body=(interface,{key:('b',False)},[])))
      return ()
  async def check():
    link=Link(None,'00:11:22:33:44:55')
    with pytest.raises(b.QueryError,match='ble_disconnected'):
      await asyncio.wait_for(link.command('220101'),.2)
    assert not link.command_active
  asyncio.run(check())


def test_overlapping_requests_rejected_without_queuing_and_cancel_releases_owner():
  class Link(b.Link):
    async def call(self,*args,**kwargs):
      await asyncio.Event().wait()
  async def check():
    link=Link(None,'00:11:22:33:44:55')
    pending=asyncio.create_task(link.command('220101'))
    await asyncio.sleep(0)
    with pytest.raises(b.QueryError,match='overlapping_command'):
      await link.command('220105')
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
      await pending
    assert not link.command_active
  asyncio.run(check())


class TwoUarts(DiscoveryLink):
  async def objects(self):
    objects=await super().objects()
    def variants(**fields):
      return {key:('s',value) for key,value in fields.items()}
    service=self.target+'/serial'
    objects[service]={'org.bluez.GattService1':variants(UUID=b.SERIAL_SERVICE)}
    objects[service+'/rx']={b.GATT:variants(UUID=b.SERIAL_RX,Service=service,Flags=['notify'])}
    objects[service+'/tx']={b.GATT:variants(UUID=b.SERIAL_TX,Service=service,Flags=['read','write-without-response'])}
    # Same UUID on another, unregistered device cannot enter the route list.
    objects['/org/bluez/hci0/dev_other/serial/rx']=objects[service+'/rx']
    return objects


def test_split_elm_uart_first_then_vendor_only_after_cleanup():
  async def check():
    first=TwoUarts()
    await first.connect()
    assert first.route == 'elm_ff12' and first.write_type == 'command'
    assert first.uart != first.write_uart
    assert first.commands == ['ATI',*b.SETUP]
    assert 'ATAT1' in first.commands
    assert not any(method=='StartDiscovery' for _,method in first.calls)  # Cached identity.
    await first.close()
    second=TwoUarts()
    second.route_preference=1
    await second.connect()
    assert second.route == 'vendor' and second.write_type == 'request'
    assert second.uart == second.write_uart
    await second.close()
    assert {p for p,m in first.calls+second.calls if m in ('Connect','Disconnect')} == {first.target}
  asyncio.run(check())


def test_recovery_scans_only_le_and_stops_before_connecting():
  class Link(TwoUarts):
    async def call(self,path,interface,method,signature=None,body=(),**kwargs):
      if method=='SetDiscoveryFilter':
        assert body == ({'Transport':('s','le'),'DuplicateData':('b',False)},)
      return await super().call(path,interface,method,signature,body,**kwargs)
  async def check():
    link=Link()
    link.force_scan=True
    await link.connect()
    methods=[method for _,method in link.calls]
    assert methods.index('StartDiscovery') < methods.index('StopDiscovery') < methods.index('Connect')
    assert link.scan is None
    await link.close()
    assert methods.count('Connect') == 1
  asyncio.run(check())


@pytest.mark.parametrize('nrc', ['10','11','12','13','21','22','31','33'])
def test_completed_ecu_rejection_backs_off_did_without_dropping_ble(nrc,monkeypatch):
  clock=[100.]
  monkeypatch.setattr(b,'time',NS(monotonic=lambda:clock[0]))
  raw=b'7EC037F22'+nrc.encode()+b'00000000\r>'
  assert b.ecu_rejected(raw)
  class Link:
    async def command(self,command):
      return raw
  async def check():
    poller=b.MetricPoller()
    stamp,values,times=await poller.poll(Link())
    assert values == times == {} and poller.last_result == 'ecu_rejected'
    assert poller.ecu_rejections == 1 and poller.no_data_count == 0
    assert poller.retry_at['220101'] == 102.
  asyncio.run(check())


@pytest.mark.parametrize('bad', [b'7EC037F227800000000\r>',b'7ED037F223100000000\r>',
                               b'7EC037F2E3100000000\r>',b'7EC037F223100000000\r>>',
                               b'7EC037F223100000000\r7EC037F223100000000\r>'])
def test_pending_wrong_source_or_mixed_negative_reply_never_becomes_harmless(bad):
  assert not b.ecu_rejected(bad)


def test_both_routes_still_require_known_adapter_model():
  async def check():
    link=TwoUarts(wrong_identity=True)
    with pytest.raises(ValueError,match='identity'):
      await link.connect()
    await link.close()
    assert not link.commands
  asyncio.run(check())


@pytest.mark.parametrize('failure', ['elm_prompt_timeout', 'gatt_write_timeout', 'ble_disconnected'])
def test_session_retry_changes_uart_only_after_cleanup_and_five_second_wait(failure,monkeypatch):
  from contextlib import asynccontextmanager
  import jeepney.io.asyncio
  sessions,events=[],[]
  config={'version':1,'address':'00:11:22:33:44:55','confirmed_fields':[]}
  @asynccontextmanager
  async def router(*args):
    yield None
  class Session:
    def __init__(self,router,address):
      assert all(session.closed for session in sessions)
      self.closed=False
      sessions.append(self)
    async def connect(self):
      events.append(('connect',self.route_preference,self.force_scan,self.response_timeout))
      raise b.QueryError(failure)
    async def close(self):
      self.closed=True
      events.append(('close',))
  async def sleep(delay):
    assert delay == 5. and sessions[-1].closed
    events.append(('wait',delay))
    if len(sessions) == 3:
      raise asyncio.CancelledError
  monkeypatch.setattr(jeepney.io.asyncio,'open_dbus_router',router)
  monkeypatch.setattr(b,'Link',Session)
  monkeypatch.setattr(b,'read_json',lambda _:config)
  monkeypatch.setattr(b,'write_snapshot',lambda *args,**kwargs:None)
  monkeypatch.setattr(b.asyncio,'sleep',sleep)
  with pytest.raises(asyncio.CancelledError):
    asyncio.run(b.run())
  assert [event[0] for event in events] == ['connect','close','wait']*3
  assert [event[1] for event in events if event[0]=='connect'] == [0,1,0]
  assert [event[2] for event in events if event[0]=='connect'] == [False,True,True]
  expected=[3.,6.,6.] if failure=='elm_prompt_timeout' else [3.,3.,3.]
  assert [event[3] for event in events if event[0]=='connect'] == expected
