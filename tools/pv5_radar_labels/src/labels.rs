//! Advisory classes for measured frontRadar IDs, in camera capture clock time.
use std::collections::{BTreeMap, BTreeSet};

pub const SAMPLE_NS: u64 = 200_000_000;
pub const MAX_RADAR_AGE_NS: u64 = 100_000_000;
const MAX_CALIBRATION_AGE_NS: u64 = 1_000_000_000;
const LABEL_TTL_NS: u64 = 300_000_000;

#[derive(Clone, Copy, Debug)]
pub struct RadarPoint {
    /// Recorded frontRadar.trackId. Caller filters source and measured bit.
    pub slot: u32,
    pub d_rel: f64,
    pub y_rel: f64,
    pub v_rel: f64,
}

#[derive(Clone, Copy, Debug)]
pub struct Detection {
    pub class_id: u8,
    pub confidence: f64,
    pub bbox: [f64; 4],
}

#[derive(Clone, Copy, Debug)]
pub struct FrameGeometry {
    pub capture_ns: u64,
    pub calibration_ns: u64,
    /// Maps calibrated vehicle coordinates (forward, left, up, 1) to this view.
    pub projection: [[f64; 4]; 3],
    pub image_width: u32,
    pub image_height: u32,
}

#[derive(Clone, Copy, Debug)]
pub struct ClassLabel {
    pub slot: u32,
    /// An association supported by motion alone cannot seed radar-only HUD hold.
    pub motion_only: bool,
    pub class_id: u8,
    pub confidence: f64,
    pub bbox: [f64; 4],
    /// Measured radar row at the currently matched radar timestamp.
    pub point: RadarPoint,
    pub evidence_ns: u64,
    pub expires_ns: u64,
}

pub fn class_name(id: u8) -> Option<&'static str> {
    Some(match id {
        0 => "person",
        1 => "bicycle",
        2 => "car",
        3 => "motorcycle",
        5 => "bus",
        7 => "truck",
        _ => return None,
    })
}

#[derive(Clone, Copy)]
struct History {
    radar: RadarPoint,
    radar_ns: u64,
    evidence_radar_ns: u64,
    evidence: Option<ClassLabel>,
    confirmed: bool,
    motion: [Option<MotionSample>; 2],
}

#[derive(Clone, Copy)]
struct MotionSample {
    stamp: u64,
    bbox: [f64; 4],
    pixel: [f64; 2],
    class_id: u8,
}

impl MotionSample {
    fn follows(self, prev: Self) -> bool {
        if self.class_id != prev.class_id || !same_box_region(self.bbox, prev.bbox) {
            return false;
        }
        (0..2).all(|axis| {
            let old_size = prev.bbox[axis + 2] - prev.bbox[axis];
            let size = self.bbox[axis + 2] - self.bbox[axis];
            // Map the previous radar pixel with the box translation and scale.
            let expected = self.bbox[axis] + (prev.pixel[axis] - prev.bbox[axis]) * size / old_size;
            (self.pixel[axis] - expected).abs() <= (0.12 * size.min(old_size)).clamp(1., 3.)
        })
    }
}

fn coherent_motion(history: &mut [Option<MotionSample>; 2], sample: MotionSample) -> bool {
    let [first, last] = *history;
    if let Some(last) = last {
        if sample.stamp - last.stamp <= 150_000_000 && sample.follows(last) {
            if last.stamp == sample.stamp {
                return false;
            }
            *history = [Some(last), Some(sample)];
            return first.is_some_and(|first| {
                if sample.stamp - first.stamp > 250_000_000 || !sample.follows(first) {
                    return false;
                }
                // Require observed movement in the same direction on at least
                // one image axis. A stationary coincidence cannot seed a class.
                (0..2).any(|axis| {
                    let radar_step = sample.pixel[axis] - first.pixel[axis];
                    let box_step = (sample.bbox[axis] + sample.bbox[axis + 2]
                        - first.bbox[axis]
                        - first.bbox[axis + 2])
                        * 0.5;
                    radar_step.abs() >= 1. && box_step.abs() >= 1. && radar_step * box_step > 0.
                })
            });
        }
    }
    *history = [None, Some(sample)];
    false
}

