"""Export only committed renderer/server sources; never export vehicle captures or credentials."""
import argparse
import copy
import hashlib
import io
import json
import posixpath
from pathlib import Path
import subprocess
import tarfile

ROOT = Path(__file__).resolve().parents[2]
PATHS = ['LICENSE', 'opendbc_repo/LICENSE', 'openpilot/__init__.py', 'openpilot/common', 'openpilot/cereal',
         'openpilot/selfdrive/carrot/cluster', 'openpilot/selfdrive/carrot/carrot_navi.py',
         'openpilot/selfdrive/carrot/carrot_navi_cereal.py', 'openpilot/selfdrive/carrot/deceleration_source.py',
         'openpilot/selfdrive/controls/lib', 'openpilot/selfdrive/modeld/constants.py',
         'openpilot/selfdrive/modeld/jetlink/cinque_v2.json', 'openpilot/selfdrive/modeld/jetlink/jetson_model.json', 'openpilot/selfdrive/assets',
         'openpilot/selfdrive/modeld/jetlink/local_host_release.json',
         'openpilot/system/hardware', 'openpilot/system/version.py',
         'opendbc_repo/opendbc/car/car.capnp', 'opendbc_repo/opendbc/car/include',
         'third_party/jetlink', 'tools/jetlink']


def relay_member(source, member):
  """Resolve tracked internal links and remove group-write bits for relay archives."""
  output = copy.copy(member)
  target = member
  seen = set()
  while target.issym():
    if target.name in seen or posixpath.isabs(target.linkname):
      raise ValueError('Unsafe/cyclic source bundle link')
    seen.add(target.name)
    name = posixpath.normpath(posixpath.join(posixpath.dirname(target.name), target.linkname))
    if name.startswith('../'):
      raise ValueError('Source bundle link escapes archive')
    target = source.getmember(name)
  if not (target.isfile() or target.isdir()):
    raise ValueError('Unsupported source bundle member')
  output.type, output.size, output.linkname = target.type, target.size, ''
  output.mode = 0o755 if target.isdir() or target.mode & 0o111 else 0o644
  output.uid = output.gid = 0
  output.uname = output.gname = ''
  return output, source.extractfile(target) if target.isfile() else None


def main():
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument('output', type=Path)
  parser.add_argument('--regular-files', action='store_true', help='Flatten internal links and normalize modes for the isolated relay receiver')
  args = parser.parse_args()
  git = ['git', '-c', f'safe.directory={ROOT.as_posix()}', '-C', str(ROOT)]
  subprocess.run([*git, 'diff', '--quiet', 'HEAD', '--', *PATHS], check=True)
  untracked = subprocess.check_output([*git, 'ls-files', '--others', '--exclude-standard', '--', *PATHS])
  if untracked.strip():
    raise RuntimeError('Commit or remove untracked host source files before exporting')
  commit = subprocess.check_output([*git, 'rev-parse', 'HEAD'])
  # Inspect cleanliness using the checkout's own CRLF policy; only the Linux
  # export forces LF. Changing that policy for diff misclassifies Windows files.
  archive = subprocess.check_output([*git, '-c', 'core.autocrlf=false', '-c', 'core.eol=lf',
                                     'archive', 'HEAD', *PATHS])
  with tarfile.open(fileobj=io.BytesIO(archive), mode='r:') as source, tarfile.open(args.output, 'w:gz') as target:
    for member in source:
      if args.regular_files:
        # This upstream symlink is dangling in carrot-nav. It describes the
        # comma OS updater, which must never run in the Jetson DATA runtime.
        if member.name == 'openpilot/system/hardware/comma/agnos.json':
          continue
        normalized, content = relay_member(source, member)
        target.addfile(normalized, content)
      else:
        target.addfile(member, source.extractfile(member) if member.isfile() else None)
    entry = tarfile.TarInfo('SOURCE_COMMIT')
    entry.size = len(commit)
    target.addfile(entry, io.BytesIO(commit))
  sha = hashlib.sha256()
  with args.output.open('rb') as bundle:
    for block in iter(lambda: bundle.read(4 << 20), b''):
      sha.update(block)
  args.output.with_suffix(args.output.suffix + '.sha256').write_text(
    f'{sha.hexdigest()}  {args.output.name}\n', encoding='utf-8', newline='\n')
  args.output.with_suffix(args.output.suffix + '.json').write_text(json.dumps({
    'source_commit': commit.decode().strip(), 'bundle': args.output.name,
    'size': args.output.stat().st_size, 'sha256': sha.hexdigest(),
  }, indent=2) + '\n', encoding='utf-8', newline='\n')
  print(commit.decode().strip(), args.output)


if __name__ == '__main__':
  main()
