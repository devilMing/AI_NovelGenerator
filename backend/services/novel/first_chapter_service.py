"""Persistent novel-to-first-chapter job. Original business saves remain authoritative."""
from __future__ import annotations

import asyncio
from contextlib import suppress
from copy import deepcopy
from datetime import timedelta, timezone
import logging
import json
import time
from uuid import uuid4

from bson import ObjectId, BSON
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError
from pydantic import Field, field_validator

from backend.api.contracts import StrictRequestModel
from backend.api.error_protocol import DomainApiError, exception_to_error
from backend.db.base import BaseRepository
from backend.db.transaction import run_mongo_write_unit
from backend.db.utils import get_utc_now
from backend.db.write_contracts import canonical_request_hash
from backend.services.assistant.runtime import book_lease
from backend.services.novel import first_chapter_plan as plan
from backend.services.novel.first_chapter_review import review_candidate
from backend.services.novel.first_chapter_save import save_candidate
from backend.services.novel.first_chapter_log import timeline
from backend.services.novel.volume_writing_log import safe_event, public as public_log
from backend.services.task_flows.contracts import GraphRunRequest
from backend.services.task_flows.integration import generate_task_flow
from backend.services.task_flows.registry import CONTENT_TASKS, GENERATION_FIELDS
from backend.services.task_flows.store import flow_store

logger = logging.getLogger(__name__)
OWNER = uuid4().hex
_workers = {}


class Start(StrictRequestModel):
    user_guidance: str = Field(default="", max_length=1000)
    include_optional: bool = True
    generation_params: dict = Field(default_factory=dict)

    @field_validator("generation_params")
    @classmethod
    def parameters(cls, value):
        if set(value) - GENERATION_FIELDS:
            raise ValueError("生成参数包含未声明字段")
        return value


def collection(name="jobs"):
    return BaseRepository(f"first_chapter_{name}").collection


def now():
    return get_utc_now().replace(tzinfo=None)


def public(job):
    if not job:
        return None
    return {"id": str(job["_id"]), "novel_id": str(job["novel_id"]),
            **{k: job.get(k) for k in ("status", "active", "pause_requested", "cancel_requested", "current", "completed_steps", "error", "chapter_id")},
            "has_saved_candidate": job.get("has_saved_candidate", False),
            **{k: job[k].replace(tzinfo=timezone.utc).isoformat() for k in ("created_at", "updated_at")}}


async def latest(novel_id):
    await plan.load_writing_parents(novel_id, "novel", novel_id)
    job = await collection().find_one({"novel_id": ObjectId(novel_id)}, sort=[("created_at", -1), ("_id", -1)])
    if job and job["status"] == "running" and job["lease_until"] <= now():
        await collection().update_one({"_id": job["_id"], "status": "running", "lease_until": {"$lte": now()}},
            {"$set": {"status": "paused", "updated_at": now(), "error": {"code": "FIRST_CHAPTER_INTERRUPTED", "message": "服务已中断，已保存进度；请继续。"}}})
        job = await collection().find_one({"_id": job["_id"]})
    return public(job)


async def history(novel_id):
    rows = await collection().find({"novel_id": ObjectId(novel_id)}).sort([("created_at", -1), ("_id", -1)]).limit(30).to_list(30)
    return [public(row) for row in rows]


async def check_volume_job(novel_id):
    if await BaseRepository("volume_writing_jobs").collection.find_one({"novel_id": ObjectId(novel_id), "active": True}):
        raise DomainApiError("FIRST_CHAPTER_BUSY", "这本书已有逐卷任务，请先完成或结束它。", status_code=409)


async def start(novel_id, request, key):
    # Replay before book admission: network retries must work while the worker owns the book.
    existing = await collection().find_one({"novel_id": ObjectId(novel_id), "start_key": key})
    if existing:
        await plan.load_writing_parents(novel_id, "novel", novel_id)
        if existing["fingerprint"] != canonical_request_hash(request.model_dump(mode="json")):
            raise DomainApiError("FIRST_CHAPTER_KEY_CONFLICT", "同一开始请求不能用于不同设置", status_code=409)
        return public(existing)
    async with book_lease(novel_id, f"admit-first-chapter:{uuid4().hex}"):
        result = await _start(novel_id, request, key)
    schedule(ObjectId(result["id"]))
    return result


