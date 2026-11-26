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
}

#[derive(Default)]
pub struct Labeler {
    tracks: BTreeMap<u32, History>,
    last_capture: Option<u64>,
    last_radar: Option<u64>,
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

// Some(true) can match; Some(false) can only veto a nearby rival. Giving a
// competing point a modest localization margin avoids hard-boundary ID swaps.
fn candidate(d: Detection, point: [f64; 2], g: &FrameGeometry) -> Option<bool> {
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
    let dy = ((point[1] - y2) / h).abs();
    (dx <= 0.60 && dy <= 0.384).then_some(dx <= 0.50 && dy <= 0.32)
}

fn same_box_region(a: [f64; 4], b: [f64; 4]) -> bool {
    let iw = (a[2].min(b[2]) - a[0].max(b[0])).max(0.0);
    let ih = (a[3].min(b[3]) - a[1].max(b[1])).max(0.0);
    let intersection = iw * ih;
    let union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - intersection;
    intersection >= 0.20 * union
}

impl Labeler {
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
        for &p in points.iter().filter(|p| plausible(**p)) {
            if let Some(uv) = g.project(p, radar_ns) {
                for (j, d) in dets.iter().enumerate() {
                    if let Some(can_match) = candidate(*d, uv, g) {
                        let degree = hits.entry(p.slot).or_insert((j, 0, can_match));
                        degree.1 += 1;
                        det_degrees[j] += 1;
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
            };
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
            if let Some(label) = h.evidence.filter(|_| h.confirmed) {
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
        assert_eq!(candidate(D, [56.6, 59.0], &g), Some(false));
        assert_eq!(candidate(D, [55.9, 59.0], &g), Some(true));
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
}
