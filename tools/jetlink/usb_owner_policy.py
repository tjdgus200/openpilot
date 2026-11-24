"""Offline candidate for PV5's shared USB port owner decision.

Consumes native pandaStates. It does not bind USB, start a service, or authorize SSH.
"""

JETLINK = "jetlink"
PC = "pc"
PANDA_TIMEOUT_NS = 250_000_000
DETACHED_DWELL_NS = 500_000_000
# White/Grey have no harness detection. Their default notConnected is not evidence.
HARNESS_PANDA_TYPES = frozenset(("blackPanda", "uno", "dos", "redPanda", "redPandaV2", "tres", "cuatro"))


class UsbOwnerPolicy:
  def __init__(self):
    self._clear_detachment()

  def _clear_detachment(self):
    self._first_stamp = 0
    self._last_stamp = 0
    self._board_types = ()

  def owner(self, panda_states, now_ns, *, adb_requested):
    """Return the desired owner, with now_ns sampled from CLOCK_BOOTTIME."""
    board_types = ()
    stamp = 0
    try:
      if adb_requested is True and panda_states.valid is True:
        stamp = panda_states.logMonoTime
        boards = panda_states.pandaStates
        if 0 < stamp <= now_ns < stamp + PANDA_TIMEOUT_NS and 1 <= len(boards) <= 8:
          board_types = tuple(str(board.pandaType) for board in boards
                              if str(board.harnessStatus) == "notConnected"
                              and board.ignitionLine is False and board.ignitionCan is False)
          if len(board_types) != len(boards) or not HARNESS_PANDA_TYPES.issuperset(board_types):
            board_types = ()
    except (AttributeError, TypeError, ValueError):
      pass

    if not board_types or stamp < self._last_stamp:
      self._clear_detachment()
      return JETLINK

    if board_types != self._board_types or stamp - self._last_stamp >= PANDA_TIMEOUT_NS:
      self._first_stamp = stamp
      self._board_types = board_types
    self._last_stamp = stamp
    return PC if stamp - self._first_stamp >= DETACHED_DWELL_NS else JETLINK
