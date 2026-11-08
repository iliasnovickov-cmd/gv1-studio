"""GV1 container writer and strict structural validator."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import struct
from typing import Callable, Protocol, Sequence
import uuid
import zlib

from .errors import JobCancelled, ValidationFailure

WIDTH = 320
HEIGHT = 180
FPS_NUM = 24
FPS_DEN = 1
AUDIO_RATE = 48_000
AUDIO_CHANNELS = 2
AUDIO_BITS = 16
AUDIO_FRAME_BYTES = AUDIO_CHANNELS * (AUDIO_BITS // 8)
AUDIO_BYTES_PER_VIDEO_FRAME = AUDIO_RATE * AUDIO_FRAME_BYTES * FPS_DEN // FPS_NUM
JPEG_MAX_BYTES = 32 * 1024
HEADER_SIZE = 32
MAX_FRAMES = 199_999
UINT32_LIMIT = 1 << 32
HEADER = struct.Struct("<4sHHHHIIHHII")
ENTRY = struct.Struct("<II")


class CancellationFlag(Protocol):
    def is_set(self) -> bool: ...


@dataclass(frozen=True, slots=True)
class GV1Info:
    path: Path
    size: int
    frames: int
    duration_seconds: float
    audio_bytes: int
    largest_jpeg: int
    crc32: str
    sha256: str


def _check_cancel(cancel: CancellationFlag) -> None:
    if cancel.is_set():
        raise JobCancelled("Job cancelled")


def _frame_size(path: Path) -> int:
    size = path.stat().st_size
    if not 4 <= size <= JPEG_MAX_BYTES:
        raise ValidationFailure(
            f"JPEG frame {path.name} is {size} bytes; allowed range is 4..{JPEG_MAX_BYTES}"
        )
    with path.open("rb") as stream:
        if stream.read(2) != b"\xff\xd8":
            raise ValidationFailure(f"frame {path.name} is not a JPEG")
        stream.seek(-2, os.SEEK_END)
        if stream.read(2) != b"\xff\xd9":
            raise ValidationFailure(f"frame {path.name} has no JPEG end marker")
    return size


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def pack_gv1(
    frames: Sequence[Path],
    audio_path: Path,
    output_path: Path,
    cancel: CancellationFlag,
    progress: Callable[[float], None] | None = None,
) -> GV1Info:
    """Build and atomically commit a GV1 file.

    Audio is truncated or zero-padded to the exact duration implied by the
    24-fps frame count. Checksums cover every byte of the committed file.
    """
    if not frames or len(frames) > MAX_FRAMES:
        raise ValidationFailure(f"frame count must be between 1 and {MAX_FRAMES}")
    _check_cancel(cancel)
    sizes: list[int] = []
    for index, frame in enumerate(frames):
        _check_cancel(cancel)
        sizes.append(_frame_size(frame))
        if progress and (index % 128 == 0 or index + 1 == len(frames)):
            progress(5.0 * (index + 1) / len(frames))

    frame_count = len(frames)
    audio_bytes = frame_count * AUDIO_BYTES_PER_VIDEO_FRAME
    audio_offset = HEADER_SIZE + frame_count * ENTRY.size
    first_frame = audio_offset + audio_bytes
    total_size = first_frame + sum(sizes)
    if audio_bytes >= UINT32_LIMIT or total_size >= UINT32_LIMIT:
        raise ValidationFailure("GV1 exceeds the format's 32-bit size/offset limit")

    offsets: list[int] = []
    position = first_frame
    for size in sizes:
        offsets.append(position)
        position += size

    output_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = output_path.with_name(f".{output_path.name}.{uuid.uuid4().hex}.part")
    crc = 0
    sha = hashlib.sha256()
    written = 0

    def emit(stream, data: bytes) -> None:
        nonlocal crc, written
        stream.write(data)
        crc = zlib.crc32(data, crc)
        sha.update(data)
        written += len(data)

    try:
        with temp_path.open("xb") as output:
            emit(
                output,
                HEADER.pack(
                    b"GV1\0",
                    WIDTH,
                    HEIGHT,
                    FPS_NUM,
                    FPS_DEN,
                    frame_count,
                    AUDIO_RATE,
                    AUDIO_CHANNELS,
                    AUDIO_BITS,
                    audio_bytes,
                    audio_offset,
                ),
            )
            for offset, size in zip(offsets, sizes, strict=True):
                emit(output, ENTRY.pack(offset, size))

            remaining = audio_bytes
            if audio_path.is_file():
                with audio_path.open("rb") as audio:
                    while remaining:
                        _check_cancel(cancel)
                        block = audio.read(min(1024 * 1024, remaining))
                        if not block:
                            break
                        emit(output, block)
                        remaining -= len(block)
            zeroes = bytes(1024 * 1024)
            while remaining:
                _check_cancel(cancel)
                take = min(len(zeroes), remaining)
                emit(output, zeroes[:take])
                remaining -= take

            payload_total = sum(sizes)
            payload_done = 0
            for index, frame in enumerate(frames):
                _check_cancel(cancel)
                with frame.open("rb") as source:
                    while True:
                        block = source.read(64 * 1024)
                        if not block:
                            break
                        emit(output, block)
                        payload_done += len(block)
                if progress and (index % 16 == 0 or index + 1 == frame_count):
                    fraction = payload_done / payload_total if payload_total else 1.0
                    progress(5.0 + 95.0 * fraction)
            output.flush()
            os.fsync(output.fileno())

        if written != total_size:
            raise ValidationFailure(f"internal GV1 size mismatch: {written} != {total_size}")
        # Validate the temporary bytes before making the destination visible.
        validate_gv1(temp_path, checksums=False)
        os.replace(temp_path, output_path)
        _fsync_directory(output_path.parent)
    except BaseException:
        try:
            temp_path.unlink(missing_ok=True)
        finally:
            raise

    return GV1Info(
        path=output_path,
        size=total_size,
        frames=frame_count,
        duration_seconds=frame_count / FPS_NUM,
        audio_bytes=audio_bytes,
        largest_jpeg=max(sizes),
        crc32=f"{crc & 0xFFFFFFFF:08X}",
        sha256=sha.hexdigest().upper(),
    )


def validate_gv1(path: Path, *, checksums: bool = True) -> GV1Info:
    """Strictly validate header, exact contiguous layout, frame caps, and hashes."""
    file_size = path.stat().st_size
    if file_size < HEADER_SIZE:
        raise ValidationFailure("GV1 is shorter than its header")
    crc = 0
    sha = hashlib.sha256()
    with path.open("rb") as stream:
        raw_header = stream.read(HEADER_SIZE)
        values = HEADER.unpack(raw_header)
        magic, width, height, fps_num, fps_den, frames, rate, channels, bits, audio_bytes, audio_off = values
        if magic != b"GV1\0":
            raise ValidationFailure("GV1 magic is invalid")
        if (width, height, fps_num, fps_den) != (WIDTH, HEIGHT, FPS_NUM, FPS_DEN):
            raise ValidationFailure("GV1 video format is not 320x180 at 24/1 fps")
        if (rate, channels, bits) != (AUDIO_RATE, AUDIO_CHANNELS, AUDIO_BITS):
            raise ValidationFailure("GV1 audio format is not PCM s16le 48 kHz stereo")
        if not 1 <= frames <= MAX_FRAMES:
            raise ValidationFailure("GV1 frame count is invalid")
        expected_audio = frames * AUDIO_BYTES_PER_VIDEO_FRAME
        expected_audio_off = HEADER_SIZE + frames * ENTRY.size
        if audio_bytes != expected_audio or audio_off != expected_audio_off:
            raise ValidationFailure("GV1 audio/index layout is invalid")
        entries: list[tuple[int, int]] = []
        for _ in range(frames):
            raw_entry = stream.read(ENTRY.size)
            if len(raw_entry) != ENTRY.size:
                raise ValidationFailure("GV1 index is truncated")
            entries.append(ENTRY.unpack(raw_entry))
        expected_offset = audio_off + audio_bytes
        largest = 0
        for offset, size in entries:
            if offset != expected_offset or not 4 <= size <= JPEG_MAX_BYTES:
                raise ValidationFailure("GV1 JPEG index is non-contiguous or out of range")
            expected_offset += size
            largest = max(largest, size)
        if expected_offset != file_size:
            raise ValidationFailure("GV1 final size does not match its index")
        for offset, size in entries:
            stream.seek(offset)
            if stream.read(2) != b"\xff\xd8":
                raise ValidationFailure("GV1 frame lacks a JPEG start marker")
            stream.seek(offset + size - 2)
            if stream.read(2) != b"\xff\xd9":
                raise ValidationFailure("GV1 frame lacks a JPEG end marker")

    if checksums:
        with path.open("rb") as stream:
            while block := stream.read(1024 * 1024):
                crc = zlib.crc32(block, crc)
                sha.update(block)
    return GV1Info(
        path=path,
        size=file_size,
        frames=frames,
        duration_seconds=frames / FPS_NUM,
        audio_bytes=audio_bytes,
        largest_jpeg=largest,
        crc32=f"{crc & 0xFFFFFFFF:08X}" if checksums else "",
        sha256=sha.hexdigest().upper() if checksums else "",
    )
