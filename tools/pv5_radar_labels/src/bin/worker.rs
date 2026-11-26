#[cfg(target_os = "linux")]
fn main() -> Result<(), Box<dyn std::error::Error>> {
    use pv5_radar_labels::worker::run;
    use std::path::PathBuf;
    let a: Vec<_> = std::env::args_os().skip(1).map(PathBuf::from).collect();
    if !(1..=2).contains(&a.len()) {
        return Err("usage: radar-label-worker PRIVATE_RUN_DIRECTORY [HUD_PACKET]".into());
    }
    let hud = a
        .get(1)
        .cloned()
        .unwrap_or_else(|| "/dev/shm/carrot-jetlink-hud.packet".into());
    run(&a[0], &hud)
}
#[cfg(not(target_os = "linux"))]
fn main() {
    eprintln!("the radar label worker requires Linux");
    std::process::exit(1);
}
