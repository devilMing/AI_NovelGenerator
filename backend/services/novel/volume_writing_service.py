"""持久化逐卷队列；生成和确认仍调用原任务图及事务服务。"""
from __future__ import annotations

import asyncio
from contextlib import suppress
from copy import deepcopy
from datetime import timedelta, timezone
import logging
import time
from uuid import uuid4

from bson import ObjectId
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from backend.api.error_protocol import DomainApiError, exception_to_error
from backend.db.base import BaseRepository
from backend.db.utils import get_utc_now
from backend.db.write_contracts import canonical_request_hash
from backend.services.assistant.runtime import book_lease
from backend.services.novel.chapter_service import ChapterService
from backend.services.novel.volume_writing_contract import VolumeWritingStart
from backend.services.novel import volume_writing_log
from backend.services.novel.writing_context import load_writing_parents
from backend.services.novel.writing_contract import WritingConfirmV1
from backend.services.novel.writing_generation import confirm_writing
from backend.services.task_flows.contracts import GraphRunRequest
from backend.services.task_flows.integration import generate_task_flow
from backend.services.task_flows.store import flow_store

logger = logging.getLogger(__name__)
OWNER = uuid4().hex
LEASE_SECONDS = 45
_workers: dict[str, asyncio.Task] = {}


def now():
    return get_utc_now().replace(tzinfo=None)


def jobs():
    return BaseRepository("volume_writing_jobs").collection


def planned_count(scene: dict, existing: int) -> int:
    return (scene.get("planning") or {}).get("target_chapter_count") or max(1, existing)


async def catalog(novel_id: str, volume_id: str) -> dict:
    await load_writing_parents(novel_id, "volume", volume_id)
    query = {"novel_id": ObjectId(novel_id), "volume_id": ObjectId(volume_id), "is_deleted": False}
    arcs = await BaseRepository("arcs").collection.find(query).sort([("order_index", 1), ("_id", 1)]).to_list(None)
    arc_order = {arc["_id"]: index for index, arc in enumerate(arcs)}
    scenes = await BaseRepository("scenes").collection.find(query).to_list(None)
    scenes = [scene for scene in scenes if scene["arc_id"] in arc_order]
    scenes.sort(key=lambda scene: (arc_order[scene["arc_id"]], scene["order_index"], str(scene["_id"])))
    directory = await ChapterService.list_chapters(novel_id, volume_id=volume_id)
    # 使用同一套父链过滤后的目录，不把回收站或失联子项纳入任务。
    documents = await BaseRepository("chapters").collection.find({**query, "_id": {"$in": [row["_id"] for row in directory]}}).to_list(None)
    by_id = {doc["_id"]: doc for doc in documents}
    chapters = [by_id[row["_id"]] for row in directory if row["_id"] in by_id]
    fields = ("_id", "arc_id", "order_index", "title", "summary", "planning")
    signature = canonical_request_hash({"arcs": [str(arc["_id"]) for arc in arcs],
        "scenes": [{key: str(scene[key]) if key in {"_id", "arc_id"} else scene.get(key) for key in fields} for scene in scenes]})
    written = sum(bool(chapter.get("content", "").strip()) for chapter in chapters)
    missing = sum(max(0, planned_count(scene, sum(chapter["scene_id"] == scene["_id"] for chapter in chapters))
                      - sum(chapter["scene_id"] == scene["_id"] for chapter in chapters)) for scene in scenes)
    return {"scenes": scenes, "chapters": chapters, "signature": signature,
            "summary": {"scenes": len(scenes), "chapters": len(chapters), "written": written,
                        "missing_plans": missing, "pending_bodies": len(chapters) - written + missing}}


def public_job(job: dict | None):
    if job is None:
        return None
    pending = job.get("pending") or {}
    return {"id": str(job["_id"]), "novel_id": str(job["novel_id"]), "volume_id": str(job["volume_id"]),
            "status": job["status"], "active": job["active"], "pause_requested": job.get("pause_requested", False),
            "cancel_requested": job.get("cancel_requested", False), "summary": job["summary"],
            "created_chapters": len(job.get("created_ids", [])), "written_chapters": len(job.get("written_ids", [])),
            "skipped_chapters": len(job.get("skipped_ids", [])), "completed_scenes": len(job.get("completed_scenes", [])),
            "current": job.get("current"), "error": job.get("error"),
            "has_saved_candidate": pending.get("result") is not None,
            "created_at": job["created_at"].replace(tzinfo=timezone.utc).isoformat(),
            "updated_at": job["updated_at"].replace(tzinfo=timezone.utc).isoformat()}


