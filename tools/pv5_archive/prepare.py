"""Bounded PC privacy preprocessing, never model inference or vehicle control."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import time
from openpilot.system.pv5_incidents.archive_protocol import MAX_FILE, NAMES
from openpilot.system.pv5_incidents.archive_stream import decoded, messages
from openpilot.system.pv5_incidents.archive_sanitize import BoundedWriter, excluded, identity
from tools.pv5_archive.schema import full_schema


def prepare(source, output, reserve=0, maximum=MAX_FILE, ffmpeg=None, ffprobe=None):
  source, output = Path(source), Path(output)
  if source.name not in NAMES or output.is_symlink() or source.resolve() == output.resolve():
    raise ValueError('invalid_preparation_path')
  before, started = identity(source), time.monotonic()
  if not 0 < before[2] <= MAX_FILE or not 0 < maximum <= MAX_FILE:
    raise ValueError('preparation_budget')
  last_space = [0.0]
  def check():
    now = time.monotonic()
    if now - started > 180:
      raise ValueError('preparation_deadline')
    # Sealed input under an owner file lock. No per-event stat/glob on the device.
    if now - last_space[0] >= 1:
      last_space[0] = now
      if shutil.disk_usage(output.parent).free < reserve + 4 * 1024**2:
        raise ValueError('preparation_space')
    return True
  def helper(args):
    check()
    result = subprocess.run(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            timeout=min(60, max(1, 180 - (time.monotonic() - started))),
                            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
    if result.returncode or len(result.stdout) > 8192:
      raise ValueError('preparation_media_failed')
    return result.stdout
  def streams(path):
    return [row['codec_type'] for row in json.loads(helper([ffprobe, '-v', 'error', '-protocol_whitelist', 'file,pipe',
              '-show_entries', 'stream=codec_type', '-of', 'json', str(path)]))['streams']]
  removed, name = 0, source.name
  try:
    if name.startswith('rlog'):
      import zstandard
      with full_schema() as schema, decoded(source) as src, output.open('xb') as raw:
        writer = BoundedWriter(raw, maximum, check)
        with zstandard.ZstdCompressor(level=1).stream_writer(writer, closefd=False) as dst:
          for frame in messages(src, check):
            with schema.Event.from_bytes(frame) as event:
              drop = excluded(event.which())
            if drop:
              removed += 1
            else:
              dst.write(frame)
        raw.flush()
        os.fsync(raw.fileno())
      name = 'rlog.zst'
    elif source.suffix == '.hevc':
      with source.open('rb') as src, output.open('xb') as raw:
        writer = BoundedWriter(raw, maximum, check)
        while block := src.read(1024**2):
          writer.write(block)
        raw.flush()
        os.fsync(raw.fileno())
    else:
      ffmpeg, ffprobe = ffmpeg or shutil.which('ffmpeg'), ffprobe or shutil.which('ffprobe')
      if not ffmpeg or not ffprobe:
        raise ValueError('preparation_media_unavailable')
      kinds = streams(source)
      if kinds.count('video') != 1 or any(kind not in ('video', 'audio', 'data') for kind in kinds):
        raise ValueError('unexpected_media_streams')
      helper([ffmpeg, '-nostdin', '-v', 'error', '-protocol_whitelist', 'file,pipe', '-i', str(source),
              '-map', '0:v:0', '-c:v', 'copy', '-an', '-sn', '-dn', '-map_metadata', '-1', '-map_chapters', '-1',
              '-fs', str(maximum), '-f', 'mpegts', str(output)])
      if output.stat().st_size >= maximum - 65536 or streams(output) != ['video']:
        raise ValueError('preparation_truncated_or_audio')
      name = source.stem + '.ts'
      with output.open('r+b') as raw:
        os.fsync(raw.fileno())
    check()
    if identity(source) != before or not 0 < output.stat().st_size <= maximum:
      raise ValueError('preparation_source_or_output_changed')
    return {'name': name, 'size': output.stat().st_size, 'filtered_messages': removed}
  except BaseException:
    output.unlink(missing_ok=True)
    raise


def main():
  from tools.pv5_archive.limits import apply
  apply(active_processes=2, cpu_seconds=180)
  parser = argparse.ArgumentParser()
  parser.add_argument('--job', type=Path, required=True)
  parser.add_argument('--result', type=Path, required=True)
  args = parser.parse_args()
  if args.job.stat().st_size > 8192:
    raise ValueError('preparation_request_budget')
  job = json.loads(args.job.read_bytes())
  result = prepare(**job)
  args.result.write_text(json.dumps(result), encoding='utf-8')


if __name__ == '__main__':
  main()
