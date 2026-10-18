"""不写入项目配置的运行中日志开关。"""

import asyncio
from datetime import datetime, timezone, timedelta
from typing import Literal
from fastapi import APIRouter, Query
from backend.api.error_protocol import DomainApiError
from backend.services.runtime_logs import get_store
from pydantic import BaseModel, ConfigDict

from backend.runtime import (
    get_backend_log_level,
    is_backend_debug_enabled,
    set_backend_debug_enabled,
)

router = APIRouter(prefix="/api/runtime", tags=["runtime"])


@router.get("/logs")
async def query_logs(after: int | None = Query(None, ge=0), before: int | None = Query(None, ge=1),
                     start: datetime | None = None, end: datetime | None = None,
                     level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] | None = None,
                     category: Literal["all", "llm", "system"] = "all", q: str = Query("", max_length=300),
                     limit: int = Query(50, ge=1, le=200)):
    def stamp(value):
        if value is None:
            return None
        return (value if value.tzinfo else value.replace(tzinfo=timezone(timedelta(hours=8)))).astimezone(timezone.utc).isoformat(timespec="milliseconds")
    if after is not None and before is not None or start and end and stamp(start) > stamp(end):
        raise DomainApiError("LOG_QUERY_INVALID", "请检查时间范围和分页参数", status_code=422)
    try:
        return await asyncio.to_thread(get_store().query, after=after, before=before, start=stamp(start), end=stamp(end), level=level, category=category, q=q, limit=limit)
    except Exception as exc:
        raise DomainApiError("LOG_QUERY_UNAVAILABLE", "日志查询暂时失败，请缩小时间范围或稍后重试", status_code=503) from exc


@router.get("/logs/{identity}")
async def log_detail(identity: int):
    if identity < 1:
        raise DomainApiError("LOG_ID_INVALID", "日志编号无效", status_code=422)
    result = await asyncio.to_thread(get_store().detail, identity)
    if result is None:
        raise DomainApiError("LOG_NOT_FOUND", "日志不存在或已超过保留期限", status_code=404)
    return result


class DebugLoggingRequest(BaseModel):
    """启动器提交的调试日志开关。"""

    model_config = ConfigDict(extra="forbid", strict=True)
    debug: bool


@router.get("/logging")
async def get_logging_state() -> dict[str, bool | str]:
    """读取当前进程的日志开关。

    Returns:
        调试开关和当前应用日志级别。
    """
    return {"debug": is_backend_debug_enabled(), "level": get_backend_log_level()}


@router.put("/logging")
async def update_logging_state(request: DebugLoggingRequest) -> dict[str, bool | str]:
    """立即调整当前进程的日志输出。

    Args:
        request: 要生效的调试日志开关。

    Returns:
        已生效的调试开关和日志级别。
    """
    set_backend_debug_enabled(request.debug)
    return {"debug": is_backend_debug_enabled(), "level": get_backend_log_level()}
