"""Native FFmpeg media normalization and GV1 packaging."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
import math
from pathlib import Path
import re
import shutil
from typing import Callable

from .config import Settings
from .errors import JobCancelled, ValidationFailure
from .gv1 import GV1Info, JPEG_MAX_BYTES, MAX_FRAMES, pack_gv1
from .process import run_process

ProgressReporter = Callable[[str, float, str], None]


@dataclass(frozen=True, slots=True)
class MediaInfo:
    duration: float
    has_audio: bool
    format_name: str


class Converter:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.ffmpeg = settings.resolve_program(settings.ffmpeg)
        self.ffprobe = settings.resolve_program(settings.ffprobe)

    def require_tools(self) -> tuple[str, str]:
        if not self.ffmpeg:
            raise ValidationFailure("FFmpeg is not installed or GV1_FFMPEG is invalid")
        if not self.ffprobe:
            raise ValidationFailure("FFprobe is not installed or GV1_FFPROBE is invalid")
        return self.ffmpeg, self.ffprobe

    async def probe(
        self,
        source: Path,
        cancel: asyncio.Event,
        *,
        require_mp4: bool,
    ) -> MediaInfo:
        _, ffprobe = self.require_tools()
        if not source.is_file() or source.stat().st_size <= 0:
            raise ValidationFailure("input media is empty or missing")
        if source.stat().st_size > self.settings.max_input_bytes:
            raise ValidationFailure("input media exceeds the configured size limit")
        result = await run_process(
            [
                ffprobe,
                "-v",
                "error",
                "-show_entries",
                "format=duration,format_name:stream=codec_type,duration",
                "-of",
                "json",
                str(source),
            ],
            cancel,
        )
        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise ValidationFailure("FFprobe returned malformed media metadata") from exc
        streams = data.get("streams") or []
        if not any(item.get("codec_type") == "video" for item in streams):
            raise ValidationFailure("input has no video stream")
        format_data = data.get("format") or {}
        format_name = str(format_data.get("format_name") or "")
        if require_mp4 and not ({"mov", "mp4"} & set(format_name.split(","))):
            raise ValidationFailure("uploaded file is not an MP4 container")
        duration_values: list[float] = []
        for raw in [format_data.get("duration"), *(item.get("duration") for item in streams)]:
            try:
                value = float(raw)
            except (TypeError, ValueError):
                continue
            if math.isfinite(value) and value > 0:
                duration_values.append(value)
        if not duration_values:
            raise ValidationFailure("input duration is unavailable or zero")
        duration = max(duration_values)
        if duration > self.settings.max_duration_seconds:
            raise ValidationFailure(
                f"input is {duration:.1f}s; configured maximum is {self.settings.max_duration_seconds}s"
            )
        return MediaInfo(
            duration=duration,
            has_audio=any(item.get("codec_type") == "audio" for item in streams),
            format_name=format_name,
        )

    async def convert(
        self,
        source: Path,
        output: Path,
        work_dir: Path,
        cancel: asyncio.Event,
        report: ProgressReporter,
        *,
        require_mp4: bool,
    ) -> GV1Info:
        ffmpeg, _ = self.require_tools()
        report("probing", 1.0, "Inspecting input media")
        media = await self.probe(source, cancel, require_mp4=require_mp4)
        if cancel.is_set():
            raise JobCancelled("Job cancelled")
        report("frames", 4.0, "Encoding 320x180 MJPEG at 24 fps")

        scratch = work_dir / "conversion"
        frames_dir = scratch / "frames"
        audio_path = scratch / "audio.pcm"
        await asyncio.to_thread(shutil.rmtree, scratch, True)
        frames_dir.mkdir(parents=True, exist_ok=True)
        frame_pattern = frames_dir / "%08d.jpg"
        expected_frames = max(1, int(math.ceil(media.duration * 24.0)))
        quality_steps = (5, 7, 9, 12, 15, 19, 23, 27, 31)
        selected_frames: list[Path] = []

        try:
            for attempt, quality in enumerate(quality_steps):
                if cancel.is_set():
                    raise JobCancelled("Job cancelled")
                if attempt:
                    await asyncio.to_thread(shutil.rmtree, frames_dir, True)
                    frames_dir.mkdir(parents=True, exist_ok=True)
                    report(
                        "frames",
                        5.0,
                        f"Retrying JPEG quality q={quality} to satisfy the 32 KiB frame limit",
                    )

                def video_line(source_name: str, line: str) -> None:
                    if source_name != "stdout" or not line.startswith("frame="):
                        return
                    try:
                        count = int(line.partition("=")[2])
                    except ValueError:
                        return
                    fraction = min(1.0, count / expected_frames)
                    report("frames", 5.0 + 58.0 * fraction, f"Encoding video frames (q={quality})")

                video_filter = (
                    "fps=24,scale=320:180:force_original_aspect_ratio=decrease:flags=lanczos,"
                    "pad=320:180:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1"
                )
                await run_process(
                    [
                        ffmpeg,
                        "-hide_banner",
                        "-loglevel",
                        "error",
                        "-nostdin",
                        "-y",
                        "-progress",
                        "pipe:1",
                        "-nostats",
                        "-i",
                        str(source),
                        "-map",
                        "0:v:0",
                        "-an",
                        "-vf",
                        video_filter,
                        "-c:v",
                        "mjpeg",
                        "-pix_fmt",
                        "yuvj420p",
                        "-q:v",
                        str(quality),
                        str(frame_pattern),
                    ],
                    cancel,
                    line_handler=video_line,
                )
                selected_frames = sorted(frames_dir.glob("*.jpg"))
                if not selected_frames:
                    raise ValidationFailure("FFmpeg produced no video frames")
                if len(selected_frames) > MAX_FRAMES:
                    raise ValidationFailure(f"video produced more than {MAX_FRAMES} frames")
                largest = max(frame.stat().st_size for frame in selected_frames)
                if largest <= JPEG_MAX_BYTES:
                    break
                if quality == quality_steps[-1]:
                    raise ValidationFailure(
                        f"largest JPEG is {largest} bytes even at q={quality}; limit is {JPEG_MAX_BYTES}"
                    )
            report("audio", 65.0, "Converting audio to PCM s16le 48 kHz stereo")

            if media.has_audio:
                time_pattern = re.compile(r"^out_time_(?:us|ms)=([0-9]+)$")

                def audio_line(source_name: str, line: str) -> None:
                    if source_name != "stdout":
                        return
                    match = time_pattern.match(line)
                    if not match:
                        return
                    # Current FFmpeg reports out_time_us; older builds used the
                    # misleading out_time_ms key for the same microsecond value.
                    seconds = int(match.group(1)) / 1_000_000.0
                    fraction = min(1.0, seconds / media.duration)
                    report("audio", 65.0 + 12.0 * fraction, "Converting audio")

                await run_process(
                    [
                        ffmpeg,
                        "-hide_banner",
                        "-loglevel",
                        "error",
                        "-nostdin",
                        "-y",
                        "-progress",
                        "pipe:1",
                        "-nostats",
                        "-i",
                        str(source),
                        "-map",
                        "0:a:0",
                        "-vn",
                        "-ac",
                        "2",
                        "-ar",
                        "48000",
                        "-c:a",
                        "pcm_s16le",
                        "-f",
                        "s16le",
                        str(audio_path),
                    ],
                    cancel,
                    line_handler=audio_line,
                )
            else:
                audio_path.parent.mkdir(parents=True, exist_ok=True)
                audio_path.write_bytes(b"")
                report("audio", 77.0, "Input has no audio; inserting silence")

            report("packaging", 78.0, "Writing indexed GV1 container")
            loop = asyncio.get_running_loop()

            def package_progress(percent: float) -> None:
                loop.call_soon_threadsafe(
                    report,
                    "packaging",
                    78.0 + 22.0 * percent / 100.0,
                    "Writing and checksumming GV1",
                )

            result = await asyncio.to_thread(
                pack_gv1,
                selected_frames,
                audio_path,
                output,
                cancel,
                package_progress,
            )
            report("ready", 100.0, "GV1 conversion complete")
            return result
        finally:
            await asyncio.to_thread(shutil.rmtree, scratch, True)
