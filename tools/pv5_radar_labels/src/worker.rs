//! Associate received wide-camera detections with measured radar track IDs.
use crate::labels::{ClassLabel, Detection, FrameGeometry, RadarPoint};
use serde_json::{json, Value};
use std::collections::BTreeMap;

const HEADER_BYTES: usize = 36;
const DETECTION_BYTES: usize = 24;
const MAX_DETECTIONS: usize = 100;
const DATAGRAM_BYTES: usize = HEADER_BYTES + MAX_DETECTIONS * DETECTION_BYTES;
const MAX_PENDING: usize = 6;
const PAIR_MAX_NS: u64 = 250_000_000;
const HUD_BYTES: usize = 96 * 1024 + 8;

#[derive(Debug)]
struct Request {
    frame: u32,
    received: u64,
    fingerprint: String,
    detections: Vec<Detection>,
}

fn within(now: u64, stamp: u64, limit: u64) -> bool {
    stamp != 0 && now.checked_sub(stamp).is_some_and(|dt| dt <= limit)
}

fn request(data: &[u8], now: u64) -> Option<Request> {
    if !(HEADER_BYTES..=DATAGRAM_BYTES).contains(&data.len()) || &data[..4] != b"RLD2" {
        return None;
    }
    let frame = u32::from_le_bytes(data[4..8].try_into().ok()?);
    let received = u64::from_le_bytes(data[8..16].try_into().ok()?);
    let count = u32::from_le_bytes(data[32..36].try_into().ok()?) as usize;
    if count > MAX_DETECTIONS
        || data.len() != HEADER_BYTES + count * DETECTION_BYTES
        || !within(now, received, PAIR_MAX_NS)
    {
        return None;
    }
    use std::fmt::Write;
    let mut fingerprint = String::with_capacity(32);
    for byte in &data[16..32] {
        write!(&mut fingerprint, "{byte:02x}").ok()?;
    }
    let mut detections = Vec::with_capacity(count);
    for item in data[HEADER_BYTES..].chunks_exact(DETECTION_BYTES) {
        let class_id = u8::try_from(u32::from_le_bytes(item[..4].try_into().ok()?)).ok()?;
        let values: [f64; 5] = std::array::from_fn(|i| {
            f64::from(f32::from_le_bytes(
                item[4 + 4 * i..8 + 4 * i].try_into().unwrap(),
            ))
        });
        if !values.iter().all(|v| v.is_finite()) || !(0.0..=1.0).contains(&values[0]) {
            return None;
        }
        detections.push(Detection {
            class_id,
            confidence: values[0],
            bbox: values[1..].try_into().ok()?,
        });
    }
    Some(Request {
        frame,
        received,
        fingerprint,
        detections,
    })
}

struct Metadata {
    received: u64,
    sent: u64,
    markers: Vec<Value>,
}

struct PairedFrame {
    geometry: FrameGeometry,
    radar_ns: u64,
    radar_epoch: Option<u64>,
    points: Vec<RadarPoint>,
    host_capture: u64,
}

fn seconds_to_ns(seconds: f64) -> Option<u64> {
    (seconds.is_finite() && seconds > 0.0 && seconds < u64::MAX as f64 / 1e9)
        .then(|| (seconds * 1e9).round() as u64)
}

impl Metadata {
    fn read(bytes: &[u8], now: u64) -> Option<Self> {
        if !(9..=HUD_BYTES).contains(&bytes.len()) {
            return None;
        }
        let received = seconds_to_ns(f64::from_le_bytes(bytes[..8].try_into().ok()?))?;
        if !within(now, received, PAIR_MAX_NS) {
            return None;
        }
        let mut value: Value = serde_json::from_slice(&bytes[8..]).ok()?;
        let sent = seconds_to_ns(value["sent"].as_f64()?)?;
        let Value::Array(markers) = value["radar_labels"].take() else {
            return None;
        };
        (markers.len() <= MAX_PENDING).then_some(Self {
            received,
            sent,
            markers,
        })
    }

