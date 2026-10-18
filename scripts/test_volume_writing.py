"""Integration tests against a fresh database restored from an explicit local checkpoint.

Set VOLUME_WRITING_TEST_ARCHIVE to mongodb.archive.gz. Models are stubbed; the
original context validation, transaction saves, revisions, receipts and MongoDB
indexes are exercised. The source database is never connected for writes.
"""
import asyncio
from copy import deepcopy
from datetime import timedelta
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch, AsyncMock
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pymongo import AsyncMongoClient
from backend.api.error_protocol import DomainApiError
from backend.db import mongo
from backend.services.novel import volume_writing_service as service
from backend.services.novel import volume_writing_log as logs
from backend.services.novel.chapter_service import ChapterService
from backend.services.novel.volume_writing_contract import VolumeWritingStart
from backend.services.novel.writing_context import load_writing_context
from backend.services.novel.writing_generation import confirm_writing, merge_writing_reads, writing_candidate_fingerprint
from backend.services.task_flows.graph import flow_fingerprint
from backend.services.task_flows.registry import TASKS
from backend.services.task_flows.store import flow_store


ARCHIVE = os.environ.get("VOLUME_WRITING_TEST_ARCHIVE")


@unittest.skipUnless(ARCHIVE, "Requires an explicit local test archive; no live database is used by default")
class VolumeWritingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        asyncio.get_running_loop().set_debug(False)
        asyncio.get_running_loop().slow_callback_duration = 30
        self.database = "novel_volume_writing_test_" + uuid4().hex
        self.archive = Path(ARCHIVE).resolve()
        source = json.loads((self.archive.parent / "manifest.json").read_text(encoding="utf-8"))["database"]
        self.assertNotEqual(source, self.database)
        container_path = "/tmp/volume-writing-tests.archive.gz"
        subprocess.run(["docker", "cp", str(self.archive), f"novel-generator-mongodb:{container_path}"], check=True, capture_output=True)
        subprocess.run(["docker", "exec", "novel-generator-mongodb", "mongorestore", f"--archive={container_path}", "--gzip",
            f"--nsInclude={source}.*", f"--nsFrom={source}.*", f"--nsTo={self.database}.*", "--stopOnError"], check=True, capture_output=True)
        self.env = patch.dict(os.environ, {"NOVEL_GENERATOR_MONGO_DATABASE_NAME": self.database})
        self.env.start()
        uri, _, timeout = mongo._get_connection_settings()
        self.client = AsyncMongoClient(uri, serverSelectionTimeoutMS=timeout)
        self.old_client = mongo.client
        self.old_settings = mongo._active_connection_settings
        mongo.client = self.client
        mongo._active_connection_settings = (uri, self.database, timeout)
        self.addAsyncCleanup(self.cleanup)
        self.db = self.client[self.database]
        novel = await self.db.novels.find_one({"is_deleted": False})
        volume = await self.db.volumes.find_one({"novel_id": novel["_id"], "is_deleted": False}, sort=[("order_index", 1)])
        self.novel, self.volume = str(novel["_id"]), str(volume["_id"])
        self.before = await service.catalog(self.novel, self.volume)
        self.original_bodies = {str(row["_id"]): row["content"] for row in self.before["chapters"] if row["content"].strip()}
        self.calls = []
        self.gate = None
        self.entered = asyncio.Event()
        self.manual_edit = False
        self.generator = patch.object(service, "generate_task_flow", self.generate)
        self.generator.start()
        self.reviewer = patch.object(service, "review_candidate", AsyncMock(return_value={"summary": "测试审校通过", "issues": []}))
        self.reviewer.start()

    async def cleanup(self):
        await service.shutdown()
        await asyncio.sleep(0)
        if hasattr(self, "generator"):
            self.generator.stop()
        if hasattr(self, "reviewer"):
            self.reviewer.stop()
        self.assertTrue(self.database.startswith("novel_volume_writing_test_"))
        await self.client.drop_database(self.database)
        await self.client.close()
        mongo.client = self.old_client
        mongo._active_connection_settings = self.old_settings
        self.env.stop()

    async def generate(self, task, request, *, scope, **_kwargs):
        self.calls.append((task, scope["target_id"]))
        self.entered.set()
        if self.gate is not None:
            await self.gate.wait()
        flow = flow_store().get(request.flow_ref.id, request.flow_ref.revision)
        position = deepcopy(scope)
        position.update(reading_version=1, reading_intent="chapter_before" if task == "write_chapter_by_ai" else "append")
        version = next((node.config.get("chapter_context_version") for node in flow.nodes if node.type == "agent" and node.config.get("chapter_context_version")), None)
        if version:
            position.update(chapter_context_version=version, chapter_selection_complete=True)
        data = await load_writing_context(task, request.flow_inputs, position)
        if task == "write_chapter_by_ai":
            candidate = {"content": f"  集成测试正文 {scope['target_id']}\n\n保留段落和缩进。  "}
        else:
            count = data["request"].count
            scene = data["chain"][-1][1]
            budget = scene["planning"].get("target_word_count")
            used = sum((row.get("planning") or {}).get("target_word_count") or 0 for row in data["context"]["existing_children"])
            words = max(count, (budget - used) if budget else 3000 * count)
            candidate = {"chapters": [{"title": f"集成测试计划-{scope['target_id']}-{index}", "summary": "仅用于隔离测试",
                "planning": {"goal": "完成本章测试目标", "start_state": "本章开始状态", "end_state": "本章结束状态",
                             "beats": ["验证保存顺序"], "target_word_count": words // count + (index < words % count)}} for index in range(count)]}
        business = data["request"].model_dump(mode="json")
        snapshot = {"flow_ref": request.flow_ref.model_dump(mode="json"), "definition_hash": flow_fingerprint(flow),
            "flow_inputs": request.flow_inputs, "scope": position, "business_input": business,
            "execution": {"reading_contract": "position_v1", "writing_reads": []}}
        spec = TASKS[task]
        common = {"workflow": task, "step": spec.step, "schema_id": spec.schema_id, "schema_version": spec.schema_version}
        context_basis = merge_writing_reads([data["context_basis"]])
        basis = {**common, "novel_id": self.novel, "generation_input": business,
            "context_basis": context_basis, "source_refs": data["source_refs"],
            "flow_ref": snapshot["flow_ref"], "flow_snapshot": snapshot,
            "input_fingerprint": writing_candidate_fingerprint(task, business, context_basis, snapshot)}
        if self.manual_edit:
            self.manual_edit = False
            await ChapterService.update_chapter(scope["target_id"], {"expected_version": scope["expected_version"], "content": "作者手工保存的正文"})
        return {**common, "candidate": candidate, "basis": basis, "context": data["context"]}

    async def wait_status(self, expected, timeout=120):
        async with asyncio.timeout(timeout):
            while True:
                job = await service.latest(self.novel, self.volume)
                if job and job["status"] in expected:
                    return job
                await asyncio.sleep(0.05)

    async def begin(self, key="test-start"):
        return await service.start(self.novel, self.volume, VolumeWritingStart(), key)

    async def assert_complete(self):
        job = await self.wait_status({"completed", "failed"})
        self.assertEqual(job["status"], "completed", job.get("error"))
        after = await service.catalog(self.novel, self.volume)
        self.assertTrue(all(row["content"].strip() for row in after["chapters"]))
        for chapter_id, content in self.original_bodies.items():
            actual = await ChapterService.get_chapter(chapter_id)
            self.assertEqual(actual["content"], content)
        self.assertEqual(job["created_chapters"], self.before["summary"]["missing_plans"])
        self.assertEqual(job["written_chapters"], self.before["summary"]["pending_bodies"])
        self.assertEqual(job["skipped_chapters"], len(self.original_bodies))
        self.assertEqual(job["completed_scenes"], len(self.before["scenes"]))
        return job

    async def test_complete_volume_and_repeat_run_never_rewrites_saved_text(self):
        await self.begin()
        await self.assert_complete()
        calls = list(self.calls)
        await self.begin("second-run")
        job = await self.wait_status({"completed", "failed"})
        self.assertEqual(job["status"], "completed", job["error"])
        self.assertEqual(job["written_chapters"], 0)
        self.assertEqual(job["created_chapters"], 0)
        self.assertEqual(self.calls, calls)

    async def test_pause_keeps_generated_result_and_resume_does_not_regenerate_it(self):
        self.gate = asyncio.Event()
        job = await self.begin()
        await self.entered.wait()
        await service.control(self.novel, self.volume, job["id"], "pause")
        self.gate.set()
        paused = await self.wait_status({"paused", "failed"})
        self.assertEqual(paused["status"], "paused", paused["error"])
        self.assertTrue(paused["has_saved_candidate"])
        self.assertEqual(paused["written_chapters"], 0)
        first = self.calls[0]
        await service.control(self.novel, self.volume, job["id"], "resume")
        await self.assert_complete()
        self.assertEqual(self.calls.count(first), 1)

    async def test_unknown_save_response_replays_same_receipt_without_duplicate_writes(self):
        failed = False
        async def uncertain(*args, **kwargs):
            nonlocal failed
            saved = await confirm_writing(*args, **kwargs)
            if not failed:
                failed = True
                raise ConnectionError("Simulated lost save acknowledgement")
            return saved
        with patch.object(service, "confirm_writing", uncertain):
            job = await self.begin()
            failure = await self.wait_status({"failed"})
            self.assertTrue(failure["has_saved_candidate"])
            first = self.calls[0]
            await service.control(self.novel, self.volume, job["id"], "resume")
            await self.assert_complete()
            self.assertEqual(self.calls.count(first), 1)

    async def test_concurrent_start_keys_allow_only_one_task(self):
        self.gate = asyncio.Event()
        first = await self.begin()
        replay = await self.begin()
        self.assertEqual(first["id"], replay["id"])
        with self.assertRaises(DomainApiError) as raised:
            await self.begin("other-start")
        self.assertEqual(raised.exception.code, "VOLUME_WRITING_BUSY")
        await service.control(self.novel, self.volume, first["id"], "cancel")
        self.gate.set()
        job = await self.wait_status({"cancelled", "failed"})
        self.assertEqual(job["status"], "cancelled")
        self.assertEqual(job["written_chapters"], 0)

    async def test_manual_saved_body_is_never_overwritten(self):
        self.manual_edit = True
        await self.begin()
        job = await self.wait_status({"failed"})
        record = await ChapterService.get_chapter(self.calls[0][1])
        self.assertEqual(record["content"], "作者手工保存的正文")
        self.assertTrue(job["has_saved_candidate"])

    async def test_service_shutdown_preserves_pending_step_for_restart(self):
        self.gate = asyncio.Event()
        job = await self.begin()
        await self.entered.wait()
        await service.shutdown()
        paused = await self.wait_status({"paused"})
        self.assertEqual(paused["written_chapters"], 0)
        self.gate.set()
        with patch.object(service, "OWNER", uuid4().hex):
            await service.control(self.novel, self.volume, job["id"], "resume")
            await self.assert_complete()

    async def test_parent_plan_change_pauses_instead_of_changing_scope(self):
        self.gate = asyncio.Event()
        job = await self.begin()
        await self.entered.wait()
        await service.control(self.novel, self.volume, job["id"], "pause")
        self.gate.set()
        await self.wait_status({"paused"})
        scene = self.before["scenes"][-1]
        await self.db.scenes.update_one({"_id": scene["_id"]}, {"$set": {"title": "作者修改了场景"}})
        await service.control(self.novel, self.volume, job["id"], "resume")
        failed = await self.wait_status({"failed"})
        self.assertEqual(failed["error"]["code"], "VOLUME_WRITING_STRUCTURE_CHANGED")
        self.assertEqual(failed["written_chapters"], 0)

    async def test_persisted_logs_keep_retries_paginate_and_enforce_scope(self):
        first = True
        async def logged(task, request, *, scope, on_event, **kwargs):
            nonlocal first
            data = {"node_id": "test-node", "instance_path": "test/model", "label": "生成正文"}
            await on_event({"event": "step", "data": {**data, "state": "running"}})
            if first:
                first = False
                await on_event({"event": "error", "data": {**data, "state": "failed", "message": "模拟首次连接失败"}})
                raise ConnectionError("Simulated retry log failure")
            result = await self.generate(task, request, scope=scope, **kwargs)
            await on_event({"event": "step", "data": {**data, "state": "done", "elapsed_ms": 1200,
                "outputs": {"content": "可查看的测试输出", "api_key": "must-never-leak"}}})
            return result
        with patch.object(service, "generate_task_flow", logged):
            job = await self.begin()
            await self.wait_status({"failed"})
            page = await logs.read(self.novel, self.volume, job["id"])
            self.assertFalse(page["legacy"])
            self.assertTrue(any(row["state"] == "failed" for row in page["items"]))
            stored = await service.jobs().find_one({"_id": service.ObjectId(job["id"])})
            await logs.record(stored["_id"], stored["pending"]["key"], stored["pending"]["log_attempt"], stored["current"],
                {"instance_path": "interrupted", "label": "中断前的步骤", "state": "running"})
            await service.control(self.novel, self.volume, job["id"], "resume")
            await self.assert_complete()
        rows, cursor = [], None
        while True:
            page = await logs.read(self.novel, self.volume, job["id"], cursor, limit=3)
            rows.extend(page["items"])
            if not page["before"]:
                break
            cursor = page["before"]
        self.assertEqual(len(rows), len({row["id"] for row in rows}))
        self.assertEqual(len(rows), await logs.collection().count_documents({"job_id": service.ObjectId(job["id"])}))
        self.assertTrue(any(row["label"] == "保存并核对" and row["state"] == "done" for row in rows))
        self.assertTrue(any(row["state"] == "failed" for row in rows))
        self.assertTrue(any(row["label"] == "中断前的步骤" and row["state"] == "stopped" for row in rows))
        self.assertNotIn("must-never-leak", json.dumps(rows))
        with self.assertRaises(DomainApiError) as failure:
            await logs.read(str(service.ObjectId()), self.volume, job["id"])
        self.assertEqual(failure.exception.status_code, 404)

    async def test_novel_overview_follows_active_task_across_volumes_and_recovers_expired_lease(self):
        self.assertIsNone(await service.overview(self.novel))
        self.gate = asyncio.Event()
        job = await self.begin()
        await self.entered.wait()
        await service.control(self.novel, self.volume, job["id"], "pause")
        self.gate.set()
        await self.wait_status({"paused"})
        stored = await service.jobs().find_one({"_id": service.ObjectId(job["id"])})
        other_volume = await self.db.volumes.find_one({"novel_id": stored["novel_id"], "_id": {"$ne": stored["volume_id"]}, "is_deleted": False})
        recent = {**stored, "_id": service.ObjectId(), "volume_id": other_volume["_id"], "active": False, "status": "completed",
            "start_key": "other-volume-ended", "created_at": service.now() + timedelta(hours=1)}
        await service.jobs().insert_one(recent)
        overview = await service.overview(self.novel)
        self.assertEqual(overview["id"], job["id"])
        self.assertEqual(overview["status"], "paused")
        self.assertTrue(overview["volume_title"])
        self.assertNotIn("pending", overview)
        self.assertNotIn("settings", overview)
        from fastapi import FastAPI
        from httpx import ASGITransport, AsyncClient
        from backend.api.default_routers.volume_writing_router import overview_router
        app = FastAPI()
        app.include_router(overview_router)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get(f"/api/novels/{self.novel.upper()}/volume-writing-job")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["job"]["id"], job["id"])
            self.assertEqual((await client.get("/api/novels/not-an-id/volume-writing-job")).status_code, 422)
            self.assertEqual((await client.get(f"/api/novels/{service.ObjectId()}/volume-writing-job")).status_code, 404)
        await service.jobs().update_one({"_id": stored["_id"]}, {"$set": {"status": "running", "owner": "stale-service", "lease_until": service.now() - timedelta(seconds=5)}})
        overview = await service.overview(self.novel)
        self.assertEqual(overview["status"], "paused")
        self.assertEqual(overview["error"]["code"], "VOLUME_WRITING_INTERRUPTED")
        await service.jobs().update_one({"_id": stored["_id"]}, {"$set": {"status": "cancelled", "active": False}})
        self.assertEqual((await service.overview(self.novel))["id"], str(recent["_id"]))
        await self.db.volumes.update_one({"_id": other_volume["_id"]}, {"$set": {"is_deleted": True}})
        self.assertEqual((await service.overview(self.novel))["id"], job["id"])
        with self.assertRaises(DomainApiError) as failure:
            await service.overview(str(service.ObjectId()))
        self.assertEqual(failure.exception.status_code, 404)


class VolumeWritingUnitTests(unittest.TestCase):
    def test_log_output_is_bounded_and_excludes_credentials_and_private_fields(self):
        data = logs.safe_event({"node_id": "one", "state": "done", "agent_continuation": {"token": "hidden"},
            "outputs": {"public": "保留正文", "nested": [{"access_token": "hidden", "thinking": "hidden"}], "large": "字" * 20000}})
        self.assertLessEqual(len(data["output"]), logs.OUTPUT_LIMIT)
        self.assertTrue(data["output_truncated"])
        self.assertNotIn("hidden", json.dumps(data))
        self.assertNotIn("agent_continuation", data)

    def test_legacy_log_preserves_only_available_steps_and_status(self):
        job = {"status": "cancelled", "error": None, "current": {"stage": "saving", "title": "第九章", "node": ""},
            "pending": {"request": {"cached_nodes": [{"node_id": "read", "instance_path": "test/read", "outputs": {"text": "已读资料"}}]}}}
        rows = logs.legacy_rows(job)
        self.assertEqual(rows[0]["state"], "done")
        self.assertEqual(rows[-1]["state"], "stopped")
        self.assertNotIn("elapsed_ms", rows[0])

    def test_missing_target_keeps_existing_plans_or_adds_one_to_empty_scene(self):
        self.assertEqual(service.planned_count({"planning": {}}, 3), 3)
        self.assertEqual(service.planned_count({"planning": {}}, 0), 1)
        self.assertEqual(service.planned_count({"planning": {"target_chapter_count": 2}}, 1), 2)


class VolumeWritingSelectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_partial_plan_is_filled_before_writing_empty_chapters(self):
        from bson import ObjectId
        scene_id, chapter_id = ObjectId(), ObjectId()
        scene = {"_id": scene_id, "title": "Scene", "planning": {"target_chapter_count": 2}}
        chapter = {"_id": chapter_id, "scene_id": scene_id, "title": "Existing plan", "content": ""}
        job = {"_id": ObjectId(), "catalog_signature": "fixed", "written_ids": []}
        data = {"signature": "fixed", "scenes": [scene], "chapters": [chapter]}
        with patch.object(service, "update", AsyncMock()):
            task, target, count = await service.next_step(job, data)
            self.assertEqual((task, target["_id"], count), ("plan_chapters_by_ai", scene_id, 1))
            data["chapters"].append({**chapter, "_id": ObjectId(), "content": "Already saved"})
            task, target, _ = await service.next_step(job, data)
            self.assertEqual((task, target["_id"]), ("write_chapter_by_ai", chapter_id))


if __name__ == "__main__":
    unittest.main()
