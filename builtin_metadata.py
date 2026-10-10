"""Small lossless timestamp editor: EXIF JPEG and ISO BMFF movie header dates.

Preserves image/video payload and unrelated metadata. It deliberately does not
claim to rewrite XMP, IPTC, GPS dates, maker notes, or vendor-specific timestamps.
"""
import datetime as dt
from pathlib import Path
import struct

EPOCH = dt.datetime(1904, 1, 1, tzinfo=dt.timezone.utc)
DATES = {0x9003: 'DateTimeOriginal', 0x9004: 'CreateDate', 0x9010: 'OffsetTime',
         0x9011: 'OffsetTimeOriginal', 0x9012: 'OffsetTimeDigitized'}
SIZES = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 6: 1, 7: 1, 8: 2, 9: 4, 10: 8, 11: 4, 12: 8, 13: 4}
WARNING = ('Built-in editor changes only EXIF date/offset fields in JPEG or movie/track/media '
           'header dates in MP4/MOV. XMP, IPTC, GPS, maker-note, vendor, and filesystem dates '
           'are preserved and may differ. No location fields are changed.')


def jpeg_segments(data):
    if not data.startswith(b'\xff\xd8'):
        raise ValueError('Not a JPEG file.')
    pos = 2
    segments = []
    saw_frame = False
    while pos < len(data):
        start = pos
        if data[pos] != 255:
            raise ValueError('Malformed JPEG segment.')
        while pos < len(data) and data[pos] == 255:
            pos += 1
        if pos == len(data):
            raise ValueError('Truncated JPEG marker.')
        marker = data[pos]
        pos += 1
        if marker == 0xD9:
            raise ValueError('JPEG has no image scan.')
        if marker == 0 or marker == 0xD8 or 0xD0 <= marker <= 0xD7:
            raise ValueError('Unexpected JPEG marker.')
        if marker == 1:
            continue
        if pos + 2 > len(data):
            raise ValueError('Truncated JPEG segment length.')
        length = int.from_bytes(data[pos:pos + 2], 'big')
        end = pos + length
        if length < 2 or end > len(data):
            raise ValueError('Invalid JPEG segment length.')
        if marker == 0xDA:
            if not saw_frame or length < 6 or data.rfind(b'\xff\xd9', end) < end:
                raise ValueError('JPEG scan header or end marker is missing.')
            components = data[pos + 2]
            if not components or length != 6 + 2 * components:
                raise ValueError('Malformed JPEG scan header.')
            return segments
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            if length < 11 or data[pos + 3:pos + 5] == b'\0\0' or data[pos + 5:pos + 7] == b'\0\0':
                raise ValueError('Malformed JPEG frame header.')
            saw_frame = True
        segments.append((marker, start, end, pos + 2))
        pos = end
    raise ValueError('JPEG has no scan or end marker.')


