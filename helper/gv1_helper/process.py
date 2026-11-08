"""Cancellation-aware native subprocess execution."""
from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass
import os
from pathlib import Path
import signal
import subprocess
from typing import Callable, Sequence

from .errors import JobCancelled, ProcessFailure


@dataclass(frozen=True, slots=True)
class ProcessOutput:
    stdout: str
    stderr: str
    returncode: int


async def _stop_process(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    try:
        if os.name == "nt":
            process.terminate()
        else:
            os.killpg(process.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        await asyncio.wait_for(process.wait(), timeout=4.0)
        return
    except asyncio.TimeoutError:
        pass
    try:
        if os.name == "nt":
            process.kill()
        else:
            os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    await process.wait()


async def run_process(
    args: Sequence[str],
    cancel: asyncio.Event,
    *,
    cwd: Path | None = None,
    line_handler: Callable[[str, str], None] | None = None,
) -> ProcessOutput:
    """Run a command without a shell, draining both pipes and honoring cancel."""
    if not args:
        raise ValueError("process argument list is empty")
    kwargs: dict[str, object] = {}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    else:
        kwargs["start_new_session"] = True
    child_env = os.environ.copy()
    child_env["PYTHONUTF8"] = "1"
    child_env["PYTHONIOENCODING"] = "utf-8"
    try:
        process = await asyncio.create_subprocess_exec(
            *args,
            cwd=str(cwd) if cwd else None,
            env=child_env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **kwargs,
        )
    except OSError as exc:
        raise ProcessFailure(Path(args[0]).name, -1, str(exc)) from exc

    stdout_lines: deque[str] = deque(maxlen=5000)
    stderr_lines: deque[str] = deque(maxlen=5000)

    async def pump(reader: asyncio.StreamReader | None, source: str, sink: deque[str]) -> None:
        if reader is None:
            return
        while line := await reader.readline():
            text = line.decode("utf-8", "replace").rstrip("\r\n")
            sink.append(text)
            if line_handler:
                line_handler(source, text)

    pumps = [
        asyncio.create_task(pump(process.stdout, "stdout", stdout_lines)),
        asyncio.create_task(pump(process.stderr, "stderr", stderr_lines)),
    ]
    wait_task = asyncio.create_task(process.wait())
    cancel_task = asyncio.create_task(cancel.wait())
    was_cancelled = False
    try:
        done, _ = await asyncio.wait({wait_task, cancel_task}, return_when=asyncio.FIRST_COMPLETED)
        if cancel_task in done and cancel.is_set() and not wait_task.done():
            was_cancelled = True
            await _stop_process(process)
        returncode = await wait_task
        await asyncio.gather(*pumps)
    except asyncio.CancelledError:
        await _stop_process(process)
        await asyncio.gather(*pumps, return_exceptions=True)
        raise
    finally:
        cancel_task.cancel()
        await asyncio.gather(cancel_task, return_exceptions=True)

    if was_cancelled or cancel.is_set():
        raise JobCancelled("Job cancelled")
    stdout = "\n".join(stdout_lines)
    stderr = "\n".join(stderr_lines)
    if returncode:
        detail_lines = list(stderr_lines)[-12:] or list(stdout_lines)[-12:]
        detail = " | ".join(line.strip() for line in detail_lines if line.strip())
        if len(detail) > 2000:
            detail = detail[-2000:]
        raise ProcessFailure(Path(args[0]).name, returncode, detail)
    return ProcessOutput(stdout=stdout, stderr=stderr, returncode=returncode)
