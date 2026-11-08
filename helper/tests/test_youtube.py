from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from gv1_helper.config import Settings
from gv1_helper.errors import ProcessFailure
from gv1_helper import youtube


def settings_for(tmp_path: Path) -> Settings:
    return Settings(
        data_dir=tmp_path / "var",
        max_input_bytes=8 * 1024 * 1024,
        max_duration_seconds=60,
        result_ttl_seconds=300,
    )


@pytest.mark.parametrize("returncode", [0, 101])
def test_single_video_download_avoids_limit_sentinel_and_accepts_after_move(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, returncode: int
) -> None:
    captured: list[str] = []

    async def fake_run_process(args, cancel, *, cwd, line_handler):
        del cancel
        captured.extend(args)
        output = Path(cwd) / "source.mp4"
        output.write_bytes(b"complete mp4 fixture")
        line_handler("stdout", f"__GV1_FILE__:{output}")
        if returncode:
            raise ProcessFailure("python.exe", returncode, "download limit reached")
        return None

    monkeypatch.setattr(youtube, "ytdlp_available", lambda: True)
    monkeypatch.setattr(youtube, "run_process", fake_run_process)
    reports: list[tuple[float, str]] = []
    work = tmp_path / "work"
    work.mkdir()

    result = asyncio.run(
        youtube.download_youtube(
            "https://www.youtube.com/watch?v=abcdefghijk",
            work,
            settings_for(tmp_path),
            asyncio.Event(),
            lambda progress, message: reports.append((progress, message)),
        )
    )

    assert result == work / "youtube" / "source.mp4"
    assert "--no-playlist" in captured
    assert "--max-downloads" not in captured
    assert reports[-1] == (100.0, "YouTube download complete")


def test_non_limit_failure_is_not_hidden(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_run_process(args, cancel, *, cwd, line_handler):
        del args, cancel
        output = Path(cwd) / "source.mp4"
        output.write_bytes(b"incomplete fixture")
        line_handler("stdout", f"__GV1_FILE__:{output}")
        raise ProcessFailure("python.exe", 1, "network failure")

    monkeypatch.setattr(youtube, "ytdlp_available", lambda: True)
    monkeypatch.setattr(youtube, "run_process", fake_run_process)
    work = tmp_path / "work"
    work.mkdir()

    with pytest.raises(ProcessFailure, match="status 1"):
        asyncio.run(
            youtube.download_youtube(
                "https://youtu.be/abcdefghijk",
                work,
                settings_for(tmp_path),
                asyncio.Event(),
                lambda progress, message: None,
            )
        )
