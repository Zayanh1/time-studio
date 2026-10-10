#!/usr/bin/env python3
"""Local, copy-only media timestamp editor with a dependency-free fallback."""
import argparse
import datetime as dt
import email.parser
import email.policy
import hashlib
import json
import mimetypes
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse
import builtin_metadata

ROOT = Path(__file__).resolve().parent
MAX_BYTES = 150 * 1024 * 1024
STORE = tempfile.TemporaryDirectory(prefix='time-studio-')
UPLOADS = {}
DOWNLOADS = {}
LOCK = threading.Lock()


def timestamp(value):
    if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d\d-\d\dT\d\d:\d\d(?::\d\d)?(?:Z|[+-]\d\d:\d\d)', value):
        raise ValueError('Use an ISO date and time with explicit offset, e.g. 2026-10-09T16:30:00-04:00.')
    if value[-1] != 'Z' and (int(value[-5:-3]) > 14 or int(value[-2:]) > 59):
        raise ValueError('Invalid timezone offset.')
    parsed = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
    if abs(parsed.utcoffset()) > dt.timedelta(hours=14):
        raise ValueError('Timezone offset must be between -14:00 and +14:00.')
    if parsed.year < 1904:
        raise ValueError('Choose a date in 1904 or later for media compatibility.')
    return parsed


def exiftool(args):
    executable = shutil.which(os.environ.get('EXIFTOOL', 'exiftool'))
    if not executable:
        raise ValueError('ExifTool is required. Install it, then restart this server.')
    result = subprocess.run([executable, *args], capture_output=True, text=True,
                            timeout=90, env={**os.environ, 'TZ': 'UTC'})
    if result.returncode:
        raise ValueError(result.stderr.strip() or 'ExifTool could not process this file.')
    return result


def metadata_engine():
    return 'exiftool' if shutil.which(os.environ.get('EXIFTOOL', 'exiftool')) else 'builtin'


def inspect(path):
    if metadata_engine() == 'builtin':
        return builtin_metadata.inspect(path)
    data = json.loads(exiftool(['-j', '-G1', '-s', '-api', 'QuickTimeUTC=1',
                               '-time:all', '-FileType', '-MIMEType', str(path)]).stdout)[0]
    return {k: v for k, v in data.items() if k != 'SourceFile'}


def write_args(kind, when):
    wall = when.strftime('%Y:%m:%d %H:%M:%S')
    off = when.strftime('%z')
    off = off[:3] + ':' + off[3:]
    if kind == 'JPEG':
        return [f'-EXIF:{tag}={wall}' for tag in ('DateTimeOriginal', 'CreateDate', 'ModifyDate')] + [
            f'-EXIF:{tag}={off}' for tag in ('OffsetTimeOriginal', 'OffsetTimeDigitized', 'OffsetTime')
        ] + [f'-EXIF:{tag}=' for tag in ('SubSecTimeOriginal', 'SubSecTimeDigitized', 'SubSecTime')]
    return [f'-QuickTime:{tag}={wall}{off}' for tag in (
        'CreateDate', 'ModifyDate', 'TrackCreateDate', 'TrackModifyDate', 'MediaCreateDate', 'MediaModifyDate'
    )] + [f'-Keys:CreationDate={wall}{off}']


def digest(path):
    result = hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            result.update(chunk)
    return result.hexdigest()


