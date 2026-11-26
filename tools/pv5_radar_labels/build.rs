use std::{env, path::PathBuf, process::Command};

fn run(command: &mut Command) {
    let status = command.status().expect("could not launch native build");
    assert!(status.success(), "native build failed: {command:?}");
}

fn main() {
    println!("cargo:rerun-if-changed=native");
    println!("cargo:rerun-if-env-changed=CUDA_HOME");
    println!("cargo:rerun-if-env-changed=CMAKE_CUDA_ARCHITECTURES");
    if env::var_os("CARGO_FEATURE_TENSORRT").is_none() {
        return;
    }
    let out = PathBuf::from(env::var_os("OUT_DIR").unwrap()).join("trt-native");
    let cuda = PathBuf::from(env::var_os("CUDA_HOME").unwrap_or_else(|| "/usr/local/cuda".into()));
    run(Command::new("cmake")
        .args(["-S", "native", "-B"])
        .arg(&out)
        .arg("-DCMAKE_BUILD_TYPE=Release")
        .arg(format!(
            "-DCMAKE_CUDA_COMPILER={}",
            cuda.join("bin/nvcc").display()
        ))
        .arg(format!(
            "-DCMAKE_CUDA_ARCHITECTURES={}",
            env::var("CMAKE_CUDA_ARCHITECTURES").unwrap_or_else(|_| "87".into())
        )));
    run(Command::new("cmake")
        .arg("--build")
        .arg(&out)
        .args(["--config", "Release"]));
    println!("cargo:rustc-link-search=native={}", out.display());
    println!(
        "cargo:rustc-link-search=native={}",
        cuda.join("lib64").display()
    );
    println!("cargo:rustc-link-lib=static=pv5_yolo");
    println!("cargo:rustc-link-lib=nvinfer");
    println!("cargo:rustc-link-lib=cudart");
    println!("cargo:rustc-link-lib=stdc++");
}