    fn pair(&self, r: &Request, now: u64) -> Option<PairedFrame> {
        if !within(now, r.received, PAIR_MAX_NS)
            || !within(now, self.received, PAIR_MAX_NS)
            || self.received < r.received.saturating_sub(10_000_000)
        {
            return None;
        }
        let mut matching = self.markers.iter().filter(|m| {
            m["lens"] == "wide"
                && m["frame"].as_u64() == Some(u64::from(r.frame))
                && m["fingerprint"].as_str() == Some(r.fingerprint.as_str())
        });
        let m = matching.next()?;
        if matching.next().is_some() {
            return None;
        }
        let rows = m["projection"].as_array()?;
        if rows.len() != 3 {
            return None;
        }
        let mut projection = [[0.; 4]; 3];
        for (dst, src) in projection.iter_mut().zip(rows) {
            let row = src.as_array()?;
            if row.len() != 4 {
                return None;
            }
            for (x, v) in dst.iter_mut().zip(row) {
                *x = v.as_f64()?;
            }
        }
        let src = m["points"].as_array()?;
        if src.len() > 128 {
            return None;
        }
        let mut points = Vec::with_capacity(src.len());
        for p in src {
            if p.as_array()?.len() != 4 {
                return None;
            }
            points.push(RadarPoint {
                slot: u32::try_from(p[0].as_u64()?).ok()?,
                d_rel: p[1].as_f64()?,
                y_rel: p[2].as_f64()?,
                v_rel: p[3].as_f64()?,
            });
        }
        let geometry = FrameGeometry {
            capture_ns: m["capture_ns"].as_u64()?,
            calibration_ns: m["calibration_ns"].as_u64()?,
            projection,
            image_width: 512,
            image_height: 256,
        };
        // Compare intervals on their originating clocks. The source-side
        // sampling delay consumes part of the evidence lifetime.
        let source_age = self.sent.checked_sub(geometry.capture_ns)?;
        let host_capture = r.received.min(self.received.checked_sub(source_age)?);
        within(now, host_capture, PAIR_MAX_NS).then_some(PairedFrame {
            geometry,
            radar_ns: m["radar_ns"].as_u64()?,
            radar_epoch: m["radar_epoch"].as_u64().filter(|&epoch| epoch > 0),
            points,
            host_capture,
        })
    }
}

#[derive(Default)]
struct Publisher {
    expiry: BTreeMap<u32, (u64, u64)>,
}

impl Publisher {
    fn output(
        &mut self,
        frame: u32,
        capture: u64,
        radar_ns: u64,
        capture_host: u64,
        now: u64,
        labels: &[ClassLabel],
    ) -> Value {
        let mut next = BTreeMap::new();
        let items: Vec<_> = labels
            .iter()
            .filter_map(|l| {
                let remaining = l.expires_ns.checked_sub(capture)?;
                let translated = capture_host.checked_add(remaining)?;
                // Multiple wide frames may use the same radar evidence. Clock
                // translation jitter must never extend an existing expiry.
                let expires = self
                    .expiry
                    .get(&l.slot)
                    .filter(|&&(evidence, _)| evidence == l.evidence_ns)
                    .map_or(translated, |&(_, previous)| previous.min(translated));
                next.insert(l.slot, (l.evidence_ns, expires));
                (expires > now).then(|| {
                    let mut row = json!({"track_id":l.slot,"class_id":l.class_id,
                           "confidence":l.confidence,"expires_monotonic_ns":expires,
                           "point":[l.point.d_rel,l.point.y_rel,l.point.v_rel]});
                    if l.motion_only {
                        row["motion_only"] = json!(true);
                    }
                    row
                })
            })
            .collect();
        self.expiry = next;
        json!({"version":1,"lens":"wide","frame":frame,"capture_ns":capture,"radar_ns":radar_ns,
               "published_monotonic_ns":now,"labels":items})
    }
}

#[cfg(target_os = "linux")]
mod linux {
    use super::*;
    use crate::labels::Labeler;
    use std::{
        collections::VecDeque,
        error::Error,
        fs::{self, File, OpenOptions},
        io::{self, Read, Write},
        os::{
            fd::AsRawFd,
            unix::{
                fs::{DirBuilderExt, FileTypeExt, MetadataExt, OpenOptionsExt},
                net::UnixDatagram,
            },
        },
        path::Path,
        time::Duration,
    };