def export_copy(upload, value):
    when = timestamp(value)
    source = Path(upload['path'])
    before = inspect(source)
    kind = before.get('File:FileType')
    if kind not in ('JPEG', 'MP4', 'MOV'):
        raise ValueError('Timestamp export supports JPEG, MP4, and MOV only.')
    token = secrets.token_urlsafe(24)
    folder = Path(STORE.name) / token
    folder.mkdir()
    target = folder / ('corrected' + source.suffix)
    engine = metadata_engine()
    engine_warnings = []
    if engine == 'builtin':
        engine_warnings = builtin_metadata.edit(source, target, when)
    else:
        shutil.copy2(source, target)
        result = exiftool(['-overwrite_original', '-api', 'QuickTimeUTC=1', *write_args(kind, when), str(target)])
        if result.stderr.strip():
            engine_warnings.append(result.stderr.strip())
    after = inspect(target)
    changed = {k: {'before': before.get(k), 'after': after.get(k)} for k in before.keys() | after.keys()
               if before.get(k) != after.get(k)}
    if kind == 'JPEG':
        expected = {f'{group}:{tag}': when.strftime('%Y:%m:%d %H:%M:%S') for group, tag in (
            ('ExifIFD', 'DateTimeOriginal'), ('ExifIFD', 'CreateDate'), ('IFD0', 'ModifyDate'))}
        off = when.strftime('%z')
        off = off[:3] + ':' + off[3:]
        expected.update({f'ExifIFD:{tag}': off for tag in (
            'OffsetTimeOriginal', 'OffsetTimeDigitized', 'OffsetTime')})
    else:
        utc = when.astimezone(dt.timezone.utc).strftime('%Y:%m:%d %H:%M:%S+00:00')
        expected = {f'QuickTime:{tag}': utc for tag in ('CreateDate', 'ModifyDate')}
        for key in before.keys() | after.keys():
            if key.split(':')[-1] in ('TrackCreateDate', 'TrackModifyDate', 'MediaCreateDate', 'MediaModifyDate'):
                expected[key] = utc
        off = when.strftime('%z')
        if engine == 'exiftool':
            expected['Keys:CreationDate'] = when.strftime('%Y:%m:%d %H:%M:%S') + off[:3] + ':' + off[3:]
    verification = {k: {'expected': v, 'actual': after.get(k), 'matches': after.get(k) == v}
                    for k, v in expected.items()}
    warnings = ['Only the listed supported metadata fields are edited. Other embedded dates, filesystem dates, and cloud records may differ.',
                'Metadata is editable and does not establish attendance, time, or location. Location metadata was not changed.']
    warnings.extend(engine_warnings)
    if not all(v['matches'] for v in verification.values()):
        warnings.append('One or more primary timestamp fields did not match after writing. Review verification before using the copy.')
    report = {'filename': upload['filename'], 'output_filename': target.name,
              'engine': engine, 'supported_fields': sorted(expected),
              'verification_passed': all(v['matches'] for v in verification.values()),
              'requested_timestamp': when.isoformat(),
              'created_at': dt.datetime.now(dt.timezone.utc).isoformat(),
              'original_sha256': digest(source), 'output_sha256': digest(target),
              'before': before, 'after': after, 'changed': changed,
              'verification': verification, 'warnings': warnings}
    report_path = folder / 'timestamp-report.json'
    report_path.write_text(json.dumps(report, indent=2), encoding='utf-8')
    with LOCK:
        DOWNLOADS[token] = {'media': target, 'report': report_path}
    return {**report, 'download_url': f'/api/download/{token}/media',
            'report_url': f'/api/download/{token}/report'}


