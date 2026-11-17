"""Read-only display metadata. No credentials, IPC subscriptions, uploads or control writes."""
from dataclasses import dataclass
import math
import os
from pathlib import Path
import re
import sqlite3
import stat
import threading
import time

SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.|\-]{0,126}--[0-9]{1,6}$")
LIMIT = 4096


@dataclass(frozen=True)
class UploadStatus:
  sampled: float = 0.
  total: int | None = None
  logs: int | None = None
  full: int | None = None
  working: bool | None = None


def completed(database, folders):
  """Only matching immutable directory identities count as receiver-acknowledged."""
  try:
    info = database.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_size > 2 * 1024**2:
      return None
    db = sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True, timeout=.02)
    try:
      db.execute('PRAGMA query_only=ON')
      deadline = time.monotonic() + .05
      db.set_progress_handler(lambda: int(time.monotonic() > deadline), 100)
      rows = db.execute('SELECT segment,inode,mtime FROM completed LIMIT ?', (LIMIT + 1,)).fetchall()
      if len(rows) > LIMIT:
        return None
      return {name for name, inode, mtime in rows if folders.get(name) == (inode, mtime)}
    finally:
      db.close()
  except (OSError, sqlite3.Error, ValueError):
    return None


def worker_present(lock_file, locks_file):
  # Observe the existing worker lock; never acquire it or inspect process arguments.
  try:
    meta = lock_file.lstat()
    if not stat.S_ISREG(meta.st_mode):
      return None
    identity = (os.major(meta.st_dev), os.minor(meta.st_dev), meta.st_ino)
    with locks_file.open('rb') as stream:
      data = stream.read(256 * 1024 + 1)
    if len(data) > 256 * 1024:
      return None
    for line in data.decode('ascii', errors='strict').splitlines():
      parts = line.split()
      if len(parts) >= 6 and parts[1:4] == ['FLOCK', 'ADVISORY', 'WRITE']:
        major, minor, inode = parts[5].split(':')
        if (int(major, 16), int(minor, 16), int(inode)) == identity:
          return True
    return False
  except (OSError, ValueError, AttributeError, UnicodeError):
    return None


def collect(log_root=Path('/data/media/0/realdata'), catalog_root=Path('/data/pv5-incidents'),
            locks_file=Path('/proc/locks'), allowed=lambda: True):
  folders = {}
  try:
    with os.scandir(log_root) as entries:
      for index, entry in enumerate(entries):
        if not allowed() or index >= LIMIT:
          return UploadStatus(sampled=time.monotonic())
        if SEGMENT.fullmatch(entry.name) and entry.is_dir(follow_symlinks=False):
          meta = entry.stat(follow_symlinks=False)
          folders[entry.name] = (meta.st_ino, meta.st_mtime_ns)
  except OSError:
    return UploadStatus(sampled=time.monotonic())
  if not allowed():
    return UploadStatus(sampled=time.monotonic())
  full = completed(catalog_root / 'archive.sqlite', folders)
  early = completed(catalog_root / 'archive-home-logs.sqlite', folders)
  # Full transfer also acknowledges its rlog. Unknown catalogs never imply zero.
  full_logs = set()
  for name in full or ():
    if not allowed():
      return UploadStatus(sampled=time.monotonic())
    try:
      folder = log_root / name
      for filename in ('rlog.zst', 'rlog.bz2', 'rlog'):
        try:
          info = (folder / filename).lstat()
          if stat.S_ISREG(info.st_mode) and info.st_size > 0:
            full_logs.add(name)
            break
        except FileNotFoundError:
          continue
      info = folder.stat(follow_symlinks=False)
      if folders[name] != (info.st_ino, info.st_mtime_ns):
        return UploadStatus(sampled=time.monotonic())
    except OSError:
      return UploadStatus(sampled=time.monotonic())
  logs = None if early is None or full is None else len(early | full_logs)
  return UploadStatus(time.monotonic(), len(folders), logs, None if full is None else len(full),
                      worker_present(catalog_root / 'archive.lock', locks_file))


class StatusReader:
  """One low-priority reader, activated only while a stationary panel is visible."""
  def __init__(self, loader=collect):
    self.loader = loader
    self.value = UploadStatus()
    self.visible_until = 0.
    self.thread = None
    self.wake = threading.Event()

  def get(self, now):
    self.visible_until = now + 2.
    if self.thread is None:
      self.thread = threading.Thread(target=self._run, name='pv5-status-view', daemon=True)
      self.thread.start()
    if self.value.sampled == 0. or now - self.value.sampled >= 15.:
      self.wake.set()
    return self.value

  def _run(self):
    try:
      os.setpriority(os.PRIO_PROCESS, threading.get_native_id(), 19)
    except (AttributeError, OSError):
      pass
    next_sample = 0.
    while True:
      self.wake.wait()
      self.wake.clear()
      now = time.monotonic()
      if now < next_sample or now >= self.visible_until:
        continue
      next_sample = now + 15.
      try:
        self.value = self.loader(allowed=lambda: time.monotonic() < self.visible_until)
      except Exception:
        self.value = UploadStatus(sampled=time.monotonic())


