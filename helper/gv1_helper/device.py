"""Streaming ESP32 /api/upload client with transport cancellation."""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Callable

import httpx

from .config import Settings
from .errors import JobCancelled, ValidationFailure

UploadProgress = Callable[[float, str], None]


async def upload_to_player(
    path: Path,
    player_origin: str,
    output_name: str,
    crc32: str,
    settings: Settings,
    cancel: asyncio.Event,
    report: UploadProgress,
) -> str:
    """Stream a GV1 body while sending the firmware-required name/CRC query."""
    if not path.is_file():
        raise ValidationFailure("converted GV1 output is missing")
    total = path.stat().st_size
    if total <= 0:
        raise ValidationFailure("converted GV1 output is empty")
    endpoint = player_origin.rstrip("/") + "/api/upload"
    sent = 0

    async def body():
        nonlocal sent
        with path.open("rb") as stream:
            while True:
                if cancel.is_set():
                    raise JobCancelled("Job cancelled")
                block = await asyncio.to_thread(stream.read, settings.device_chunk_bytes)
                if not block:
                    break
                sent += len(block)
                report(100.0 * sent / total, f"Uploading to ESP32 ({sent}/{total} bytes)")
                yield block

    timeout = httpx.Timeout(connect=10.0, read=90.0, write=None, pool=10.0)
    headers = {
        "Content-Type": "application/octet-stream",
        "Content-Length": str(total),
        "Accept": "application/json, text/plain;q=0.9",
        "User-Agent": "gv1-local-helper/1.0",
    }
    async with httpx.AsyncClient(
        timeout=timeout,
        follow_redirects=False,
        trust_env=False,
    ) as client:
        request_task = asyncio.create_task(
            client.post(
                endpoint,
                params={"name": output_name, "crc": crc32},
                headers=headers,
                content=body(),
            )
        )
        cancel_task = asyncio.create_task(cancel.wait())
        try:
            done, _ = await asyncio.wait(
                {request_task, cancel_task}, return_when=asyncio.FIRST_COMPLETED
            )
            if cancel_task in done and cancel.is_set() and not request_task.done():
                request_task.cancel()
                await asyncio.gather(request_task, return_exceptions=True)
                raise JobCancelled("Job cancelled")
            response = await request_task
        except httpx.HTTPError as exc:
            raise ValidationFailure(f"ESP32 upload connection failed: {exc}") from exc
        finally:
            cancel_task.cancel()
            await asyncio.gather(cancel_task, return_exceptions=True)
    if not 200 <= response.status_code < 300:
        detail = response.text.strip().replace("\r", " ").replace("\n", " ")[:500]
        raise ValidationFailure(
            f"ESP32 upload returned HTTP {response.status_code}"
            + (f": {detail}" if detail else "")
        )
    report(100.0, "ESP32 verified and accepted the GV1 file")
    return response.text[:1000]
