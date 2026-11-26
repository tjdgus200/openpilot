//! YOLOv5n v7 FP16 on the Jetson. Create and use a Detector on its dedicated worker thread.
#[cfg(feature = "tensorrt")]
use std::{
    ffi::{c_char, c_int, c_void, CStr},
    fs::File,
    io::Read,
    path::Path,
    ptr::NonNull,
};

const SIDE: u32 = 640;
#[cfg(feature = "tensorrt")]
const CAPACITY: usize = 2048;
#[cfg(feature = "tensorrt")]
const MAX_SOURCE: usize = 32 << 20;
#[cfg(feature = "tensorrt")]
const MAX_PLAN: u64 = 128 << 20;

#[derive(Debug, Clone, Copy)]
pub struct Detection {
    pub class_id: u8,
    pub confidence: f32,
    pub xyxy: [f32; 4],
}

#[repr(C)]
#[derive(Clone, Copy, Default)]
struct Candidate {
    xyxy: [f32; 4],
    confidence: f32,
    class_id: i32,
    anchor: i32,
}

#[cfg(feature = "tensorrt")]
unsafe extern "C" {
    fn pv5_yolo_open(
        plan: *const c_void,
        length: usize,
        capacity: usize,
        error: *mut c_char,
        size: usize,
    ) -> *mut c_void;
    fn pv5_yolo_close(state: *mut c_void);
    fn pv5_yolo_detect(
        state: *mut c_void,
        pixels: *const u8,
        length: usize,
        width: c_int,
        height: c_int,
        stride: c_int,
        six: c_int,
        rw: c_int,
        rh: c_int,
        rows: *mut Candidate,
        error: *mut c_char,
        size: usize,
    ) -> c_int;
}

fn overlap(a: &[f32; 4], b: &[f32; 4]) -> f32 {
    let i = (a[2].min(b[2]) - a[0].max(b[0])).max(0.) * (a[3].min(b[3]) - a[1].max(b[1])).max(0.);
    let area = |v: &[f32; 4]| (v[2] - v[0]) * (v[3] - v[1]);
    i / (area(a) + area(b) - i)
}

fn content_shape(width: u32, height: u32) -> [u32; 2] {
    let scale = SIDE as f64 / width.max(height) as f64;
    [width, height].map(|n| (n as f64 * scale).round().max(1.) as u32)
}

fn postprocess(
    rows: &mut [Candidate],
    width: u32,
    height: u32,
    [rw, rh]: [u32; 2],
) -> Vec<Detection> {
    rows.sort_unstable_by(|a, b| {
        b.confidence
            .total_cmp(&a.confidence)
            .then(a.anchor.cmp(&b.anchor))
    });
    let mut result: Vec<Detection> = Vec::new();
    for row in rows {
        if result
            .iter()
            .any(|d| i32::from(d.class_id) == row.class_id && overlap(&d.xyxy, &row.xyxy) > 0.45)
        {
            continue;
        }
        result.push(Detection {
            class_id: row.class_id as u8,
            confidence: row.confidence,
            xyxy: row.xyxy,
        });
        if result.len() == 300 {
            break;
        }
    }
    let restore = |p: f32, original: u32, resized: u32| {
        ((p - ((SIDE - resized) / 2) as f32) * original as f32 / resized as f32)
            .clamp(0., original as f32)
    };
    for d in &mut result {
        let [x1, y1, x2, y2] = d.xyxy;
        d.xyxy = [
            restore(x1, width, rw),
            restore(y1, height, rh),
            restore(x2, width, rw),
            restore(y2, height, rh),
        ];
    }
    result.retain(|d| d.xyxy[2] > d.xyxy[0] && d.xyxy[3] > d.xyxy[1]);
    result
}

#[cfg(feature = "tensorrt")]
pub struct Detector {
    state: NonNull<c_void>,
    rows: Box<[Candidate]>,
}