#[derive(Default)]
pub struct Labeler {
    tracks: BTreeMap<u32, History>,
    last_capture: Option<u64>,
    last_radar: Option<u64>,
    last_geometry: Option<FrameGeometry>,
}

fn recent(now: u64, past: u64, max: u64) -> bool {
    past != 0 && now.checked_sub(past).is_some_and(|dt| dt <= max)
}

impl FrameGeometry {
    fn valid(&self) -> bool {
        recent(self.capture_ns, self.calibration_ns, MAX_CALIBRATION_AGE_NS)
            && self.image_height > 0
            && self.image_width > 0
            && self.projection.iter().flatten().all(|x| x.is_finite())
    }

    fn project(&self, p: RadarPoint, radar_ns: u64) -> Option<[f64; 2]> {
        let dt = (self.capture_ns - radar_ns) as f64 * 1e-9;
        // dRel uses the radar origin. The supplied camera projection uses the
        // vehicle origin, 1.52 m behind it in these PV5 captures.
        let forward = p.d_rel + dt * p.v_rel + 1.52;
        let q = self
            .projection
            .map(|row| row[0] * forward + row[1] * p.y_rel + row[3]);
        if q[2] <= 0.01 {
            return None;
        }
        let (u, v) = (q[0] / q[2], q[1] / q[2]);
        ((0.0..f64::from(self.image_width)).contains(&u)
            && (0.0..f64::from(self.image_height)).contains(&v))
        .then_some([u, v])
    }
}

fn plausible(p: RadarPoint) -> bool {
    p.d_rel.is_finite() && p.d_rel > 0.0 && p.y_rel.is_finite() && p.v_rel.is_finite()
}

fn continuous(h: History, p: RadarPoint, radar_ns: u64) -> bool {
    if !recent(radar_ns, h.radar_ns, LABEL_TTL_NS) {
        return false;
    }
    let dt = (radar_ns - h.radar_ns) as f64 * 1e-9;
    (p.d_rel - (h.radar.d_rel + h.radar.v_rel * dt)).abs() <= 0.75 + 2.0 * dt
        && (p.y_rel - h.radar.y_rel).abs() <= 0.45 + 3.0 * dt
        && (p.v_rel - h.radar.v_rel).abs() <= 2.0 + 8.0 * dt
}

// Each path counts competitors in a wider region than it accepts for a match.
#[derive(Clone, Copy, PartialEq, Debug)]
struct Gate {
    strict_candidate: bool,
    strict_match: bool,
    motion_match: bool,
}

fn candidate(d: Detection, point: [f64; 2], g: &FrameGeometry) -> Option<Gate> {
    let [x1, y1, x2, y2] = d.bbox;
    let (w, h) = (x2 - x1, y2 - y1);
    // Keep plausible competitors even below publication confidence. A weak
    // second box should veto a match rather than give its rival the radar ID.
    if class_name(d.class_id).is_none()
        || !(0.25..=1.0).contains(&d.confidence)
        || !d.bbox.iter().all(|v| v.is_finite())
        || x1 < 0.0
        || y1 < 0.0
        || x2 > f64::from(g.image_width)
        || y2 > f64::from(g.image_height)
        || w < 4.0
        || h < 4.0
    {
        return None;
    }
    let dx = ((point[0] - (x1 + x2) * 0.5) / w).abs();
    let above = (y2 - point[1]) / h;
    (dx <= 0.60 && (-0.384..=0.62).contains(&above)).then_some(Gate {
        strict_candidate: above.abs() <= 0.384,
        strict_match: dx <= 0.50 && above.abs() <= 0.32,
        motion_match: dx <= 0.50 && (-0.32..=0.55).contains(&above),
    })
}

fn same_box_region(a: [f64; 4], b: [f64; 4]) -> bool {
    let iw = (a[2].min(b[2]) - a[0].max(b[0])).max(0.0);
    let ih = (a[3].min(b[3]) - a[1].max(b[1])).max(0.0);
    let intersection = iw * ih;
    let union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - intersection;
    intersection >= 0.20 * union
}

impl Labeler {
    pub fn reset_motion(&mut self) {
        self.last_geometry = None;
    }

