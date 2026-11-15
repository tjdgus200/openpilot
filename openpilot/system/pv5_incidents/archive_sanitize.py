"""Offroad transmission copies only. Never edits or pins logger originals."""

import json
import os
from pathlib import Path
import re
import shutil
import subprocess

from openpilot.system.pv5_incidents.archive_catalog import SourceChanged
from openpilot.system.pv5_incidents.archive_protocol import NAMES, PRIVACY, SEGMENT, encode, file_id, identifier
from openpilot.system.pv5_incidents.archive_stream import decoded, messages

CACHE_LIMIT = 64 * 1024**2
COPY_LIMIT = 48 * 1024**2
# Retain driver-monitoring results, but never cabin images/encoded media/audio.
CABIN = frozenset(('driverCameraState', 'driverEncodeData', 'livestreamDriverEncodeData'))


class BoundedWriter:
  def __init__(self, raw, maximum, check):
    self.raw, self.maximum, self.check = raw, maximum, check

  def write(self, data):
    self.check()
    if self.raw.tell() + len(data) > self.maximum:
      raise ValueError('privacy_copy_budget')
    return self.raw.write(data)

  def flush(self):
    self.raw.flush()


def excluded(topic):
  return 'audio' in topic.lower() or 'microphone' in topic.lower() or topic in CABIN


def identity(path):
  info = path.stat(follow_symlinks=False)
  if path.is_symlink() or not path.is_file():
    raise SourceChanged('unsafe_privacy_source')
  return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)


class Sanitizer:
  def __init__(self, cache, check, log_schema=None, ffmpeg=None, ffprobe=None):
    self.cache, self.check, self.log_schema = Path(cache), check, log_schema
    self.ffmpeg, self.ffprobe = ffmpeg or shutil.which('ffmpeg'), ffprobe or shutil.which('ffprobe')
    if self.cache.is_symlink():
      raise ValueError('unsafe_privacy_cache')
    self.cache.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Caller holds the sole copier lock. These are only interrupted, generated
    # temporary copies; no source recording is ever unlinked.
    for path in self.cache.glob('*.part'):
      if re.fullmatch(r'[0-9a-f]{64}\.part', path.name) and not path.is_symlink():
        path.unlink()
    for path in self.cache.glob('*.data'):
      if re.fullmatch(r'[0-9a-f]{64}\.data', path.name) and not path.with_suffix('.json').exists() and not path.is_symlink():
        path.unlink()  # Auditing never committed; generated copy only.
    self.budget()

  def budget(self):
    self.check()
    size = 0
    for index, path in enumerate(self.cache.iterdir()):
      if index >= 1024 or path.is_symlink() or not path.is_file():
        raise ValueError('privacy_cache_budget')
      size += path.stat().st_size
    if size > CACHE_LIMIT or shutil.disk_usage(self.cache).free < 512 * 1024**2:
      raise ValueError('privacy_cache_budget')
    return size

  def helper(self, args, limit=8192, source_check=lambda: True):
    # Inherits the dedicated copier's process group, nice/ionice and rlimits.
    # Parent/permission loss kills that group, including ffmpeg/ffprobe.
    self.check()
    with subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as child:
      try:
        while True:
          self.check()
          source_check()
          try:
            out, _ = child.communicate(timeout=0.05)
            break
          except subprocess.TimeoutExpired:
            continue
        if child.returncode or len(out) > limit:
          raise ValueError('privacy_media_tool_failed')
        self.check()
        return out
      finally:
        if child.poll() is None:
          child.kill()
          child.wait()

  def streams(self, path):
    if not self.ffprobe:
      raise ValueError('privacy_probe_unavailable')
    value = json.loads(self.helper([self.ffprobe, '-v', 'error', '-show_entries', 'stream=codec_type', '-of', 'json', str(path)]))
    kinds = [row['codec_type'] for row in value['streams']]
    if not kinds or kinds.count('video') != 1 or any(kind not in ('video', 'audio', 'data') for kind in kinds):
      raise ValueError('unexpected_media_streams')
    return kinds

  def prepare(self, source, token):
    if not re.fullmatch(r'[0-9a-f]{64}', token):
      raise ValueError('invalid_privacy_identity')
    try:
      return self._prepare(source, token)
    except BaseException:
      (self.cache / (token + '.part')).unlink(missing_ok=True)
      raise

  def _prepare(self, source, token):
    self.check()
    before = identity(source)

    def source_check():
      self.check()
      if identity(source) != before or source.parent.is_symlink() or any(source.parent.glob('*.lock')):
        raise SourceChanged('source_changed_during_filter')
      return True

    if source.suffix == '.hevc':
      # Elementary HEVC has no audio container; never accept arbitrary suffixes.
      return source, source.name, {'audio_removed': False, 'filtered_messages': 0}
    meta = self.cache / (token + '.json')
    if meta.exists():
      if meta.is_symlink() or meta.stat().st_size > 2048:
        raise ValueError('invalid_privacy_receipt')
      saved = json.loads(meta.read_bytes())
      output = self.cache / saved['cache_name']
      if output.parent != self.cache or output.is_symlink():
        raise ValueError('invalid_privacy_receipt')
      if saved['source'] == list(before) and saved['privacy'] == PRIVACY and list(identity(output)) == saved['output']:
        return output, saved['name'], saved['audit']
      raise SourceChanged('privacy_copy_changed')
    output, temporary = self.cache / (token + '.data'), self.cache / (token + '.part')
    if output.exists():
      output.unlink()  # Our uncommitted output from an interrupted audit only.
    temporary.unlink(missing_ok=True)
    available = min(COPY_LIMIT, CACHE_LIMIT - self.budget() - 2048)
    if available <= 0:
      raise ValueError('privacy_cache_budget')
    if source.name.startswith('rlog'):
      import zstandard

      if self.log_schema is None:
        from openpilot.cereal import log

        self.log_schema = log
      counts, kept = 0, 0
      with decoded(source) as stream, temporary.open('xb') as raw:
        with zstandard.ZstdCompressor(level=1).stream_writer(BoundedWriter(raw, available, source_check), closefd=False) as target:
          for frame in messages(stream, source_check):
            with self.log_schema.Event.from_bytes(frame, traversal_limit_in_words=2**21) as event:
              topic = event.which()  # Unknown union/schema fails closed.
            if excluded(topic):
              counts += 1
            else:
              target.write(frame)
              kept += 1
            if raw.tell() > COPY_LIMIT:
              raise ValueError('privacy_copy_budget')
      if not kept:
        raise ValueError('empty_filtered_log')
      name, audit = 'rlog.zst', {'audio_removed': counts > 0, 'filtered_messages': counts}
    else:
      kinds = self.streams(source)
      if 'audio' not in kinds and 'data' not in kinds:
        return source, source.name, {'audio_removed': False, 'filtered_messages': 0}
      if not self.ffmpeg:
        raise ValueError('privacy_copy_unavailable')
      self.helper(
        [
          self.ffmpeg,
          '-nostdin',
          '-v',
          'error',
          '-readrate',
          '0.25',
          '-i',
          str(source),
          '-map',
          '0:v:0',
          '-c:v',
          'copy',
          '-an',
          '-sn',
          '-dn',
          '-map_metadata',
          '-1',
          '-map_chapters',
          '-1',
          '-fs',
          str(available),
          '-f',
          'mpegts',
          str(temporary),
        ],
        source_check=source_check,
      )
      # -fs is a last-resort output bound, not permission to transmit a truncated video.
      if temporary.stat().st_size >= available - 65536 or self.streams(temporary) != ['video']:
        raise ValueError('privacy_copy_budget_or_audio')
      name, audit = source.stem + '.ts', {'audio_removed': 'audio' in kinds, 'filtered_messages': 0}
    self.check()
    if identity(source) != before or temporary.stat().st_size > COPY_LIMIT:
      raise SourceChanged('source_changed_during_filter')
    self.budget()
    temporary.replace(output)
    saved = {
      'source': list(before),
      'output': list(identity(output)),
      'privacy': PRIVACY,
      'cache_name': output.name,
      'name': name,
      'audit': audit,
      'source_relative': [source.parent.name, source.name],
    }
    meta.write_bytes(encode(saved))
    return output, name, audit


