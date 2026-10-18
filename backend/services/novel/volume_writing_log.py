"""逐卷任务的步骤日志；单独存储，避免长篇写作撑大任务文档。"""
from __future__ import annotations

import json
from datetime import timezone

from bson import ObjectId

from backend.api.error_protocol import DomainApiError
from backend.db.base import BaseRepository
from backend.db.utils import get_utc_now

OUTPUT_LIMIT = 16000
PRIVATE_FIELDS = {"token", "access_token", "refresh_token", "id_token", "api_key", "authorization",
                  "agent_continuation", "reasoning", "reasoning_content", "thinking"}
EVENT_FIELDS = {"node_id", "instance_path", "label", "state", "elapsed_ms", "message", "round",
                "call_count", "tool_name", "network_state", "retry_attempt", "retry_limit", "retry_after", "cached"}


def collection():
    return BaseRepository("volume_writing_logs").collection


def clean(value):
    if isinstance(value, dict):
        return {str(key): clean(item) for key, item in value.items() if str(key).lower() not in PRIVATE_FIELDS}
    if isinstance(value, (list, tuple)):
        return [clean(item) for item in value]
    return value


def safe_event(data: dict) -> dict:
    event = {key: data[key] for key in EVENT_FIELDS if key in data and isinstance(data[key], (str, int, float, bool))}
    if data.get("outputs") is not None:
        output = json.dumps(clean(data["outputs"]), ensure_ascii=False, default=str, indent=2)
        event.update(output=output[:OUTPUT_LIMIT], output_truncated=len(output) > OUTPUT_LIMIT)
    return event


async def record(job_id: ObjectId, operation: str, attempt: str, current: dict, data: dict):
    stamp = get_utc_now().replace(tzinfo=None)
    identity = {"job_id": job_id, "operation": operation, "attempt": attempt,
                "instance_path": data.get("instance_path") or data.get("node_id") or "_task"}
    # 一次调用内同一节点的进度覆盖更新；重试另存一组，旧错误不会消失。
    await collection().update_one(identity, {"$set": {
        "stage": current.get("stage", "writing"), "title": current.get("title", ""),
        "target_id": current.get("target_id", ""), **safe_event(data), "updated_at": stamp,
    }, "$setOnInsert": {"created_at": stamp}}, upsert=True)


def public(row: dict) -> dict:
    return {key: (str(value) if isinstance(value, ObjectId) else value.replace(tzinfo=timezone.utc).isoformat()
                  if key in {"created_at", "updated_at"} else value)
            for key, value in row.items() if key not in {"_id", "job_id", "operation", "attempt"}} | {"id": str(row["_id"])}


def legacy_rows(job: dict) -> list[dict]:
    """旧任务仅展示确实保留的缓存；不虚构已丢失的耗时或历史。"""
    pending = job.get("pending") or {}
    current = job.get("current") or {}
    nodes = (pending.get("request") or {}).get("cached_nodes", [])
    stage = "writing" if pending.get("task") == "write_chapter_by_ai" else "planning" if pending.get("task") else current.get("stage", "writing")
    labels = {}
    ref = (job.get("flow_refs") or {}).get(pending.get("task"))
    if ref:
        from backend.services.task_flows.store import flow_store
        try:
            flow = flow_store().get(ref["id"], ref["revision"])
            labels = {node.id: node.label or node.id for node in flow.nodes}
        except KeyError:
            pass
    rows = [{"id": f"legacy-{index}", "stage": stage, "title": current.get("title", ""),
             **safe_event({**node, "label": labels.get(node["node_id"], node["node_id"]), "state": "done"})}
            for index, node in enumerate(nodes)]
    if current:
        state = "running" if job["status"] == "running" else "failed" if job.get("error") else "stopped"
        rows.append({"id": "legacy-current", "stage": current["stage"], "title": current["title"],
                     "label": current.get("node") or "", "state": state,
                     "message": (job.get("error") or {}).get("message", "")})
    return rows


async def read(novel_id: str, volume_id: str, job_id: str, before: str | None = None, limit: int = 50):
    job = await BaseRepository("volume_writing_jobs").collection.find_one({
        "_id": ObjectId(job_id), "novel_id": ObjectId(novel_id), "volume_id": ObjectId(volume_id)})
    if job is None:
        raise DomainApiError("VOLUME_WRITING_NOT_FOUND", "逐卷任务不存在", status_code=404)
    query = {"job_id": job["_id"]}
    if before:
        query["_id"] = {"$lt": ObjectId(before)}
    rows = await collection().find(query).sort("_id", -1).limit(limit + 1).to_list(limit + 1)
    more = len(rows) > limit
    rows = rows[:limit]
    legacy = not rows and not before and not await collection().find_one({"job_id": job["_id"]}, {"_id": 1})
    return {"items": legacy_rows(job) if legacy else [public(row) for row in reversed(rows)],
            "before": str(rows[-1]["_id"]) if more else None, "legacy": bool(legacy)}
