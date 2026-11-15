import unittest

from tools.pv5_archive.analyze import Triage
from tools.pv5_archive.schema import full_schema
from tools.pv5_archive.screen import select_review
from tools.pv5_archive.review import selected_samples


def result(rows, markers=(), candidates=()):
  return {'status': 'review_candidates', 'samples': rows, 'event_markers': list(markers),
          'candidates': list(candidates), 'duration_seconds': 400}


class ScreeningChecks(unittest.TestCase):
  def test_normal_noise_cruise_release_and_mild_scc_are_quiet(self):
    rows = [{'t': index / 10, 'v': 8, 'a': -0.5 if index % 2 else 0,
             'scc_active': True, 'scc_raw': -1, 'scc_metadata_fresh': True} for index in range(30)]
    value = select_review(result(rows, candidates=[{'kind': 'negative_jerk', 'count': 18}, {'kind': 'cruise_release', 'count': 1}]))
    self.assertEqual(value['candidates'], [])
    self.assertEqual(value['review_windows'], [])
    self.assertEqual(value['raw_threshold_candidate_counts']['negative_jerk'], 18)
    self.assertFalse(value['screening_uses_AI_tokens'])

  def test_strong_scc_request_requires_fresh_qualified_metadata(self):
    for fresh in (False, True):
      rows = [{'t': 20, 'v': 15, 'a': 0, 'scc_raw': -5, 'scc_metadata_fresh': fresh}]
      value = select_review(result(rows))
      self.assertEqual([row['kind'] for row in value['candidates']], ['stock_scc_hard_request'] if fresh else [])
      if fresh:
        self.assertEqual(value['review_windows'][0]['start_seconds'], 15)
        self.assertEqual(value['review_windows'][0]['end_seconds'], 25)
        self.assertEqual(rows[0]['a'], 0)  # Request is not measured deceleration.

  def test_short_ttc_needs_continuity_and_parked_close_object_is_quiet(self):
    row = {'t': 0, 'v': 15, 'a': 0, 'lead_distance': 8, 'lead_relative_speed': -5}
    self.assertFalse(select_review(result([row]))['candidates'])
    self.assertTrue(select_review(result([row, dict(row, t=0.1)]))['candidates'])
    self.assertTrue(select_review(result([dict(row, lead_distance=4)]))['candidates'])
    self.assertFalse(select_review(result([dict(row, v=0, lead_distance=1)]))['candidates'])

  def test_hard_braking_and_credible_abrupt_onset_are_retained(self):
    rows = [{'t': 0, 'v': 15, 'a': 0}, {'t': 0.1, 'v': 15, 'a': -1.6}, {'t': 0.2, 'v': 15, 'a': -3.2}]
    value = select_review(result(rows))
    self.assertEqual({row['kind'] for row in value['candidates']}, {'hard_deceleration', 'abrupt_deceleration_onset'})
    self.assertEqual(len(value['review_windows']), 1)

  def test_radar_jump_and_target_transition_are_distinguished_with_full_schema(self):
    with full_schema() as schema:
      for changed_id, expected in ((7, 'radar_value_jump'), (8, 'new_close_target')):
        triage = Triage()
        for index, (distance, identity) in enumerate(((50, 7), (20, changed_id))):
          event = schema.Event.new_message(logMonoTime=100000000000 + index * 50000000, valid=True)
          lead = event.init('radarState').leadOne
          lead.status, lead.radar, lead.radarTrackId, lead.dRel, lead.vRel = True, True, identity, distance, -2.0
          with schema.Event.from_bytes(event.to_bytes()) as parsed:
            triage.accept(parsed)
        value = select_review(triage.result())
        self.assertEqual({row['kind'] for row in value['candidates']}, {expected})

  def test_aeb_and_disable_are_kept_and_windows_are_bounded(self):
    markers = [{'kind': 'recorded_stock_aeb', 't': index * 40} for index in range(10)]
    markers.append({'kind': 'recorded_system_disable', 't': 0})
    value = select_review(result([], markers=markers))
    self.assertEqual(len(value['review_windows']), 8)
    self.assertTrue(value['review_window_limit_reached'])
    self.assertTrue(all(0 <= row['end_seconds'] - row['start_seconds'] <= 20 for row in value['review_windows']))

  def test_late_emergency_has_priority_over_many_earlier_low_priority_windows(self):
    markers = [{'kind': 'radar_value_jump', 't': index * 30} for index in range(10)]
    markers.append({'kind': 'recorded_stock_aeb', 't': 380})
    value = select_review(result([], markers=markers))
    self.assertTrue(any('recorded_stock_aeb' in row['reasons'] for row in value['review_windows']))
    self.assertEqual(len(value['review_windows']), 8)

  def test_reviewer_only_receives_selected_window_and_alignment_is_not_invented(self):
    rows = [{'t': index, 'v': 20, 'a': 0} for index in range(60)]
    value = select_review(result(rows, markers=[{'kind': 'recorded_stock_aeb', 't': 30}]))
    review = selected_samples(value, 0)
    self.assertEqual([row['t'] for row in review['samples']], list(range(25, 36)))
    self.assertFalse(review['video_time_alignment_verified'])
    with self.assertRaises(ValueError):
      selected_samples(value, 8)