class Tiff:
    def __init__(self, data):
        self.data = bytearray(data)
        if len(data) < 8 or data[:2] not in (b'II', b'MM'):
            raise ValueError('Malformed EXIF byte order.')
        self.order = '<' if data[:2] == b'II' else '>'
        if self.unpack('H', 2)[0] != 42:
            raise ValueError('Unsupported EXIF TIFF header.')
        self.root_offset = self.unpack('I', 4)[0]
        self.root, self.root_next = self.ifd(self.root_offset)
        pointer = self.root.get(0x8769)
        if pointer is not None:
            if self.unpack_entry(pointer)[1:3] != (4, 1):
                raise ValueError('Invalid EXIF IFD pointer.')
            exif_offset = struct.unpack(self.order + 'I', pointer[8:12])[0]
            if exif_offset == self.root_offset:
                raise ValueError('Cyclic EXIF IFD pointer.')
            self.exif, self.exif_next = self.ifd(exif_offset)
        else:
            self.exif, self.exif_next = {}, 0
        # Validate referenced GPS/thumbnail directories while retaining their offsets.
        if self.root_next:
            self.ifd(self.root_next)
        if 0x8825 in self.root:
            raw = self.root[0x8825]
            if self.unpack_entry(raw)[1:3] != (4, 1):
                raise ValueError('Invalid GPS IFD pointer.')
            self.ifd(struct.unpack(self.order + 'I', raw[8:12])[0])

    def unpack(self, fmt, offset):
        size = struct.calcsize(fmt)
        if offset < 0 or offset + size > len(self.data):
            raise ValueError('EXIF offset outside its segment.')
        return struct.unpack_from(self.order + fmt, self.data, offset)

    def unpack_entry(self, raw):
        return struct.unpack(self.order + 'HHI4s', raw)

    def ifd(self, offset):
        if offset < 8:
            raise ValueError('Invalid EXIF directory offset.')
        count = self.unpack('H', offset)[0]
        if offset + 2 + 12 * count + 4 > len(self.data):
            raise ValueError('Truncated EXIF directory.')
        entries = {}
        for i in range(count):
            start = offset + 2 + i * 12
            raw = bytes(self.data[start:start + 12])
            tag, kind, number, value = self.unpack_entry(raw)
            if tag in entries:
                raise ValueError('Duplicate EXIF tag.')
            if kind not in SIZES:
                raise ValueError('Unsupported EXIF field type.')
            size = SIZES[kind] * number
            if size > 4:
                pointer = struct.unpack(self.order + 'I', value)[0]
                if pointer < 8 or pointer + size > len(self.data):
                    raise ValueError('EXIF field points outside its segment.')
            entries[tag] = raw
        nxt = self.unpack('I', offset + 2 + 12 * count)[0]
        if nxt and (nxt < 8 or nxt + 2 > len(self.data)):
            raise ValueError('Invalid next EXIF directory offset.')
        return entries, nxt

    def ascii(self, raw):
        _, kind, count, value = self.unpack_entry(raw)
        if kind != 2:
            return None
        if count > 4:
            offset = struct.unpack(self.order + 'I', value)[0]
            value = self.data[offset:offset + count]
        else:
            value = value[:count]
        return bytes(value).rstrip(b'\0').decode('ascii', errors='replace')

    def append_ascii(self, tag, value):
        raw = value.encode('ascii') + b'\0'
        if len(self.data) % 2:
            self.data.append(0)
        offset = len(self.data)
        self.data.extend(raw)
        return struct.pack(self.order + 'HHII', tag, 2, len(raw), offset)

    def append_ifd(self, entries, nxt):
        if len(self.data) % 2:
            self.data.append(0)
        offset = len(self.data)
        self.data.extend(struct.pack(self.order + 'H', len(entries)))
        for tag in sorted(entries):
            self.data.extend(entries[tag])
        self.data.extend(struct.pack(self.order + 'I', nxt))
        return offset

    def edit(self, when):
        wall = when.strftime('%Y:%m:%d %H:%M:%S')
        off = when.strftime('%z')
        off = off[:3] + ':' + off[3:]
        self.root[0x132] = self.append_ascii(0x132, wall)
        for tag in DATES:
            self.exif[tag] = self.append_ascii(tag, wall if tag in (0x9003, 0x9004) else off)
        for tag in (0x9290, 0x9291, 0x9292):
            self.exif.pop(tag, None)
        exif_offset = self.append_ifd(self.exif, self.exif_next)
        self.root[0x8769] = struct.pack(self.order + 'HHII', 0x8769, 4, 1, exif_offset)
        root_offset = self.append_ifd(self.root, self.root_next)
        struct.pack_into(self.order + 'I', self.data, 4, root_offset)
        return bytes(self.data)


def jpeg_exif(data):
    matches = [(start, end, body) for marker, start, end, body in jpeg_segments(data)
               if marker == 0xE1 and data[body:body + 6] == b'Exif\0\0']
    if len(matches) > 1:
        raise ValueError('Multiple EXIF segments are not supported.')
    if not matches:
        return None, None
    start, end, body = matches[0]
    return (start, end), Tiff(data[body + 6:end])


def boxes(data, start, end):
    pos = start
    while pos < end:
        if end - pos < 8:
            raise ValueError('Truncated movie atom.')
        size, kind = struct.unpack_from('>I4s', data, pos)
        header = 8
        if size == 1:
            if end - pos < 16:
                raise ValueError('Truncated extended movie atom.')
            size = struct.unpack_from('>Q', data, pos + 8)[0]
            header = 16
        elif size == 0:
            size = end - pos
        if size < header or pos + size > end:
            raise ValueError('Movie atom exceeds its parent.')
        yield kind, pos + header, pos + size
        pos += size


