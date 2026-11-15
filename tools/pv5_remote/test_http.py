import http.client
from http.server import BaseHTTPRequestHandler
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest

from openpilot.system.pv5_incidents.archive_protocol import encode, headers
from tools.pv5_archive.store import BoundedHTTPServer
from tools.pv5_remote.pc import Bootstrap, PATH, handler
from tools.pv5_remote.test_remote import key


class HttpChecks(unittest.TestCase):
  def test_real_isolated_http_auth_pin_body_limits_and_legacy_delegation(self):
    class Base(BaseHTTPRequestHandler):
      def setup(self):
        super().setup()
        self.connection.settimeout(2)

      def do_POST(self):
        self.send_response(422)
        self.send_header('Content-Length', '0')
        self.end_headers()

      def log_message(self, *_):
        pass

    with tempfile.TemporaryDirectory() as temporary:
      root, secret = Path(temporary), bytes(32)
      (root / 'client_ed25519.pub').write_text(key(1))
      (root / 'client_ed25519').write_text('NEVER_READ_PRIVATE_KEY')
      server = BoundedHTTPServer(('127.0.0.1', 0), handler(Base, Bootstrap(root, secret, lambda: True)))
      threading.Thread(target=server.serve_forever, daemon=True).start()
      body = encode({'version': 1, 'host_key': key(2)})

      def post(path, data, signed=False):
        connection = http.client.HTTPConnection(*server.server_address, timeout=3)
        try:
          connection.request('POST', path, data, headers=headers(secret, path, data, now=time.time()) if signed else {})
          response = connection.getresponse()
          return response.status, response.read(8192)
        finally:
          connection.close()

      try:
        self.assertEqual(post(PATH, body)[0], 403)
        self.assertFalse((root / 'host_key.pub').exists())
        self.assertEqual(post(PATH, b'x' * 2049)[0], 413)
        self.assertEqual(post(PATH, b'{"probe":1}', True)[0], 422)
        code, data = post(PATH, body, True)
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(data)['client_public_key'], key(1))
        self.assertNotIn(b'NEVER_READ_PRIVATE_KEY', data)
        self.assertEqual(post('/v1/incident', b'{"probe":1}')[0], 422)
      finally:
        server.shutdown()
        server.server_close()


if __name__ == '__main__':
  unittest.main()
