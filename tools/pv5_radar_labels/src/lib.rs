pub mod labels;
#[cfg(any(feature = "tensorrt", test))]
pub mod detector;
#[cfg(any(target_os = "linux", test))]
pub mod worker;
