"""Opt-in PV5 battery telemetry in the low-priority comma HUD worker.

No active UDS/ISO-TP requests or flow control. There is deliberately no bundled
enabled PID profile: PV5 offsets and coolant/Battery Care semantics need a
parked-vehicle comparison first. Without a validated profile no CAN socket opens.
A separately configured BLE reader supplies a bounded RAM snapshot; this HUD
worker itself never opens Bluetooth or issues diagnostic requests.
"""
import json
from pathlib import Path
import time

from openpilot.selfdrive.carrot.cluster.cluster_battery import (
    METRICS, finite_number, packet_from_metrics, valid_value,
)

PROFILE_PATH = Path('/data/jetlink/pv5-battery-profile.json')
MAX_PROFILE_BYTES = 8192
MAX_SIGNALS = 16
MAX_MESSAGE_BYTES = 1024
REPORT_INTERVAL_S = 1.0
POWER_INTERVAL_S = 0.5
SOURCE_LIMITS = {
    'pack_voltage_v': (0.0, 1500.0), 'pack_current_a': (-2000.0, 2000.0),
    'max_cell_v': (0.0, 5.0), 'min_cell_v': (0.0, 5.0),
}
PRODUCED = (METRICS - {'temp_change_5min_c'}) | SOURCE_LIMITS.keys()


def integer(value, low, high):
    return type(value) is int and low <= value <= high


def validate_profile(value, vehicle):
    if (not isinstance(value, dict) or type(value.get('version')) is not int or value['version'] != 1
        or vehicle != 'KIA_PV5' or value.get('vehicle') != vehicle or value.get('validated') is not True
        or set(value) - {'version', 'vehicle', 'validated', 'validation_reference', 'signals'}):
        return None
    reference = value.get('validation_reference')
    if not isinstance(reference, str) or not reference.strip() or not 1 <= len(reference) <= 128:
        return None
    signals = value.get('signals')
    if not isinstance(signals, list) or not 1 <= len(signals) <= MAX_SIGNALS:
        return None
    names = set()
    for signal in signals:
        if not isinstance(signal, dict) or set(signal) - {
            'name', 'bus', 'response_id', 'did', 'offset', 'size', 'signed', 'scale', 'bias', 'modes', 'mask', 'shift', 'invalid_raw',
        }:
            return None
        name = signal.get('name')
        if not isinstance(name, str) or name not in PRODUCED or name in names:
            return None
        names.add(name)
        for field, low, high in (('bus', 0, 7), ('response_id', 1, 0x7FF), ('did', 0, 0xFFFF), ('offset', 0, 1020)):
            if not integer(signal.get(field), low, high):
                return None
        if type(signal.get('size')) is not int or signal['size'] not in (1, 2, 4):
            return None
        if type(signal.get('signed', False)) is not bool:
            return None
        bits = signal['size'] * 8
        low = -(1 << (bits - 1)) if signal.get('signed', False) else 0
        high = (1 << (bits - 1)) - 1 if signal.get('signed', False) else (1 << bits) - 1
        invalid = signal.get('invalid_raw', [])
        if not isinstance(invalid, list) or len(invalid) > 16 or any(not integer(v, low, high) for v in invalid):
            return None
        if 'mask' in signal:
            if signal.get('signed', False) or not integer(signal['mask'], 1, (1 << (signal['size'] * 8)) - 1):
                return None
            if not integer(signal.get('shift', 0), 0, signal['size'] * 8 - 1):
                return None
        elif 'shift' in signal:
            return None
        if name == 'thermal_mode':
            if 'scale' in signal or 'bias' in signal:
                return None
            modes = signal.get('modes')
            if not isinstance(modes, dict) or not 1 <= len(modes) <= 8:
                return None
            for code, mode in modes.items():
                if not isinstance(code, str) or not code.isascii() or not code.isdigit() or not valid_value(name, mode):
                    return None
        else:
            if 'modes' in signal:
                return None
            for field, default in (('scale', 1.0), ('bias', 0.0)):
                number = signal.get(field, default)
                if not finite_number(number) or abs(number) > 10000 or (field == 'scale' and number == 0):
                    return None
    return signals