async def _start(novel_id, request, key):
    await plan.load_writing_parents(novel_id, "novel", novel_id)
    if not key or len(key) > 128:
        raise DomainApiError("FIRST_CHAPTER_KEY_INVALID", "开始请求键无效", status_code=422)
    settings = request.model_dump(mode="json")
    digest = canonical_request_hash(settings)
    existing = await collection().find_one({"novel_id": ObjectId(novel_id), "start_key": key})
    if existing:
        if digest != existing["fingerprint"]:
            raise DomainApiError("FIRST_CHAPTER_KEY_CONFLICT", "同一开始请求不能用于不同设置", status_code=409)
        return public(existing)
    await check_volume_job(novel_id)
    await collection().create_index("novel_id", unique=True, partialFilterExpression={"active": True}, name="one_active_first_chapter")
    await collection().create_index([("novel_id", 1), ("start_key", 1)], unique=True)
    job = {"_id": ObjectId(), "novel_id": ObjectId(novel_id), "start_key": key, "fingerprint": digest, "settings": settings,
           "status": "running", "active": True, "owner": OWNER, "lease_until": now() + timedelta(seconds=45),
           "created_at": now(), "updated_at": now(), "completed_steps": [], "pending": None, "current": None,
           "pause_requested": False, "cancel_requested": False, "error": None, "has_saved_candidate": False}
    try:
        await collection().insert_one(job)
    except DuplicateKeyError as exc:
        existing = await collection().find_one({"novel_id": job["novel_id"], "start_key": key, "fingerprint": digest})
        if existing:
            return public(existing)
        raise DomainApiError("FIRST_CHAPTER_BUSY", "这本书已有一键写作任务，请继续原任务。", status_code=409) from exc
    return public(job)


async def control(novel_id, job_id, action):
    await latest(novel_id)
    query = {"_id": ObjectId(job_id), "novel_id": ObjectId(novel_id)}
    job = await collection().find_one(query)
    if not job:
        raise DomainApiError("FIRST_CHAPTER_NOT_FOUND", "一键写作任务不存在", status_code=404)
    if action == "resume" and job["active"] and job["status"] in {"paused", "failed"}:
        await check_volume_job(novel_id)
        if str(job["_id"]) in _workers:
            await asyncio.gather(_workers[str(job["_id"])] , return_exceptions=True)
        job = await collection().find_one_and_update({**query, "status": {"$in": ["paused", "failed"]}},
            {"$set": {"status": "running", "pause_requested": False, "cancel_requested": False, "owner": OWNER,
                      "lease_until": now() + timedelta(seconds=45), "updated_at": now(), "error": None}}, return_document=ReturnDocument.AFTER)
        if job:
            schedule(job["_id"])
    elif action in {"pause", "cancel"}:
        if job["status"] == "running":
            await collection().update_one({**query, "status": "running"}, {"$set": {f"{'pause' if action == 'pause' else 'cancel'}_requested": True}})
        elif action == "cancel" and job["active"]:
            await collection().update_one({**query, "status": {"$in": ["paused", "failed"]}}, {"$set": {"status": "cancelled", "active": False, "updated_at": now()}})
    return public(await collection().find_one(query))


async def update(job_id, fields):
    result = await collection().update_one({"_id": job_id, "owner": OWNER, "status": "running"}, {"$set": {**fields, "updated_at": now()}})
    if result.matched_count != 1:
        raise asyncio.CancelledError()


async def stop(job_id):
    job = await collection().find_one({"_id": job_id})
    if job["owner"] != OWNER or job["status"] != "running":
        raise asyncio.CancelledError()
    if job["pause_requested"] or job["cancel_requested"]:
        await update(job_id, {"status": "cancelled" if job["cancel_requested"] else "paused", "active": not job["cancel_requested"]})
        return True
    return False


async def log(job_id, operation, attempt, label, data, *, session=None):
    await collection("logs").update_one({"job_id": job_id, "operation": str(operation), "attempt": attempt,
        "instance_path": data.get("instance_path") or data.get("node_id") or "_step"},
        {"$set": {"stage": "writing" if label == "撰写第一章正文" else "planning", "title": label,
                  **safe_event(data), "updated_at": now()}, "$setOnInsert": {"created_at": now()}}, upsert=True, session=session)