    type Result<T> = std::result::Result<T, Box<dyn Error>>;
    #[repr(C)]
    struct Timespec {
        sec: std::ffi::c_long,
        ns: std::ffi::c_long,
    }
    unsafe extern "C" {
        fn clock_gettime(id: i32, ts: *mut Timespec) -> i32;
        fn geteuid() -> u32;
        fn flock(fd: i32, op: i32) -> i32;
    }
    fn clock() -> Result<u64> {
        let mut t = Timespec { sec: 0, ns: 0 };
        if unsafe { clock_gettime(1, &mut t) } != 0 {
            return Err(io::Error::last_os_error().into());
        }
        Ok(t.sec as u64 * 1_000_000_000 + t.ns as u64)
    }

    fn socket(dir: &Path) -> Result<(File, UnixDatagram)> {
        match fs::DirBuilder::new().mode(0o700).create(dir) {
            Ok(()) => (),
            Err(e) if e.kind() == io::ErrorKind::AlreadyExists => (),
            Err(e) => return Err(e.into()),
        }
        let info = fs::symlink_metadata(dir)?;
        if !info.is_dir() || info.uid() != unsafe { geteuid() } || info.mode() & 0o077 != 0 {
            return Err("worker directory must be private and owned by the worker".into());
        }
        let lock = OpenOptions::new()
            .read(true)
            .write(true)
            .create(true)
            .truncate(false)
            .mode(0o600)
            .open(dir.join("worker.lock"))?;
        // LOCK_EX | LOCK_NB serializes stale-socket removal after a crash.
        if unsafe { flock(lock.as_raw_fd(), 2 | 4) } != 0 {
            return Err(io::Error::last_os_error().into());
        }
        let path = dir.join("detections.sock");
        match fs::symlink_metadata(&path) {
            Ok(m) if m.file_type().is_socket() => fs::remove_file(&path)?,
            Ok(_) => return Err("worker socket path occupied by a non-socket".into()),
            Err(e) if e.kind() == io::ErrorKind::NotFound => (),
            Err(e) => return Err(e.into()),
        }
        Ok((lock, UnixDatagram::bind(path)?))
    }

    fn save(dir: &Path, value: &Value) -> Result<()> {
        let temp = dir.join("labels.tmp");
        let mut f = OpenOptions::new()
            .write(true)
            .create(true)
            .truncate(true)
            .mode(0o600)
            .open(&temp)?;
        serde_json::to_writer(&mut f, value)?;
        f.flush()?;
        fs::rename(temp, dir.join("labels.json"))?;
        Ok(())
    }

    fn read_packet(
        path: &Path,
        buf: &mut Vec<u8>,
        previous: &mut Option<(u64, i64, i64)>,
    ) -> Option<()> {
        let f = File::open(path).ok()?;
        let stat = f.metadata().ok()?;
        let signature = (stat.ino(), stat.ctime(), stat.ctime_nsec());
        if !stat.is_file() || stat.len() > HUD_BYTES as u64 || *previous == Some(signature) {
            return None;
        }
        *previous = Some(signature);
        buf.clear();
        f.take((HUD_BYTES + 1) as u64).read_to_end(buf).ok()?;
        (buf.len() <= HUD_BYTES).then_some(())
    }

