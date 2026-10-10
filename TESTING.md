# Testing & Validation

The app runs with Python 3.10+ and no third-party runtime dependencies. ExifTool is optional. Pillow and FFmpeg/ffprobe are used only for independent test fixtures and verification.

## Test suite coverage

- Clock logic: pause/resume continuity, catching up after suspension, crossing midnight, explicit form timestamp for exports, leap-day validation, invalid UTC offsets, 12/24-hour display and seconds. The test executes the actual frontend script in a minimal DOM harness.
- Real JPEG upload, inspection, export, download, and JSON-report workflow through the HTTP API with ExifTool absent. Independent Pillow readback confirmed DateTimeOriginal and OffsetTimeOriginal. Source bytes and decoded pixels were unchanged.
- JPEG roundtrips with and without existing EXIF, including preserved orientation, GPS entries, camera data and a maker-note value. Encoded image data outside the EXIF segment was byte-identical. Duplicate EXIF, malformed offsets, and invalid headers are rejected.
- Big-endian TIFF preservation of the next-directory pointer and unrelated entries.
- Real FFmpeg-generated MP4 and MOV edits. Independent ffprobe readback confirmed movie and stream creation times in UTC. Only the intended fixed-size timestamp bytes changed; media-data hashes and original source files were unchanged.
- Both video formats exported through the server and verified all six time fields in each single-track sample.
- Version 1 / extended-size movie atoms and rejection of version 0 date overflow.
- Server date validation, mismatch reporting, original preservation, status responses, and cross-origin rejection.

## Run checks

```sh
python3 -m unittest -v
node test_clock.cjs
```

Node.js is required only for the clock test harness. Independent JPEG tests require Pillow; independent video tests require FFmpeg and ffprobe. Tests with unavailable fixture tools skip rather than adding an app runtime dependency.

## Notes & verification limits

- **Browser & UI layout:** The DOM test harness validates frontend clock logic and state transitions (`test_clock.cjs`), but responsive visual layout and mobile browser fullscreen APIs should be validated across target physical devices (e.g., mobile Safari / Chrome).
- **ExifTool integration:** If ExifTool is installed on PATH, the server utilizes it automatically for expanded tag coverage; otherwise the built-in pure-Python fallback handles standard EXIF/QuickTime edits.
- **Proprietary metadata handling:** The sample tests do not guarantee compatibility with every camera's proprietary metadata. The built-in JPEG writer preserves unknown TIFF entries and existing relative offsets, but rejects EXIF segments exceeding maximum size limits. Always retain original files.
- **Scope of edits:** JPEG EXIF original/create/modify dates and explicit UTC offsets (active fractional-second fields removed); video numeric movie/track/media creation/modification times in UTC. Existing XMP, IPTC, textual QuickTime/Keys, manufacturer tags, GPS, filesystem timestamps, and cloud upload dates remain distinct and outside the built-in writer's scope.