def fresh(sm, name, now, ttl=2.):
  try:
    return bool(sm.seen[name] and sm.valid[name] and 0 <= now - sm.recv_time[name] < ttl)
  except (AttributeError, KeyError, TypeError):
    return False


def show_home(ui):
  cp = getattr(ui, "CP", None)
  return bool(getattr(ui, "dashcam_mode", False) or (cp is not None and str(cp.carFingerprint) == "KIA_PV5"))


def show_onroad(ui, now, alert=False):
  if alert or not ui.started or not fresh(ui.sm, 'deviceState', now):
    return False
  try:
    cp = ui.sm['carParams']
    dashcam = getattr(ui, 'dashcam_mode', False) or (ui.sm.valid['carParams'] and (cp.passive or cp.dashcamOnly))
    if not dashcam:
      return False
    sm = ui.sm
    if fresh(sm, 'selfdriveState', now, .5) and (sm['selfdriveState'].enabled or sm['selfdriveState'].active):
      return False
    if fresh(sm, 'carControl', now, .5) and (sm['carControl'].enabled or sm['carControl'].latActive or sm['carControl'].longActive):
      return False
    if fresh(sm, 'carState', now, .5):
      cs = sm['carState']
      if cs.canValid and not cs.canTimeout:
        if getattr(cs, "stockAeb", False) or getattr(cs, "stockFcw", False):
          return False
        if not all(fresh(sm, name, now, .5) for name in ("selfdriveState", "carControl")):
          return False
        return bool(cs.standstill and cs.vEgoRaw == 0 and abs(cs.vEgo) <= .01 and not cs.cruiseState.enabled)
    # Home dashcam can have no valid carState. Require fresh physical disconnection.
    ps = sm['pandaStates']
    return bool(fresh(sm, 'pandaStates', now, .5) and 1 <= len(ps) <= 8 and all(
      str(p.pandaType) != 'unknown' and str(p.harnessStatus) == 'notConnected'
      and not p.ignitionLine and not p.ignitionCan for p in ps))
  except (AttributeError, KeyError, TypeError, ValueError):
    return False


def number(value, low, high, suffix, digits=0):
  if isinstance(value, (int, float)) and math.isfinite(value) and low <= value <= high:
    return f'{value:.{digits}f}{suffix}'
  return '—'


def temperature(values):
  values = [v for v in values if isinstance(v, (int, float)) and math.isfinite(v) and -40 <= v <= 150]
  return number(max(values), -40, 150, '°C') if values else '—'


def progress(name, count, total):
  if count is None or total is None:
    return f'{name}  확인 중'
  if total == 0:
    return f'{name}  보관 구간 없음'
  return f'{name}  {count}/{total} 구간 ({100 * count / total:.0f}%)'


def lines(status, sm, now):
  recent = 0 <= now - status.sampled < 45.
  snapshot = status if recent else UploadStatus()
  job = '준비·전송 작업 중' if snapshot.working is True else '작업 대기 / 조건 확인' if snapshot.working is False else '작업 상태 확인 중'
  power, cpu, gpu, network, recording = '—', '—', '—', '연결 확인 중', '녹화 확인 중'
  if fresh(sm, 'deviceState', now):
    ds = sm['deviceState']
    power = number(ds.powerDrawW, .01, 200, ' W', 1)
    cpu, gpu = temperature(ds.cpuTempC), temperature(ds.gpuTempC)
    network = {'wifi': 'Wi-Fi', 'ethernet': '유선', 'none': '오프라인'}.get(str(ds.networkType), '모바일')
  if fresh(sm, 'managerState', now):
    processes = {str(p.name): p.running for p in sm['managerState'].processes}
    recording = '녹화 실행' if processes.get('loggerd') and processes.get('encoderd') else '녹화 중지' if processes.get('loggerd') is False and processes.get('encoderd') is False else '녹화 일부 / 확인 필요'
  age = f'{int(now - status.sampled)}초 전' if recent else '갱신 대기'
  return [job, progress('로그', snapshot.logs, snapshot.total), progress('영상 포함', snapshot.full, snapshot.total),
          f'전력 {power} · CPU {cpu} · GPU {gpu}', f'{recording} · {network}',
          f'보관 구간 수 기준 · {age}', 'PC 수신 완료 기준 · 분석은 별도']
