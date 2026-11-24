from types import SimpleNamespace
import unittest

from usb_owner_policy import DETACHED_DWELL_NS, JETLINK, PANDA_TIMEOUT_NS, PC, UsbOwnerPolicy


MS = 1_000_000
START = 1000 * MS


def board(variant="dos", harness="notConnected", line=False, can=False):
  return SimpleNamespace(pandaType=variant, harnessStatus=harness, ignitionLine=line, ignitionCan=can)


def sample(stamp, *boards, valid=True):
  return SimpleNamespace(pandaStates=boards, logMonoTime=stamp, valid=valid)


class UsbOwnerPolicyTest(unittest.TestCase):
  def setUp(self):
    self.policy = UsbOwnerPolicy()

  def qualify(self, *boards):
    for tick in range(6):
      stamp = START + tick * 100 * MS
      self.assertEqual(self.policy.owner(sample(stamp, *boards), stamp, adb_requested=True),
                       PC if tick == 5 else JETLINK)
    return stamp

  def test_boot_and_missing(self):
    self.assertEqual(self.policy.owner(None, START, adb_requested=True), JETLINK)
    self.assertEqual(self.policy.owner(sample(0, board()), 0, adb_requested=True), JETLINK)

  def test_connected_harness_even_with_both_ignitions_off(self):
    for harness in ("normal", "flipped"):
      for tick in range(10):
        stamp = START + tick * 100 * MS
        self.assertEqual(self.policy.owner(sample(stamp, board(harness=harness)), stamp, adb_requested=True), JETLINK)

  def test_two_valid_detached_boards(self):
    self.qualify(board(), board("redPanda"))

  def test_boards_without_harness_detection_never_qualify(self):
    for boards in ((board("whitePanda"),), (board("greyPanda"),), (board(), board("whitePanda"))):
      with self.subTest(boards=boards):
        for tick in range(8):
          stamp = START + tick * 100 * MS
          self.assertEqual(self.policy.owner(sample(stamp, *boards), stamp, adb_requested=True), JETLINK)

  def test_duplicate_does_not_qualify_or_refresh(self):
    data = sample(START, board())
    for age in (0, 100 * MS, PANDA_TIMEOUT_NS, DETACHED_DWELL_NS):
      self.assertEqual(self.policy.owner(data, START + age, adb_requested=True), JETLINK)

  def test_no_gap_bridging(self):
    for tick in range(5):
      stamp = START + tick * 100 * MS
      self.assertEqual(self.policy.owner(sample(stamp, board()), stamp, adb_requested=True), JETLINK)
    stamp += PANDA_TIMEOUT_NS
    self.assertEqual(self.policy.owner(sample(stamp, board()), stamp, adb_requested=True), JETLINK)

  def test_faults_revoke_an_active_pc_selection(self):
    for evidence in (
        lambda t: None,
        lambda t: sample(t),
        lambda t: sample(t, board("unknown")),
        lambda t: sample(t, board("pedal")),
        lambda t: sample(t, board(harness="normal")),
        lambda t: sample(t, board(), board("redPanda", harness="flipped")),
        lambda t: sample(t, board(line=True)),
        lambda t: sample(t, board(can=True)),
        lambda t: sample(t, *([board()] * 9)),
        lambda t: sample(t, board(), valid=False),
        lambda t: sample(t + MS, board()),
        lambda t: sample(t - PANDA_TIMEOUT_NS, board()),
        lambda t: sample(t - 2 * MS, board()),
    ):
      with self.subTest(evidence=evidence):
        self.policy = UsbOwnerPolicy()
        now = self.qualify(board()) + MS
        self.assertEqual(self.policy.owner(evidence(now), now, adb_requested=True), JETLINK)
        self.assertEqual(self.policy.owner(sample(now, board()), now, adb_requested=True), JETLINK)

  def test_revoke_at_freshness_boundary(self):
    stamp = self.qualify(board())
    data = sample(stamp, board())
    self.assertEqual(self.policy.owner(data, stamp + PANDA_TIMEOUT_NS - 1, adb_requested=True), PC)
    self.assertEqual(self.policy.owner(data, stamp + PANDA_TIMEOUT_NS, adb_requested=True), JETLINK)

  def test_adding_board_restarts_confirmation(self):
    now = self.qualify(board()) + 100 * MS
    self.assertEqual(self.policy.owner(sample(now, board(), board("redPanda")), now, adb_requested=True), JETLINK)

  def test_adb_toggle_revokes_and_requires_fresh_confirmation(self):
    stamp = self.qualify(board()) + 100 * MS
    data = sample(stamp, board())
    self.assertEqual(self.policy.owner(data, stamp, adb_requested=False), JETLINK)
    self.assertEqual(self.policy.owner(data, stamp, adb_requested=True), JETLINK)


if __name__ == "__main__":
  unittest.main()
