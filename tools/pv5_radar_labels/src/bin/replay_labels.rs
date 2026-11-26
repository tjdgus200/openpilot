//! Replay recorded camera detections and measured radar rows (no fusion IDs).
use pv5_radar_labels::labels::{ClassLabel, Detection, FrameGeometry, Labeler, RadarPoint};
use serde_json::{json, Value};
use std::{
    collections::BTreeMap,
    env,
    error::Error,
    fs::File,
    io::{self, BufRead, BufReader, BufWriter, Write},
    path::Path,
};

type Result<T> = std::result::Result<T, Box<dyn Error>>;

#[derive(Default)]
struct ReplayWorker {
    labeler: Labeler,
    published: BTreeMap<u32, ClassLabel>,
}

impl ReplayWorker {
    fn sample(
        &mut self,
        g: &FrameGeometry,
        radar_ns: u64,
        points: &[RadarPoint],
        ds: &[Detection],
    ) {
        self.published = self
            .labeler
            .update(g, radar_ns, points, Some(ds))
            .into_iter()
            .map(|l| (l.slot, l))
            .collect();
    }
    fn displayed(&self, slot: u32, now: u64) -> Option<&ClassLabel> {
        self.published.get(&slot).filter(|l| now < l.expires_ns)
    }
}

fn load(path: &Path) -> Result<Value> {
    Ok(serde_json::from_reader(BufReader::new(File::open(path)?))?)
}

fn matrix(v: &Value) -> Result<[[f64; 4]; 3]> {
    let mut m = [[0.; 4]; 3];
    for (i, row) in m.iter_mut().enumerate() {
        for (j, x) in row.iter_mut().enumerate() {
            *x = v[i][j].as_f64().ok_or("invalid projection")?;
        }
    }
    Ok(m)
}

fn capture(frame: &Value, wide: bool) -> Result<u64> {
    if !wide {
        return frame["timestamp_mid_ns"]
            .as_u64()
            .ok_or("missing capture time".into());
    }
    let wide = &frame["wide"];
    let sof = wide["timestamp_sof_ns"]
        .as_u64()
        .ok_or("missing wide SOF")?;
    let eof = wide["timestamp_eof_ns"]
        .as_u64()
        .ok_or("missing wide EOF")?;
    if sof == 0 || eof < sof {
        return Err("invalid wide capture time".into());
    }
    Ok(sof + (eof - sof) / 2)
}

