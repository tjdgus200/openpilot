import importlib.util
from pathlib import Path
import pytest

spec = importlib.util.spec_from_file_location('probe_report', Path(__file__).with_name('probe_report.py'))
report = importlib.util.module_from_spec(spec)
spec.loader.exec_module(report)


def test_expiration_disables_report():
  assert report.destination({'url': 'http://10.37.50.41:17088/test', 'expires': 0}) is None


def test_private_literal_destination():
  url = 'http://10.37.50.41:17088/test'
  assert report.destination({'url': url, 'expires': 1e12}) == url


@pytest.mark.parametrize('url', ['http://8.8.8.8:80/x', 'http://example.com:80/x',
                                'http://user:pass@10.0.0.1:80/x', 'http://10.0.0.1:80/x?secret=x'])
def test_no_external_or_credential_destination(url):
  with pytest.raises(ValueError):
    report.destination({'url': url, 'expires': 1e12})