async def preview(novel_id: str, volume_id: str):
    data = await catalog(novel_id, volume_id)
    return data["summary"]


async def latest(novel_id: str, volume_id: str):
    await load_writing_parents(novel_id, "volume", volume_id)
    job = await jobs().find_one({"novel_id": ObjectId(novel_id), "volume_id": ObjectId(volume_id)}, sort=[("created_at", -1), ("_id", -1)])
    if job and job["status"] == "running" and job["lease_until"] <= now():
        changed = await jobs().find_one_and_update({"_id": job["_id"], "status": "running", "lease_until": {"$lte": now()}},
            {"$set": {"status": "paused", "updated_at": now(), "error": {"code": "VOLUME_WRITING_INTERRUPTED", "message": "服务曾中断，进度已保留，请继续任务。"}}},
            return_document=ReturnDocument.AFTER)
        job = changed or await jobs().find_one({"_id": job["_id"]})
    return public_job(job)


async def overview(novel_id: str):
    """当前小说优先显示未结束任务，否则显示最近一轮；不随当前篇章变化。"""
    await load_writing_parents(novel_id, "novel", novel_id)
    volumes = await BaseRepository("volumes").collection.find({"novel_id": ObjectId(novel_id), "is_deleted": False},
        {"_id": 1, "title": 1}).to_list(None)
    by_id = {row["_id"]: row["title"] for row in volumes}
    job = await jobs().find_one({"novel_id": ObjectId(novel_id), "volume_id": {"$in": list(by_id)}},
        sort=[("active", -1), ("created_at", -1), ("_id", -1)])
    if job is None:
        return None
    # 复用同一卷的租约过期处理，服务重启后显示“已暂停”，不冒充仍在执行。
    result = await latest(novel_id, str(job["volume_id"]))
    return {**result, "volume_title": by_id[job["volume_id"]]}


async def start(novel_id: str, volume_id: str, request: VolumeWritingStart, key: str):
    if not key or len(key) > 128:
        raise DomainApiError("VOLUME_WRITING_KEY_INVALID", "请提供有效的开始请求键", status_code=422)
    fingerprint = canonical_request_hash({"volume_id": volume_id, **request.model_dump(mode="json")})
    previous = await jobs().find_one({"novel_id": ObjectId(novel_id), "active": False}, sort=[("created_at", -1), ("_id", -1)])
    if previous and str(previous["_id"]) in _workers:
        # 终态已写入时，上一轮仍可能正在释放书籍占用；先等待清理完成。
        await asyncio.gather(_workers[str(previous["_id"])], return_exceptions=True)
    existing = await jobs().find_one({"novel_id": ObjectId(novel_id), "start_key": key})
    if existing:
        if existing["request_fingerprint"] != fingerprint:
            raise DomainApiError("VOLUME_WRITING_KEY_CONFLICT", "同一开始请求不能用于另一份设置", status_code=409)
        return public_job(existing)
    data = await catalog(novel_id, volume_id)
    if not data["scenes"]:
        raise DomainApiError("VOLUME_WRITING_NO_SCENES", "当前卷没有可写作的场景，请先保存场景规划。", status_code=422)
    refs = {}
    for task, field in (("plan_chapters_by_ai", "plan_flow_ref"), ("write_chapter_by_ai", "writing_flow_ref")):
        ref = getattr(request, field)
        try:
            flow = flow_store().get(ref.id, ref.revision) if ref else flow_store().get_default(task)
        except KeyError as exc:
            raise DomainApiError("VOLUME_WRITING_FLOW_NOT_FOUND", "所选流程修订不存在，请检查默认流程。", status_code=422) from exc
        if flow.task != task:
            raise DomainApiError("VOLUME_WRITING_FLOW_INVALID", "所选流程不属于对应写作任务", status_code=422)
        refs[task] = {"id": flow.id, "revision": flow.revision}
    await jobs().create_index("novel_id", unique=True, partialFilterExpression={"active": True}, name="one_active_volume_writing_per_novel")
    await jobs().create_index([("novel_id", 1), ("start_key", 1)], unique=True, name="volume_writing_start_key")
    job = {"_id": ObjectId(), "novel_id": ObjectId(novel_id), "volume_id": ObjectId(volume_id), "start_key": key,
           "request_fingerprint": fingerprint, "settings": request.model_dump(mode="json"), "flow_refs": refs,
           "catalog_signature": data["signature"], "summary": data["summary"], "status": "running", "active": True,
           "owner": OWNER, "lease_until": now() + timedelta(seconds=LEASE_SECONDS), "created_at": now(), "updated_at": now(),
           "pause_requested": False, "cancel_requested": False, "created_ids": [], "written_ids": [], "skipped_ids": [],
           "completed_scenes": [], "pending": None, "current": None, "error": None}
    try:
        await jobs().insert_one(job)
    except DuplicateKeyError as exc:
        replay = await jobs().find_one({"novel_id": ObjectId(novel_id), "start_key": key, "request_fingerprint": fingerprint})
        if replay:
            return public_job(replay)
        raise DomainApiError("VOLUME_WRITING_BUSY", "这本小说已有逐卷写作任务，请先继续或结束原任务。", status_code=409) from exc
    schedule(job["_id"])
    return public_job(job)


