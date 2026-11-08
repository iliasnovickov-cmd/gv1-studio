from __future__ import annotations

import pytest

from gv1_helper.errors import ValidationFailure
from gv1_helper.security import (
    github_pages_origin,
    looks_like_mp4,
    validate_output_name,
    validate_player_url,
    validate_youtube_url,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://youtube.com/watch?v=abc#ignored", "https://youtube.com/watch?v=abc"),
        ("https://www.youtube.com/shorts/abc", "https://www.youtube.com/shorts/abc"),
        ("https://youtu.be/abc?t=4", "https://youtu.be/abc?t=4"),
        ("https://music.youtube.com/watch?v=abc", "https://music.youtube.com/watch?v=abc"),
    ],
)
def test_youtube_url_accepts_only_canonical_hosts(raw: str, expected: str) -> None:
    assert validate_youtube_url(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "http://youtube.com/watch?v=x",
        "https://youtube.com.evil.test/watch?v=x",
        "https://evil.test/?next=youtube.com",
        "https://user@youtube.com/watch?v=x",
        "https://youtube.com:8443/watch?v=x",
        "https://127.0.0.1/watch?v=x",
        "https://www.youtubе.com/watch?v=x",  # Cyrillic e.
    ],
)
def test_youtube_url_rejects_spoofs(raw: str) -> None:
    with pytest.raises(ValidationFailure):
        validate_youtube_url(raw)


def test_player_url_accepts_only_private_literal_origins() -> None:
    assert validate_player_url("http://192.168.1.54/") == "http://192.168.1.54"
    assert validate_player_url("http://10.0.0.8:8080") == "http://10.0.0.8:8080"
    assert validate_player_url("http://[fd00::1234]:8080") == "http://[fd00::1234]:8080"
    for raw in (
        "https://192.168.1.54",
        "http://example.com",
        "http://8.8.8.8",
        "http://127.0.0.1",
        "http://192.168.1.54/admin",
        "http://user@192.168.1.54",
    ):
        with pytest.raises(ValidationFailure):
            validate_player_url(raw)


def test_output_name_matches_firmware_rules() -> None:
    assert validate_output_name(None, "my: unsafe/video") == "my_ unsafe_video.gv1"
    assert validate_output_name("Фильм.GV1", "ignored") == "Фильм.gv1"
    with pytest.raises(ValidationFailure):
        validate_output_name("../movie.gv1", "ignored")
    with pytest.raises(ValidationFailure):
        validate_output_name("bad:name", "ignored")


def test_origins_and_mp4_signature() -> None:
    assert github_pages_origin("https://example.github.io")
    assert github_pages_origin("http://localhost:5173")
    assert github_pages_origin("https://custom.example", ("https://custom.example",))
    assert not github_pages_origin("https://example.github.io.evil.test")
    assert looks_like_mp4(b"\x00\x00\x00\x18ftypisom\x00\x00\x00\x00")
    assert not looks_like_mp4(b"not an mp4 file")