class Handler(BaseHTTPRequestHandler):
    def reply(self, status, data):
        payload = json.dumps(data).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(payload)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(payload)

    def valid_host(self):
        return self.headers.get('Host') in (f'127.0.0.1:{self.server.server_port}', f'localhost:{self.server.server_port}')

    def do_GET(self):
        if not self.valid_host():
            return self.reply(403, {'error': 'Localhost requests only.'})
        path = urlparse(self.path).path
        if path == '/api/status':
            return self.reply(200, {'exiftool': bool(shutil.which(os.environ.get('EXIFTOOL', 'exiftool'))),
                                    'engine': metadata_engine(), 'metadata_available': True,
                                    'max_bytes': MAX_BYTES})
        match = re.fullmatch(r'/api/download/([A-Za-z0-9_-]+)/(media|report)', path)
        if match:
            with LOCK:
                file = DOWNLOADS.get(match[1], {}).get(match[2])
            if not file:
                return self.reply(404, {'error': 'Download expired or unavailable.'})
            return self.file(file, attachment=True)
        if path in ('/', '/index.html'):
            return self.file(ROOT / 'index.html')
        return self.reply(404, {'error': 'Not found.'})

    def file(self, path, attachment=False):
        if not path.is_file():
            return self.reply(404, {'error': 'File not found.'})
        self.send_response(200)
        self.send_header('Content-Type', mimetypes.guess_type(path)[0] or 'application/octet-stream')
        self.send_header('Content-Length', str(path.stat().st_size))
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Cache-Control', 'no-store')
        if attachment:
            self.send_header('Content-Disposition', f'attachment; filename="{path.name}"')
        self.end_headers()
        with path.open('rb') as stream:
            shutil.copyfileobj(stream, self.wfile)

    def do_POST(self):
        origin = self.headers.get('Origin')
        if not self.valid_host() or (origin and origin not in (
                f'http://localhost:{self.server.server_port}', f'http://127.0.0.1:{self.server.server_port}')):
            return self.reply(403, {'error': 'Localhost requests only.'})
        try:
            size = int(self.headers.get('Content-Length', '0'))
            if size <= 0 or size > MAX_BYTES + 1024 * 1024:
                return self.reply(413, {'error': 'Maximum upload size is 150 MB.'})
            body = self.rfile.read(size)
            if len(body) != size:
                raise ValueError('Incomplete upload.')
            path = urlparse(self.path).path
            if path == '/api/inspect':
                content_type = self.headers.get('Content-Type', '')
                if not content_type.startswith('multipart/form-data;'):
                    raise ValueError('Upload a file using multipart/form-data.')
                message = email.parser.BytesParser(policy=email.policy.default).parsebytes(
                    f'Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n'.encode() + body)
                parts = [p for p in message.iter_parts() if p.get_param('name', header='content-disposition') == 'file']
                if len(parts) != 1:
                    raise ValueError('Upload exactly one file.')
                filename = Path(parts[0].get_filename() or 'upload').name
                payload = parts[0].get_payload(decode=True)
                if not payload or len(payload) > MAX_BYTES:
                    raise ValueError('File must be nonempty and no larger than 150 MB.')
                suffix = Path(filename).suffix.lower()
                if suffix not in ('.jpg', '.jpeg', '.mp4', '.mov'):
                    raise ValueError('Choose a JPEG, MP4, or MOV file.')
                token = secrets.token_urlsafe(24)
                destination = Path(STORE.name) / (token + suffix)
                destination.write_bytes(payload)
                try:
                    metadata = inspect(destination)
                    if metadata.get('File:FileType') not in ('JPEG', 'MP4', 'MOV'):
                        raise ValueError('The file contents are not a supported media format.')
                except Exception:
                    destination.unlink(missing_ok=True)
                    raise
                with LOCK:
                    UPLOADS[token] = {'path': str(destination), 'filename': filename}
                return self.reply(200, {'upload_id': token, 'filename': filename, 'metadata': metadata,
                                        'engine': metadata_engine(), 'supported': True})
            if path == '/api/export':
                data = json.loads(body)
                if not isinstance(data, dict):
                    raise ValueError('Expected a JSON object.')
                with LOCK:
                    upload = UPLOADS.get(str(data.get('upload_id', '')))
                if not upload:
                    raise ValueError('Upload and inspect a file first.')
                return self.reply(200, export_copy(upload, data.get('timestamp')))
            return self.reply(404, {'error': 'Not found.'})
        except (ValueError, subprocess.TimeoutExpired) as error:
            self.reply(400, {'error': str(error)})
        except Exception:
            self.reply(500, {'error': 'Could not process this file.'})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8765)
    args = parser.parse_args()
    server = ThreadingHTTPServer(('127.0.0.1', args.port), Handler)
    print(f'Time Studio: http://127.0.0.1:{args.port}', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        STORE.cleanup()


if __name__ == '__main__':
    main()
