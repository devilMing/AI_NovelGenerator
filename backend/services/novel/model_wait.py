"""Bound model waits without cancelling business saves or losing durable checkpoints."""
from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable, TypeVar

from backend.api.error_protocol import DomainApiError

T = TypeVar("T")
MODEL_TOTAL_SECONDS = 300
STOP_GRACE_SECONDS = 15
POLL_SECONDS = 1
PROGRESS_SECONDS = 5
CLEANUP_SECONDS = 5
logger = logging.getLogger(__name__)


def _consume_result(task):
    if not task.cancelled():
        task.exception()


class ModelWaitStopped(Exception):
    """The current request did not finish within the requested stop grace period."""


async def wait_for_model(
    call: Callable[[], Awaitable[T]],
    *,
    control: Callable[[], Awaitable[dict]],
    progress: Callable[[dict], Awaitable[None]],
    label: str,
    total_seconds: float | None = MODEL_TOTAL_SECONDS,
) -> T:
    """Prefer completed results; otherwise cancel only the unfinished model await.

    The caller persists a returned result before honouring pause. No business save
    belongs inside ``call``. Workflow callbacks may persist completed checkpoints.
    """
    initial = await control()
    if initial.get("pause_requested") or initial.get("cancel_requested"):
        await progress({"state": "stopped", "elapsed_ms": 0, "message": "已请求停止，未发送新的模型请求。"})
        raise ModelWaitStopped()
    loop = asyncio.get_running_loop()
    started = loop.time()
    deadline = None if total_seconds is None else started + total_seconds
    stop_deadline = None
    next_progress = started + PROGRESS_SECONDS
    task = asyncio.create_task(call())
    try:
        while True:
            deadlines = [loop.time() + POLL_SECONDS]
            deadlines.extend(value for value in (deadline, stop_deadline) if value is not None)
            await asyncio.wait({task}, timeout=max(0, min(deadlines) - loop.time()))
            # A result arriving at the same instant as pause/timeout must survive.
            if task.done():
                return task.result()
            state = await control()
            if task.done():
                return task.result()
            stamp = loop.time()
            stopping = state.get("pause_requested") or state.get("cancel_requested")
            if stopping and stop_deadline is None:
                stop_deadline = stamp + STOP_GRACE_SECONDS
            if stop_deadline is not None and stamp >= stop_deadline:
                await progress({"state": "stopped", "elapsed_ms": int((stamp - started) * 1000),
                                "message": "停止等待已超时，已中止未完成的模型请求；保存内容和完成步骤保留，继续时仅重试未完成步骤。"})
                raise ModelWaitStopped()
            if deadline is not None and stamp >= deadline:
                if stopping:
                    raise ModelWaitStopped()
                await progress({"state": "failed", "elapsed_ms": int((stamp - started) * 1000),
                                "message": "模型请求超过总时限，已保留完成步骤，可继续重试。"})
                raise DomainApiError(
                    "VOLUME_WRITING_MODEL_TIMEOUT",
                    f"{label}超过 {total_seconds:g} 秒总时限，已中止本次等待。已保存正文和完成步骤保留，请继续重试。",
                    status_code=504,
                )
            if stamp >= next_progress:
                remaining = max(0, int(stop_deadline - stamp + 0.999)) if stop_deadline is not None else None
                message = (f"已请求停止，最多再等待 {remaining} 秒保存当前返回结果。" if stopping
                           else f"正在等待模型返回，已等待 {int(stamp - started)} 秒。")
                await progress({"state": "running", "elapsed_ms": int((stamp - started) * 1000), "message": message})
                next_progress = stamp + PROGRESS_SECONDS
    finally:
        if not task.done():
            task.cancel()
            # Broken stream cleanup must not make pause itself wait forever.
            try:
                await asyncio.wait({task}, timeout=CLEANUP_SECONDS)
            finally:
                if not task.done():
                    task.cancel()
                    task.add_done_callback(_consume_result)
                    logger.warning("模型连接清理超过时限；已停止等待，未完成结果不会被采用。")
        if task.done():
            _consume_result(task)
