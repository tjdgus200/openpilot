from benchmark_hud_pipeline import simulate


def test_model_distinguishes_throughput_and_latency():
  args = {'render_ms': 20., 'encode_ms': 22., 'usb_ms': 12.}
  before = simulate(**args, asynchronous=False)
  after = simulate(**args, asynchronous=True)
  assert before['fps'] < after['fps'] <= 20.
  assert after['age_ms']['mean'] > before['age_ms']['mean']  # Extra ownership copy, no fake latency saving.


def test_model_stalls_replace_pending_without_catchup():
  args = {'render_ms': 15., 'encode_ms': 18., 'usb_ms': 40., 'asynchronous': True,
          'stall_every': 20, 'stall_ms': 300., 'seconds': 30.}
  first = simulate(**args)
  assert first == simulate(**args)
  assert 0 < first['fps'] < 20. and first['pending_replacements_or_expiry'] > 0
