from types import SimpleNamespace

import pytest

from openpilot.common.hud_driving_feedback import (
  lane_line_probabilities, lane_confidence_grade, lane_confidence_score, lane_probability_text,
)


@pytest.mark.parametrize('values,expected', [
  ([1., .95, .82, 1.], (.95, .82)),
  ([0., 1., 0., 0.], (1., 0.)),
  ([1., float('nan'), .82, 1.], (None, .82)),
  ([1., .95, float('inf'), 1.], (.95, None)),
  ([1., -.1, 1.1, 1.], (None, None)),
  ([1., True, .82, 1.], (None, .82)),
  ([], (None, None)), ([.95, .82], (None, None)),
  (None, (None, None)),
])
def test_reads_only_current_lane_and_preserves_each_valid_side(values, expected):
  assert lane_line_probabilities(SimpleNamespace(laneLineProbs=values)) == expected


def test_missing_lane_head_never_falls_back_to_disengagement_grade():
  assert lane_line_probabilities(None) == (None, None)
  assert lane_line_probabilities(SimpleNamespace(confidence='green')) == (None, None)


@pytest.mark.parametrize('left,right,score,grade', [
  (.95, .82, .82, 'yellow'), (.85, 1., .85, 'green'),
  (.5, .99, .5, 'yellow'), (.49, 1., .49, 'red'),
  (0., 0., 0., 'red'), (None, .95, None, ''), (.95, None, None, ''),
  (float('nan'), .95, None, ''), (.95, 1.1, None, ''),
])
def test_bar_uses_weaker_side_and_requires_both_sides(left, right, score, grade):
  assert lane_confidence_score(left, right) == score
  assert lane_confidence_grade(score) == grade


def test_probability_text_distinguishes_no_detection_from_no_data():
  assert lane_probability_text(0.) == '0%'
  assert lane_probability_text(1.) == '100%'
  assert lane_probability_text(None) == '--'
  assert lane_probability_text(float('nan')) == '--'
