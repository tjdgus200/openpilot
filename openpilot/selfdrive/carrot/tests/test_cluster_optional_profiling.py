from types import SimpleNamespace as NS

import pytest

import cluster_h264_pipeline as h264
import cluster_usb_pipeline as jpeg


@pytest.mark.parametrize('enabled', [False, True])
def test_jpeg_output_and_send_order_do_not_depend_on_profiling(monkeypatch, enabled):
  calls = []
  def clock():
    assert enabled, 'disabled encoder profiler read a clock'
    return 1.
  monkeypatch.setattr(jpeg, 'time', NS(perf_counter=clock))
  display = NS(profile_enabled=enabled, encode_jpeg=lambda *args: calls.append(('encode', args)) or b'jpeg',
               send_jpeg=lambda data: calls.append(('send', data)), clear_profile_samples=lambda: None,
               profile_samples=lambda: [('usb', 1.)])
  pipeline = jpeg.AsyncJpegUsbPipeline(display)
  frames = iter([(b'pixels', 2, 3)] * 1000 + [None])
  pipeline._take_pending = lambda: next(frames)
  pipeline._run()
  pipeline.check_error()
  assert calls == [('encode', (b'pixels', 2, 3)), ('send', b'jpeg')] * 1000
  assert len(pipeline._samples) == (256 if enabled else 0)
  assert bool(pipeline.profile_samples()) == enabled
  assert not pipeline._samples


@pytest.mark.parametrize('enabled', [False, True])
@pytest.mark.parametrize('diagnose', [0., 3600.])
def test_h264_ack_and_chunk_bytes_preserved_without_profiling(monkeypatch, enabled, diagnose):
  sent = []
  display = NS(profile_enabled=enabled, send_h264_chunk=lambda *args, **kw: sent.append((args, kw)))
  pipeline = h264.H264UsbPipeline(display, width=64, height=64, encoder_align=1, fps=10, bitrate='1m', gop=10,
                                  backend='native', library_path='', ffmpeg_path='ffmpeg', ffmpeg_encoder='libx264',
                                  device_path='', input_format='nv12', slice_max_bytes=4096, rate_control='vbr-cfr',
                                  realtime_priority=False, requested_chunk_size=0, wait_for_ack=True, soft_ack=False,
                                  dump_path='', debug=False, diagnose_interval_s=diagnose)
  def clock():
    assert enabled or diagnose, 'disabled sender profiler read a clock'
    return 1.
  monkeypatch.setattr(h264, 'time', NS(perf_counter=clock))
  for _ in range(1000):
    pipeline._send_h264_chunk(b'packet', 4096, source='native', is_last=True)
  assert sent == [((b'packet',), {'is_last': True, 'wait_for_ack': True, 'require_ack_response': True})] * 1000
  assert pipeline._chunks_completed == 1000
  assert len(pipeline._samples) == (256 if enabled else 0)
  assert bool(pipeline.profile_samples()) == enabled
  if not enabled:
    class NoNativeCalls:
      def __getattr__(self, name):
        pytest.fail('disabled profiler queried native GPU timings')
    pipeline._add_native_timing_samples(NoNativeCalls(), 1)
