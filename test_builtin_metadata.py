import datetime as dt
import hashlib
import json
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
import unittest
try:
    from PIL import Image
except ImportError:
    Image = None
import builtin_metadata as bm

WHEN = dt.datetime.fromisoformat('2026-10-09T16:30:00-04:00')

class BuiltinTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self.tmp.name)
    def tearDown(self):
        self.tmp.cleanup()

    @unittest.skipUnless(Image, 'Pillow required for independent JPEG validation')
    def test_jpeg_preserves_pixels_orientation_gps_and_maker_note(self):
        source, target = self.folder / 'in.jpg', self.folder / 'out.jpg'
        exif = Image.Exif()
        exif[0x112] = 6
        exif[0x10f] = 'Test camera'
        exif[0x8769] = {0x9003: '2020:01:01 10:00:00', 0x927c: b'private maker bytes', 0x9291: '123'}
        exif[0x8825] = {1: 'N', 2: (33.0, 44.0, 55.0), 3: 'W', 4: (84.0, 22.0, 11.0)}
        Image.new('RGB', (17, 11), (55, 123, 240)).save(source, exif=exif)
        original = source.read_bytes()
        bm.edit(source, target, WHEN)
        result = bm.inspect(target)
        self.assertEqual(result['ExifIFD:DateTimeOriginal'], '2026:10:09 16:30:00')
        self.assertEqual(result['ExifIFD:OffsetTimeOriginal'], '-04:00')
        self.assertEqual(source.read_bytes(), original)
        with Image.open(source) as a, Image.open(target) as b:
            self.assertEqual(a.tobytes(), b.tobytes())
            self.assertEqual(b.getexif()[0x112], 6)
            self.assertEqual(a.getexif().get_ifd(0x8825), b.getexif().get_ifd(0x8825))
            self.assertEqual(b.getexif().get_ifd(0x8769)[0x927c], b'private maker bytes')
            self.assertNotIn(0x9291, b.getexif().get_ifd(0x8769))
        # Encoded JPEG is byte-for-byte identical after removing only EXIF APP1.
        def without_exif(data):
            span, _ = bm.jpeg_exif(data)
            return data[:span[0]] + data[span[1]:]
        self.assertEqual(without_exif(original), without_exif(target.read_bytes()))

    @unittest.skipUnless(Image, 'Pillow required for JPEG fixture')
    def test_jpeg_without_exif_and_duplicate_exif_rejection(self):
        source, target = self.folder / 'in.jpg', self.folder / 'out.jpg'
        Image.new('RGB', (8, 8)).save(source)
        bm.edit(source, target, WHEN)
        self.assertEqual(bm.inspect(target)['IFD0:ModifyDate'], '2026:10:09 16:30:00')
        self.assertEqual(target.read_bytes()[2:4], b'\xff\xe0')
        data = target.read_bytes()
        span, _ = bm.jpeg_exif(data)
        source.write_bytes(data[:2] + data[span[0]:span[1]] + data[2:])
        with self.assertRaisesRegex(ValueError, 'Multiple EXIF'):
            bm.edit(source, target, WHEN)

    def test_malformed_tiff_and_atom_rejection(self):
        with self.assertRaises(ValueError):
            bm.jpeg_segments(b'\xff\xd8\xff\xd9')
        with self.assertRaises(ValueError):
            bm.Tiff(b'II\x2a\0\xff\xff\xff\x7f')
        with self.assertRaises(ValueError):
            bm.movie_fields(struct.pack('>I4s', 999, b'moov'))

    def test_big_endian_preserves_next_directory_and_unknown_entries(self):
        raw = (b'MM' + struct.pack('>HIH', 42, 8, 2)
               + struct.pack('>HHI', 0x112, 3, 1) + b'\0\x06\0\0'
               + struct.pack('>HHII', 0x10f, 2, 6, 44)
               + struct.pack('>IHI', 38, 0, 0) + b'Brand\0')
        before = bm.Tiff(raw)
        result = before.edit(WHEN)
        after = bm.Tiff(result)
        self.assertEqual(after.root_next, 38)
        self.assertEqual(after.root[0x112], before.root[0x112])
        self.assertEqual(after.ascii(after.root[0x10f]), 'Brand')
        self.assertEqual(after.ascii(after.exif[0x9003]), '2026:10:09 16:30:00')
        self.assertEqual(result[8:len(raw)], raw[8:])

    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'ffmpeg/ffprobe required')
    def test_actual_mp4_and_mov_ffprobe_and_payload_preservation(self):
        for suffix in ('mp4', 'mov'):
            with self.subTest(suffix=suffix):
                source, target = self.folder / ('in.' + suffix), self.folder / ('out.' + suffix)
                subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'color=c=blue:s=32x32:d=0.2',
                                '-c:v', 'mpeg4', '-y', str(source)], check=True)
                original = source.read_bytes()
                bm.edit(source, target, WHEN)
                result = bm.inspect(target)
                self.assertEqual(result['QuickTime:CreateDate'], '2026:10:09 20:30:00+00:00')
                self.assertEqual(result['Track1:MediaCreateDate'], '2026:10:09 20:30:00+00:00')
                probe = json.loads(subprocess.check_output(['ffprobe', '-v', 'error', '-show_format', '-show_streams',
                                                            '-of', 'json', str(target)]))
                self.assertEqual(probe['format']['tags']['creation_time'], '2026-10-09T20:30:00.000000Z')
                self.assertEqual(probe['streams'][0]['tags']['creation_time'], '2026-10-09T20:30:00.000000Z')
                _, fields = bm.movie_fields(original)
                allowed = {i for _, offset, width in fields for i in range(offset, offset + width)}
                output = target.read_bytes()
                self.assertEqual(len(original), len(output))
                self.assertTrue(all(a == b or i in allowed for i, (a, b) in enumerate(zip(original, output))))
                def payload_hash(data):
                    return [hashlib.sha256(data[b:e]).hexdigest() for kind, b, e in bm.boxes(data, 0, len(data)) if kind == b'mdat']
                self.assertEqual(payload_hash(original), payload_hash(output))
                self.assertEqual(source.read_bytes(), original)
                with self.assertRaisesRegex(ValueError, 'version'):
                    bm.edit(source, target, WHEN.replace(year=2050))

    def test_version_one_extended_atoms(self):
        def atom(kind, payload):
            return struct.pack('>I4sQ', 1, kind, 16 + len(payload)) + payload
        data = atom(b'moov', atom(b'mvhd', b'\x01\0\0\0' + b'\0' * 16))
        source, target = self.folder / 'in.mp4', self.folder / 'out.mp4'
        source.write_bytes(data)
        bm.edit(source, target, WHEN.replace(year=2050))
        self.assertEqual(bm.inspect(target)['QuickTime:CreateDate'], '2050:10:09 20:30:00+00:00')

if __name__ == '__main__':
    unittest.main()