def movie_fields(data):
    fields = []
    track = 0
    found_moov = False
    kind = 'MP4'
    def header(atom, body, end, prefix):
        if end - body < 4:
            raise ValueError('Truncated movie header.')
        version = data[body]
        width = {0: 4, 1: 8}.get(version)
        if width is None or end - body < 4 + width * 2:
            raise ValueError('Unsupported or truncated movie timestamp header.')
        names = {b'mvhd': ('CreateDate', 'ModifyDate'),
                 b'tkhd': ('TrackCreateDate', 'TrackModifyDate'),
                 b'mdhd': ('MediaCreateDate', 'MediaModifyDate')}[atom]
        for i, name in enumerate(names):
            fields.append((prefix + ':' + name, body + 4 + i * width, width))
    for atom, body, end in boxes(data, 0, len(data)):
        if atom == b'ftyp':
            if end - body < 8:
                raise ValueError('Malformed movie file type atom.')
            if data[body:body + 4] == b'qt  ':
                kind = 'MOV'
        if atom != b'moov':
            continue
        if found_moov:
            raise ValueError('Multiple movie containers are not supported.')
        found_moov = True
        for child, cb, ce in boxes(data, body, end):
            if child == b'mvhd':
                header(child, cb, ce, 'QuickTime')
            elif child == b'trak':
                track += 1
                prefix = f'Track{track}'
                for sub, sb, se in boxes(data, cb, ce):
                    if sub == b'tkhd':
                        header(sub, sb, se, prefix)
                    elif sub == b'mdia':
                        for leaf, lb, le in boxes(data, sb, se):
                            if leaf == b'mdhd':
                                header(leaf, lb, le, prefix)
    if not found_moov or not any(key == 'QuickTime:CreateDate' for key, _, _ in fields):
        raise ValueError('No supported movie header found.')
    if len({key for key, _, _ in fields}) != len(fields):
        raise ValueError('Duplicate movie timestamp headers are not supported.')
    return kind, fields


def inspect(path):
    path = Path(path)
    data = path.read_bytes()
    if data.startswith(b'\xff\xd8'):
        _, tiff = jpeg_exif(data)
        result = {'File:FileType': 'JPEG', 'File:MIMEType': 'image/jpeg'}
        if tiff:
            if 0x132 in tiff.root:
                result['IFD0:ModifyDate'] = tiff.ascii(tiff.root[0x132])
            for tag, name in DATES.items():
                if tag in tiff.exif:
                    result['ExifIFD:' + name] = tiff.ascii(tiff.exif[tag])
        return result
    kind, fields = movie_fields(data)
    if path.suffix.lower() == '.mov':
        kind = 'MOV'
    result = {'File:FileType': kind, 'File:MIMEType': 'video/quicktime' if kind == 'MOV' else 'video/mp4'}
    for key, offset, width in fields:
        try:
            value = EPOCH + dt.timedelta(seconds=int.from_bytes(data[offset:offset + width], 'big'))
            result[key] = value.strftime('%Y:%m:%d %H:%M:%S+00:00')
        except OverflowError:
            raise ValueError('Movie timestamp is outside the supported calendar range.')
    return result


def edit(source, target, when):
    source, target = Path(source), Path(target)
    if source.resolve() == target.resolve():
        raise ValueError('Write to a separate output copy.')
    if when.tzinfo is None or when.utcoffset() is None:
        raise ValueError('Timestamp requires a timezone offset.')
    data = source.read_bytes()
    if data.startswith(b'\xff\xd8'):
        span, tiff = jpeg_exif(data)
        if tiff is None:
            tiff = Tiff(b'II\x2a\0\x08\0\0\0\0\0\0\0\0\0')
        payload = b'Exif\0\0' + tiff.edit(when)
        if len(payload) + 2 > 65535:
            raise ValueError('Updated EXIF exceeds the JPEG APP1 segment size limit.')
        segment = b'\xff\xe1' + struct.pack('>H', len(payload) + 2) + payload
        if span:
            start, end = span
        else:
            start = end = 2
            for marker, seg_start, seg_end, _ in jpeg_segments(data):
                if marker != 0xE0 or seg_start != end:
                    break
                start = end = seg_end
        output = data[:start] + segment + data[end:]
    else:
        _, fields = movie_fields(data)
        seconds = int((when.astimezone(dt.timezone.utc) - EPOCH).total_seconds())
        if seconds < 0:
            raise ValueError('Movie timestamps must be in 1904 or later.')
        output = bytearray(data)
        for _, offset, width in fields:
            if seconds >= 2 ** (8 * width):
                raise ValueError('Requested date exceeds this movie header version range (version 0 ends in 2040).')
            output[offset:offset + width] = seconds.to_bytes(width, 'big')
    target.write_bytes(output)
    return [WARNING]