    /// None means skipped inference. Some(empty) is a measured miss and
    /// immediately clears the label. Returned boxes belong to evidence_ns.
    pub fn update(
        &mut self,
        g: &FrameGeometry,
        radar_ns: u64,
        points: &[RadarPoint],
        detections: Option<&[Detection]>,
    ) -> Vec<ClassLabel> {
        let now = g.capture_ns;
        let backwards = self.last_capture.is_some_and(|t| now <= t)
            || self.last_radar.is_some_and(|t| radar_ns < t);
        self.last_capture = Some(now);
        self.last_radar = Some(radar_ns);
        if backwards
            || !g.valid()
            || !recent(now, radar_ns, MAX_RADAR_AGE_NS)
            || points.len() > 128
            || detections.is_some_and(|d| d.len() > 256)
        {
            self.tracks.clear();
            return vec![];
        }
        let mut seen = BTreeSet::new();
        let same_view = self.last_geometry.replace(*g).is_some_and(|old| {
            old.projection == g.projection
                && old.image_width == g.image_width
                && old.image_height == g.image_height
        });
        let mut duplicates = BTreeSet::new();
        for p in points {
            if !seen.insert(p.slot) {
                duplicates.insert(p.slot);
            }
        }
        // A match must be unique in BOTH directions before class/confidence
        // confirmation. Two radar returns under the same box both abstain.
        let mut hits = BTreeMap::<u32, (usize, usize, bool)>::new();
        let dets = detections.unwrap_or_default();
        let mut det_degrees = vec![0; dets.len()];
        let mut motion_hits = BTreeMap::new();
        let mut motion_degrees = vec![0; dets.len()];
        for &p in points.iter().filter(|p| plausible(**p)) {
            if let Some(uv) = g.project(p, radar_ns) {
                for (j, d) in dets.iter().enumerate() {
                    if let Some(gate) = candidate(*d, uv, g) {
                        if gate.strict_candidate {
                            let degree = hits.entry(p.slot).or_insert((j, 0, gate.strict_match));
                            degree.1 += 1;
                            det_degrees[j] += 1;
                        }
                        let entry = motion_hits.entry(p.slot).or_insert((j, 0, gate, uv));
                        entry.1 += 1;
                        motion_degrees[j] += 1;
                    }
                }
            }
        }
        let mut next = BTreeMap::new();
        let mut result = vec![];
        for &p in points
            .iter()
            .filter(|p| plausible(**p) && !duplicates.contains(&p.slot))
        {
            let old = self
                .tracks
                .get(&p.slot)
                .copied()
                .filter(|h| continuous(*h, p, radar_ns));
            let old_evidence = old.and_then(|h| h.evidence).filter(|l| now < l.expires_ns);
            let mut h = History {
                radar: p,
                radar_ns,
                evidence: old_evidence,
                evidence_radar_ns: old.map_or(0, |h| h.evidence_radar_ns),
                confirmed: old_evidence.is_some() && old.is_some_and(|h| h.confirmed),
                motion: [None; 2],
            };
            let mut motion_label = None;
            if let Some(&(j, 1, gate, pixel)) = motion_hits.get(&p.slot) {
                let d = dets[j];
                if gate.motion_match && motion_degrees[j] == 1 && d.confidence >= 0.50 {
                    if same_view {
                        h.motion = old.map_or([None; 2], |h| h.motion);
                    }
                    let sample = MotionSample {
                        stamp: radar_ns,
                        bbox: d.bbox,
                        pixel,
                        class_id: d.class_id,
                    };
                    if coherent_motion(&mut h.motion, sample) && !gate.strict_match {
                        motion_label = Some(ClassLabel {
                            slot: p.slot,
                            motion_only: true,
                            class_id: d.class_id,
                            confidence: d.confidence,
                            bbox: d.bbox,
                            point: p,
                            evidence_ns: now,
                            expires_ns: now.saturating_add(LABEL_TTL_NS),
                        });
                    }
                }
            }
            if detections.is_some() {
                h.evidence = None;
                h.confirmed = false;
                if let Some(&(j, 1, true)) = hits.get(&p.slot) {
                    let d = dets[j];
                    if det_degrees[j] == 1 && d.confidence >= 0.50 {
                        let agrees = old_evidence.is_some_and(|l| {
                            l.class_id == d.class_id && same_box_region(l.bbox, d.bbox)
                        });
                        if agrees && old.is_some_and(|h| h.evidence_radar_ns == radar_ns) {
                            // One radar reading can span multiple wide frames.
                            // A repeated match maintains the original lifetime.
                            h.evidence = old_evidence;
                            h.confirmed = old.is_some_and(|h| h.confirmed);
                        } else {
                            h.confirmed =
                                agrees && old.is_some_and(|h| h.evidence_radar_ns < radar_ns);
                            h.evidence_radar_ns = radar_ns;
                            h.evidence = Some(ClassLabel {
                                slot: p.slot,
                                motion_only: false,
                                class_id: d.class_id,
                                confidence: d.confidence,
                                bbox: d.bbox,
                                point: p,
                                evidence_ns: now,
                                expires_ns: now.saturating_add(LABEL_TTL_NS),
                            });
                        }
                    }
                }
            }
            if let Some(label) = h.evidence.filter(|_| h.confirmed).or(motion_label) {
                result.push(ClassLabel { point: p, ..label });
            }
            next.insert(p.slot, h);
        }
        self.tracks = next;
        result
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const T: u64 = 10_000_000_000;
    const P: RadarPoint = RadarPoint {
        slot: 56,
        d_rel: 10.0,
        y_rel: 0.0,
        v_rel: 0.0,
    };
    const D: Detection = Detection {
        class_id: 0,
        confidence: 0.81,
        bbox: [44.0, 20.0, 56.0, 60.0],
    };

    fn frame(t: u64) -> FrameGeometry {
        FrameGeometry {
            capture_ns: t,
            calibration_ns: t - 1,
            projection: [
                [50.0, -100.0, 0.0, 0.0],
                [50.0, 0.0, 0.0, 115.2],
                [1.0, 0.0, 0.0, 0.0],
            ],
            image_width: 100,
            image_height: 100,
        }
    }
    fn confirmed() -> Labeler {
        let mut m = Labeler::default();
        assert!(m.update(&frame(T), T, &[P], Some(&[D])).is_empty());
        let t = T + SAMPLE_NS;
        let labels = m.update(&frame(t), t, &[P], Some(&[D]));
        assert_eq!(labels.len(), 1);
        assert_eq!(labels[0].slot, 56);
        assert_eq!(labels[0].class_id, 0);
        assert_eq!(labels[0].evidence_ns, t);
        m
    }
    #[test]
    fn skipped_frames_never_refresh_capture_or_expiry() {
        let mut m = confirmed();
        let sample = T + SAMPLE_NS;
        for dt in [50_000_000, 100_000_000, 200_000_000, LABEL_TTL_NS - 1] {
            let t = sample + dt;
            let labels = m.update(&frame(t), t, &[P], None);
            assert_eq!(labels[0].evidence_ns, sample);
            assert_eq!(labels[0].expires_ns, sample + LABEL_TTL_NS);
        }
        let t = sample + LABEL_TTL_NS;
        assert!(m.update(&frame(t), t, &[P], None).is_empty());
    }
    #[test]
    fn sampled_miss_slot_loss_and_discontinuity_each_clear_history() {
        let t = T + SAMPLE_NS + 50_000_000;
        for (points, ds) in [
            (vec![P], Some(vec![])),
            (vec![], None),
            (vec![RadarPoint { d_rel: 33.0, ..P }], None),
            (vec![RadarPoint { y_rel: 4.0, ..P }], None),
            (vec![RadarPoint { v_rel: 7.0, ..P }], None),
            (vec![P, P], Some(vec![D])),
        ] {
            let mut m = confirmed();
            assert!(m.update(&frame(t), t, &points, ds.as_deref()).is_empty());
            let next = t + 50_000_000;
            assert!(m.update(&frame(next), next, &[P], Some(&[D])).is_empty());
        }
    }
    #[test]
    fn competing_box_or_competing_radar_id_veto_both() {
        let t = T + SAMPLE_NS * 2;
        let p2 = RadarPoint {
            slot: 57,
            y_rel: 0.05,
            ..P
        };
        assert!(confirmed()
            .update(&frame(t), t, &[P, p2], Some(&[D]))
            .is_empty());
        let weak = Detection {
            class_id: 3,
            confidence: 0.33,
            ..D
        };
        assert!(confirmed()
            .update(&frame(t), t, &[P], Some(&[D, weak]))
            .is_empty());
    }

    #[test]
    fn nearby_competitor_just_outside_box_must_only_veto() {
        let g = frame(T);
        assert!(!candidate(D, [56.6, 59.0], &g).unwrap().strict_match);
        assert!(candidate(D, [55.9, 59.0], &g).unwrap().strict_match);
        let t = T + SAMPLE_NS * 2;
        let near_edge = RadarPoint {
            slot: 39,
            y_rel: -0.76,
            ..P
        };
        assert!(confirmed()
            .update(&frame(t), t, &[P, near_edge], Some(&[D]))
            .is_empty());
        assert!(!same_box_region([10., 0., 20., 20.], [18., 0., 28., 20.]));
    }
    #[test]
    fn class_change_requires_new_pair_and_two_independent_radar_samples() {
        let mut m = confirmed();
        let t = T + SAMPLE_NS * 2;
        let car = Detection { class_id: 2, ..D };
        assert!(m.update(&frame(t), t, &[P], Some(&[car])).is_empty());
        assert!(m.update(&frame(t + 1), t, &[P], Some(&[car])).is_empty());
        assert_eq!(
            m.update(&frame(t + SAMPLE_NS), t + SAMPLE_NS, &[P], Some(&[car]))[0].class_id,
            2
        );
    }
    #[test]
    fn camera_cadence_above_radar_cadence_preserves_independent_confirmation_and_expiry() {
        let mut m = Labeler::default();
        let period = 50_000_000;
        assert!(m.update(&frame(T), T, &[P], Some(&[D])).is_empty());
        assert!(m.update(&frame(T + period), T, &[P], Some(&[D])).is_empty());
        let later = T + period * 2;
        let confirmed = m.update(&frame(later), later, &[P], Some(&[D]));
        assert_eq!(confirmed[0].evidence_ns, later);
        let repeated = m.update(&frame(later + period), later, &[P], Some(&[D]));
        assert_eq!(repeated[0].evidence_ns, later);
        assert_eq!(repeated[0].expires_ns, confirmed[0].expires_ns);
        assert!(m
            .update(&frame(later + period * 2), later, &[P], Some(&[]))
            .is_empty());
    }
    #[test]
    fn malformed_time_calibration_projection_and_score_abstain() {
        let t = T + SAMPLE_NS * 2;
        assert!(confirmed()
            .update(&frame(t), t + 1, &[P], Some(&[D]))
            .is_empty());
        assert!(confirmed()
            .update(&frame(t), t - MAX_RADAR_AGE_NS - 1, &[P], Some(&[D]))
            .is_empty());
        assert!(confirmed()
            .update(&frame(T), T, &[P], Some(&[D]))
            .is_empty());
        for c in [0, t + 1, t - MAX_CALIBRATION_AGE_NS - 1] {
            assert!(confirmed()
                .update(
                    &FrameGeometry {
                        calibration_ns: c,
                        ..frame(t)
                    },
                    t,
                    &[P],
                    None
                )
                .is_empty());
        }
        let mut g = frame(t);
        g.projection[0][0] = f64::NAN;
        assert!(confirmed().update(&g, t, &[P], None).is_empty());
        for d in [
            Detection {
                confidence: f64::NAN,
                ..D
            },
            Detection {
                confidence: 0.49,
                ..D
            },
            Detection { class_id: 18, ..D },
            Detection {
                bbox: [1., 2., f64::INFINITY, 20.],
                ..D
            },
        ] {
            assert!(confirmed()
                .update(&frame(t), t, &[P], Some(&[d]))
                .is_empty());
        }
    }
    #[test]
    fn using_sorted_ids_cannot_break_ties_or_invent_a_track() {
        let mut m = Labeler::default();
        for (frame_no, id) in [9, 27, 35, 9].into_iter().enumerate() {
            let t = T + SAMPLE_NS * frame_no as u64;
            assert!(m
                .update(&frame(t), t, &[RadarPoint { slot: id, ..P }], Some(&[D]))
                .is_empty());
        }
    }
    #[test]
    fn projection_compensates_longitudinal_velocity_only_to_capture() {
        let mut g = frame(T);
        g.projection = [[1., 0., 0., 0.], [0., 1., 0., 50.], [0., 0., 0., 1.]];
        let uv = g
            .project(RadarPoint { v_rel: -10., ..P }, T - 100_000_000)
            .unwrap();
        assert!((uv[0] - 10.52).abs() < 1e-8);
    }

    // Radar projects to (x, 60); bottom 77 gives a 42.5% vertical residual.
    fn moving(x: f64) -> (RadarPoint, Detection) {
        let p = RadarPoint {
            y_rel: (50. - x) * 0.1152,
            ..P
        };
        let d = Detection {
            bbox: [x - 6., 37., x + 6., 77.],
            ..D
        };
        (p, d)
    }

    #[test]
    fn vertical_match_requires_three_independent_coherent_samples() {
        let mut m = Labeler::default();
        for (i, (x, radar_ms, expected)) in [
            (50., 0, false),
            (51., 50, false),
            (51., 50, false),
            (52., 150, true),
        ]
        .into_iter()
        .enumerate()
        {
            let (p, d) = moving(x);
            let labels = m.update(
                &frame(T + i as u64 * 50_000_000),
                T + radar_ms * 1_000_000,
                &[p],
                Some(&[d]),
            );
            assert_eq!(!labels.is_empty(), expected);
            assert!(labels.iter().all(|label| label.motion_only));
        }
    }

    #[test]
    fn view_change_or_radar_reset_needs_three_new_motion_observations() {
        for geometry_changed in [true, false] {
            let mut m = Labeler::default();
            for i in 0..5 {
                let (p, d) = moving(50. + i as f64);
                let mut g = frame(T + i * 50_000_000);
                if geometry_changed && i >= 2 {
                    g.projection[0][0] += 0.25;
                } else if !geometry_changed && i == 2 {
                    m.reset_motion();
                }
                let labels = m.update(&g, g.capture_ns, &[p], Some(&[d]));
                assert_eq!(!labels.is_empty(), i == 4);
            }
        }
    }

    #[test]
    fn stationary_or_diverging_pixels_do_not_confirm_vertical_match() {
        for stationary in [true, false] {
            let mut m = Labeler::default();
            for i in 0..8 {
                let step = if stationary { 0. } else { i as f64 * 0.6 };
                let (p, _) = moving(50. + step);
                let (_, d) = moving(50. - step);
                let t = T + i * 50_000_000;
                assert!(m.update(&frame(t), t, &[p], Some(&[d])).is_empty());
            }
        }
    }

    #[test]
    fn ambiguity_gap_and_reused_slots_interrupt_motion_confirmation() {
        for fault in 0..7 {
            let mut m = Labeler::default();
            for i in 0..4 {
                let (mut p, d) = moving(50. + i as f64);
                let mut t = T + i * 50_000_000;
                if fault == 3 && i >= 2 {
                    t += 200_000_000;
                }
                if fault == 4 && i >= 2 {
                    p.slot += 1;
                }
                if fault == 5 && i == 2 {
                    p.v_rel += 9.;
                }
                let mut points = vec![p];
                let mut boxes = vec![d];
                if i == 2 {
                    match fault {
                        0 => points.push(RadarPoint { slot: 99, ..p }),
                        1 => boxes.push(Detection {
                            confidence: 0.3,
                            ..d
                        }),
                        2 => boxes.clear(),
                        6 => boxes[0].class_id = 2,
                        _ => (),
                    }
                }
                assert!(m.update(&frame(t), t, &points, Some(&boxes)).is_empty());
            }
        }
    }

    #[test]
    fn vertical_candidate_does_not_veto_or_claim_an_existing_strict_box() {
        let mut m = Labeler::default();
        for i in 0..6 {
            let (p, d) = moving(50. + i as f64);
            let g = frame(T + i * 50_000_000);
            let mut strict = p;
            strict.d_rel = 5.;
            strict.slot = 29;
            strict.y_rel = (50. - (50. + i as f64)) * (strict.d_rel + 1.52) / 100.;
            let labels = m.update(&g, g.capture_ns, &[p, strict], Some(&[d]));
            if i > 0 {
                assert_eq!(labels.iter().map(|l| l.slot).collect::<Vec<_>>(), [29]);
            }
        }
    }
}