fn main() -> Result<()> {
    let args: Vec<_> = env::args().collect();
    if !(3..=5).contains(&args.len()) {
        return Err(
            "usage: replay_labels AUDIT_DIR SCENE [road|road_warp|wide_warp [DETECTIONS_NDJSON]]"
                .into(),
        );
    }
    let scene: u32 = args[2].parse()?;
    let view = args.get(3).map_or("road", String::as_str);
    if !matches!(view, "road" | "road_warp" | "wide_warp") {
        return Err("unknown view".into());
    }
    let wide = view == "wide_warp";
    let base = Path::new(&args[1]);
    // Additional moving-pedestrian clips store all inputs side-by-side.
    let inputs = if base.join("yolo_three").is_dir() {
        base.join("yolo_three")
    } else {
        base.to_owned()
    };
    let record = load(&base.join(format!("scene{scene}-replay.private.json")))?;
    let tracks = record["tracks"].as_array().ok_or("missing radar rows")?;
    let cal = record["cal"].as_array();
    if cal.is_none() && inputs != base {
        return Err("missing calibration rows".into());
    }
    // The archived road-warp metadata already contains each wide exposure
    // timestamp and wide ground projection. The detection stream is separate.
    let geometry_view = if wide { "road_warp" } else { view };
    let meta = load(&inputs.join(format!("{scene}-{geometry_view}.private.json")))?;
    let mut frames = BTreeMap::new();
    for frame in meta["frames"].as_array().ok_or("missing frames")? {
        let id = frame["source_frame"]
            .as_u64()
            .ok_or("missing frame number")?;
        if frames.insert(id, frame).is_some() {
            return Err("duplicate metadata".into());
        }
    }
    let default_detections = inputs.join(format!("{scene}-{view}-detections.private.ndjson"));
    let detector = File::open(args.get(4).map(Path::new).unwrap_or(&default_detections))?;
    let mut out = BufWriter::new(io::stdout().lock());
    let mut worker = ReplayWorker::default();
    // Retain the historical quarter-rate replay for the two road views.
    // The wide worker attempts every supplied frame.
    let first_frame = frames.first_key_value().ok_or("no camera frames")?.0;
    let sample_phase = (!wide).then_some(first_frame % 4);
    let mut previous_capture = 0;
    for line in BufReader::new(detector).lines() {
        let mut d: Value = serde_json::from_str(&line?)?;
        // replay-detector emits exactly one array per input frame, in order,
        // and exits nonzero on partial input. Only use ordinal pairing on an
        // explicitly supplied, full-stream dump of the requested packed view.
        if d.is_array() && args.len() == 5 && matches!(view, "road_warp" | "wide_warp") {
            let (source_frame, frame) = frames.first_key_value().ok_or("extra detector row")?;
            d = json!({"source_frame":source_frame,"timestamp_mid_ns":capture(frame, wide)?,
                "scene":scene,"stream":view,"width":512,"height":256,"detections":d});
        }
        let n = d["source_frame"].as_u64().ok_or("missing frame number")?;
        let frame = frames.remove(&n).ok_or("unmatched camera frame")?;
        let now = capture(frame, wide)?;
        if d["timestamp_mid_ns"].as_u64() != Some(now)
            || now <= previous_capture
            || d["scene"].as_u64() != Some(u64::from(scene))
            || d["stream"] != view
        {
            return Err("capture identity mismatch".into());
        }
        previous_capture = now;
        let calibration_ns = frame["calibration_log_ns"]
            .as_u64()
            .ok_or("calibration timestamp")?;
        let calibration_ok = if let Some(cal) = cal {
            let cal_ix =
                cal.partition_point(|c| c[0].as_u64().is_some_and(|t| t <= calibration_ns));
            cal_ix.checked_sub(1).is_some_and(|j| {
                cal[j][0].as_u64() == Some(calibration_ns) && cal[j][1] == "calibrated"
            })
        } else {
            // Archived flat clips were produced by pipeline/run_clip.py, which
            // rejects any source state other than "calibrated" before writing
            // per-frame geometry. These clips omit the duplicate state rows.
            frame["rpy_rad"].as_array().is_some_and(|v| {
                v.len() == 3 && v.iter().all(|x| x.as_f64().is_some_and(f64::is_finite))
            }) && frame["camera_height_m"]
                .as_f64()
                .is_some_and(|h| h.is_finite() && h > 0.)
        };
        let width = u32::try_from(d["width"].as_u64().ok_or("width")?)?;
        let height = u32::try_from(d["height"].as_u64().ok_or("height")?)?;
        if meta["view_size_px"] != json!([width, height]) {
            return Err("view dimensions".into());
        }
        let projection = if view == "road" {
            "ground_to_source_3x4"
        } else {
            "ground_to_model_3x4"
        };
        let geometry = FrameGeometry {
            capture_ns: now,
            calibration_ns: if calibration_ok { calibration_ns } else { 0 },
            projection: matrix(if wide {
                &frame["wide"][projection]
            } else {
                &frame[projection]
            })?,
            image_width: width,
            image_height: height,
        };
        let radar_ix = tracks.partition_point(|r| r[0].as_u64().is_some_and(|t| t <= now));
        let mut points = vec![];
        let mut radar_ns = 0;
        if let Some(ix) = radar_ix.checked_sub(1) {
            let row = &tracks[ix];
            radar_ns = row[0].as_u64().ok_or("missing radar timestamp")?;
            if row[1] == true && row[2] == false {
                for p in row[3].as_array().ok_or("radar points")? {
                    if p[1] == "frontRadar" && p[2] == true {
                        points.push(RadarPoint {
                            slot: u32::try_from(p[0].as_u64().ok_or("radar ID")?)?,
                            d_rel: p[3].as_f64().ok_or("range")?,
                            y_rel: p[4].as_f64().ok_or("lateral")?,
                            v_rel: p[5].as_f64().ok_or("relative velocity")?,
                        });
                    }
                }
            }
        }
        let sampled = sample_phase.is_none_or(|phase| n % 4 == phase);
        let mut dets = vec![];
        if sampled {
            for det in d["detections"].as_array().ok_or("missing detections")? {
                dets.push(Detection {
                    class_id: u8::try_from(det["class_id"].as_u64().ok_or("class")?)?,
                    confidence: det["confidence"].as_f64().ok_or("confidence")?,
                    bbox: [
                        det["x1"].as_f64().ok_or("x1")?,
                        det["y1"].as_f64().ok_or("y1")?,
                        det["x2"].as_f64().ok_or("x2")?,
                        det["y2"].as_f64().ok_or("y2")?,
                    ],
                });
            }
            worker.sample(&geometry, radar_ns, &points, &dets);
        }
        let results: Vec<_> = points.iter().map(|p| {
            let l = worker.displayed(p.slot,now);
            let mut result = json!({"slot":p.slot,"d_rel":p.d_rel,"y_rel":p.y_rel,"class_id":l.map(|l|l.class_id),
                "label_bbox":l.map(|l|l.bbox),"confidence":l.map(|l|l.confidence),
                "evidence_ns":l.map(|l|l.evidence_ns),"expires_ns":l.map(|l|l.expires_ns)});
            if wide {
                result["point"] = json!(l.map(|l| [l.point.d_rel, l.point.y_rel, l.point.v_rel]));
            }
            result
        }).collect();
        serde_json::to_writer(
            &mut out,
            &json!({"scene":scene,"stream":view,"source_frame":n,
            "timestamp_mid_ns":now,"radar_ns":radar_ns,"sampled":sampled,
            "source_frame_mod4_phase":sample_phase,
            "calibration_source":if cal.is_some() {"recorded_state"} else {"archived_clip_geometry"},
            "tracks":results}),
        )?;
        writeln!(out)?;
    }
    if !frames.is_empty() {
        return Err("camera detection records missing".into());
    }
    out.flush()?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn unobserved_radar_reuse_cannot_reset_or_extend_worker_snapshot() {
        let mut w = ReplayWorker::default();
        let p = RadarPoint {
            slot: 56,
            d_rel: 10.0,
            y_rel: 0.0,
            v_rel: 0.0,
        };
        let d = Detection {
            class_id: 2,
            confidence: 0.8,
            bbox: [44., 20., 56., 60.],
        };
        let mut g = FrameGeometry {
            capture_ns: 1_000_000_000,
            calibration_ns: 900_000_000,
            projection: [[50., -100., 0., 0.], [50., 0., 0., 115.2], [1., 0., 0., 0.]],
            image_width: 100,
            image_height: 100,
        };
        w.sample(&g, g.capture_ns, &[p], &[d]);
        g.capture_ns += 200_000_000;
        w.sample(&g, g.capture_ns, &[p], &[d]);
        assert!(w.displayed(90, g.capture_ns + 50_000_000).is_none());
        let seen = w.displayed(56, g.capture_ns + 100_000_000).unwrap();
        assert_eq!(seen.evidence_ns, g.capture_ns);
        assert!(w.displayed(56, seen.expires_ns).is_none());
        g.capture_ns += 200_000_000;
        w.sample(&g, g.capture_ns, &[], &[]);
        assert!(w.displayed(56, g.capture_ns).is_none());
    }
}
