"""API and internal job models."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class YouTubeJobRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str = Field(min_length=1, max_length=2048)
    authorized: bool = Field(
        description="Must be true to confirm that the caller is authorized to download this media."
    )
    auto_send: bool = False
    player_url: str | None = Field(default=None, max_length=256)
    output_name: str | None = Field(default=None, max_length=200)


class SendRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    player_url: str = Field(min_length=1, max_length=256)


class JobView(BaseModel):
    id: str
    status: str
    stage: str
    progress: float
    message: str
    error: str | None
    output_name: str
    size: int | None
    crc32: str | None
    sha256: str | None
    download_url: str | None
    created_at: datetime
    updated_at: datetime
    frames: int | None = None
    duration_seconds: float | None = None
    largest_jpeg: int | None = None


@dataclass(slots=True)
class JobRecord:
    id: str
    source_kind: Literal["file", "youtube"]
    output_name: str
    auto_send: bool = False
    player_url: str | None = None
    source_url: str | None = None
    source_path: Path | None = None
    result_path: Path | None = None
    status: str = "queued"
    stage: str = "queued"
    progress: float = 0.0
    message: str = "Queued"
    error: str | None = None
    size: int | None = None
    crc32: str | None = None
    sha256: str | None = None
    frames: int | None = None
    duration_seconds: float | None = None
    largest_jpeg: int | None = None
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    operation_task: asyncio.Task[None] | None = field(default=None, repr=False)

    def touch(self) -> None:
        self.updated_at = utc_now()

    def as_view(self) -> JobView:
        available = self.result_path is not None and self.result_path.is_file()
        return JobView(
            id=self.id,
            status=self.status,
            stage=self.stage,
            progress=round(max(0.0, min(100.0, self.progress)), 2),
            message=self.message,
            error=self.error,
            output_name=self.output_name,
            size=self.size,
            crc32=self.crc32,
            sha256=self.sha256,
            download_url=f"/api/jobs/{self.id}/download" if available else None,
            created_at=self.created_at,
            updated_at=self.updated_at,
            frames=self.frames,
            duration_seconds=self.duration_seconds,
            largest_jpeg=self.largest_jpeg,
        )
