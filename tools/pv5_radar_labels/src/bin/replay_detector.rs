//! Offline runner. Feed consecutive, already-warped 512x256 six-plane road frames.
use pv5_radar_labels::detector;
use std::{
    error::Error,
    io::{self, BufReader, BufWriter, Read, Write},
    path::PathBuf,
};

fn main() -> Result<(), Box<dyn Error>> {
    let args: Vec<_> = std::env::args_os().skip(1).collect();
    if args.len() != 1 {
        return Err("usage: replay-detector ENGINE.plan < road.yuv6".into());
    }
    let mut buffer = vec![0; 512 * 256 * 3 / 2];
    let plan = PathBuf::from(&args[0]);
    let mut engine = detector::Detector::open_with_source_capacity(&plan, buffer.len())?;
    let mut input = BufReader::new(io::stdin().lock());
    let mut out = BufWriter::new(io::stdout().lock());
    while input.read(&mut buffer[..1])? == 1 {
        input.read_exact(&mut buffer[1..])?;
        let detections = engine.detect_yuv6(&buffer, 512, 256)?;
        write!(out, "[")?;
        for (i, d) in detections.iter().enumerate() {
            if i > 0 {
                write!(out, ",")?;
            }
            let [x1, y1, x2, y2] = d.xyxy;
            write!(out, "{{\"class_id\":{},\"confidence\":{},\"x1\":{x1},\"y1\":{y1},\"x2\":{x2},\"y2\":{y2}}}", d.class_id, d.confidence)?;
        }
        writeln!(out, "]")?;
    }
    out.flush()?;
    Ok(())
}