    pub fn run(dir: &Path, hud: &Path) -> Result<()> {
        let (_lock, rx) = socket(dir)?;
        let mut buf = [0; DATAGRAM_BYTES + 1];
        let mut packet = Vec::new();
        let mut metadata = None;
        let mut previous_packet = None;
        let mut labeler = Labeler::default();
        let mut publisher = Publisher::default();
        let mut pending = VecDeque::<Request>::with_capacity(MAX_PENDING);
        let mut latest: Option<(u32, u64)> = None;
        let mut last_paired: Option<(u32, u64)> = None;
        let mut last_radar_epoch: Option<u64> = None;
        let poll = Some(Duration::from_millis(5));
        save(dir, &publisher.output(0, 0, 0, 0, clock()?, &[]))?;
        loop {
            // Block when idle. While a request waits for HUD metadata, check
            // its updated packet and receive any newer detections each tick.
            rx.set_read_timeout(if pending.is_empty() { None } else { poll })?;
            match rx.recv(&mut buf) {
                Ok(n) => {
                    let now = clock()?;
                    if let Some(r) = request(&buf[..n], now) {
                        if latest.is_some_and(|(_, stamp)| r.received <= stamp) {
                            continue;
                        }
                        if latest.is_some_and(|(frame, _)| r.frame <= frame) {
                            pending.clear();
                            labeler = Labeler::default();
                            last_paired = None;
                            metadata = None;
                            save(dir, &publisher.output(r.frame, 0, 0, 0, now, &[]))?;
                        }
                        latest = Some((r.frame, r.received));
                        if pending.len() == MAX_PENDING {
                            pending.pop_front();
                        }
                        pending.push_back(r);
                    }
                }
                Err(e)
                    if matches!(
                        e.kind(),
                        io::ErrorKind::TimedOut
                            | io::ErrorKind::WouldBlock
                            | io::ErrorKind::Interrupted
                    ) =>
                {
                    ()
                }
                Err(e) => return Err(e.into()),
            }
            let now = clock()?;
            while pending
                .front()
                .is_some_and(|r| !within(now, r.received, PAIR_MAX_NS))
            {
                let frame = pending.pop_front().unwrap().frame;
                labeler = Labeler::default();
                last_paired = None;
                save(dir, &publisher.output(frame, 0, 0, 0, now, &[]))?;
            }
            if pending.is_empty() {
                continue;
            }
            if read_packet(hud, &mut packet, &mut previous_packet).is_some() {
                metadata = Metadata::read(&packet, clock()?);
            }
            let Some(metadata) = &metadata else { continue };
            let now = clock()?;
            let mut newest = None;
            for (i, r) in pending.iter().enumerate() {
                let Some(PairedFrame {
                    geometry: g,
                    radar_ns,
                    radar_epoch,
                    points,
                    host_capture,
                }) = metadata.pair(r, now)
                else {
                    continue;
                };
                // The auxiliary detector drops busy frames by design. One skipped
                // offer can still have two fresh, independently matched radar samples.
                let continues = last_paired.is_some_and(|(frame, capture)| {
                    match r.frame.checked_sub(frame) {
                        Some(1) => true,
                        Some(2) => g.capture_ns.checked_sub(capture)
                            .is_some_and(|dt| (1..=150_000_000).contains(&dt)),
                        _ => false,
                    }
                });
                if !continues {
                    labeler = Labeler::default();
                    publisher = Publisher::default();
                }
                // A reset in the comma's radar history may fall between two
                // matched camera frames. Old senders only support spatial evidence.
                if radar_epoch.is_none() || radar_epoch != last_radar_epoch {
                    labeler.reset_motion();
                }
                last_radar_epoch = radar_epoch;
                let labels = labeler.update(&g, radar_ns, &points, Some(&r.detections));
                last_paired = Some((r.frame, g.capture_ns));
                newest = Some((
                    i,
                    publisher.output(r.frame, g.capture_ns, radar_ns, host_capture, now, &labels),
                ));
            }
            if let Some((i, value)) = newest {
                // Any unmatched earlier requests can no longer be published.
                pending.drain(..=i);
                save(dir, &value)?;
            }
        }
    }

    #[cfg(test)]
    mod tests {
        use super::*;

        fn wait_for_output(dir: &Path, frame: u32) -> Value {
            let start = std::time::Instant::now();
            while start.elapsed() < Duration::from_secs(3) {
                if let Ok(data) = fs::read(dir.join("labels.json")) {
                    let v: Value = serde_json::from_slice(&data).expect("atomic complete JSON");
                    if v["frame"] == frame {
                        return v;
                    }
                }
                std::thread::sleep(Duration::from_millis(3));
            }
            panic!("no output for frame {frame}");
        }

        #[test]
        fn datagrams_wait_for_independent_wide_evidence_in_one_hud_snapshot() {
            verify_frame_pair(2, true);
        }

        #[test]
        fn one_missed_detector_offer_preserves_spatial_confirmation() {
            verify_frame_pair(3, true);
            verify_frame_pair(4, false);
        }

