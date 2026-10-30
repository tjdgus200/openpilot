"""Exact N16 mapped uint8 staging, qualified before serving any real frame.

Both CUDA graphs retain fixed bindings and the same recurrence/finite checks.
The extra pinned allocation is384KiB; unsupported/probe failures retain CPU.
Compilation and paired qualification happen in the existing preparation job.
"""
import ctypes
import hashlib
import os
from pathlib import Path

import numpy as np

from jetlink.server.backends.trt import cudart
from jetlink.server.backends.trt.device_state import _checked

SOURCE = r'''
__device__ unsigned short u8_half_bits(unsigned int x) {
  if (!x) return 0;
  unsigned int e = 31u - __clz(x);
  return (unsigned short)(((e + 15u) << 10) | ((x - (1u << e)) << (10u - e)));
}
extern "C" __global__ void cast_image(const unsigned char *src, unsigned short *dst,
                                    unsigned long long n) {
  for (unsigned long long i = (unsigned long long)blockIdx.x * blockDim.x + threadIdx.x;
       i < n; i += (unsigned long long)gridDim.x * blockDim.x)
    dst[i] = u8_half_bits(src[i]);
}
'''


def configure(engine, spec):
  """Default-attempt only on reviewed N16+Orin; never broaden other layouts."""
  from jetlink.output_projection import eligible
  engine.image_fusion_reason = 'unsupported'
  if os.getenv('CARROT_TRT_IMAGE_FUSION', '1') == '0':
    engine.image_fusion_reason = 'disabled'
    return None
  if not eligible(spec) or not Path('/etc/nv_tegra_release').is_file() or engine.device_state is None:
    return None
  candidate = None
  try:
    candidate = ImageFusion(engine, spec)
    engine.image_fusion = candidate
    engine.image_fusion_reason = 'qualifying'
    return candidate
  except (ImportError, OSError, ValueError, RuntimeError):
    # A failed CUDA synchronization is not safe to hide as a CPU fallback.
    cudart.stream_sync(engine.stream)
    engine.image_fusion_reason = 'unavailable'
    return None


