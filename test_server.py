import datetime as dt
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import server


class TimestampTests(unittest.TestCase):
    def test_offset_preserved_and_utc_conversion(self):
        value = server.timestamp('2026-10-09T16:30:00-04:00')
        self.assertEqual(value.astimezone(dt.timezone.utc).hour, 20)
        self.assertEqual(value.utcoffset(), dt.timedelta(hours=-4))

    def test_rejects_ambiguous_or_invalid_dates(self):
        for value in ['2026-10-09T16:30', '2026-02-30T16:30Z', '1900-01-01T12:00Z', None,
                      '2026-10-09T16:30+99:00', '2026-10-09T16:30+00:99',
                      '2026-10-09T16:30+14:01', '-execute']:
            with self.subTest(value=value), self.assertRaises(ValueError):
                server.timestamp(value)

    def test_jpeg_clears_stale_fractional_seconds(self):
        args = server.write_args('JPEG', server.timestamp('2026-10-09T16:30:00-04:00'))
        self.assertIn('-EXIF:OffsetTimeOriginal=-04:00', args)
        self.assertIn('-EXIF:SubSecTimeOriginal=', args)
        self.assertFalse(any('GPS' in arg for arg in args))

    def test_export_preserves_original_and_reports_mismatch(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / 'original.jpg'
            source.write_bytes(b'original fixture')
            before = {'File:FileType': 'JPEG', 'ExifIFD:DateTimeOriginal': '2020:01:01 12:00:00'}
            after = {'File:FileType': 'JPEG', 'ExifIFD:DateTimeOriginal': '2026:10:09 16:30:00'}
            with patch.object(server, 'inspect', side_effect=[before, after]), patch.object(server, 'metadata_engine', return_value='exiftool'), patch.object(server, 'exiftool') as tool:
                tool.return_value.stderr = ''
                result = server.export_copy({'path': str(source), 'filename': 'original.jpg'},
                                            '2026-10-09T16:30:00-04:00')
            self.assertEqual(source.read_bytes(), b'original fixture')
            self.assertNotEqual(tool.call_args[0][0][-1], str(source))
            self.assertTrue(result['verification']['ExifIFD:DateTimeOriginal']['matches'])
            self.assertFalse(result['verification']['IFD0:ModifyDate']['matches'])
            self.assertIn('did not match', result['warnings'][-1])
            token = result['report_url'].split('/')[-2]
            report = json.loads(server.DOWNLOADS[token]['report'].read_text())
            self.assertEqual(report['requested_timestamp'], '2026-10-09T16:30:00-04:00')


if __name__ == '__main__':
    unittest.main()
