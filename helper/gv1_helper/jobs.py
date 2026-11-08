"""In-memory asynchronous job queue and lifecycle management."""
from __future__ import annotations

import asyncio
from datetime import timedelta
import logging
from pathlib import Path
import shutil
from typing import Literal
import uuid

from .config import Settings
from .converter import Converter
from .device import upload_to_player
from .errors import HelperError, JobCancelled, ValidationFailure
from .models import JobRecord, JobView, utc_now
from .youtube import download_youtube

LOGGER = logging.getLogger(__name__)


class JobNotFound(KeyError):
    pass


class JobStateError(RuntimeError):
    pass


class JobManager:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.converter = Converter(settings)
        self.jobs: dict[str, JobRecord] = {}
        self.queue: asyncio.Queue[str] = asyncio.Queue()
        self.workers: list[asyncio.Task[None]] = []
        self.sweeper: asyncio.Task[None] | None = None
        self.background: set[asyncio.Task[None]] = set()
        self._started = False

    async def start(self) -> None:
        if self._started:
            return
        self.settings.work_dir.mkdir(parents=True, exist_ok=True)
        self.settings.results_dir.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(self._remove_orphan_work)
        self.workers = [
            asyncio.create_task(self._worker(index), name=f"gv1-worker-{index}")
            for index in range(self.settings.worker_count)
        ]
        self.sweeper = asyncio.create_task(self._sweep_loop(), name="gv1-sweeper")
        self._started = True

    async def stop(self) -> None:
        if not self._started:
            return
        for job in self.jobs.values():
            if job.status in {"receiving", "queued", "running", "sending", "cancelling"}:
                job.cancel_event.set()
        tasks = [*self.workers, *self.background]
        if self.sweeper:
            tasks.append(self.sweeper)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.workers.clear()
        self.background.clear()
        self.sweeper = None
        self._started = False

    def _remove_orphan_work(self) -> None:
        if not self.settings.work_dir.exists():
            return
        for child in self.settings.work_dir.iterdir():
            if child.is_dir():
                shutil.rmtree(child, ignore_errors=True)
            else:
                child.unlink(missing_ok=True)
        cutoff = utc_now().timestamp() - self.settings.result_ttl_seconds
        if self.settings.results_dir.exists():
            for child in self.settings.results_dir.iterdir():
                try:
                    if child.stat().st_mtime < cutoff:
                        if child.is_dir():
                            shutil.rmtree(child, ignore_errors=True)
                        else:
                            child.unlink(missing_ok=True)
                except OSError:
                    LOGGER.warning("Could not inspect stale result %s", child, exc_info=True)

    def work_dir_for(self, job_id: str) -> Path:
        return self.settings.work_dir / job_id

    def result_dir_for(self, job_id: str) -> Path:
        return self.settings.results_dir / job_id

    async def create(
        self,
        source_kind: Literal["file", "youtube"],
        output_name: str,
        *,
        auto_send: bool,
        player_url: str | None,
        source_url: str | None = None,
        receiving: bool = False,
    ) -> JobRecord:
        job_id = uuid.uuid4().hex
        job = JobRecord(
            id=job_id,
            source_kind=source_kind,
            output_name=output_name,
            auto_send=auto_send,
            player_url=player_url,
            source_url=source_url,
            status="receiving" if receiving else "queued",
            stage="receiving" if receiving else "queued",
            message="Receiving MP4 upload" if receiving else "Queued",
        )
        self.jobs[job_id] = job
        self.work_dir_for(job_id).mkdir(parents=True, exist_ok=False)
        self.result_dir_for(job_id).mkdir(parents=True, exist_ok=False)
        return job

    async def discard(self, job_id: str) -> None:
        self.jobs.pop(job_id, None)
        await asyncio.gather(
            asyncio.to_thread(shutil.rmtree, self.work_dir_for(job_id), True),
            asyncio.to_thread(shutil.rmtree, self.result_dir_for(job_id), True),
        )

    async def enqueue_file(self, job_id: str, source_path: Path) -> JobView:
        job = self._get(job_id)
        job.source_path = source_path
        self._set(job, status="queued", stage="queued", progress=0.0, message="Queued for conversion")
        await self.queue.put(job_id)
        return job.as_view()

    async def enqueue_youtube(self, job_id: str) -> JobView:
        job = self._get(job_id)
        await self.queue.put(job_id)
        return job.as_view()

    def _get(self, job_id: str) -> JobRecord:
        try:
            return self.jobs[job_id]
        except KeyError as exc:
            raise JobNotFound(job_id) from exc

    def get(self, job_id: str) -> JobRecord:
        return self._get(job_id)

    def view(self, job_id: str) -> JobView:
        return self._get(job_id).as_view()

    def _set(
        self,
        job: JobRecord,
        *,
        status: str | None = None,
        stage: str | None = None,
        progress: float | None = None,
        message: str | None = None,
        error: str | None | object = ...,
    ) -> None:
        if status is not None:
            job.status = status
        if stage is not None:
            job.stage = stage
        if progress is not None:
            job.progress = max(0.0, min(100.0, progress))
        if message is not None:
            job.message = message
        if error is not ...:
            job.error = error if isinstance(error, str) else None
        job.touch()

    async def cancel(self, job_id: str) -> JobView:
        job = self._get(job_id)
        if job.status in {"completed", "failed", "cancelled"}:
            raise JobStateError(f"job is already {job.status}")
        job.cancel_event.set()
        if job.status == "queued":
            self._set(
                job,
                status="cancelled",
                stage="cancelled",
                message="Job cancelled before it started",
            )
            task = asyncio.create_task(
                asyncio.to_thread(shutil.rmtree, self.work_dir_for(job.id), True)
            )
            self._track(task)
        else:
            self._set(job, status="cancelling", stage="cancelling", message="Cancellation requested")
        return job.as_view()

    async def start_send(self, job_id: str, player_url: str) -> JobView:
        job = self._get(job_id)
        if job.status != "completed" or not job.result_path or not job.result_path.is_file():
            raise JobStateError("job must have a completed output before it can be sent")
        job.cancel_event = asyncio.Event()
        job.player_url = player_url
        self._set(
            job,
            status="sending",
            stage="sending",
            progress=0.0,
            message="Starting ESP32 upload",
            error=None,
        )
        task = asyncio.create_task(self._manual_send(job, player_url), name=f"gv1-send-{job.id}")
        job.operation_task = task
        self._track(task)
        return job.as_view()

    def _track(self, task: asyncio.Task[None]) -> None:
        self.background.add(task)
        task.add_done_callback(self.background.discard)

    async def _manual_send(self, job: JobRecord, player_url: str) -> None:
        try:
            await self._perform_send(job, player_url, automatic=False)
            self._set(
                job,
                status="completed",
                stage="ready",
                progress=100.0,
                message="GV1 uploaded and verified by ESP32",
                error=None,
            )
        except JobCancelled:
            self._set(job, status="cancelled", stage="cancelled", message="ESP32 upload cancelled")
        except HelperError as exc:
            self._set(
                job,
                status="completed",
                stage="send_failed",
                progress=100.0,
                message="Conversion is ready, but ESP32 upload failed",
                error=str(exc),
            )
        except Exception:
            LOGGER.exception("Unexpected manual send failure for job %s", job.id)
            self._set(
                job,
                status="completed",
                stage="send_failed",
                progress=100.0,
                message="Conversion is ready, but ESP32 upload failed",
                error="Unexpected ESP32 upload error",
            )
        finally:
            job.operation_task = None

    async def _worker(self, index: int) -> None:
        del index
        while True:
            job_id = await self.queue.get()
            try:
                job = self.jobs.get(job_id)
                if not job or job.status == "cancelled" or job.cancel_event.is_set():
                    continue
                await self._execute(job)
            finally:
                self.queue.task_done()

    async def _execute(self, job: JobRecord) -> None:
        final_conversion_progress = 90.0 if job.auto_send else 100.0
        try:
            self._set(
                job,
                status="running",
                stage="starting",
                progress=0.0,
                message="Starting job",
                error=None,
            )
            source = job.source_path
            youtube_offset = 0.0
            if job.source_kind == "youtube":
                if not job.source_url:
                    raise ValidationFailure("YouTube job has no URL")

                def download_report(percent: float, message: str) -> None:
                    self._set(
                        job,
                        stage="downloading",
                        progress=final_conversion_progress * 0.20 * percent / 100.0,
                        message=message,
                    )

                source = await download_youtube(
                    job.source_url,
                    self.work_dir_for(job.id),
                    self.settings,
                    job.cancel_event,
                    download_report,
                )
                youtube_offset = 0.20
                job.source_path = source
            if not source:
                raise ValidationFailure("job has no input media")

            def conversion_report(stage: str, percent: float, message: str) -> None:
                normalized = percent / 100.0
                if youtube_offset:
                    normalized = youtube_offset + (1.0 - youtube_offset) * normalized
                self._set(
                    job,
                    stage=stage,
                    progress=final_conversion_progress * normalized,
                    message=message,
                )

            output = self.result_dir_for(job.id) / job.output_name
            result = await self.converter.convert(
                source,
                output,
                self.work_dir_for(job.id),
                job.cancel_event,
                conversion_report,
                require_mp4=job.source_kind == "file",
            )
            job.result_path = result.path
            job.size = result.size
            job.crc32 = result.crc32
            job.sha256 = result.sha256
            job.frames = result.frames
            job.duration_seconds = result.duration_seconds
            job.largest_jpeg = result.largest_jpeg
            job.touch()

            if job.auto_send:
                if not job.player_url:
                    raise ValidationFailure("auto_send requires player_url")
                try:
                    await self._perform_send(job, job.player_url, automatic=True)
                except JobCancelled:
                    raise
                except HelperError as exc:
                    self._set(
                        job,
                        status="completed",
                        stage="send_failed",
                        progress=100.0,
                        message="Conversion is ready, but automatic ESP32 upload failed",
                        error=str(exc),
                    )
                    return
                message = "Conversion complete; ESP32 verified the upload"
            else:
                message = "GV1 conversion complete"
            self._set(
                job,
                status="completed",
                stage="ready",
                progress=100.0,
                message=message,
                error=None,
            )
        except JobCancelled:
            self._set(job, status="cancelled", stage="cancelled", message="Job cancelled")
        except HelperError as exc:
            self._set(
                job,
                status="failed",
                stage="failed",
                message="Job failed",
                error=str(exc),
            )
        except asyncio.CancelledError:
            job.cancel_event.set()
            self._set(job, status="cancelled", stage="cancelled", message="Service stopped")
            raise
        except Exception:
            LOGGER.exception("Unexpected failure for job %s", job.id)
            self._set(
                job,
                status="failed",
                stage="failed",
                message="Job failed",
                error="Unexpected internal conversion error",
            )
        finally:
            await asyncio.to_thread(shutil.rmtree, self.work_dir_for(job.id), True)
            if job.status in {"failed", "cancelled"} and job.result_path is None:
                await asyncio.to_thread(shutil.rmtree, self.result_dir_for(job.id), True)

    async def _perform_send(
        self,
        job: JobRecord,
        player_url: str,
        *,
        automatic: bool,
    ) -> None:
        if not job.result_path or not job.crc32:
            raise ValidationFailure("job output is not ready")
        base = 90.0 if automatic else 0.0
        span = 10.0 if automatic else 100.0
        self._set(
            job,
            status="sending",
            stage="sending",
            progress=base,
            message="Streaming GV1 to ESP32",
            error=None,
        )

        def send_report(percent: float, message: str) -> None:
            self._set(
                job,
                stage="sending",
                progress=base + span * percent / 100.0,
                message=message,
            )

        await upload_to_player(
            job.result_path,
            player_url,
            job.output_name,
            job.crc32,
            self.settings,
            job.cancel_event,
            send_report,
        )

    async def _sweep_loop(self) -> None:
        while True:
            await asyncio.sleep(60)
            cutoff = utc_now() - timedelta(seconds=self.settings.result_ttl_seconds)
            expired = [
                job_id
                for job_id, job in self.jobs.items()
                if job.status in {"completed", "failed", "cancelled"}
                and job.updated_at < cutoff
                and job.operation_task is None
            ]
            for job_id in expired:
                self.jobs.pop(job_id, None)
                await asyncio.gather(
                    asyncio.to_thread(shutil.rmtree, self.work_dir_for(job_id), True),
                    asyncio.to_thread(shutil.rmtree, self.result_dir_for(job_id), True),
                )
