"""Display-only battery values. No CAN, diagnostics, or thermal-control commands."""
from dataclasses import dataclass
import math


MAX_AGE_S = 5.0
THERMAL_MODES = frozenset(('idle', 'cooling', 'heating', 'mixed'))
LIMITS = {
    'soc_percent': (0.0, 100.0),
    'max_temp_c': (-50.0, 130.0),
    'min_temp_c': (-50.0, 130.0),
    'pack_power_kw': (-2000.0, 2000.0),
    'cell_delta_mv': (0.0, 5000.0),
    'coolant_inlet_c': (-50.0, 130.0),
    'thermal_power_kw': (0.0, 100.0),
    'dc_charge_80_min': (0.0, 3600.0),
    'temp_change_5min_c': (-180.0, 180.0),
}
METRICS = frozenset((*LIMITS, 'thermal_mode'))


def finite_number(value):
    try:
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
    except OverflowError:
        return False


def valid_value(name, value):
    if name == 'thermal_mode':
        return isinstance(value, str) and value in THERMAL_MODES
    limits = LIMITS.get(name)
    return limits is not None and finite_number(value) and limits[0] <= value <= limits[1]


@dataclass(frozen=True, slots=True)
class BatteryPanelData:
    # Each field retains its own age. A fresh SOC must not revive an old temperature.
    fields: tuple[tuple[str, float | str, float], ...] = ()
    status: str = 'waiting'

    def value(self, name):
        return next((value for key, value, _ in self.fields if key == name), None)

    @property
    def oldest_age_s(self):
        return max((age for _, _, age in self.fields), default=None)


def panel_from_wire(record, elapsed_s=0.0):
    """Decode a bounded snapshot; malformed/expired individual values stay absent."""
    if not isinstance(record, dict) or type(record.get('version')) is not int or record['version'] != 1:
        return BatteryPanelData()
    if not finite_number(elapsed_s) or elapsed_s < 0:
        return BatteryPanelData()
    fields = record.get('fields')
    if not isinstance(fields, dict) or len(fields) > len(METRICS):
        return BatteryPanelData()
    accepted = []
    for name in sorted(METRICS):
        item = fields.get(name)
        if not isinstance(item, dict):
            continue
        value, age = item.get('value'), item.get('age_s')
        if not valid_value(name, value) or not finite_number(age) or age < 0:
            continue
        age += elapsed_s
        if age <= MAX_AGE_S:
            accepted.append((name, value, age))
    values = {key: value for key, value, _ in accepted}
    # Do not present a self-contradictory temperature range.
    if values.get('min_temp_c', -math.inf) > values.get('max_temp_c', math.inf):
        accepted = [entry for entry in accepted if entry[0] not in ('min_temp_c', 'max_temp_c', 'temp_change_5min_c')]
    status = record.get('status', 'waiting')
    if status not in ('waiting', 'profile_required', 'receiving'):
        status = 'waiting'
    return BatteryPanelData(tuple(accepted), 'receiving' if accepted else ('profile_required' if status == 'profile_required' else 'waiting'))


def packet_from_metrics(metrics, now, status='waiting'):
    fields = {}
    for name, (value, stamp) in metrics.items():
        if name in METRICS and valid_value(name, value) and finite_number(stamp) and 0 <= now - stamp <= MAX_AGE_S:
            fields[name] = {'value': value, 'age_s': now - stamp}
    return {'version': 1, 'sampled_at': now, 'status': status, 'fields': fields}


def remote_panel(snapshot, now):
    """Translate sender-relative ages without assuming synchronized host clocks."""
    if snapshot is None:
        return BatteryPanelData()
    received, record = snapshot
    if not isinstance(record, dict) or not finite_number(received) or not 0 <= now - received < 0.5:
        return BatteryPanelData()
    battery, sent = record.get('battery_hud'), record.get('sent')
    if not isinstance(battery, dict) or not finite_number(sent):
        return BatteryPanelData()
    sampled = battery.get('sampled_at')
    if not finite_number(sampled) or not 0 <= sent - sampled <= MAX_AGE_S:
        return BatteryPanelData()
    # Socket transit before the receiver timestamp cannot be measured from these clocks.
    return panel_from_wire(battery, sent - sampled + now - received)


def panel_labels(data, language='en'):
    """Complete localized labels, shared by the renderer and PC checks."""
    if language == 'ko':
        return {
            'title': '배터리 종합', 'soc': '잔량', 'maximum': '최고 온도',
            'minimum': '최저', 'spread': '온도차', 'consumption': '전체 소비 전력',
            'regeneration': '전체 회생 전력', 'coolant': '배터리 냉각수 입구',
            'thermal': '열관리 상태 · 소비 전력', 'charge80': '급속충전 80% 예상 시간', 'minutes': '분',
            'cells': '셀 전압차', 'waiting': '배터리 데이터 수신 대기',
            'profile_required': 'PV5 신호 검증 · 프로필 등록 필요',
            'receiving': '항목별 유효시간 5초', 'idle': '대기', 'cooling': '냉각 중',
            'heating': '가열 중', 'mixed': '냉각·가열 작동',
        }
    if language == 'zh':
        return {
            'title': '电池信息', 'soc': '电量', 'maximum': '最高温度',
            'minimum': '最低', 'spread': '温差', 'consumption': '总耗电功率',
            'regeneration': '总回收功率', 'coolant': '电池冷却液入口',
            'thermal': '热管理状态 · 功率', 'charge80': '快充至80%预计时间', 'minutes': '分钟',
            'cells': '电芯电压差', 'waiting': '等待电池数据',
            'profile_required': '需要验证PV5信号并安装配置',
            'receiving': '每项有效期5秒', 'idle': '待机', 'cooling': '冷却中',
            'heating': '加热中', 'mixed': '冷却与加热运行',
        }
    return {
        'title': 'Battery overview', 'soc': 'Charge', 'maximum': 'Maximum temp',
        'minimum': 'Min', 'spread': 'Spread', 'consumption': 'Total battery draw',
        'regeneration': 'Total regeneration', 'coolant': 'Battery coolant inlet',
        'thermal': 'Thermal mode / electrical power', 'charge80': 'DC charge to 80% / estimate', 'minutes': 'min',
        'cells': 'Cell voltage spread', 'waiting': 'Waiting for battery data',
        'profile_required': 'PV5 signals need validation and a profile',
        'receiving': 'Each value expires after 5 s', 'idle': 'Standby', 'cooling': 'Cooling',
        'heating': 'Heating', 'mixed': 'Cooling and heating',
    }