#[cfg(feature = "tensorrt")]
impl Detector {
    /// Reserves enough source staging for 512x256 RGB or a single packed road frame.
    pub fn open(engine: &Path) -> Result<Self, String> {
        Self::open_with_source_capacity(engine, 512 * 256 * 3)
    }

    /// For full-resolution offline RGB, reserve stride * height before starting the worker.
    pub fn open_with_source_capacity(engine: &Path, capacity: usize) -> Result<Self, String> {
        if capacity == 0 || capacity > MAX_SOURCE {
            return Err("invalid source capacity".into());
        }
        let file = File::open(engine).map_err(|e| e.to_string())?;
        let len = file.metadata().map_err(|e| e.to_string())?.len();
        if len == 0 || len > MAX_PLAN {
            return Err("unexpected YOLO plan size".into());
        }
        let mut plan = Vec::with_capacity(len as usize);
        file.take(len + 1)
            .read_to_end(&mut plan)
            .map_err(|e| e.to_string())?;
        if plan.len() as u64 != len {
            return Err("YOLO plan size changed".into());
        }
        let mut error = [0; 256];
        // TensorRT consumes the serialized plan during open and owns the deserialized engine.
        let state = unsafe {
            pv5_yolo_open(
                plan.as_ptr().cast(),
                plan.len(),
                capacity,
                error.as_mut_ptr(),
                error.len(),
            )
        };
        let state = NonNull::new(state).ok_or_else(|| native_error(&error))?;
        Ok(Self {
            state,
            rows: vec![Candidate::default(); CAPACITY].into_boxed_slice(),
        })
    }

    pub fn detect_rgb(
        &mut self,
        rgb: &[u8],
        width: u32,
        height: u32,
        stride: u32,
    ) -> Result<Vec<Detection>, String> {
        self.detect(rgb, width, height, stride, false)
    }

    /// Six contiguous 8-bit planes for ONE current road frame (normally 512x256).
    pub fn detect_yuv6(
        &mut self,
        frame: &[u8],
        width: u32,
        height: u32,
    ) -> Result<Vec<Detection>, String> {
        self.detect(frame, width, height, 0, true)
    }

    fn detect(
        &mut self,
        data: &[u8],
        width: u32,
        height: u32,
        stride: u32,
        six: bool,
    ) -> Result<Vec<Detection>, String> {
        if width == 0 || height == 0 || width > 8192 || height > 8192 || stride > MAX_SOURCE as u32
        {
            return Err("unsupported source geometry".into());
        }
        let len = if six {
            if width % 2 != 0 || height % 2 != 0 {
                return Err("packed YUV needs even dimensions".into());
            }
            u64::from(width) * u64::from(height) * 3 / 2
        } else {
            if stride < 3 * width {
                return Err("RGB stride is too short".into());
            }
            u64::from(stride) * u64::from(height)
        };
        if data.len() as u64 != len || data.len() > MAX_SOURCE {
            return Err("invalid detector frame length".into());
        }
        let [rw, rh] = content_shape(width, height);
        let mut error = [0; 256];
        // The worker synchronizes its own stream before native detect returns.
        let count = unsafe {
            pv5_yolo_detect(
                self.state.as_ptr(),
                data.as_ptr(),
                data.len(),
                width as i32,
                height as i32,
                stride as i32,
                i32::from(six),
                rw as i32,
                rh as i32,
                self.rows.as_mut_ptr(),
                error.as_mut_ptr(),
                error.len(),
            )
        };
        if count < 0 || count as usize > self.rows.len() {
            return Err(native_error(&error));
        }
        Ok(postprocess(
            &mut self.rows[..count as usize],
            width,
            height,
            [rw, rh],
        ))
    }
}

#[cfg(feature = "tensorrt")]
fn native_error(bytes: &[c_char]) -> String {
    // Buffer starts zeroed and error_text() always terminates it.
    unsafe { CStr::from_ptr(bytes.as_ptr()) }
        .to_string_lossy()
        .into_owned()
}

