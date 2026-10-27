# Pinned CPU JPEG runtime

These files provide a private CPU-only runtime for the three Jetson TurboJPEG
quality candidates. They are not NVIDIA/CUDA libraries, an OS installation,
or a replacement for system JPEG libraries. H.264 startup does not load them.

- Python: PyTurboJPEG 1.8.2 (MIT), `turbojpeg.py` and `LICENSE-PyTurboJPEG`.
  Source: <https://pypi.org/project/PyTurboJPEG/1.8.2/>.
  Source archive SHA256:
  `b7d9625bbb2121b923228fc70d0c2b010b386687501f5b50acec4501222e152b`.
- Native: Ubuntu Jammy ARM64 `libturbojpeg` 2.1.2-0ubuntu1, unmodified
  `libturbojpeg.so.0.2.0` and its complete `LICENSE-libturbojpeg` copyright file.
  Source package URL:
  <https://ports.ubuntu.com/ubuntu-ports/pool/main/libj/libjpeg-turbo/libturbojpeg_2.1.2-0ubuntu1_arm64.deb>.
  Package SHA256:
  `70ffb7d3849b196d69492ec85499b04fb0a36b8e8dbbaec817ddcb20bfcbd1e4`.
  Library SHA256:
  `7aff9f95459048afcf24b4768552b4211cff65c0bf006b3be4a0dead5e75be58`.

The host checks the native library hash once before loading. NumPy is already
part of the Jetson runtime. Only the encode API is called with bounded,
renderer-owned RGBA data and 4:2:0 baseline JPEG settings. Python source/license
and ARM64 ELF checks are PC validation; real ARM64 loading/performance still
require a device test. No JPEG decoder or arbitrary library-path setting is
exposed by the Carrot Web selection.