async def logs(novel_id, job_id, before=None, limit=50, step_id=None):
    job = await collection().find_one({"_id": ObjectId(job_id), "novel_id": ObjectId(novel_id)})
    if not job:
        raise DomainApiError("FIRST_CHAPTER_NOT_FOUND", "一键写作任务不存在", status_code=404)
    query = {"job_id": ObjectId(job_id)}
    if step_id:
        if not await collection("steps").find_one({"_id": ObjectId(step_id), "job_id": ObjectId(job_id)}, {"_id": 1}):
            raise DomainApiError("FIRST_CHAPTER_NOT_FOUND", "本轮写作步骤不存在", status_code=404)
        query["operation"] = step_id
    if before:
        query["_id"] = {"$lt": ObjectId(before)}
    rows = await collection("logs").find(query).sort("_id", -1).limit(limit + 1).to_list(limit + 1)
    more = len(rows) > limit
    rows = rows[:limit]
    active_step = await collection("steps").find_one({"_id": job["pending"], "job_id": job["_id"]}, {"log_attempt": 1}) if job["status"] == "running" and job.get("pending") else None
    # Opaque correlation hashes allow old cached pages to retire obsolete live flags.
    live_key = canonical_request_hash([str(active_step["_id"]), active_step["log_attempt"]]) if active_step and active_step.get("log_attempt") else None
    items = []
    for row in reversed(rows):
        key = canonical_request_hash([str(row.get("operation")), row.get("attempt")])
        items.append({**public_log(row), "attempt_key": key, "is_live_attempt": bool(live_key and key == live_key)})
    return {"items": items, "before": str(rows[-1]["_id"]) if more else None, "legacy": False, "live_attempt_key": live_key}


def schedule(job_id):
    key = str(job_id)
    if key not in _workers:
        task = asyncio.create_task(run_job(job_id), name=f"first-chapter:{key}")
        _workers[key] = task
        task.add_done_callback(lambda _: _workers.pop(key, None))


async def make_operation(job, data, step):
    task = step["task"]
    flow = flow_store().get_default(task) if task != "confirm_existing_preparation" else None
    scope = {"novel_id": str(job["novel_id"])}
    target = next((r for name in ("volumes", "arcs", "scenes", "chapters") for r in data[name] if str(r["_id"]) == step["target"]), None)
    if task in CONTENT_TASKS:
        kind = {"generate_volume_plot_by_ai": "volume", "generate_arc_plot_by_ai": "arc", "plan_arcs_by_ai": "volume", "plan_scenes_by_ai": "arc", "plan_chapters_by_ai": "scene", "write_chapter_by_ai": "chapter"}[task]
        scope.update(target_type=kind, target_id=step["target"])
        if task.startswith("generate_") or task == "write_chapter_by_ai":
            scope["expected_version"] = target["version"]
        else:
            scope["mode"] = "append"
    elif target:
        scope.update(volume_id=step["target"], volume_expected_version=target["version"])
    if task == "create_volume_skeleton_by_ai":
        scope["novel_expected_version"] = data["novel"]["version"]
    guidance = job["settings"]["user_guidance"]
    if task == "create_characters_by_ai":
        guidance += "\n补齐书初主角与必要核心人物，不重复已有角色；必须包含主角。"
    request = GraphRunRequest(flow_ref={"id": flow.id, "revision": flow.revision} if flow else None, flow_inputs={**step["inputs"], "user_guidance": guidance},
                              scope=scope, generation_params=job["settings"]["generation_params"], use_stream=False)
    operation = {"_id": ObjectId(), "job_id": job["_id"], "step": step, "signature": plan.signature(data),
                 "request": request.model_dump(mode="json"), "result": None, "review": None, "repairs": 0,
                 "original_planning": deepcopy(target.get("planning")) if target else None, "created_at": now()}
    await collection("steps").insert_one(operation)
    await update(job["_id"], {"pending": operation["_id"], "current": {"label": step["label"], "stage": "generating", "target_id": step["target"]}})
    return operation