class PassiveDecoder:
    """Reassemble only configured ECU replies on configured receive buses."""
    def __init__(self, signals):
        self.signals = signals
        self.addresses = {(s['bus'], s['response_id']) for s in signals}
        self.pending = {}
        self.metrics = {}
        self.last_event_stamp = -1.0

    def feed(self, bus, address, data, stamp):
        key = bus, address
        if key not in self.addresses or not 0 < len(data) <= 64:
            return
        kind = data[0] >> 4
        if kind == 0:
            self.pending.pop(key, None)
            length, start = data[0] & 15, 1
            if length == 0 and len(data) > 8:
                length, start = data[1], 2
            if length and start + length <= len(data):
                self.decode_payload(bus, address, data[start:start + length], stamp)
        elif kind == 1:
            self.pending.pop(key, None)
            if len(data) < 3:
                return
            length = ((data[0] & 15) << 8) | data[1]
            if not len(data) - 2 < length <= MAX_MESSAGE_BYTES:
                return
            self.pending[key] = (length, bytearray(data[2:]), 1, stamp)
        elif kind == 2:
            item = self.pending.pop(key, None)
            if item is None:
                return
            length, buffer, sequence, first_stamp = item
            if data[0] & 15 != sequence or not 0 <= stamp - first_stamp <= 1.0:
                return
            buffer.extend(data[1:1 + length - len(buffer)])
            if len(buffer) == length:
                self.decode_payload(bus, address, bytes(buffer), stamp)
            else:
                self.pending[key] = length, buffer, (sequence + 1) & 15, first_stamp
        # Flow control is neither generated nor interpreted as telemetry.

    def decode_payload(self, bus, address, payload, stamp):
        if len(payload) < 3 or payload[0] != 0x62:
            return
        did, data = int.from_bytes(payload[1:3], 'big'), payload[3:]
        for signal in self.signals:
            if (signal['bus'], signal['response_id'], signal['did']) != (bus, address, did):
                continue
            start, size, name = signal['offset'], signal['size'], signal['name']
            if start + size > len(data):
                self.metrics.pop(name, None)
                continue
            raw = int.from_bytes(data[start:start + size], 'big', signed=signal.get('signed', False))
            if raw in signal.get('invalid_raw', ()):
                self.metrics.pop(name, None)
                continue
            if 'mask' in signal:
                raw = (raw & signal['mask']) >> signal.get('shift', 0)
            if name == 'thermal_mode':
                value = signal['modes'].get(str(raw))
            else:
                value = raw * signal.get('scale', 1.0) + signal.get('bias', 0.0)
            limits = SOURCE_LIMITS.get(name)
            valid = (finite_number(value) and limits[0] <= value <= limits[1]) if limits else valid_value(name, value)
            if valid:
                self.metrics[name] = value, stamp
            else:
                self.metrics.pop(name, None)
        configured = {signal['name'] for signal in self.signals}
        # These intermediate inputs are never sent to the HUD. Their sign and
        # scale must still be validated, even though voltage/current are hidden.
        for target, first, second, calculate in (
            ('pack_power_kw', 'pack_voltage_v', 'pack_current_a', lambda a, b: a * b / 1000.0),
            ('cell_delta_mv', 'max_cell_v', 'min_cell_v', lambda a, b: (a - b) * 1000.0),
        ):
            if target in configured:
                continue
            a, b = self.metrics.get(first), self.metrics.get(second)
            if a is not None and b is not None and abs(a[1] - b[1]) <= 0.1:
                value = calculate(a[0], b[0])
                if valid_value(target, value):
                    self.metrics[target] = value, min(a[1], b[1])
                    continue
            self.metrics.pop(target, None)

    def consume(self, event, now):
        stamp = event.logMonoTime / 1e9
        if not event.valid or not 0 <= now - stamp <= 1.0 or stamp < self.last_event_stamp:
            return
        self.last_event_stamp = stamp
        # All physical receive buses are below 128; returned TX echoes are not evidence.
        for index, frame in enumerate(event.can):
            if index >= 512:
                break
            # Skip unrelated traffic before copying its payload into Python.
            if (frame.src, frame.address) in self.addresses:
                self.feed(frame.src, frame.address, bytes(frame.dat), stamp)

    def packet(self, now):
        self.pending = {key: item for key, item in self.pending.items() if 0 <= now - item[3] <= 1.0}
        maximum = self.metrics.get('max_temp_c')
        minimum = self.metrics.get('min_temp_c')
        if maximum is not None and minimum is not None and minimum[0] > maximum[0]:
            for name in ('min_temp_c', 'max_temp_c'):
                self.metrics.pop(name, None)
        return packet_from_metrics(self.metrics, now, 'receiving')