async def control(novel_id: str, volume_id: str, job_id: str, action: str):
    await latest(novel_id, volume_id)
    query = {"_id": ObjectId(job_id), "novel_id": ObjectId(novel_id), "volume_id": ObjectId(volume_id)}
    job = await jobs().find_one(query)
    if not job:
        raise DomainApiError("VOLUME_WRITING_NOT_FOUND", "逐卷任务不存在", status_code=404)
    if action == "resume":
        previous = _workers.get(str(job["_id"]))
        if job["status"] in {"paused", "failed"} and previous is not None:
            await asyncio.gather(previous, return_exceptions=True)
        changed = await jobs().find_one_and_update({**query, "status": {"$in": ["paused", "failed"]}, "active": True},
            {"$set": {"status": "running", "pause_requested": False, "cancel_requested": False, "error": None,
                      "owner": OWNER, "lease_until": now() + timedelta(seconds=LEASE_SECONDS), "updated_at": now()}},
            return_document=ReturnDocument.AFTER)
        if changed:
            schedule(changed["_id"])
            return public_job(changed)
    elif action in {"pause", "cancel"}:
        if job["status"] == "running":
            await jobs().update_one({**query, "status": "running"}, {"$set": {f"{'pause' if action == 'pause' else 'cancel'}_requested": True, "updated_at": now()}})
        elif action == "cancel" and job["active"]:
            await jobs().update_one({**query, "status": {"$in": ["paused", "failed"]}},
                {"$set": {"status": "cancelled", "active": False, "updated_at": now()}})
    return public_job(await jobs().find_one(query))


def schedule(job_id: ObjectId):
    key = str(job_id)
    if key not in _workers:
        task = asyncio.create_task(run_job(job_id), name=f"volume-writing:{key}")
        _workers[key] = task
        def finished(result):
            _workers.pop(key, None)
            if not result.cancelled() and result.exception():
                logger.error("逐卷任务退出失败", exc_info=result.exception())
        task.add_done_callback(finished)


async def update(job_id: ObjectId, fields: dict, additions: dict | None = None):
    values = {"$set": {**fields, "updated_at": now()}}
    if additions:
        values["$addToSet"] = {key: {"$each": value} for key, value in additions.items()}
    changed = await jobs().update_one({"_id": job_id, "owner": OWNER, "status": "running"}, values)
    if changed.matched_count != 1:
        raise asyncio.CancelledError()


async def should_stop(job_id):
    job = await jobs().find_one({"_id": job_id})
    if job["owner"] != OWNER or job["status"] != "running":
        raise asyncio.CancelledError()
    if job.get("cancel_requested") or job.get("pause_requested"):
        await update(job_id, {"status": "cancelled" if job.get("cancel_requested") else "paused", "active": not job.get("cancel_requested"), "lease_until": now()})
        return True
    return False


async def next_step(job: dict, data: dict):
    if data["signature"] != job["catalog_signature"]:
        raise DomainApiError("VOLUME_WRITING_STRUCTURE_CHANGED", "卷内篇章、场景或场景规划已变化。请结束本轮并重新检查范围；已保存正文会保留。", status_code=409)
    skipped, completed = [], []
    for scene in data["scenes"]:
        children = [row for row in data["chapters"] if row["scene_id"] == scene["_id"]]
        missing = planned_count(scene, len(children)) - len(children)
        target = None
        if missing > 0:
            target = ("plan_chapters_by_ai", scene, min(missing, 20))
        else:
            for chapter in children:
                if chapter.get("content", "").strip():
                    if str(chapter["_id"]) not in job["written_ids"]:
                        skipped.append(str(chapter["_id"]))
                else:
                    target = ("write_chapter_by_ai", chapter, 0)
                    break
        if target:
            await update(job["_id"], {}, {"skipped_ids": skipped, "completed_scenes": completed})
            return target
        completed.append(str(scene["_id"]))
    await update(job["_id"], {}, {"skipped_ids": skipped, "completed_scenes": completed})
    return None


