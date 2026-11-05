"""Offline CPU release validation for an exact PV5 ONNX pin.

Uses a synthetic NV12/nearest-neighbor reference with production camera geometry.
The native camera/GPU warp, TensorRT/FP16, latency and driving remain unverified.
Requires numpy, onnx and onnxruntime. Writes large temporary models/NPZs only to
--output; never downloads weights or touches a vehicle.
"""


def main():
  if not __debug__:
    raise RuntimeError('Validation requires assertions; do not run Python with -O')
  import argparse
  import gc
  import hashlib
  import json
  from pathlib import Path
  import sys
  import time

  ROOT = Path(__file__).resolve().parents[2]
  sys.path[:0] = [str(ROOT), str(ROOT / 'third_party/jetlink')]
  parser = argparse.ArgumentParser(description='PC-only ONNX/FOV/recurrent/reset equivalence; not TensorRT or road validation')
  parser.add_argument('--model', required=True)
  parser.add_argument('--onnx', type=Path, required=True)
  parser.add_argument('--output', type=Path, required=True)
  args = parser.parse_args()
  if Path('/TICI').exists() or Path('/etc/nv_tegra_release').exists():
    raise RuntimeError('Run model validation on a PC, not comma/Jetson')
  from openpilot.common import big_model_catalog as catalog
  if args.model not in catalog.catalog() or catalog.catalog()[args.model].get('retired'):
    raise ValueError('Unknown or retired PV5 model')
  TASK = args.output.resolve()
  TASK.mkdir(parents=True, exist_ok=True)
  report_path = TASK / (args.model + '-numerical.json')
  report_path.unlink(missing_ok=True)
  import numpy as np
  import onnx
  import onnxruntime as ort
  from jetlink.onnx_patch import patch_file, strip_tinygrad_ops
  from jetlink.spec import ModelSpec

  key = args.model
  source = args.onnx.resolve()
  pin = catalog.model_spec(key)
  manifest = {'sha256': pin['sha256'], 'size': pin['nbytes']}
  with source.open('rb') as f:
    digest = hashlib.file_digest(f, 'sha256').hexdigest()
  assert digest == manifest['sha256'] == pin['sha256']
  assert source.stat().st_size == manifest['size'] == pin['nbytes']
  # Allow only built-in slice metadata, before the production metadata reader runs.
  import base64, io, pickle
  from jetlink.onnx_meta import parse_file
  class SliceOnly(pickle.Unpickler):
    def find_class(self, module, name):
      if (module, name) == ('builtins', 'slice'): return slice
      raise ValueError('Unexpected ONNX metadata class')
  metadata = parse_file(str(source))
  slices = SliceOnly(io.BytesIO(base64.b64decode(metadata.props['output_slices']))).load()
  assert {k: [v.start, v.stop] for k, v in slices.items()} == pin['output_slices']
  actual = ModelSpec(digest, source.stat().st_size, pin['frame_skip'], metadata.inputs, metadata.outputs,
                     slices, metadata.model_checkpoint)
  assert actual.to_dict() == pin

  def contract(model):
    return {which: {v.name: {'shape': [d.dim_value for d in v.type.tensor_type.shape.dim],
                             'dtype': onnx.TensorProto.DataType.Name(v.type.tensor_type.elem_type)}
                    for v in getattr(model.graph, which)} for which in ('input', 'output')}

  baseline = TASK / (key + '-reference.onnx')
  patched = TASK / (key + '-trt-input.onnx')
  model = onnx.load(str(source))
  result = dict(model=key, sha256=digest, bytes=source.stat().st_size,
                raw_contract=contract(model), full_spec_matches_pin=True, frames=[],
                scope='ONNX Runtime CPU; not physical TensorRT/GPU/road validation')
  onnx.checker.check_model(model, full_check=True)
  result['original_full_onnx_check'] = True
  result['layout_nodes_removed'] = strip_tinygrad_ops(model)
  onnx.save(model, str(baseline))
  del model
  gc.collect()
  patch_file(str(source), str(patched))
  model = onnx.load(str(patched), load_external_data=False)
  onnx.checker.check_model(model, full_check=True)
  result['patched_full_onnx_check'] = True
  result['patched_contract'] = contract(model)
  del model
  gc.collect()
  types = {'tensor(uint8)': np.uint8, 'tensor(float16)': np.float16, 'tensor(float)': np.float32}
  opts = ort.SessionOptions()
  opts.intra_op_num_threads = opts.inter_op_num_threads = 1
  opts.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
  opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_DISABLE_ALL
  opts.log_severity_level = 3

  from openpilot.common.transformations.camera import DEVICE_CAMERAS
  from openpilot.common.transformations.model import get_warp_matrix, wider_model_warp
  CAMERA=DEVICE_CAMERAS['tici','os04c10']
  FOVS=(None,65,70,75,80,None)
  def project(plane,matrix,w,h):
    y,x=np.indices((h,w),dtype=np.float32)
    x,y,z=matrix@np.stack((x.ravel(),y.ravel(),np.ones(w*h,np.float32)))
    ix=np.clip(np.rint(x/z),0,plane.shape[1]-1).astype(np.int32)
    iy=np.clip(np.rint(y/z),0,plane.shape[0]-1).astype(np.int32)
    return plane[iy,ix].reshape(h,w)
  def prepare(lens,matrix,brightness):
    width,height=lens.size
    y,x=np.indices((height,width),dtype=np.int32)
    luma=((17*x+11*y+(x//83+y//61)*37+brightness)%256).astype(np.uint8)
    y,x=np.indices((height//2,width//2),dtype=np.int32)
    u=((x*5+y*3+79)%256).astype(np.uint8)
    v=((x*7+y*2+179)%256).astype(np.uint8)
    y=project(luma,matrix,512,256)
    uv=matrix*np.array([[1.,1.,.5],[1.,1.,.5],[2.,2.,1.]],np.float32)
    return np.stack((y[::2,::2],y[1::2,::2],y[::2,1::2],y[1::2,1::2],project(u,uv,256,128),project(v,uv,256,128)))
  def camera_input(frame):
    # CPU nearest-neighbor reference for production camera geometry, not native GPU warp validation.
    rpy=np.array([.01,-.02,.005],np.float32)
    narrow=get_warp_matrix(rpy,CAMERA.fcam.intrinsics,False).astype(np.float32)
    wide=get_warp_matrix(rpy,CAMERA.ecam.intrinsics,True)
    fov=FOVS[frame//2]
    wide=wider_model_warp(wide,fov) if fov else wide.astype(np.float32)
    return np.stack((prepare(CAMERA.fcam,narrow,frame%2*19),prepare(CAMERA.ecam,wide,frame%2*19)))
  result['camera_input_scope']='Synthetic NV12/CPU nearest-neighbor reference; production geometry; not native camera/GPU warp'
  result['fov_sequence']=list(FOVS)
  images=[camera_input(i) for i in range(12)]
  assert all(np.array_equal(images[0][0],images[i][0]) for i in range(0,12,2))
  assert len({hashlib.sha256(images[i][1].tobytes()).hexdigest() for i in range(0,10,2)})==5
  result['narrow_unchanged_and_five_distinct_wide_inputs']=True

  for variant, path in (('reference', baseline), ('patched', patched)):
    print(key, variant, 'loading CPU session', flush=True)
    session = ort.InferenceSession(str(path), sess_options=opts, providers=['CPUExecutionProvider'])
    state = {}
    for frame in range(12):
      if frame in (0, 10): state = {}
      data = {}
      for v in session.get_inputs():
        typ = types[v.type]
        shape = tuple(v.shape)
        if v.name.startswith('state_'):
          data[v.name] = state.get(v.name, np.zeros(shape, typ))
        elif v.name == 'new_img':
          data[v.name] = images[frame].astype(typ)
        elif v.name == 'nav_map':
          data[v.name] = (np.arange(np.prod(shape), dtype=np.float32) % 256 / 255).astype(typ).reshape(shape)
        else:
          data[v.name] = np.zeros(shape, typ)
          if v.name in ('traffic_convention', 'desire'): data[v.name].flat[0] = 1
          if v.name == 'action_t': data[v.name].flat[0] = 0.1
      start = time.monotonic()
      values = dict(zip([v.name for v in session.get_outputs()], session.run(None, data)))
      elapsed = time.monotonic() - start
      assert all(np.isfinite(v).all() for v in values.values())
      state = {name[5:]: value.copy() for name, value in values.items() if name.startswith('next_state_')}
      archive = TASK / f'{key}-reference-{frame}.npz'
      if variant == 'reference':
        np.savez(archive, **values)
      else:
        with np.load(archive) as reference:
          checks = {}
          for name, value in values.items():
            expected = reference[name].astype(value.dtype)
            assert expected.shape == value.shape
            checks[name] = {'exact': bool(np.array_equal(expected, value)),
                            'max_abs': float(np.max(np.abs(expected.astype(np.float32) - value.astype(np.float32))))}
            assert checks[name]['exact'], (key, frame, name, checks[name])
          result['frames'].append({'frame': frame, 'reset': frame in (0, 10), 'outputs': checks})
      print(key, variant, 'frame', frame, 'finite', 'seconds', round(elapsed, 3), flush=True)
    del session, state, data, values
    gc.collect()
  result['all_outputs_numerically_exact'] = True
  with np.load(TASK / f'{key}-reference-0.npz') as first, np.load(TASK / f'{key}-reference-10.npz') as reset:
    result['reset_repeat_exact'] = all(np.array_equal(first[name], reset[name]) for name in first.files)
    assert result['reset_repeat_exact']
  result['physical_device_tested'] = False
  result['source_commit'] = __import__('subprocess').check_output(['git', '-C', str(ROOT), 'rev-parse', 'HEAD'], text=True).strip()
  report_path.write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
  print(key, 'TWELVE_FRAME_FOV_CPU_PATCH_PARITY_PASS', flush=True)


if __name__ == '__main__':
  main()
