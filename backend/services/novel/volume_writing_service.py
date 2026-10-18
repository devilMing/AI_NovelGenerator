"""持久化逐卷队列；生成和确认仍调用原任务图及事务服务。"""
from __future__ import annotations

import asyncio
from contextlib import suppress
from copy import deepcopy
from datetime import timedelta, timezone
import logging
import time
from uuid import uuid4

from bson import ObjectId, BSON
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from backend.api.error_protocol import DomainApiError, exception_to_error
from backend.db.base import BaseRepository
from backend.db.transaction import run_mongo_write_unit
from backend.db.utils import get_utc_now
from backend.db.write_contracts import canonical_request_hash
from backend.services.assistant.runtime import book_lease
from backend.services.novel.chapter_service import ChapterService
from backend.services.novel.volume_writing_contract import VolumeWritingStart
from backend.services.novel import volume_writing_log
from backend.services.novel.first_chapter_review import review_candidate
from backend.services.novel.first_chapter_service import attach_review_sources
from backend.services.novel import chapter_fact_service
from backend.services.novel.chapter_fact_contract import ChapterFactExtractionV1, ChapterFactReviewV1
from backend.services.novel.writing_context import load_writing_context, load_writing_parents
from backend.services.novel.writing_contract import WritingConfirmV1
from backend.services.novel.writing_generation import confirm_writing
from backend.services.task_flows.contracts import GraphRunRequest
from backend.services.task_flows.integration import generate_task_flow
from backend.services.task_flows.store import flow_store

logger = logging.getLogger(__name__)
OWNER = uuid4().hex
LEASE_SECONDS = 45
_workers: dict[str, asyncio.Task] = {}


