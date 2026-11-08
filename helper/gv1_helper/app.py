"""FastAPI application for the loopback-only GV1 helper."""
from __future__ import annotations

from contextlib import asynccontextmanager
import os
from pathlib import Path
import sys

from fastapi import FastAPI, File, Form, HTTPException, UploadFile, status
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from . import __version__
from .config import Settings
from .cors import BrowserAccessMiddleware
from .errors import ValidationFailure
from .jobs import JobManager, JobNotFound, JobStateError
from .models import JobView, SendRequest, YouTubeJobRequest
from .security import (
    looks_like_mp4,
    validate_output_name,
    validate_player_url,
    validate_youtube_url,
)
from .youtube import ytdlp_available


def _bad_request(exc: ValidationFailure) -> HTTPException:
    return HTTPException(status_code=422, detail=str(exc))


def create_app(settings: Settings | None = None) -> FastAPI:
    config = settings or Settings()
    manager = JobManager(config)

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        del application
        await manager.start()
        try:
            yield
        finally:
            await manager.stop()

    application = FastAPI(
        title="GV1 Local Helper",
        version=__version__,
        description="Loopback-only MP4/YouTube to ESP32 GV1 conversion service",
        lifespan=lifespan,
    )
    application.state.settings = config
    application.state.jobs = manager
    application.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=["127.0.0.1", "localhost", "[::1]", "testserver"],
    )
    application.add_middleware(BrowserAccessMiddleware, explicit_origins=config.allowed_origins)

    web_dir = Path(__file__).resolve().parents[2] / "docs"

    @application.get("/", include_in_schema=False)
    async def root():
        index = web_dir / "index.html"
        if index.is_file():
            return FileResponse(index, media_type="text/html; charset=utf-8")
        return {"service": "GV1 Local Helper", "version": __version__, "docs": "/docs"}

    @application.get("/api/health")
    async def health() -> dict[str, object]:
        ffmpeg = config.resolve_program(config.ffmpeg)
        ffprobe = config.resolve_program(config.ffprobe)
        downloader = ytdlp_available()
        return {
            "ok": bool(ffmpeg and ffprobe and downloader),
            "service": "gv1-helper",
            "version": __version__,
            "python": sys.version.split()[0],
            "loopback_only": True,
            "tools": {
                "ffmpeg": bool(ffmpeg),
                "ffprobe": bool(ffprobe),
                "yt_dlp": downloader,
            },
            "format": {
                "video": "MJPEG 320x180 24fps letterboxed",
                "audio": "PCM s16le 48000Hz stereo",
                "jpeg_max_bytes": 32768,
            },
            "limits": {
                "max_input_bytes": config.max_input_bytes,
                "max_duration_seconds": config.max_duration_seconds,
            },
        }

    @application.post(
        "/api/jobs/file",
        response_model=JobView,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def create_file_job(
        file: UploadFile = File(...),
        auto_send: bool = Form(False),
        player_url: str | None = Form(None),
        output_name: str | None = Form(None),
    ) -> JobView:
        filename = file.filename or ""
        if Path(filename).suffix.lower() != ".mp4":
            raise HTTPException(status_code=415, detail="file must have an .mp4 extension")
        try:
            name = validate_output_name(output_name, Path(filename).stem or "video")
            player = validate_player_url(player_url) if player_url else None
        except ValidationFailure as exc:
            raise _bad_request(exc) from exc
        if auto_send and not player:
            raise HTTPException(status_code=422, detail="player_url is required when auto_send is true")

        job = await manager.create(
            "file",
            name,
            auto_send=auto_send,
            player_url=player,
            receiving=True,
        )
        partial = manager.work_dir_for(job.id) / "input.mp4.part"
        source = manager.work_dir_for(job.id) / "input.mp4"
        total = 0
        prefix = bytearray()
        try:
            with partial.open("xb") as destination:
                while block := await file.read(config.upload_chunk_bytes):
                    total += len(block)
                    if total > config.max_input_bytes:
                        raise HTTPException(status_code=413, detail="MP4 exceeds configured size limit")
                    if len(prefix) < 64:
                        prefix.extend(block[: 64 - len(prefix)])
                    destination.write(block)
                destination.flush()
                os.fsync(destination.fileno())
            if total == 0:
                raise HTTPException(status_code=400, detail="uploaded MP4 is empty")
            if not looks_like_mp4(bytes(prefix)):
                raise HTTPException(status_code=415, detail="uploaded file is not an ISO-BMFF MP4")
            os.replace(partial, source)
            return await manager.enqueue_file(job.id, source)
        except HTTPException:
            partial.unlink(missing_ok=True)
            await manager.discard(job.id)
            raise
        except (OSError, RuntimeError) as exc:
            partial.unlink(missing_ok=True)
            await manager.discard(job.id)
            raise HTTPException(status_code=500, detail="could not store uploaded MP4") from exc
        finally:
            await file.close()

    @application.post(
        "/api/jobs/youtube",
        response_model=JobView,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def create_youtube_job(payload: YouTubeJobRequest) -> JobView:
        if payload.authorized is not True:
            raise HTTPException(
                status_code=403,
                detail="authorized=true is required to confirm download rights",
            )
        try:
            url = validate_youtube_url(payload.url)
            name = validate_output_name(payload.output_name, "youtube-video")
            player = validate_player_url(payload.player_url) if payload.player_url else None
        except ValidationFailure as exc:
            raise _bad_request(exc) from exc
        if payload.auto_send and not player:
            raise HTTPException(status_code=422, detail="player_url is required when auto_send is true")
        job = await manager.create(
            "youtube",
            name,
            auto_send=payload.auto_send,
            player_url=player,
            source_url=url,
        )
        try:
            return await manager.enqueue_youtube(job.id)
        except BaseException:
            await manager.discard(job.id)
            raise

    @application.get("/api/jobs/{job_id}", response_model=JobView)
    async def get_job(job_id: str) -> JobView:
        try:
            return manager.view(job_id)
        except JobNotFound as exc:
            raise HTTPException(status_code=404, detail="job not found") from exc

    @application.get("/api/jobs/{job_id}/download")
    async def download_job(job_id: str) -> FileResponse:
        try:
            job = manager.get(job_id)
        except JobNotFound as exc:
            raise HTTPException(status_code=404, detail="job not found") from exc
        if not job.result_path or not job.result_path.is_file() or not job.crc32 or not job.sha256:
            raise HTTPException(status_code=409, detail="job output is not ready")
        return FileResponse(
            job.result_path,
            media_type="application/octet-stream",
            filename=job.output_name,
            headers={
                "X-CRC32": job.crc32,
                "X-SHA256": job.sha256,
                "ETag": f'"{job.sha256}"',
                "Cache-Control": "no-store",
            },
        )

    @application.post(
        "/api/jobs/{job_id}/send",
        response_model=JobView,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def send_job(job_id: str, payload: SendRequest) -> JobView:
        try:
            player = validate_player_url(payload.player_url)
            return await manager.start_send(job_id, player)
        except ValidationFailure as exc:
            raise _bad_request(exc) from exc
        except JobNotFound as exc:
            raise HTTPException(status_code=404, detail="job not found") from exc
        except JobStateError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @application.delete(
        "/api/jobs/{job_id}",
        response_model=JobView,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def cancel_job(job_id: str) -> JobView:
        try:
            return await manager.cancel(job_id)
        except JobNotFound as exc:
            raise HTTPException(status_code=404, detail="job not found") from exc
        except JobStateError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    if web_dir.is_dir():
        application.mount("/", StaticFiles(directory=web_dir, html=True), name="web")
    return application


app = create_app()