async def make_pending(job, task, target, count):
    chapter = task == "write_chapter_by_ai"
    inputs = {"user_guidance": job["settings"]["user_guidance"]}
    if not chapter:
        inputs.update(count=count, count_mode="fixed")
    scope = {"novel_id": str(job["novel_id"]), "target_type": "chapter" if chapter else "scene", "target_id": str(target["_id"]),
             **({"expected_version": target["version"]} if chapter else {"mode": "append"})}
    request = GraphRunRequest(flow_ref=job["flow_refs"][task], flow_inputs=inputs, scope=scope,
                              generation_params=job["settings"]["generation_params"], use_stream=False)
    current = {"stage": "writing" if chapter else "planning", "title": target["title"], "target_id": str(target["_id"]),
               "scene_id": str(target["scene_id"] if chapter else target["_id"]), "node": ""}
    pending = {"task": task, "request": request.model_dump(mode="json"), "key": uuid4().hex, "result": None}
    await update(job["_id"], {"pending": pending, "current": current})
    return pending


async def perform(job: dict, pending: dict):
    job_id = job["_id"]
    await volume_writing_log.collection().create_index([("job_id", 1), ("_id", -1)], name="volume_writing_log_history")
    await volume_writing_log.collection().update_many({"job_id": job_id, "state": "running"}, {"$set": {"state": "stopped"}})
    pending["log_attempt"] = uuid4().hex
    await update(job_id, {"pending": pending})
    current_log = job.get("current") or (await jobs().find_one({"_id": job_id})).get("current") or {}
    async def log(data):
        await volume_writing_log.record(job_id, pending["key"], pending["log_attempt"], current_log, data)
    if pending["result"] is None:
        request = GraphRunRequest.model_validate(pending["request"])
        # 只有当前服务仍持有的续跑凭证可使用；重启后仍保留已完成节点。
        if pending.get("continuation_owner") != OWNER:
            request.agent_continuation = None
        if pending["task"] == "write_chapter_by_ai":
            current = await ChapterService.get_chapter(request.scope["target_id"], novel_id=str(job["novel_id"]))
            if current.get("content", "").strip():
                await update(job_id, {"pending": None}, {"skipped_ids": [str(current["_id"])]})
                return
        progress_times = {}
        async def event(value):
            data = value.get("data", {})
            kind = value.get("event")
            path = data.get("instance_path") or data.get("node_id") or "_task"
            stamp = time.monotonic()
            # 流式文字片段不入日志；进度最多每秒更新一次，节点终态立即保存。
            if kind in {"step", "error"} or kind == "progress" and stamp - progress_times.get(path, 0) >= 1:
                await log({**data, **({"state": "failed"} if kind == "error" else {})})
                progress_times[path] = stamp
                if data.get("label") or data.get("node_id"):
                    await update(job_id, {"current.node": data.get("label") or data["node_id"]})
            if data.get("state") in {"done", "completed", "cached"} and data.get("outputs") is not None and data.get("input_fingerprint"):
                cached = {name: data[name] for name in ("node_id", "instance_path", "input_fingerprint", "outputs")}
                nodes = pending["request"].setdefault("cached_nodes", [])
                pending["request"]["cached_nodes"] = [item for item in nodes if item["instance_path"] != cached["instance_path"]] + [cached]
                await update(job_id, {"pending": pending})
            if value.get("event") == "error":
                continuation = data.get("details", {}).get("agent_continuation") or data.get("agent_continuation")
                if continuation:
                    pending["request"]["agent_continuation"] = {"token": continuation["token"], "additional_rounds": continuation.get("resume_rounds", 0) if continuation.get("reason") == "network" else 2}
                    pending["continuation_owner"] = OWNER
                    await update(job_id, {"pending": pending})
        try:
            result = await generate_task_flow(pending["task"], request, scope=request.scope, stream=False, on_event=event)
        except DomainApiError as exc:
            if exc.code in {"AGENT_CONTINUATION_EXPIRED", "AGENT_CONTINUATION_CHANGED"}:
                pending["request"].pop("agent_continuation", None)
                pending.pop("continuation_owner", None)
                await update(job_id, {"pending": pending})
                raise DomainApiError("VOLUME_WRITING_CONTINUATION_RESET", "本步续跑凭证已失效，已保留完成节点。再次继续将重试未完成的模型步骤。", status_code=409) from exc
            raise
        pending["result"] = result
        # 先持久保存完整候选和固定确认键，再进行业务写入。
        await update(job_id, {"pending": pending, "current.stage": "saving", "current.node": ""})
    if await should_stop(job_id):
        return
    result = pending["result"]
    chapter = pending["task"] == "write_chapter_by_ai"
    await log({"instance_path": "_save", "label": "保存并核对", "state": "running"})
    request = WritingConfirmV1.model_validate({"candidate": result["candidate"], "basis": result["basis"],
        **({"expected_version": pending["request"]["scope"]["expected_version"]} if chapter else {})})
    saved = await confirm_writing(pending["task"], pending["request"]["scope"]["target_id"], request, idempotency_key=pending["key"])
    for chapter_id in saved["ids"]:
        actual = await ChapterService.get_chapter(chapter_id, novel_id=str(job["novel_id"]))
        if str(actual["volume_id"]) != str(job["volume_id"]) or (chapter and actual["content"] != result["candidate"]["content"]):
            raise DomainApiError("VOLUME_WRITING_SAVED_CHANGED", "保存后的章节已被修改，请检查正文后结束本轮。", status_code=409)
    await log({"instance_path": "_save", "label": "保存并核对", "state": "done", "outputs": {"saved_chapter_ids": saved["ids"]}})
    await update(job_id, {"pending": None, "current": None}, {"written_ids" if chapter else "created_ids": saved["ids"]})