class ImageFusion:
  def __init__(self, engine, spec):
    self.engine, self.spec = engine, spec
    self.active, self.qualified = 0, False
    self.module = self.host = None
    self.graphs = {}
    self.cpu_cast = None
    b = engine.inputs['new_img']
    if b.dtype != np.float16 or b.shape != spec.warped_shape or b.nbytes != 786432:
      raise ValueError('Unsupported image binding')
    device = cudart.check(cudart._rt.cudaGetDevice())
    _, major, minor = cudart.device_name(device)
    if (major, minor) != (8, 7):
      raise ValueError('Orin SM87 required')
    rt = cudart._rt
    if not cudart.check(rt.cudaDeviceGetAttribute(rt.cudaDeviceAttr.cudaDevAttrCanMapHostMemory, device)):
      raise ValueError('Mapped memory unavailable')
    try:
      from cuda.bindings import driver, nvrtc
    except ImportError:
      from cuda import cuda as driver, nvrtc
    self.driver = driver
    try:
      program = _checked(nvrtc.nvrtcCreateProgram(SOURCE.encode(), b'carrot_image.cu', 0, [], []))
      try:
        # Native cubin avoids a second PTX JIT on the first captured frame.
        _checked(nvrtc.nvrtcCompileProgram(program, 1, [b'--gpu-architecture=sm_87']))
        size = _checked(nvrtc.nvrtcGetCUBINSize(program))
        if not 0 < size <= 1024 * 1024:
          raise ValueError('Image kernel budget exceeded')
        cubin = b'\0' * size
        _checked(nvrtc.nvrtcGetCUBIN(program, cubin))
      finally:
        _checked(nvrtc.nvrtcDestroyProgram(program))
      self.module = _checked(driver.cuModuleLoadData(cubin))
      self.function = _checked(driver.cuModuleGetFunction(self.module, b'cast_image'))
      self.count = b.nbytes // 2
      self.host = int(cudart.check(rt.cudaHostAlloc(self.count, 2)))
      self.device = int(cudart.check(rt.cudaHostGetDevicePointer(self.host, 0)))
      self.u8 = np.frombuffer((ctypes.c_ubyte * self.count).from_address(self.host), np.uint8).reshape(b.shape)
      self.u8.reshape(-1)[:] = np.arange(self.count, dtype=np.uint8)
      self.enqueue()
      cudart.memcpy_d2h_async(b.host_ptr, b.device_ptr, b.nbytes, engine.stream)
      cudart.stream_sync(engine.stream)
      if b.host.tobytes() != self.u8.astype(np.float16).tobytes():
        raise ValueError('GPU image bit probe failed')
      self.u8.fill(0)
      b.host.fill(0)
    except BaseException:
      self.close()
      raise

  def enqueue(self):
    args = (ctypes.c_uint64(self.device), ctypes.c_uint64(self.engine.inputs['new_img'].device_ptr),
            ctypes.c_uint64(self.count))
    pointers = (ctypes.c_void_p * 3)(*(ctypes.addressof(a) for a in args))
    _checked(self.driver.cuLaunchKernel(self.function, (self.count + 255) // 256, 1, 1,
                                      256, 1, 1, 0, self.engine.stream, ctypes.addressof(pointers), 0))

  def stage(self, source, target):
    if source.shape != self.spec.warped_shape or source.dtype != np.uint8 or target is not self.engine.inputs['new_img'].host:
      raise ValueError('Image staging ownership/shape changed')
    if self.active:
      np.copyto(self.u8, source, casting='no')
    elif self.cpu_cast is not None:
      self.cpu_cast(source, target)
    else:
      from jetlink.queues import _U8_TO_F16_BITS
      np.take(_U8_TO_F16_BITS, source.reshape(-1), out=target.reshape(-1).view(np.uint16), mode='clip')

  def select(self, mode):
    if type(mode) is not int or mode not in self.graphs:
      raise ValueError('Image graph unavailable')
    if mode != self.active:
      # Reply completion can precede the trailing state copies.
      cudart.stream_sync(self.engine.stream)
      self.active = mode
      self.engine.graph_exec = self.graphs[mode]

  def qualify(self, queues, inputs):
    self.cpu_cast = queues.input_cast
    queues.input_cast = self.stage
    self.graphs[0] = self.engine.graph_exec
    if self.graphs[0] is None:
      raise ValueError('Captured CPU graph required')
    self.active = 1
    self.engine.graph_exec = None
    if not self.engine.capture_graph():
      raise ValueError('Captured mapped graph required')
    self.graphs[1] = self.engine.graph_exec
    image = np.arange(self.count, dtype=np.uint8).reshape(self.spec.warped_shape)
    packed = np.zeros(self.spec.packed_nelem, np.float32)
    hashes = []
    for mode in (0, 1):
      self.select(mode)
      queues.reset()
      observed = []
      for i in range(4):
        pixels = image + np.uint8(i * 31)
        queues.step_into(pixels, packed, inputs)
        output = self.engine.run()
        queues.feedback(output)
        digest = hashlib.sha256()
        # Reuse already-owned output staging; no second history allocation.
        for name in sorted(self.engine.outputs):
          b = self.engine.outputs[name]
          if name not in output:
            cudart.memcpy_d2h_async(b.host_ptr, b.device_ptr, b.nbytes, self.engine.stream)
        cudart.stream_sync(self.engine.stream)
        for name in sorted(self.engine.outputs):
          digest.update(memoryview(self.engine.outputs[name].host).cast('B'))
        observed.append(digest.digest())
      hashes.append(observed)
    if hashes[0] != hashes[1]:
      raise ValueError('Paired model/state qualification failed')
    queues.reset()
    self.qualified = True
    self.engine.image_fusion_reason = 'ready'
    self.select(1)

  def close(self, keep_graph=None):
    cudart.stream_sync(self.engine.stream)
    graphs = list(self.graphs.values())
    if (self.graphs and self.engine.graph_exec is not None and self.engine.graph_exec is not keep_graph
        and all(self.engine.graph_exec is not g for g in graphs)):
      graphs.append(self.engine.graph_exec)
    for graph in graphs:
      if graph is not None and graph is not keep_graph:
        cudart.graph_exec_destroy(graph)
    self.graphs.clear()
    if graphs:
      self.engine.graph_exec = keep_graph
    if self.host is not None:
      cudart.host_free(self.host)
      self.host = None
    if self.module is not None:
      _checked(self.driver.cuModuleUnload(self.module))
      self.module = None
