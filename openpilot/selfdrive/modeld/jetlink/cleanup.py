"""Retain cleanup proofs until every previous resource has released ownership."""
from jetlink.transport.base import LinkError


class UnsafeCleanupError(LinkError):
  def __init__(self, message, *, check=None):
    super().__init__(message)
    # Only an explicitly supplied, read-only ownership check permits recovery.
    self.check = check


class CleanupFailure(str):
  def __new__(cls, message, checks):
    value = super().__new__(cls, message[:300])
    value.checks = tuple(checks) or (None,)
    value.next_check = 0.0
    return value

  def poll(self, now):
    if now < self.next_check:
      return False
    self.next_check = now + 5.0
    return self.cleared()

  def cleared(self):
    # Unknown cleanup failures stay blocked. Never call close/unbind again from
    # this poll: a late old watchdog must not tear down a newly bound gadget.
    if any(not callable(check) for check in self.checks):
      return False
    try:
      return all(check() is True for check in self.checks)
    except Exception:
      return False

  @classmethod
  def combine(cls, *failures):
    failures = [failure for failure in failures if failure]
    if not failures:
      return None
    # Keep earlier failed proofs even when a later resource reports a new
    # error; a successful second close must never erase an old live reader.
    checks = [check for failure in failures for check in (
      failure.checks if isinstance(failure, cls) else (None,))]
    return cls(str(failures[0]), checks)
