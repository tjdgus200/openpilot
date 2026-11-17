"""Bounded home-only segment pipelines. The existing owner process/permission pipe remains authoritative."""
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
import threading


def run_parallel(catalog, transfer, commit, allowed, workers=4):
  """Selection/completion are serial; independent file preparation/transfers overlap.

  transfer(candidate, continuing) owns its connection and returns byte count.
  No candidate becomes completed until that callable verifies every file ACK.
  """
  if type(workers) is not int or not 1 <= workers <= 4:
    raise ValueError('invalid_upload_parallelism')
  stopping = threading.Event()
  continuing = lambda: not stopping.is_set() and allowed() is True
  reserved, active = set(), {}
  total_bytes, segments, exhausted = 0, 0, False
  pool = ThreadPoolExecutor(workers, thread_name_prefix='pv5-upload')
  try:
    while True:
      if not continuing():
        raise RuntimeError('parallel_upload_permission_lost')
      while len(active) < workers and not exhausted:
        candidate = catalog.next(commit, exclude=reserved)
        if candidate is None:
          exhausted = True
          break
        name = candidate['manifest']['segment']
        if name in reserved:
          raise ValueError('duplicate_parallel_candidate')
        reserved.add(name)
        active[pool.submit(transfer, candidate, continuing)] = candidate
      if not active:
        return {'reason': 'idle', 'bytes': total_bytes, 'segments': segments}
      done, _ = wait(active, timeout=.05, return_when=FIRST_COMPLETED)
      for future in done:
        candidate = active.pop(future)
        count = future.result()
        if type(count) is not int or count < 0:
          raise ValueError('invalid_transfer_result')
        if not continuing():
          raise RuntimeError('parallel_upload_permission_lost')
        catalog.completed(candidate)
        total_bytes += count
        segments += 1
  finally:
    stopping.set()
    for future in active:
      future.cancel()
    pool.shutdown(wait=True, cancel_futures=True)
