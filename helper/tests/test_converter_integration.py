from __future__ import annotations

import asyncio
from pathlib import Path
import shutil
import struct
import subprocess

import pytest

from gv1_helper.config import Settings
from gv1_helper.converter import Converter
from gv1_helper.gv1 import ENTRY, HEADER_SIZE, validate_gv1


def jpeg_dimensions(data: bytes) -> tuple[int, int]:
    assert data[:2] == b"\xff\xd8"
    position = 2
    sof_markers = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
    while position + 4 <= len(data):
        if data[position] != 0xFF:
            position += 1
            continue
        marker = data[position + 1]
        position += 2
        if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
            continue
        length = struct.unpack_from(">H", data, position)[0]
        if marker in sof_markers:
            height, width = struct.unpack_from(">HH", data, position + 3)
            return width, height
        position += length
    raise AssertionError("JPEG SOF marker not found")


@pytest.mark.integration
def test_native_ffmpeg_conversion_is_exact(tmp_path: Path) -> None:
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        pytest.skip("FFmpeg/FFprobe not installed")
    source = tmp_path / "source.mp4"
    subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=1280x720:rate=30",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=44100",
            "-t",
            "0.5",
            "-shortest",
            "-c:v",
            "mpeg4",
            "-q:v",
            "8",
            "-c:a",
            "aac",
            str(source),
        ],
        check=True,
    )
    settings = Settings(
        data_dir=tmp_path / "var",
        ffmpeg=ffmpeg,
        ffprobe=ffprobe,
        max_input_bytes=10 * 1024 * 1024,
        max_duration_seconds=60,
        result_ttl_seconds=300,
    )
    converter = Converter(settings)
    work = tmp_path / "work"
    work.mkdir()
    output = tmp_path / "output.gv1"
    reports: list[tuple[str, float, str]] = []

    result = asyncio.run(
        converter.convert(
            source,
            output,
            work,
            asyncio.Event(),
            lambda stage, progress, message: reports.append((stage, progress, message)),
            require_mp4=True,
        )
    )
    validated = validate_gv1(output)
    assert validated == result
    assert 10 <= result.frames <= 13
    assert result.largest_jpeg <= 32 * 1024
    assert reports[-1][0] == "ready"

    data = output.read_bytes()
    frame_offset, frame_size = ENTRY.unpack_from(data, HEADER_SIZE)
    assert jpeg_dimensions(data[frame_offset : frame_offset + frame_size]) == (320, 180)
