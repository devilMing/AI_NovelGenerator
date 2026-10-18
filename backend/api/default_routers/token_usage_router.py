"""Read-only local token statistics, independent of the novel database."""
import asyncio
from datetime import datetime, timezone, timedelta
from typing import Literal

from fastapi import APIRouter, Query
from backend.api.error_protocol import DomainApiError
from backend.services.token_usage import get_store

router = APIRouter(prefix="/api/runtime/token-usage", tags=["runtime"])


@router.get("")
async def query_usage(start: datetime | None = None, end: datetime | None = None,
                      provider: str | None = Query(None, max_length=300), model: str | None = Query(None, max_length=300),
                      novel_id: str | None = Query(None, max_length=100), volume_id: str | None = Query(None, max_length=100),
                      task: str | None = Query(None, max_length=200),
                      status: Literal["pending", "completed", "failed", "cancelled", "interrupted"] | None = None,
                      group: Literal["model", "provider", "novel", "volume", "task"] = "model",
                      bucket: Literal["hour", "day"] = "day", offset_minutes: int = Query(480, ge=-840, le=840),
                      before: int | None = Query(None, ge=1), limit: int = Query(50, ge=1, le=100)):
    def stamp(value):
        return (value if value.tzinfo else value.replace(tzinfo=timezone(timedelta(hours=8)))).astimezone(timezone.utc).isoformat(timespec="milliseconds") if value else None
    if start and end and stamp(start) >= stamp(end):
        raise DomainApiError("TOKEN_USAGE_RANGE_INVALID", "结束时间需要晚于开始时间", status_code=422)
    try:
        result = await asyncio.to_thread(get_store().query, start=stamp(start), end=stamp(end), provider=provider,
            model=model, novel_id=novel_id, volume_id=volume_id, task=task, status=status, group=group,
            bucket=bucket, offset_minutes=offset_minutes, before=before, limit=limit)
        # Known task IDs can be named without guessing historical book attribution.
        from backend.services.task_flows.registry import TASKS
        labels = {key: spec.label for key, spec in TASKS.items()}
        labels.update(provider_test="接口测试", assistant="创作助手")
        for row in result["items"]:
            if row["task"] in labels:
                row["scope"].setdefault("task_label", labels[row["task"]])
        for option in result["options"]["task"]:
            option["label"] = labels.get(option["value"], option["label"])
        if group == "task":
            for item in result["groups"]:
                item["name"] = labels.get(item["label"], item["name"])
        return result
    except Exception as exc:
        raise DomainApiError("TOKEN_USAGE_UNAVAILABLE", "Token 统计暂时无法读取，请稍后重试", status_code=503) from exc
