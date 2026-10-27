"""One in-flight response, priority fairness and bounded optional generations."""
import asyncio
from types import SimpleNamespace as NS

import pytest

from openpilot.selfdrive.carrot import obd_battery as b
from test_obd_battery import payload, response, soc_payload, coolant_payload
from test_battery_heatmap import block


def data(command):
  return response({'220101': payload(), '220105': soc_payload(), '220106': coolant_payload()}.get(command) or block(command))


def test_overdue_power_is_reselected_before_next_slow_did(monkeypatch):
  clock, commands, active = [100.], [], [0]
  monkeypatch.setattr(b, 'time', NS(monotonic=lambda: clock[0]))
  class Link:
    async def command(self, command):
      active[0] += 1
      assert active[0] == 1
      commands.append(command)
      await asyncio.sleep(0)
      clock[0] += .6 if command == '220105' else .1
      active[0] -= 1
      return data(command)
  async def check():
    poller, link = b.MetricPoller(), Link()
    for _ in range(4):
      before = len(commands)
      await poller.poll(link)
      assert len(commands) == before + 1
      clock[0] += poller.delay(link)
    assert commands == ['220101', '220105', '220101', '220106']
    assert active[0] == 0
    assert poller.observed['coolant_inlet_c'] > poller.observed['pack_power_kw']
    assert poller.observed['soc_percent'] < poller.observed['pack_power_kw']
  asyncio.run(check())


@pytest.mark.parametrize('seconds', [.6, 1.2])
def test_overloaded_power_does_not_starve_basic_fields_and_defers_cells(monkeypatch, seconds):
  clock, commands = [100.], []
  monkeypatch.setattr(b, 'time', NS(monotonic=lambda: clock[0]))
  class Link:
    async def command(self, command):
      commands.append(command)
      clock[0] += seconds
      return data(command)
  async def check():
    poller, link = b.MetricPoller(details=True), Link()
    for _ in range(18):
      await poller.poll(link, parked=True)
      clock[0] += poller.delay(link)
    assert commands[:6] == ['220101','220105','220101','220106','220101','220105']
    assert set(commands) == set(poller.due)
    assert not poller.cell_blocks and 'cell_voltages_v' not in poller.values
    assert len(poller.due) == 3 and len(poller.values) <= len(b.FIELDS)
  asyncio.run(check())


def test_priority_is_checked_after_transport_quiet_gap_and_idle_does_not_poll(monkeypatch):
  clock, commands = [100.], []
  monkeypatch.setattr(b, 'time', NS(monotonic=lambda: clock[0]))
  class Link:
    next_request = 0.
    async def command(self, command):
      commands.append(command)
      clock[0] += .1
      self.next_request = clock[0] + (.5 if command == '220105' else .05)
      return data(command)
  async def check():
    poller, link = b.MetricPoller(), Link()
    await poller.poll(link)
    clock[0] += poller.delay(link)
    await poller.poll(link)
    assert commands == ['220101','220105']
    await poller.poll(link)
    assert commands == ['220101','220105']  # No write inside the quiet interval.
    clock[0] += poller.delay(link)
    await poller.poll(link)
    assert commands[-1] == '220101'  # Reselect, not an earlier queued 0106.
    poller.due = dict.fromkeys(poller.due, clock[0] + .4)
    count = len(commands)
    await poller.poll(link)
    assert len(commands) == count and poller.delay(link) == pytest.approx(.4)
  asyncio.run(check())


def test_expired_scan_restarts_with_only_the_new_generation(monkeypatch):
  clock = [100.]
  monkeypatch.setattr(b, 'time', NS(monotonic=lambda: clock[0]))
  class Link:
    async def command(self, command):
      return response(block(command,195))
  async def check():
    poller, link = b.MetricPoller(details=True), Link()
    poller.due = dict.fromkeys(poller.due, 200.)
    poller.cell_started = 97.
    poller.cell_blocks = {'220102':block('220102',194)}
    await poller.poll(link,parked=True)
    assert not poller.cell_blocks and poller.next_cells == 102.
    clock[0] = 102.
    for _ in range(4):
      await poller.poll(link,parked=True)
      clock[0] += .1
    assert poller.values['cell_voltages_v'] == [3.9]*108
    assert poller.observed['cell_voltages_v'] == 102.
  asyncio.run(check())


def test_park_loss_during_final_cell_response_clears_array(monkeypatch):
  clock, allowed = [100.], [True]
  monkeypatch.setattr(b, 'time', NS(monotonic=lambda: clock[0]))
  class Link:
    async def command(self, command):
      assert command == '22010A'
      clock[0] += .2
      allowed[0] = False
      return response(block(command))
  async def check():
    poller = b.MetricPoller(details=True)
    poller.due = dict.fromkeys(poller.due, 200.)
    poller.cell_started = 99.
    poller.cell_blocks = {cmd:block(cmd) for cmd,_ in b.CELL_BLOCKS[:3]}
    poller.values['cell_voltages_v'], poller.observed['cell_voltages_v'] = [3.88]*108,99.
    await poller.poll(Link(),parked=lambda:allowed[0])
    assert not poller.cell_blocks and 'cell_voltages_v' not in poller.values
  asyncio.run(check())


def test_temperature_pages_are_not_combined_across_slow_or_failed_reads(monkeypatch):
  clock = [100.]
  monkeypatch.setattr(b, 'time', NS(monotonic=lambda: clock[0]))
  class Link:
    async def command(self, command):
      clock[0] += .6
      return data(command)
  async def check():
    poller = b.MetricPoller()
    await poller.poll(Link())
    await poller.poll(Link())
    assert 'soc_percent' in poller.values and 'module_temps_c' not in poller.values
    poller.first_temperature_page = (clock[0],payload())
    class NoData:
      async def command(self, command):
        return b'NO DATA\r>'
    await poller.poll(NoData())  # Overdue power is next.
    assert poller.first_temperature_page is None
  asyncio.run(check())


def test_optional_request_uses_available_slot_and_limits_scan_frequency(monkeypatch):
  clock, commands = [100.], []
  monkeypatch.setattr(b, 'time', NS(monotonic=lambda:clock[0]))
  class Link:
    async def command(self, command):
      commands.append(command)
      clock[0] += .02
      return data(command)
  async def check():
    poller, link = b.MetricPoller(details=True), Link()
    poller.due = dict.fromkeys(poller.due, 101.)
    poller.read_seconds = .12
    clock[0] = 100.9
    await poller.poll(link,parked=True)
    assert not commands and poller.delay(link) == pytest.approx(.1)
    # Four bounded transactions fit in the next idle window; do not increase
    # background request rate just because the adapter is fast.
    poller.due = dict.fromkeys(poller.due, 110.)
    for _ in range(4):
      await poller.poll(link,parked=True)
    assert commands == [cmd for cmd,_ in b.CELL_BLOCKS]
    assert poller.next_cells == pytest.approx(104.9)
    count = len(commands)
    clock[0] = 104.8
    await poller.poll(link,parked=True)
    assert len(commands) == count
  asyncio.run(check())