class PrivacyCatalog:
  def __init__(self, catalog, sanitizer):
    self.raw, self.sanitizer = catalog, sanitizer
    self.allowed = catalog.allowed
    for path in sanitizer.cache.glob('*.json'):
      catalog.check()
      if not re.fullmatch(r'[0-9a-f]{64}\.json', path.name) or path.is_symlink() or path.stat().st_size > 2048:
        raise ValueError('invalid_privacy_receipt')
      saved = json.loads(path.read_bytes())
      relative = saved.get('source_relative', [])
      if len(relative) != 2 or not SEGMENT.fullmatch(relative[0]) or relative[1] not in NAMES:
        raise ValueError('invalid_privacy_source')
      source = catalog.root / relative[0] / relative[1]
      try:
        changed = list(identity(source)) != saved['source'] or source.parent.is_symlink()
      except FileNotFoundError:
        changed = True
      if changed:
        path.with_suffix('.data').unlink(missing_ok=True)
        path.unlink()

  def verify(self, candidate):
    self.raw.check()
    folder = self.raw.root / candidate['original']['manifest']['segment']
    if folder.is_symlink() or self.raw.locked(folder):
      raise SourceChanged('source_reopened')
    for entry in candidate['original']['manifest']['files']:
      if identity(folder / entry['name']) != candidate['original']['identities'][entry['file_id']]:
        raise SourceChanged('source_changed')

  def next(self, commit):
    original = self.raw.next(commit)
    if original is None:
      return None
    value = dict(original['manifest'], privacy=PRIVACY, files=[])
    candidate = {'original': original, 'manifest': value, 'sources': {}, 'cache_tokens': []}
    for entry in original['manifest']['files']:
      self.verify(candidate)
      token = identifier(self.raw.key, 'privacy', [PRIVACY, entry['file_id']])
      source = self.raw.root / value['segment'] / entry['name']
      output, name, _audit = self.sanitizer.prepare(source, token)
      self.verify(candidate)
      info = identity(output)
      new = {'name': name, 'size': info[2], 'mtime_ns': entry['mtime_ns']}
      new['file_id'] = file_id(self.raw.key, value['segment'], name, new['size'], new['mtime_ns'])
      value['files'].append(new)
      candidate['sources'][new['file_id']] = (output, info)
      candidate['cache_tokens'].append(token)
    return candidate

  def read(self, candidate, entry, offset, size):
    self.verify(candidate)
    path, expected = candidate['sources'][entry['file_id']]
    if identity(path) != expected or not 0 <= offset < entry['size'] or not 0 < size <= 65536:
      raise SourceChanged('privacy_copy_changed')
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    try:
      os.lseek(fd, offset, os.SEEK_SET)
      data = os.read(fd, min(size, entry['size'] - offset))
    finally:
      os.close(fd)
    self.verify(candidate)
    if identity(path) != expected or len(data) != min(size, entry['size'] - offset):
      raise SourceChanged('privacy_copy_changed')
    return data

  def completed(self, candidate):
    self.verify(candidate)
    self.raw.completed(candidate['original'])
    for token in candidate['cache_tokens']:
      for suffix in ('.data', '.json', '.part'):
        (self.sanitizer.cache / (token + suffix)).unlink(missing_ok=True)