async def run_job(job_id: ObjectId):
    task = asyncio.current_task()
    async def heartbeat():
        while True:
            await asyncio.sleep(10)
            try:
                changed = await jobs().update_one({"_id": job_id, "owner": OWNER, "status": "running"},
                    {"$set": {"lease_until": now() + timedelta(seconds=LEASE_SECONDS)}})
                if changed.matched_count != 1:
                    task.cancel()
                    return
            except asyncio.CancelledError:
                raise
            except Exception:
                task.cancel()
                return
    renewal = asyncio.create_task(heartbeat())
    try:
        job = await jobs().find_one({"_id": job_id})
        async with book_lease(str(job["novel_id"]), f"volume-writing:{job_id}"):
            while not await should_stop(job_id):
                job = await jobs().find_one({"_id": job_id})
                data = await catalog(str(job["novel_id"]), str(job["volume_id"]))
                if data["signature"] != job["catalog_signature"]:
                    raise DomainApiError("VOLUME_WRITING_STRUCTURE_CHANGED", "场景范围或规划已变化，请结束本轮后重新开始。已保存章节保留。", status_code=409)
                pending = job.get("pending")
                if pending is None:
                    step = await next_step(job, data)
                    if step is None:
                        await update(job_id, {"status": "completed", "active": False, "current": None, "lease_until": now()})
                        break
                    pending = await make_pending(job, *step)
                await perform(job, deepcopy(pending))
    except asyncio.CancelledError:
        await jobs().update_one({"_id": job_id, "owner": OWNER, "status": "running"},
            {"$set": {"status": "paused", "lease_until": now(), "updated_at": now(), "error": {"code": "VOLUME_WRITING_INTERRUPTED", "message": "服务停止或连接中断，任务进度已保留。"}}})
    except Exception as exc:
        logger.exception("逐卷写作暂停 job=%s", job_id)
        _, code, message, fields, _ = exception_to_error(exc, fallback_message="逐卷写作暂时失败，进度已保留，请重试。")
        failed_job = await jobs().find_one({"_id": job_id})
        pending = failed_job.get("pending") or {}
        try:
            await volume_writing_log.record(job_id, pending.get("key", "_job"), pending.get("log_attempt", uuid4().hex), failed_job.get("current") or {},
                {"instance_path": "_error", "label": "任务错误", "state": "failed", "message": message})
        except Exception:
            logger.exception("逐卷任务错误日志保存失败 job=%s", job_id)
        await jobs().update_one({"_id": job_id, "owner": OWNER, "status": "running"},
            {"$set": {"status": "failed", "lease_until": now(), "updated_at": now(), "error": {"code": code, "message": message, "field_errors": fields}}})
    finally:
        renewal.cancel()
        with suppress(asyncio.CancelledError):
            await renewal


async def shutdown():
    tasks = list(_workers.values())
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