class BatteryPublisher:
    def __init__(self, profile_path=PROFILE_PATH):
        self.path = profile_path
        self.vehicle = None
        self.next_profile = self.next_report = self.next_power_report = 0.0
        self.decoder = self.socket = None
        self.report = None
        self.messaging = None
        self.profile_bytes = None
        self.slow_metrics = {}
        self.ble_record = None
        self.ble_enabled = False
        self.ble_identity = None
        self.next_ble_read = 0.0

    def close(self):
        self.socket = self.decoder = self.messaging = None
        self.report = self.profile_bytes = None
        self.vehicle = None
        self.next_profile = self.next_report = self.next_power_report = 0.0
        self.slow_metrics = {}
        self.ble_record = None
        self.ble_enabled = False
        self.ble_identity = None
        self.next_ble_read = 0.0

    def update(self, vehicle, enabled, now):
        if not enabled:
            self.close()
            return None
        if vehicle != self.vehicle:
            self.close()
            self.vehicle = vehicle
        # Bounded RAM read at 2 Hz; no Bluetooth, subprocess or vehicle requests
        # on the USB HUD worker. BLE connection/response waits run elsewhere.
        from openpilot.selfdrive.carrot.obd_battery import CONFIG, SNAPSHOT, config_id, configuration, read_json, snapshot_packet
        if now >= self.next_ble_read:
            self.next_ble_read = now + POWER_INTERVAL_S
            self.ble_enabled = vehicle == 'KIA_PV5' and CONFIG.is_file()
            self.ble_record = None
            self.ble_identity = None
            if self.ble_enabled:
                try:
                    self.ble_identity = config_id(configuration(read_json(CONFIG)))
                    self.ble_record = read_json(SNAPSHOT)
                except (OSError, ValueError, TypeError):
                    pass
        if self.ble_enabled:
            self.socket = self.decoder = None
            return snapshot_packet(self.ble_record, now, self.ble_identity)
        if now >= self.next_profile:
            self.next_profile = now + 5.0
            try:
                with self.path.open('rb') as file:
                    raw = file.read(MAX_PROFILE_BYTES + 1)
                signals = validate_profile(json.loads(raw) if len(raw) <= MAX_PROFILE_BYTES else None, vehicle)
            except (OSError, ValueError, TypeError):
                raw, signals = None, None
            if raw != self.profile_bytes or signals is None or self.decoder is None:
                self.socket = self.decoder = None
                self.report = None
                self.slow_metrics = {}
                self.next_report = self.next_power_report = 0.0
                self.profile_bytes = raw
                if signals is not None:
                    from openpilot.cereal import messaging
                    self.messaging = messaging
                    self.socket = messaging.sub_sock('can', conflate=False, timeout=0)
                    self.decoder = PassiveDecoder(signals)
        if self.decoder is not None:
            started = time.monotonic()
            for _ in range(16):
                if time.monotonic() - started >= 0.002:
                    break
                event = self.messaging.recv_one_or_none(self.socket)
                if event is None:
                    break
                self.decoder.consume(event, now)
        if self.decoder is not None:
            if self.report is None or now >= self.next_report:
                self.next_report = now + REPORT_INTERVAL_S
                # Validate ranges at 1 Hz. Preserve receive stamps in the cache.
                self.decoder.packet(now)
                self.slow_metrics = {k: v for k, v in self.decoder.metrics.items()
                                     if k in METRICS and k != 'pack_power_kw'}
            if self.report is None or now >= self.next_power_report:
                self.next_power_report = now + POWER_INTERVAL_S
                metrics = dict(self.slow_metrics)
                if 'pack_power_kw' in self.decoder.metrics:
                    metrics['pack_power_kw'] = self.decoder.metrics['pack_power_kw']
                # Rebase ages, never refresh receive stamps. The existing 10 Hz
                # HUD worker supplies the cadence; no new wakeup or CAN request.
                self.report = packet_from_metrics(metrics, now, 'receiving')
        elif self.report is None or now >= self.next_report:
            self.next_report = now + REPORT_INTERVAL_S
            self.report = packet_from_metrics({}, now, 'profile_required')
        return self.report