        #[test]
        fn motion_confirmation_restarts_at_radar_epoch_change_and_requires_epoch() {
            let dir = std::env::temp_dir().join(format!(
                "rl-{}-{}",
                std::process::id(),
                clock().unwrap()
            ));
            let db = dir.clone();
            std::thread::spawn(move || run(&db, &db.join("hud.packet")).unwrap());
            wait_for_output(&dir, 0);
            let sender = UnixDatagram::unbound().unwrap();
            for (frame, epoch, associated) in [
                (1, Some(10), false),
                (3, Some(20), false),
                (4, Some(20), false),
                (5, Some(20), true),
                (6, None, false),
                (7, None, false),
                (8, None, false),
            ] {
                let now = clock().unwrap();
                let offset = frame as f32;
                let mut data = crate::worker::tests::datagram(frame, now);
                data[44..48].copy_from_slice(&(44. + offset).to_le_bytes());
                data[52..56].copy_from_slice(&(56. + offset).to_le_bytes());
                sender.send_to(&data, dir.join("detections.sock")).unwrap();
                let mut marker = crate::worker::tests::marker(frame, now - 8_000_000, "wide");
                marker["projection"] = json!([
                    [0., 8., 0., 50.],
                    [0., 0., 0., 42.],
                    [0., 0., 0., 1.]
                ]);
                marker["points"] = json!([[56, 10., offset / 8., 0.]]);
                if let Some(epoch) = epoch {
                    marker["radar_epoch"] = json!(epoch);
                }
                let now = clock().unwrap();
                let bytes = crate::worker::tests::packet(now, now, vec![marker]);
                fs::write(dir.join("packet.tmp"), bytes).unwrap();
                fs::rename(dir.join("packet.tmp"), dir.join("hud.packet")).unwrap();
                let value = wait_for_output(&dir, frame);
                assert!(value["radar_ns"].as_u64().unwrap() > 0, "frame {frame}");
                if associated {
                    assert_eq!(value["labels"][0]["track_id"], 56, "frame {frame}");
                    assert_eq!(value["labels"][0]["motion_only"], true, "frame {frame}");
                } else {
                    assert_eq!(value["labels"], json!([]), "frame {frame}");
                }
            }
            fs::remove_dir_all(dir).unwrap();
        }

        fn verify_frame_pair(second: u32, associated: bool) {
            let dir = std::env::temp_dir().join(format!(
                "rl-{}-{}",
                std::process::id(),
                clock().unwrap()
            ));
            let db = dir.clone();
            std::thread::spawn(move || run(&db, &db.join("hud.packet")).unwrap());
            wait_for_output(&dir, 0);
            let sender = UnixDatagram::unbound().unwrap();
            let mut markers = vec![];
            for frame in [1, second] {
                let now = clock().unwrap();
                sender
                    .send_to(
                        &crate::worker::tests::datagram(frame, now),
                        dir.join("detections.sock"),
                    )
                    .unwrap();
                markers.push(crate::worker::tests::marker(frame, now - 8_000_000, "wide"));
                std::thread::sleep(Duration::from_millis(10));
            }
            let now = clock().unwrap();
            let bytes = crate::worker::tests::packet(now, now, markers);
            fs::write(dir.join("packet.tmp"), bytes).unwrap();
            fs::rename(dir.join("packet.tmp"), dir.join("hud.packet")).unwrap();
            let value = wait_for_output(&dir, second);
            assert_eq!(value["lens"], "wide");
            if associated {
                assert_eq!(value["labels"][0]["track_id"], 56);
                assert_eq!(value["labels"][0]["class_id"], 2);
                assert_eq!(value["labels"][0]["point"], json!([10.0, 0.0, 0.0]));
            } else {
                assert_eq!(value["labels"], json!([]));
            }
            assert!(value["radar_ns"].as_u64().unwrap() > 0);
            fs::remove_dir_all(dir).unwrap();
        }
    }
}

#[cfg(target_os = "linux")]
pub use linux::run;

#[cfg(test)]
mod tests {
    use super::*;
    const NOW: u64 = 1_000_000_000;

