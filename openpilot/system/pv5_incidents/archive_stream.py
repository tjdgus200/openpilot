"""Bounded native Cap'n Proto framing; no reduced schema or whole-log decode."""

import bz2
from contextlib import contextmanager
import struct

MAX_MESSAGE = 16 * 1024**2
MAX_DECODED = 1024**3


@contextmanager
def decoded(path):
  with path.open('rb') as raw:
    magic = raw.read(4)
    raw.seek(0)
    if path.name.endswith('.zst') or magic == b'\x28\xb5\x2f\xfd':
      import zstandard

      with zstandard.ZstdDecompressor().stream_reader(raw) as stream:
        yield stream
    elif path.name.endswith('.bz2') or magic.startswith(b'BZh'):
      with bz2.BZ2File(raw) as stream:
        yield stream
    else:
      yield raw


def messages(stream, check=lambda: True, limit=MAX_DECODED):
  consumed = 0

  def read(size, eof=False):
    nonlocal consumed
    if size < 0 or size > MAX_MESSAGE + 512 or consumed + size > limit:
      raise ValueError('decoded_log_budget')
    chunks, remaining = [], size
    while remaining:
      check()
      block = stream.read(min(65536, remaining))
      if not block:
        if eof and remaining == size:
          return b''
        raise ValueError('truncated_native_log')
      chunks.append(block)
      remaining -= len(block)
      consumed += len(block)
    return b''.join(chunks)

  while header := read(4, eof=True):
    count = struct.unpack('<I', header)[0] + 1
    if not 1 <= count <= 64:
      raise ValueError('native_segment_table_budget')
    table = read(4 * count)
    padding = read(4) if count % 2 == 0 else b''
    size = sum(struct.unpack('<' + 'I' * count, table)) * 8
    if size > MAX_MESSAGE:
      raise ValueError('native_message_budget')
    yield header + table + padding + read(size)
