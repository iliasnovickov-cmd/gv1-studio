# GV1 Local Helper

Python 3.11 loopback service used by the GitHub Pages frontend. It accepts an MP4 or an explicitly authorized YouTube URL, performs native FFmpeg conversion, atomically writes the firmware's exact GV1 container, and can stream that file to an ESP32 media player.

The service never binds a LAN/public interface: the supplied runner always uses **127.0.0.1**. Browser origins are restricted to GitHub Pages, configured exact origins, and localhost development origins.

## Install and run

FFmpeg and FFprobe must be installed and available on **PATH** (or selected with **GV1_FFMPEG** / **GV1_FFPROBE**).

~~~powershell
cd gv1-converter-web/helper
py -3.11 -m venv .venv
.venv/Scripts/Activate.ps1
python -m pip install -r requirements.txt
python run.py --port 8765
~~~

Equivalent module entry point: **python -m gv1_helper --port 8765**.

Runtime work and results default to **helper/var/**; successful results expire after 24 hours. Relevant environment variables:

| Variable | Default | Meaning |
|---|---:|---|
| GV1_MAX_INPUT_BYTES | 2147483648 | MP4/download byte cap (maximum 4 GiB) |
| GV1_MAX_DURATION_SECONDS | 7200 | media duration cap (hard maximum 8332) |
| GV1_WORKERS | 1 | concurrent conversion workers (1–4) |
| GV1_RESULT_TTL_SECONDS | 86400 | completed job retention |
| GV1_ALLOWED_ORIGINS | empty | comma-separated additional exact browser origins |
| GV1_FFMPEG, GV1_FFPROBE | executable names | explicit native tool paths |

## Exact output format

All conversions use:

- independent MJPEG frames, 320×180, CFR 24/1 fps;
- Lanczos aspect-preserving scale and black letterbox/pillarbox padding;
- FFmpeg quality fallback until **every JPEG is at most 32 KiB**;
- PCM signed 16-bit little-endian, 48 kHz, stereo;
- silence padding or truncation to exactly **frames × 8000** audio bytes;
- a 32-byte little-endian header followed by 8-byte frame index entries, audio, then contiguous JPEGs;
- fsync plus same-directory atomic replace;
- streamed CRC32 and SHA-256 over the complete GV1 file.

Header layout (**<4sHHHHIIHHII**): **GV1\0**, width, height, fps numerator, fps denominator, frame count, audio rate, channel count, sample bits, audio byte count, audio offset. Each index entry is **<II**: absolute JPEG offset and JPEG size. Files and offsets are strictly below 4 GiB.

## API contract

Base URL: **http://127.0.0.1:8765**.

### GET /api/health

Returns tool availability, limits, version, and format details. **ok** is false if FFmpeg, FFprobe, or yt-dlp is unavailable.

### POST /api/jobs/file

**multipart/form-data** fields:

- **file** — required .mp4 file;
- **auto_send** — optional boolean, default false;
- **player_url** — required when auto_send=true;
- **output_name** — optional firmware-safe name; .gv1 is appended.

Returns HTTP 202 with a job object after the upload has been safely staged.

### POST /api/jobs/youtube

JSON:

~~~json
{
  "url": "https://www.youtube.com/watch?v=...",
  "authorized": true,
  "auto_send": false,
  "player_url": null,
  "output_name": "movie.gv1"
}
~~~

**authorized** is mandatory and must be true: it is the caller's explicit confirmation that they own the media or have permission to download it. The helper does not accept cookies, credentials, arbitrary extractor URLs, playlists, or live streams and does not bypass DRM. Only exact canonical HTTPS YouTube/youtu.be hosts pass validation.

### GET /api/jobs/{id}

Returns current asynchronous state.

### GET /api/jobs/{id}/download

Downloads a completed GV1. Response headers include **X-CRC32**, **X-SHA256**, and an SHA-256 **ETag**.

### POST /api/jobs/{id}/send

JSON: **{"player_url":"http://192.168.1.54"}**. Returns HTTP 202 and streams the output asynchronously to:

**POST {player_url}/api/upload?name=<encoded-name>&crc=<8-hex-CRC32>**

The body is streamed with a fixed **Content-Length**; progress appears on the same job. Redirects and environment proxies are disabled. For SSRF resistance, **player_url** must be a literal RFC1918/link-local HTTP address with no path, query, credentials, or fragment.

### DELETE /api/jobs/{id}

Requests cancellation and returns HTTP 202. FFmpeg/yt-dlp processes are terminated and partial output is removed. A terminal job returns 409.

### Job JSON

Every job response includes these frontend fields (nullable before output exists):

~~~json
{
  "id": "hex id",
  "status": "queued|running|sending|cancelling|completed|failed|cancelled",
  "stage": "queued|downloading|probing|frames|audio|packaging|sending|ready|failed|cancelled",
  "progress": 0.0,
  "message": "human readable state",
  "error": null,
  "output_name": "movie.gv1",
  "size": null,
  "crc32": null,
  "sha256": null,
  "download_url": null
}
~~~

The object additionally includes timestamps and, when ready, frame count, duration, and largest JPEG size. Conversion success is retained even if an automatic/manual ESP32 send fails: **status=completed**, **stage=send_failed**, and **error** describes the transfer failure so it can be retried.

## Browser access, CORS, and PNA

The middleware allows **https://<account>.github.io**, exact origins from **GV1_ALLOWED_ORIGINS**, and localhost development origins. It rejects every other browser **Origin**. OPTIONS preflights include **Access-Control-Allow-Private-Network: true** when Chromium requests PNA access and expose download/checksum headers. Requests without an Origin (CLI/native clients) remain usable. Host headers are restricted to loopback names to reduce DNS-rebinding risk.

## Tests

~~~powershell
python -m pip install -r requirements-dev.txt
pytest
~~~

Tests cover URL/name/SSRF checks, CORS/PNA behavior, exact GV1 layout/checksums/atomicity, API shape, and a local FFmpeg integration conversion.