    pub(super) fn datagram(frame: u32, received: u64) -> Vec<u8> {
        let mut data = b"RLD2".to_vec();
        data.extend(frame.to_le_bytes());
        data.extend(received.to_le_bytes());
        data.extend([0x31; 16]);
        data.extend(1u32.to_le_bytes());
        data.extend(2u32.to_le_bytes());
        for x in [0.8f32, 44., 20., 56., 60.] {
            data.extend(x.to_le_bytes());
        }
        data
    }

    pub(super) fn marker(frame: u32, capture: u64, lens: &str) -> Value {
        json!({"lens":lens,"frame":frame,"capture_ns":capture,"fingerprint":"31".repeat(16),
               "calibration_ns":capture-1,"radar_ns":capture-1,
               "projection":[[50.,-100.,0.,0.],[50.,0.,0.,115.2],[1.,0.,0.,0.]],
               "points":[[56,10.,0.,0.]]})
    }

    pub(super) fn packet(received: u64, sent: u64, markers: Vec<Value>) -> Vec<u8> {
        let mut data = (received as f64 / 1e9).to_le_bytes().to_vec();
        data.extend(
            serde_json::to_vec(&json!({"sent":sent as f64/1e9,"radar_labels":markers})).unwrap(),
        );
        data
    }

    #[test]
    fn datagram_requires_exact_finite_fresh_payload() {
        let data = datagram(5, NOW);
        let r = request(&data, NOW + 1).unwrap();
        assert_eq!(r.frame, 5);
        assert_eq!(r.fingerprint, "31".repeat(16));
        assert_eq!(r.detections[0].bbox, [44., 20., 56., 60.]);
        assert!(request(&data[..data.len() - 1], NOW).is_none());
        assert!(request(&data, NOW - 1).is_none());
        assert!(request(&data, NOW + PAIR_MAX_NS + 1).is_none());
        let mut invalid = data;
        invalid[40..44].copy_from_slice(&f32::NAN.to_le_bytes());
        assert!(request(&invalid, NOW).is_none());
    }

    #[test]
    fn only_unique_fresh_wide_frame_and_fingerprint_pair() {
        let r = request(&datagram(5, NOW), NOW).unwrap();
        let c = NOW - 10_000_000;
        let wide = marker(5, c, "wide");
        let narrow = marker(5, c, "road");
        let different_frame = marker(6, c, "wide");
        let received = NOW + 1;
        let mut wrong_sign = wide.clone();
        wrong_sign["fingerprint"] = json!("00".repeat(16));
        for markers in [
            vec![narrow.clone()],
            vec![different_frame],
            vec![wrong_sign],
            vec![wide.clone(), wide.clone()],
        ] {
            let m = Metadata::read(&packet(received, NOW, markers), received).unwrap();
            assert!(m.pair(&r, received).is_none());
        }
        let m = Metadata::read(&packet(received, NOW, vec![narrow, wide]), received).unwrap();
        let pair = m.pair(&r, received).unwrap();
        assert_eq!(
            (
                pair.geometry.capture_ns,
                pair.radar_ns,
                pair.points[0].slot,
                pair.host_capture,
                pair.radar_epoch
            ),
            (c, c - 1, 56, c + 1, None)
        );
        assert!(m.pair(&r, NOW + PAIR_MAX_NS + 1).is_none());
    }

    #[test]
    fn repeated_radar_evidence_cannot_extend_host_expiry() {
        let label = ClassLabel {
            slot: 56,
            motion_only: false,
            class_id: 2,
            confidence: 0.8,
            bbox: [0.; 4],
            point: RadarPoint {
                slot: 56,
                d_rel: 10.0,
                y_rel: 0.0,
                v_rel: 0.0,
            },
            evidence_ns: NOW,
            expires_ns: NOW + 300,
        };
        let mut p = Publisher::default();
        let a = p.output(1, NOW, NOW, NOW, NOW, &[label]);
        let b = p.output(2, NOW + 30, NOW, NOW + 80, NOW + 95, &[label]);
        assert_eq!(a["labels"][0]["expires_monotonic_ns"], NOW + 300);
        assert_eq!(
            a["labels"][0]["expires_monotonic_ns"],
            b["labels"][0]["expires_monotonic_ns"]
        );
        let c = p.output(3, NOW + 30, NOW, NOW + 100, NOW + 301, &[label]);
        assert_eq!(c["labels"], json!([]));
    }
}
