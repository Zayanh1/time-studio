"""Actual upload/export/download integration. Pillow is only a test dependency."""
import io
import json
import threading
import unittest
import urllib.request
import urllib.error
from unittest.mock import patch
try:
    from PIL import Image
except ImportError:
    Image = None
import server


@unittest.skipUnless(Image, 'Pillow needed only to generate and independently read JPEG fixtures')
class APITests(unittest.TestCase):
    def test_builtin_upload_export_download(self):
        with patch.object(server, 'metadata_engine', return_value='builtin'):
            http = server.ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
            thread = threading.Thread(target=http.serve_forever, daemon=True)
            thread.start()
            base = f'http://127.0.0.1:{http.server_port}'
            try:
                status = json.load(urllib.request.urlopen(base + '/api/status'))
                self.assertTrue(status['metadata_available'])
                original = io.BytesIO()
                Image.new('RGB', (8, 8), '#245644').save(original, format='JPEG')
                raw = original.getvalue()
                boundary = b'TimeStudioIntegrationBoundary'
                body = b'--' + boundary + b'\r\nContent-Disposition: form-data; name="file"; filename="sample.jpg"\r\nContent-Type: image/jpeg\r\n\r\n' + raw + b'\r\n--' + boundary + b'--\r\n'
                request = urllib.request.Request(base+'/api/inspect', data=body, headers={'Content-Type': 'multipart/form-data; boundary='+boundary.decode()})
                upload = json.load(urllib.request.urlopen(request))
                request = urllib.request.Request(base+'/api/export', data=json.dumps({'upload_id':upload['upload_id'], 'timestamp':'2026-10-09T16:30:00-04:00'}).encode(), headers={'Content-Type':'application/json'})
                result = json.load(urllib.request.urlopen(request))
                self.assertTrue(result['verification_passed'])
                self.assertEqual(result['engine'], 'builtin')
                edited = urllib.request.urlopen(base+result['download_url']).read()
                with Image.open(io.BytesIO(edited)) as image:
                    self.assertEqual(image.getexif().get_ifd(0x8769)[0x9003], '2026:10:09 16:30:00')
                    self.assertEqual(image.getexif().get_ifd(0x8769)[0x9011], '-04:00')
                    with Image.open(io.BytesIO(raw)) as before:
                        self.assertEqual(before.tobytes(), image.tobytes())
                self.assertEqual(server.Path(server.UPLOADS[upload['upload_id']]['path']).read_bytes(), raw)
                report = json.load(urllib.request.urlopen(base+result['report_url']))
                self.assertEqual(report['requested_timestamp'], '2026-10-09T16:30:00-04:00')
                request = urllib.request.Request(base+'/api/export', data=b'{}', headers={'Origin':'https://example.com', 'Content-Type':'application/json'})
                with self.assertRaises(urllib.error.HTTPError) as error:
                    urllib.request.urlopen(request)
                self.assertEqual(error.exception.code, 403)
            finally:
                http.shutdown()
                http.server_close()
                thread.join()

if __name__ == '__main__':
    unittest.main()