class _StopAfterCheckpoint(Exception):
    """Internal control flow: a durable graph node finished after pause/cancel."""


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
    cached_outputs = [row.get("outputs") for row in (pending.get("request") or {}).get("cached_nodes", [])]
    has_saved_candidate = pending.get("result") is not None or any(
        isinstance(output, dict) and ("content" in output or "chapters" in output)
        for output in cached_outputs
    )
    return {"id": str(job["_id"]), "novel_id": str(job["novel_id"]), "volume_id": str(job["volume_id"]),
            "status": job["status"], "active": job["active"], "pause_requested": job.get("pause_requested", False),
            "cancel_requested": job.get("cancel_requested", False), "summary": job["summary"],
            "created_chapters": len(job.get("created_ids", [])), "written_chapters": len(job.get("written_ids", [])),
            "skipped_chapters": len(job.get("skipped_ids", [])), "completed_scenes": len(job.get("completed_scenes", [])),
            "current": job.get("current"), "error": job.get("error"),
            "has_saved_candidate": has_saved_candidate,
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
    existing = await jobs().find_one({"novel_id": ObjectId(novel_id), "start_key": key})
    if existing:
        if existing["request_fingerprint"] != canonical_request_hash({"volume_id": volume_id, **request.model_dump(mode="json")}):
            raise DomainApiError("VOLUME_WRITING_KEY_CONFLICT", "同一开始请求不能用于另一份设置", status_code=409)
        return public_job(existing)
    async with book_lease(novel_id, f"admit-volume-writing:{uuid4().hex}"):
        result = await _start(novel_id, volume_id, request, key)
    schedule(ObjectId(result["id"]))
    return result


async def _start(novel_id: str, volume_id: str, request: VolumeWritingStart, key: str):
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
    if await BaseRepository("first_chapter_jobs").collection.find_one({"novel_id": ObjectId(novel_id), "active": True}):
        raise DomainApiError("VOLUME_WRITING_BUSY", "这本书已有一键首章任务，请先完成或结束原任务。", status_code=409)
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
    return public_job(job)


async def control(novel_id: str, volume_id: str, job_id: str, action: str):
    await latest(novel_id, volume_id)
    query = {"_id": ObjectId(job_id), "novel_id": ObjectId(novel_id), "volume_id": ObjectId(volume_id)}
    job = await jobs().find_one(query)
    if not job:
        raise DomainApiError("VOLUME_WRITING_NOT_FOUND", "逐卷任务不存在", status_code=404)
    if action == "resume":
        if await BaseRepository("first_chapter_jobs").collection.find_one({"novel_id": ObjectId(novel_id), "active": True}):
            raise DomainApiError("VOLUME_WRITING_BUSY", "这本书已有一键首章任务，请先完成或结束原任务。", status_code=409)
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
                    fact_batch = await BaseRepository("chapter_fact_batches").collection.find_one({
                        "novel_id": job["novel_id"], "chapter_id": chapter["_id"],
                        "chapter_version": chapter["version"], "status": "confirmed",
                    }, {"_id": 1})
                    if fact_batch is None:
                        target = ("close_existing_chapter_facts", chapter, 0)
                        break
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
    if task == "close_existing_chapter_facts":
        revision = await BaseRepository("chapter_revisions").collection.find_one({
            "chapter_id": target["_id"], "version": target["version"],
        })
        if revision is None:
            raise DomainApiError(
                "VOLUME_WRITING_REVISION_MISSING",
                "已有正文缺少对应的不可变历史版本，无法安全更新章后事实。",
                status_code=409,
            )
        scope = {
            "novel_id": str(job["novel_id"]), "target_type": "chapter",
            "target_id": str(target["_id"]), "expected_version": target["version"],
        }
        context = await load_writing_context(
            "write_chapter_by_ai", {}, scope, check_ready=False,
        )
        pending = {
            "task": task,
            "request": {"scope": scope, "generation_params": job["settings"]["generation_params"]},
            "key": uuid4().hex,
            "result": {
                "context": context["context"],
                "basis": {
                    "context_basis": context["context_basis"],
                    "source_refs": context["source_refs"],
                },
            },
            "review": {"summary": "已有正文仅补建章后事实，不重新生成或覆盖正文。", "issues": []},
            "saved_chapter": {
                "id": str(target["_id"]), "version": target["version"],
                "revision_id": str(revision["_id"]), "content_hash": revision["content_hash"],
            },
            "fact_closure": {"attempts": [], "reviews": []},
        }
        current = {
            "stage": "fact_extracting", "title": target["title"],
            "target_id": str(target["_id"]), "scene_id": str(target["scene_id"]), "node": "",
        }
        await update(job["_id"], {"pending": pending, "current": current})
        return pending
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
    if pending["task"] == "close_existing_chapter_facts":
        await perform_fact_closure(job, pending, log, current_log)
        if await should_stop(job_id):
            return
        await update(
            job_id, {"pending": None, "current": None},
            {"skipped_ids": [pending["saved_chapter"]["id"]]},
        )
        return
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
            continuation = data.get("details", {}).get("agent_continuation") or data.get("agent_continuation")
            if continuation and continuation.get("flow_recovery"):
                pending["request"]["agent_continuation"] = {
                    "token": continuation["token"],
                    "additional_rounds": continuation.get("resume_rounds", 0) if continuation.get("reason") == "network" else 2,
                }
                pending["continuation_owner"] = OWNER
                await update(job_id, {"pending": pending})
            # 流式文字片段不入日志；进度最多每秒更新一次，节点终态立即保存。
            if kind in {"step", "error"} or kind == "progress" and stamp - progress_times.get(path, 0) >= 1:
                await log({**data, **({"state": "failed"} if kind == "error" else {})})
                progress_times[path] = stamp
                if data.get("label") or data.get("node_id"):
                    await update(job_id, {"current.node": data.get("label") or data["node_id"]})
            checkpoint_saved = (data.get("state") in {"done", "completed", "cached"}
                                and data.get("outputs") is not None and data.get("input_fingerprint"))
            if checkpoint_saved:
                cached = {name: data[name] for name in ("node_id", "instance_path", "input_fingerprint", "outputs")}
                nodes = pending["request"].setdefault("cached_nodes", [])
                pending["request"]["cached_nodes"] = [item for item in nodes if item["instance_path"] != cached["instance_path"]] + [cached]
                await update(job_id, {"pending": pending})
                control = await jobs().find_one({"_id": job_id}, {"owner": 1, "status": 1, "pause_requested": 1, "cancel_requested": 1})
                if (control and control.get("owner") == OWNER and control.get("status") == "running"
                        and (control.get("pause_requested") or control.get("cancel_requested"))):
                    raise _StopAfterCheckpoint()
            if value.get("event") == "error":
                if continuation:
                    pending["request"]["agent_continuation"] = {"token": continuation["token"], "additional_rounds": continuation.get("resume_rounds", 0) if continuation.get("reason") == "network" else 2}
                    pending["continuation_owner"] = OWNER
                    await update(job_id, {"pending": pending})
        try:
            result = await generate_task_flow(pending["task"], request, scope=request.scope, stream=False, on_event=event)
        except _StopAfterCheckpoint:
            return
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
    if pending.get("review") is None:
        if not result.get("context"):
            # As in first-chapter writing, retain paid output before reading audit sources.
            result = await attach_review_sources(pending["task"], result, pending["request"]["generation_params"])
            if len(BSON.encode({"result": result})) > 12_000_000:
                raise DomainApiError("VOLUME_WRITING_REVIEW_TOO_LARGE", "审校资料过大，原候选已保留，请缩小流程资料范围。", status_code=422)
            pending["result"] = result
        await update(job_id, {"pending": pending, "current.stage": "reviewing", "current.node": "一致性检查"})
        current_log["stage"] = "reviewing"
        await log({"instance_path": "_consistency_review", "label": "核对剧情与事实一致性", "state": "running"})
        pending["review"] = await review_candidate(pending["task"], result, {})
        await update(job_id, {"pending": pending})
        await log({"instance_path": "_consistency_review", "label": "核对剧情与事实一致性",
                   "state": "failed" if pending["review"]["issues"] else "done", "outputs": pending["review"]})
    if pending["review"]["issues"]:
        raise DomainApiError("VOLUME_WRITING_CONSISTENCY_CONFLICT", "一致性检查未通过，候选与引证已保留，尚未正式保存。请查看日志，处理冲突后结束本轮重新生成。", status_code=409)
    if await should_stop(job_id):
        return
    chapter = pending["task"] == "write_chapter_by_ai"
    if pending.get("saved_chapter") is None:
        await update(job_id, {"current.stage": "saving", "current.node": ""})
        current_log["stage"] = "saving"
        await log({"instance_path": "_save", "label": "保存并核对", "state": "running"})
        request = WritingConfirmV1.model_validate({"candidate": result["candidate"], "basis": result["basis"],
            **({"expected_version": pending["request"]["scope"]["expected_version"]} if chapter else {})})
        async def save(session):
            saved = await confirm_writing(pending["task"], pending["request"]["scope"]["target_id"], request,
                                          idempotency_key=pending["key"], session=session)
            meta = None
            for chapter_id in saved["ids"]:
                actual = await ChapterService.get_chapter(chapter_id, novel_id=str(job["novel_id"]), session=session)
                if str(actual["volume_id"]) != str(job["volume_id"]) or (chapter and actual["content"] != result["candidate"]["content"]):
                    raise DomainApiError("VOLUME_WRITING_SAVED_CHANGED", "保存后的章节已被修改，请检查正文后结束本轮。", status_code=409)
                if chapter:
                    revision = await record_revision_review(job, pending, actual, session=session)
                    meta = {"id": str(actual["_id"]), "version": actual["version"], "revision_id": str(revision["_id"]),
                            "content_hash": revision["content_hash"]}
            return saved, meta
        saved, meta = await run_mongo_write_unit(save, "save_volume_writing_with_review", require_transaction=True)
        await log({"instance_path": "_save", "label": "保存并核对", "state": "done", "outputs": {"saved_chapter_ids": saved["ids"]}})
        if not chapter:
            await update(job_id, {"pending": None, "current": None}, {"created_ids": saved["ids"]})
            return
        pending["saved_chapter"] = meta
        pending["fact_closure"] = pending.get("fact_closure") or {"attempts": [], "reviews": []}
        await update(job_id, {"pending": pending, "current.stage": "fact_extracting", "current.node": ""})
    if await should_stop(job_id):
        return
    await perform_fact_closure(job, pending, log, current_log)
    if await should_stop(job_id):
        return
    await update(job_id, {"pending": None, "current": None}, {"written_ids": [pending["saved_chapter"]["id"]]})


async def record_revision_review(job, pending, actual, *, session):
    """The immutable adopted revision and its review must commit together."""
    revision = await BaseRepository("chapter_revisions").collection.find_one({"chapter_id": actual["_id"], "version": actual["version"]}, session=session)
    if revision is None:
        raise DomainApiError("VOLUME_WRITING_REVISION_MISSING", "正文历史版本尚未确认，候选已保留，请检查保存记录。", status_code=409)
    await BaseRepository("volume_writing_reports").collection.update_one({"_id": f"{job['_id']}:{pending['key']}"},
        {"$setOnInsert": {"job_id": job["_id"], "novel_id": job["novel_id"], "chapter_id": actual["_id"],
                          "revision_id": revision["_id"], "content_hash": revision["content_hash"],
                          "review": pending["review"], "basis": pending["result"]["basis"], "created_at": now()}}, upsert=True, session=session)
    return revision


async def perform_fact_closure(job: dict, pending: dict, log, current_log: dict) -> None:
    """Resume extraction/review/adoption from the last durable chapter checkpoint."""
    job_id = job["_id"]
    state = pending.setdefault("fact_closure", {"attempts": [], "reviews": []})
    saved = pending["saved_chapter"]
    chapter = await BaseRepository("chapters").collection.find_one({
        "_id": ObjectId(saved["id"]), "novel_id": job["novel_id"], "is_deleted": False,
    })
    revision = await BaseRepository("chapter_revisions").collection.find_one({
        "_id": ObjectId(saved["revision_id"]), "chapter_id": ObjectId(saved["id"]),
        "content_hash": saved["content_hash"],
    })
    if chapter is None or revision is None or chapter["version"] != saved["version"]:
        raise DomainApiError("VOLUME_WRITING_SAVED_CHANGED", "正文保存后已经被修改，章后事实不会套用到新版本。", status_code=409)

    source_refs = pending["result"].get("basis", {}).get("source_refs", [])
    allowed = {(row["resource_type"], row["id"]) for row in source_refs
               if row.get("resource_type") in {"character", "faction", "character_relation", "item", "location", "world_rule", "foreshadowing"}}
    allowed.add(("novel", str(job["novel_id"])))
    context = pending["result"].get("context") or {}
    prior = context.get("confirmed_chapter_facts") or []
    current = {(row["subject_type"], row["subject_id"], row["field"]): row["after"] for row in prior}
    material = {
        "chapter_revision": {"chapter_id": saved["id"], "revision_id": saved["revision_id"],
                             "version": saved["version"], "content_hash": saved["content_hash"], "content": revision["content"]},
        "allowed_subjects": [{"subject_type": kind, "subject_id": identity} for kind, identity in sorted(allowed)],
        "current_facts": prior,
        "writing_context": context,
    }

    async def verify_revision():
        actual = await BaseRepository("chapters").collection.find_one({"_id": chapter["_id"], "novel_id": job["novel_id"], "is_deleted": False})
        if actual is None or actual["version"] != saved["version"] or actual["content"] != revision["content"]:
            raise DomainApiError("VOLUME_WRITING_SAVED_CHANGED", "事实模型请求前正文已经变化。", status_code=409)

    while True:
        if state.get("extraction") is None:
            await update(job_id, {"current.stage": "fact_extracting", "current.node": "提取章后事实"})
            current_log["stage"] = "fact_extracting"
            await log({"instance_path": "_fact_extract", "label": "提取章后事实", "state": "running"})
            extracted, usage = await chapter_fact_service.extract_facts(
                material,
                repair=state["attempts"][-1] if state["attempts"] else None,
                before_send=verify_revision,
            )
            state["extraction"] = extracted
            state["extraction_usage"] = usage
            await update(job_id, {"pending": pending})
            await log({"instance_path": "_fact_extract", "label": "提取章后事实", "state": "done",
                       "outputs": {"count": len(extracted["facts"]), "summary": extracted["summary"]}})
        extraction_model = ChapterFactExtractionV1.model_validate(state["extraction"])
        try:
            chapter_fact_service.validate_extraction(extraction_model, revision["content"], allowed, current)
        except DomainApiError as exc:
            if exc.code not in {
                "CHAPTER_FACT_SUBJECT_UNKNOWN", "CHAPTER_FACT_EVIDENCE_MISMATCH",
                "CHAPTER_FACT_BEFORE_CHANGED",
            }:
                raise
            if len(state["attempts"]) >= 2:
                raise DomainApiError(
                    "VOLUME_WRITING_FACT_CONFLICT",
                    "章后事实两次自动修订后仍未通过确定性校验，正文已保存，事实候选和日志已保留。",
                    status_code=409,
                ) from exc
            state["attempts"].append({
                "extraction": state["extraction"], "reviews": [],
                "validation_error": {"code": exc.code, "message": exc.message, "details": exc.details},
            })
            state["extraction"] = None
            state["extraction_usage"] = None
            state["reviews"] = []
            await update(job_id, {"pending": pending})
            attempt_number = len(state["attempts"])
            await log({"instance_path": f"_fact_repair_{attempt_number}", "label": "修订章后事实候选", "state": "done",
                       "outputs": {"attempt": attempt_number, "validation_error": exc.code}})
            if await should_stop(job_id):
                return
            continue
        if await should_stop(job_id):
            return

        required_reviews = 2 if any(row.risk == "high" for row in extraction_model.facts) else 1
        while len(state["reviews"]) < required_reviews:
            number = len(state["reviews"]) + 1
            await update(job_id, {"current.stage": "fact_reviewing", "current.node": "独立审核章后事实"})
            current_log["stage"] = "fact_reviewing"
            await log({"instance_path": f"_fact_review_{number}", "label": "独立审核章后事实", "state": "running"})
            reviewed, usage = await chapter_fact_service.review_facts(material, state["extraction"], before_send=verify_revision)
            state["reviews"].append({"review": reviewed, "usage": usage})
            await update(job_id, {"pending": pending})
            await log({"instance_path": f"_fact_review_{number}", "label": "独立审核章后事实", "state": "done",
                       "outputs": reviewed})
            if await should_stop(job_id):
                return
        review_models = [ChapterFactReviewV1.model_validate(row["review"], context={"fact_count": len(extraction_model.facts)}) for row in state["reviews"]]
        invalid_reviews = [index for index, review in enumerate(review_models) if any(
            decision.source_quote and decision.source_quote not in revision["content"]
            for decision in review.decisions
        )]
        if invalid_reviews:
            review_attempts = state.setdefault("review_attempts", [])
            if len(review_attempts) >= 2:
                raise DomainApiError(
                    "CHAPTER_FACT_REVIEW_EVIDENCE_MISMATCH",
                    "事实审核两次自动重试后仍引用了正文中不存在的文字，正文和事实候选已保留。",
                    status_code=422,
                )
            review_attempts.append({
                "reviews": [state["reviews"][index] for index in invalid_reviews],
                "validation_error": {
                    "code": "CHAPTER_FACT_REVIEW_EVIDENCE_MISMATCH",
                    "message": "事实审核引用了正文中不存在的文字",
                    "review_indices": invalid_reviews,
                },
            })
            state["reviews"] = [row for index, row in enumerate(state["reviews"]) if index not in invalid_reviews]
            await update(job_id, {"pending": pending})
            await log({
                "instance_path": f"_fact_review_retry_{len(review_attempts)}",
                "label": "重新审核章后事实",
                "state": "done",
                "outputs": {"attempt": len(review_attempts), "validation_error": "CHAPTER_FACT_REVIEW_EVIDENCE_MISMATCH"},
            })
            if await should_stop(job_id):
                return
            continue
        merged = chapter_fact_service.merge_reviews(extraction_model, review_models)
        if not merged["blocking"]:
            state["merged"] = merged
            await update(job_id, {"pending": pending})
            break
        if len(state["attempts"]) >= 2:
            raise DomainApiError("VOLUME_WRITING_FACT_CONFLICT", "章后事实两次自动修订后仍存在阻断冲突，正文已保存，事实候选和审核日志已保留。", status_code=409)
        state["attempts"].append({"extraction": state["extraction"], "reviews": state["reviews"], "merged": merged})
        state["extraction"] = None
        state["extraction_usage"] = None
        state["reviews"] = []
        await update(job_id, {"pending": pending})
        await log({"instance_path": f"_fact_repair_{len(state['attempts'])}", "label": "修订章后事实候选", "state": "done",
                   "outputs": {"attempt": len(state["attempts"]), "blocked_indices": merged["blocked_indices"]}})
        if await should_stop(job_id):
            return

    if state.get("batch_id") is None:
        await update(job_id, {"current.stage": "fact_saving", "current.node": "更新事实库"})
        current_log["stage"] = "fact_saving"
        await log({"instance_path": "_fact_save", "label": "更新事实库", "state": "running"})
        batch = await chapter_fact_service.adopt_fact_batch(
            str(job["novel_id"]), chapter, revision, state["extraction"],
            [row["review"] for row in state["reviews"]], state["merged"], state["attempts"],
        )
        state["batch_id"] = batch["_id"]
        await update(job_id, {"pending": pending})
        await log({"instance_path": "_fact_save", "label": "更新事实库", "state": "done",
                   "outputs": {"batch_id": batch["_id"], "accepted": len(batch["facts"]),
                               "omitted": len(batch["omitted_indices"]), "no_changes": not batch["facts"]}})


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
