"""Pack a lossless reconstructed road warp in Jetlink's current-frame format."""
import argparse
import contextlib
import json
import os
from pathlib import Path
import subprocess
import tempfile


def pack(clip, destination):
  command = ['ffmpeg', '-nostdin', '-v', 'error', '-i', str(clip), '-map', '0:v:0', '-vsync', '0',
             '-f', 'rawvideo', '-pix_fmt', 'yuv420p', '-threads', '1', 'pipe:1']
  # Refuse a video with a different shape before interpreting raw bytes.
  meta = subprocess.check_output(['ffprobe', '-v', 'error', '-select_streams', 'v:0', '-show_entries',
                                  'stream=width,height,pix_fmt', '-of', 'json', str(clip)])
  if json.loads(meta)['streams'] != [dict(width=512, height=256, pix_fmt='yuv420p')]:
    raise ValueError('expected the reconstructed 512x256 planar road warp')
  n = 512 * 256
  with tempfile.TemporaryDirectory(dir=destination.parent) as scratch:
    packed = Path(scratch) / 'road.yuv6'
    with packed.open('xb') as out, subprocess.Popen(command, stdout=subprocess.PIPE) as proc:
      with contextlib.closing(proc.stdout):
        while buf := proc.stdout.read(n * 3 // 2):
          if len(buf) != n * 3 // 2:
            raise ValueError('truncated image')
          for col, row in ((0, 0), (0, 1), (1, 0), (1, 1)):
            out.write(b''.join(buf[start+col:start+512:2] for start in range(row*512, n, 1024)))
          out.write(buf[n:])
      if proc.wait() or out.tell() == 0:
        raise RuntimeError('video decode failed')
    os.link(packed, destination)


if __name__ == '__main__':
  cli = argparse.ArgumentParser(description=__doc__)
  cli.add_argument('clip', type=Path)
  cli.add_argument('destination', type=Path)
  pack(**vars(cli.parse_args()))