#[cfg(feature = "tensorrt")]
impl Drop for Detector {
    fn drop(&mut self) {
        unsafe {
            pv5_yolo_close(self.state.as_ptr());
        }
    }
}

// The Jetlink host calls this ABI from one dedicated thread in its CUDA
// primary context. Only compact, post-NMS wide-image boxes leave this thread.
#[cfg(all(feature = "tensorrt", target_os = "linux"))]
mod wide_ffi {
    use super::*;
    use std::{ffi::OsStr, os::unix::ffi::OsStrExt, panic::AssertUnwindSafe, slice};

    #[repr(C)]
    pub struct WideBox {
        class_id: u32,
        confidence: f32,
        xyxy: [f32; 4],
    }

    #[no_mangle]
    pub unsafe extern "C" fn pv5_wide_open(path: *const c_char) -> *mut Detector {
        if path.is_null() {
            return std::ptr::null_mut();
        }
        std::panic::catch_unwind(|| {
            let path = Path::new(OsStr::from_bytes(unsafe { CStr::from_ptr(path) }.to_bytes()));
            Detector::open_with_source_capacity(path, 512 * 256 * 3 / 2)
                .map(|d| Box::into_raw(Box::new(d)))
                .unwrap_or(std::ptr::null_mut())
        })
        .unwrap_or(std::ptr::null_mut())
    }

    #[no_mangle]
    pub unsafe extern "C" fn pv5_wide_detect(
        detector: *mut Detector,
        pixels: *const u8,
        len: usize,
        out: *mut WideBox,
        capacity: usize,
    ) -> c_int {
        if detector.is_null()
            || pixels.is_null()
            || out.is_null()
            || len != 196608
            || capacity != 100
        {
            return -1;
        }
        std::panic::catch_unwind(AssertUnwindSafe(|| {
            let frame = unsafe { slice::from_raw_parts(pixels, len) };
            match unsafe { &mut *detector }.detect_yuv6(frame, 512, 256) {
                Ok(boxes) if boxes.len() <= capacity => {
                    for (i, d) in boxes.iter().enumerate() {
                        unsafe {
                            out.add(i).write(WideBox {
                                class_id: u32::from(d.class_id),
                                confidence: d.confidence,
                                xyxy: d.xyxy,
                            })
                        }
                    }
                    boxes.len() as c_int
                }
                Ok(_) => 0, // A crowded frame cannot be represented without truncation.
                Err(_) => -1,
            }
        }))
        .unwrap_or(-1)
    }

    #[no_mangle]
    pub unsafe extern "C" fn pv5_wide_close(detector: *mut Detector) {
        if !detector.is_null() {
            drop(unsafe { Box::from_raw(detector) });
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn stable_anchors_class_aware_nms_and_geometry() {
        let mut rows = [
            Candidate {
                xyxy: [80., 200., 160., 320.],
                confidence: 0.8,
                class_id: 2,
                anchor: 3,
            },
            Candidate {
                xyxy: [81., 201., 160., 320.],
                confidence: 0.8,
                class_id: 2,
                anchor: 1,
            },
            Candidate {
                xyxy: [80., 200., 160., 320.],
                confidence: 0.7,
                class_id: 0,
                anchor: 2,
            },
            Candidate {
                xyxy: [120., 20., 150., 40.],
                confidence: 0.95,
                class_id: 7,
                anchor: 4,
            },
        ];
        let shape = content_shape(512, 256);
        assert_eq!(shape, [640, 320]);
        let result = postprocess(&mut rows, 512, 256, shape);
        assert_eq!(result.len(), 2);
        assert_eq!(result[0].class_id, 2);
        assert_eq!(result[1].class_id, 0);
        assert!((result[0].xyxy[0] - 64.8).abs() < 1e-4);
        assert_eq!(result[1].xyxy, [64., 32., 128., 128.]);
    }
}