async def perform(job, operation, *, checkpoint_lock=None):
    job_id, op_id = job["_id"], operation["_id"]
    task, label = operation["step"]["task"], operation["step"]["label"]
    attempt = uuid4().hex
    await collection("steps").update_one({"_id": op_id, "job_id": job_id}, {"$set": {"log_attempt": attempt}})
    progress_times = {}
    async def event(value):
        data = value.get("data", {})
        path = data.get("instance_path") or data.get("node_id") or "_step"
        stamp = time.monotonic()
        if value.get("event") in {"step", "error"} or value.get("event") == "progress" and stamp - progress_times.get(path, 0) >= 1:
            await log(job_id, op_id, attempt, label, data)
            progress_times[path] = stamp
        if data.get("state") in {"done", "completed", "cached"} and data.get("outputs") is not None and data.get("input_fingerprint"):
            cached = {k: data[k] for k in ("node_id", "instance_path", "input_fingerprint", "outputs")}
            nodes = operation["request"].get("cached_nodes", [])
            operation["request"]["cached_nodes"] = [n for n in nodes if n["instance_path"] != cached["instance_path"]] + [cached]
            await collection("steps").update_one({"_id": op_id}, {"$set": {"request": operation["request"]}})
        if value.get("event") == "error":
            continuation = data.get("details", {}).get("agent_continuation") or data.get("agent_continuation")
            if continuation:
                operation["request"]["agent_continuation"] = {"token": continuation["token"], "additional_rounds": continuation.get("resume_rounds", 0) if continuation.get("reason") == "network" else 2}
                operation["continuation_owner"] = OWNER
                await collection("steps").update_one({"_id": op_id}, {"$set": {"request": operation["request"], "continuation_owner": OWNER}})
    if operation["result"] is None:
        data = await plan.snapshot(str(job["novel_id"]))
        if plan.signature(data) != operation["signature"]:
            raise DomainApiError("FIRST_CHAPTER_SOURCE_CHANGED", "资料已变化，已保留原步骤；请结束本轮后重新开始以复用最新内容。", status_code=409)
        request = GraphRunRequest.model_validate(operation["request"])
        if operation.get("continuation_owner") != OWNER:
            request.agent_continuation = None
        if task == "confirm_existing_preparation":
            volume = next(r for r in data["volumes"] if str(r["_id"]) == operation["step"]["target"])
            kind = operation["step"]["inputs"]["kind"]
            states = [r for r in data.get("faction_states" if kind == "factions" else "character_states", []) if r["volume_id"] == volume["_id"]]
            result = {"candidate": {"kind": kind, "volume_id": str(volume["_id"]), "expected_version": volume["version"], "kept_participation_plans": states},
                      "context": {"plot": volume.get("plot", ""), "existing_participation_plans": states, "location_ids": volume.get("location_ids", [])}}
        else:
            try:
                result = await generate_task_flow(task, request, scope=request.scope, stream=False, on_event=event)
            except DomainApiError as exc:
                if exc.code in {"AGENT_CONTINUATION_EXPIRED", "AGENT_CONTINUATION_CHANGED"}:
                    operation["request"].pop("agent_continuation", None)
                    await collection("steps").update_one({"_id": op_id}, {"$set": {"request": operation["request"]}, "$unset": {"continuation_owner": ""}})
                    raise DomainApiError("FIRST_CHAPTER_CONTINUATION_RESET", "整理续跑凭证已失效，完成节点已保留；再次继续将重试未完成步骤。", status_code=409) from exc
                raise
        # Missing plot may be filled; existing formal planning is not silently rewritten.
        if task.startswith("generate_") and operation.get("original_planning"):
            result["candidate"]["planning"] = operation["original_planning"]
        if len(BSON.encode({"result": result})) > 12_000_000:
            raise DomainApiError("FIRST_CHAPTER_RESULT_TOO_LARGE", "步骤结果过大，请缩小自定义流程范围。", status_code=422)
        operation["result"] = result
        await collection("steps").update_one({"_id": op_id}, {"$set": {"result": result}})
        await update(job_id, {"has_saved_candidate": True})
    if await stop(job_id):
        return
    if operation.get("review") is None:
        if not operation["result"].get("context") or (task == "create_volume_skeleton_by_ai" and "book_start_character_facts" not in operation["result"]["context"]):
            # Persist AI output first. A failed source read must not lose a paid result.
            data = await plan.snapshot(str(job["novel_id"]))
            if plan.signature(data) != operation["signature"]:
                raise DomainApiError("FIRST_CHAPTER_SOURCE_CHANGED", "生成后资料已变化，已保留候选；请结束本轮重新核对。", status_code=409)
            result = await attach_review_sources(task, operation["result"], operation["request"]["generation_params"])
            if len(BSON.encode({"result": result})) > 12_000_000:
                raise DomainApiError("FIRST_CHAPTER_RESULT_TOO_LARGE", "审核资料过大，原候选已保留，请缩小流程范围。", status_code=422)
            operation["result"] = result
            await collection("steps").update_one({"_id": op_id}, {"$set": {"result": result}})
        await update(job_id, {"current.stage": "reviewing"})
        await log(job_id, op_id, attempt, label, {"instance_path": "_review", "label": "核对剧情与事实一致性", "state": "running"})
        report = await review_candidate(task, operation["result"], {})
        operation["review"] = report
        await collection("steps").update_one({"_id": op_id}, {"$set": {"review": report}})
        await log(job_id, op_id, attempt, label, {"instance_path": "_review", "label": "核对剧情与事实一致性", "state": "failed" if report["issues"] else "done", "outputs": report})
    if operation["review"]["issues"]:
        # Keep every rejected candidate and its evidence; a bounded repair reruns only this step.
        if operation["repairs"] >= 2 or task == "confirm_existing_preparation":
            raise DomainApiError("FIRST_CHAPTER_CONSISTENCY_CONFLICT", "两次修订后仍有一致性冲突，候选与引证已保留在日志；请处理来源或调整流程后重新开始。", status_code=409)
        await collection("candidates").insert_one({"job_id": job_id, "step_id": op_id, "result": operation["result"], "review": operation["review"], "created_at": now()})
        feedback = "\n修订本步候选，仅解决以下有依据的冲突，不改变已保存资料：" + str(operation["review"]["issues"])
        guidance = job["settings"]["user_guidance"] + feedback
        if len(guidance) > 2000:
            raise DomainApiError("FIRST_CHAPTER_REPAIR_TOO_LARGE", "冲突报告较长，已保留候选，请人工核对日志。", status_code=409)
        request = deepcopy(operation["request"])
        request["cached_nodes"] = []
        request.pop("agent_continuation", None)
        request["flow_inputs"]["user_guidance"] = guidance
        await collection("steps").update_one({"_id": op_id}, {"$set": {"result": None, "review": None, "request": request}, "$inc": {"repairs": 1}, "$unset": {"continuation_owner": ""}})
        await update(job_id, {"has_saved_candidate": False, "current.stage": "repairing"})
        return
    if await stop(job_id):
        return
    await update(job_id, {"current.stage": "saving"})
    await log(job_id, op_id, attempt, label, {"instance_path": "_save", "label": "保存并核对", "state": "running"})
    async def save(session):
        current = await collection().find_one({"_id": job_id}, session=session)
        if current["pending"] != op_id:
            return  # A previously committed transaction already advanced the checkpoint.
        if current["owner"] != OWNER or current["status"] != "running":
            raise DomainApiError("FIRST_CHAPTER_INTERRUPTED", "任务占用已变化，候选已保留。", status_code=409)
        data = await plan.snapshot(str(job["novel_id"]), session=session)
        if plan.signature(data) != operation["signature"]:
            raise DomainApiError("FIRST_CHAPTER_SOURCE_CHANGED", "候选生成后资料已修改，已保留候选；请结束本轮重新核对，不会覆盖已有内容。", status_code=409)
        saved = await save_candidate(str(job["novel_id"]), task, operation["result"], f"first-chapter:{op_id}", session=session)
        await collection("steps").update_one({"_id": op_id}, {"$set": {"saved": True, "saved_at": now()}}, session=session)
        await collection().update_one({"_id": job_id, "pending": op_id}, {"$set": {"pending": None, "has_saved_candidate": False, "current": None, "updated_at": now()},
                                     "$push": {"completed_steps": label}}, session=session)
        if task == "write_chapter_by_ai":
            chapter_id = operation["step"]["target"]
            revision = await BaseRepository("chapter_revisions").collection.find_one({"chapter_id": ObjectId(chapter_id)}, sort=[("version", -1)], session=session)
            await collection("reports").insert_one({"novel_id": job["novel_id"], "job_id": job_id, "chapter_id": ObjectId(chapter_id),
                "revision_id": revision["_id"], "content_hash": revision["content_hash"], "review": operation["review"],
                "basis": operation["result"]["basis"], "created_at": now()}, session=session)
        # Commit the final log alongside its saved checkpoint, including unknown-commit recovery.
        await log(job_id, op_id, attempt, label, {"instance_path": "_save", "label": "保存并核对", "state": "done"}, session=session)
        return saved
    # The lease heartbeat writes the same job document. Serialize our own writes
    # rather than racing it against the transaction or replaying unknown commits.
    async with checkpoint_lock if checkpoint_lock is not None else asyncio.Lock():
        await run_mongo_write_unit(save, "save_first_chapter_step", require_transaction=True)


