"""Constrained, cancellation-aware yt-dlp integration."""
from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
import re
import shutil
import sys
from typing import Callable

from .config import Settings
from .errors import JobCancelled, ProcessFailure, ValidationFailure
from .process import run_process

DownloadProgress = Callable[[float, str], None]
_PERCENT = re.compile(r"__GV1_PROGRESS__:\s*([0-9]+(?:\.[0-9]+)?)%")
_FILE_PREFIX = "__GV1_FILE__:"


def ytdlp_available() -> bool:
    return importlib.util.find_spec("yt_dlp") is not None


async def download_youtube(
    url: str,
    work_dir: Path,
    settings: Settings,
    cancel: asyncio.Event,
    report: DownloadProgress,
) -> Path:
    """Download one non-live YouTube item using the dedicated extractor."""
    if not ytdlp_available():
        raise ValidationFailure("yt-dlp is not installed")
    download_dir = work_dir / "youtube"
    await asyncio.to_thread(shutil.rmtree, download_dir, True)
    download_dir.mkdir(parents=True, exist_ok=True)
    template = download_dir / "source.%(ext)s"
    printed_path: Path | None = None

    def parse_line(source_name: str, line: str) -> None:
        nonlocal printed_path
        if source_name != "stdout":
            return
        match = _PERCENT.search(line)
        if match:
            report(min(100.0, float(match.group(1))), "Downloading authorized YouTube media")
        if line.startswith(_FILE_PREFIX):
            candidate = Path(line[len(_FILE_PREFIX) :].strip())
            printed_path = candidate if candidate.is_absolute() else download_dir / candidate

    command = [
            sys.executable,
            "-m",
            "yt_dlp",
            "--ignore-config",
            "--no-playlist",
            "--no-colors",
            "--no-warnings",
            "--newline",
            "--no-write-thumbnail",
            "--no-write-subs",
            "--no-write-auto-subs",
            "--match-filter",
            "!is_live & !was_live",
            "--max-filesize",
            str(settings.max_input_bytes),
            "--socket-timeout",
            "20",
            "--retries",
            "3",
            "--fragment-retries",
            "3",
            "--format",
            "bv*[height<=1080]+ba/b[height<=1080]",
            "--merge-output-format",
            "mp4",
            "--output",
            str(template),
            "--progress-template",
            "download:__GV1_PROGRESS__:%(progress._percent_str)s",
            "--print",
            "after_move:__GV1_FILE__:%(filepath)s",
        ]
    ffmpeg = settings.resolve_program(settings.ffmpeg)
    if ffmpeg:
        command.extend(["--ffmpeg-location", ffmpeg])
    command.append(url)
    try:
        await run_process(
            command,
            cancel,
            cwd=download_dir,
            line_handler=parse_line,
        )
    except ProcessFailure as exc:
        # yt-dlp uses 101 for its download-limit sentinel.  If after_move
        # already reported a complete file, conversion can safely continue.
        if exc.returncode != 101 or not printed_path or not printed_path.is_file():
            raise
    if cancel.is_set():
        raise JobCancelled("Job cancelled")

    candidates = [
        path
        for path in download_dir.glob("source.*")
        if path.is_file() and path.suffix.lower() not in {".part", ".ytdl", ".temp"}
    ]
    if printed_path and printed_path.is_file():
        selected = printed_path
    elif candidates:
        selected = max(candidates, key=lambda item: item.stat().st_size)
    else:
        raise ValidationFailure("yt-dlp completed without a media file")
    try:
        selected.resolve().relative_to(download_dir.resolve())
    except ValueError as exc:
        raise ValidationFailure("yt-dlp produced a path outside its work directory") from exc
    size = selected.stat().st_size
    if size <= 0 or size > settings.max_input_bytes:
        raise ValidationFailure("downloaded media is empty or exceeds the configured limit")
    report(100.0, "YouTube download complete")
    return selected
