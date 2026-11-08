"""Configuration with conservative local-service defaults."""
from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
import shutil


def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise RuntimeError(f"{name} must be between {minimum} and {maximum}")
    return value


def _default_data_dir() -> Path:
    return Path(__file__).resolve().parent.parent / "var"


@dataclass(slots=True)
class Settings:
    data_dir: Path = field(default_factory=_default_data_dir)
    ffmpeg: str = field(default_factory=lambda: os.getenv("GV1_FFMPEG", "ffmpeg"))
    ffprobe: str = field(default_factory=lambda: os.getenv("GV1_FFPROBE", "ffprobe"))
    max_input_bytes: int = field(
        default_factory=lambda: _env_int(
            "GV1_MAX_INPUT_BYTES", 2 * 1024 * 1024 * 1024, 1024 * 1024, 4 * 1024 * 1024 * 1024
        )
    )
    max_duration_seconds: int = field(
        default_factory=lambda: _env_int("GV1_MAX_DURATION_SECONDS", 7200, 1, 8332)
    )
    worker_count: int = field(
        default_factory=lambda: _env_int("GV1_WORKERS", 1, 1, 4)
    )
    result_ttl_seconds: int = field(
        default_factory=lambda: _env_int("GV1_RESULT_TTL_SECONDS", 24 * 3600, 300, 7 * 24 * 3600)
    )
    upload_chunk_bytes: int = 1024 * 1024
    device_chunk_bytes: int = 256 * 1024
    allowed_origins: tuple[str, ...] = field(
        default_factory=lambda: tuple(
            item.strip().rstrip("/")
            for item in os.getenv("GV1_ALLOWED_ORIGINS", "").split(",")
            if item.strip()
        )
    )

    @property
    def work_dir(self) -> Path:
        return self.data_dir / "work"

    @property
    def results_dir(self) -> Path:
        return self.data_dir / "results"

    def resolve_program(self, configured: str) -> str | None:
        candidate = Path(configured)
        if candidate.parent != Path(".") or candidate.is_absolute():
            return str(candidate) if candidate.is_file() else None
        return shutil.which(configured)