async def attach_review_sources(task, result, generation_params):
    from backend.services.task_flows.domain import load_task_context
    basis = result["basis"]
    original = basis["flow_snapshot"]
    if not result.get("context"):
        context = await load_task_context(task, original["business_input"], original["scope"], generation_params)
        ports = {}
        for key, value in context.ports.items():
            if isinstance(value, str):
                try:
                    value = json.loads(value)
                except ValueError:
                    pass
            ports[key] = value
        result["context"] = {"task_source_ports": ports}
    if task == "create_volume_skeleton_by_ai" and "book_start_character_facts" not in result["context"]:
        # Skeleton design sources are deliberately unlocated. The automatic
        # first volume must ALSO respect facts already confirmed at book start.
        from backend.services.novel.character_fact_service import read_character_facts
        novel_id = original["scope"]["novel_id"]
        ids = sorted({r["id"] for r in basis["source_refs"] if r["resource_type"] == "character"})
        facts = await read_character_facts(novel_id, ids, {"target_type": "novel", "target_id": novel_id,
                                                        "reading_intent": "book_start", "reading_version": 1})
        result["context"]["book_start_character_facts"] = facts["characters"]
    return result


async def run_job(job_id):
    task = asyncio.current_task()
    checkpoint_lock = asyncio.Lock()
    async def heartbeat():
        try:
            while True:
                await asyncio.sleep(10)
                async with checkpoint_lock:
                    result = await collection().update_one({"_id": job_id, "owner": OWNER, "status": "running"}, {"$set": {"lease_until": now() + timedelta(seconds=45)}})
                if result.matched_count != 1:
                    task.cancel()
                    return
        except asyncio.CancelledError:
            raise
        except Exception:
            task.cancel()
    renewal = asyncio.create_task(heartbeat())
    try:
        job = await collection().find_one({"_id": job_id})
        async with book_lease(str(job["novel_id"]), f"first-chapter:{job_id}"):
            while not await stop(job_id):
                job = await collection().find_one({"_id": job_id})
                operation = await collection("steps").find_one({"_id": job["pending"]}) if job["pending"] else None
                if operation is None:
                    data = await plan.snapshot(str(job["novel_id"]))
                    step = plan.choose_step(data, job["settings"])
                    if step is None:
                        await update(job_id, {"status": "completed", "active": False, "current": None, "chapter_id": str(plan.first_chapter(data)["_id"])})
                        break
                    if job["completed_steps"] and step["label"] == job["completed_steps"][-1]:
                        raise DomainApiError("FIRST_CHAPTER_NO_PROGRESS", "该步骤未补齐必需资料，已保留保存结果，请核对日志或修改默认流程后继续。", status_code=409)
                    operation = await make_operation(job, data, step)
                await perform(job, operation, checkpoint_lock=checkpoint_lock)
    except asyncio.CancelledError:
        await collection().update_one({"_id": job_id, "owner": OWNER, "status": "running"}, {"$set": {"status": "paused", "lease_until": now(), "updated_at": now()}})
    except Exception as exc:
        logger.exception("一键首章任务暂停 job=%s", job_id)
        _, code, message, fields, _ = exception_to_error(exc, fallback_message="一键写作失败，已保留进度，请继续。")
        job = await collection().find_one({"_id": job_id})
        with suppress(Exception):
            await log(job_id, job.get("pending") or "_job", uuid4().hex, (job.get("current") or {}).get("label", "一键写作"),
                      {"instance_path": "_error", "label": "任务错误", "state": "failed", "message": message})
        await collection().update_one({"_id": job_id, "owner": OWNER, "status": "running"}, {"$set": {"status": "failed", "lease_until": now(), "updated_at": now(), "error": {"code": code, "message": message, "field_errors": fields}}})
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
