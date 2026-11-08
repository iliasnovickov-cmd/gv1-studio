from __future__ import annotations

import hashlib
from pathlib import Path
import struct
import zlib

import pytest

from gv1_helper.errors import JobCancelled, ValidationFailure
from gv1_helper.gv1 import (
    AUDIO_BYTES_PER_VIDEO_FRAME,
    ENTRY,
    HEADER,
    HEADER_SIZE,
    JPEG_MAX_BYTES,
    pack_gv1,
    validate_gv1,
)


class Flag:
    def __init__(self, value: bool = False) -> None:
        self.value = value

    def is_set(self) -> bool:
        return self.value


def frame_bytes(payload_size: int, fill: int) -> bytes:
    return b"\xff\xd8" + bytes([fill]) * payload_size + b"\xff\xd9"


def test_pack_exact_layout_checksums_and_atomic_commit(tmp_path: Path) -> None:
    frames = []
    for index, payload in enumerate((19, 37, 61), start=1):
        path = tmp_path / f"{index:08d}.jpg"
        path.write_bytes(frame_bytes(payload, index))
        frames.append(path)
    audio = tmp_path / "audio.pcm"
    audio.write_bytes(b"\x01\x02\x03\x04" * 17)
    output = tmp_path / "result.gv1"
    progress: list[float] = []

    info = pack_gv1(frames, audio, output, Flag(), progress.append)

    data = output.read_bytes()
    assert len(data) == info.size
    assert info.crc32 == f"{zlib.crc32(data) & 0xFFFFFFFF:08X}"
    assert info.sha256 == hashlib.sha256(data).hexdigest().upper()
    assert not list(tmp_path.glob("*.part"))
    (
        magic,
        width,
        height,
        fps_num,
        fps_den,
        count,
        rate,
        channels,
        bits,
        audio_bytes,
        audio_offset,
    ) = HEADER.unpack_from(data)
    assert (magic, width, height, fps_num, fps_den) == (b"GV1\0", 320, 180, 24, 1)
    assert (count, rate, channels, bits) == (3, 48_000, 2, 16)
    assert audio_bytes == 3 * AUDIO_BYTES_PER_VIDEO_FRAME
    assert audio_offset == HEADER_SIZE + 3 * ENTRY.size

    entries = [ENTRY.unpack_from(data, HEADER_SIZE + i * ENTRY.size) for i in range(3)]
    first_jpeg = audio_offset + audio_bytes
    assert entries[0][0] == first_jpeg
    assert entries[1][0] == entries[0][0] + entries[0][1]
    assert entries[2][0] == entries[1][0] + entries[1][1]
    audio_payload = data[audio_offset:first_jpeg]
    assert audio_payload[: audio.stat().st_size] == audio.read_bytes()
    assert set(audio_payload[audio.stat().st_size :]) == {0}
    assert progress[-1] == 100.0

    validated = validate_gv1(output)
    assert validated == info


def test_pack_rejects_oversized_jpeg_without_output(tmp_path: Path) -> None:
    frame = tmp_path / "frame.jpg"
    frame.write_bytes(frame_bytes(JPEG_MAX_BYTES, 1))
    with pytest.raises(ValidationFailure, match="allowed range"):
        pack_gv1([frame], tmp_path / "none.pcm", tmp_path / "bad.gv1", Flag())
    assert not (tmp_path / "bad.gv1").exists()


def test_pack_honors_cancellation(tmp_path: Path) -> None:
    frame = tmp_path / "frame.jpg"
    frame.write_bytes(frame_bytes(10, 1))
    with pytest.raises(JobCancelled):
        pack_gv1([frame], tmp_path / "none.pcm", tmp_path / "cancelled.gv1", Flag(True))
    assert not (tmp_path / "cancelled.gv1").exists()


def test_validator_rejects_wrong_fps(tmp_path: Path) -> None:
    frame = tmp_path / "frame.jpg"
    frame.write_bytes(frame_bytes(10, 1))
    output = tmp_path / "file.gv1"
    pack_gv1([frame], tmp_path / "none.pcm", output, Flag())
    data = bytearray(output.read_bytes())
    struct.pack_into("<H", data, 8, 25)
    output.write_bytes(data)
    with pytest.raises(ValidationFailure, match="24/1"):
        validate_gv1(output)
