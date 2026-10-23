"""Real Mongo transactions in an isolated temporary database; AI/adapter boundaries are simulated."""
import asyncio
import os
import unittest
from copy import deepcopy
from unittest.mock import patch
from uuid import uuid4

from bson import ObjectId
from pymongo import AsyncMongoClient

from backend.api.error_protocol import DomainApiError
from backend.db import mongo
from backend.services.novel import first_chapter_service as service


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.name = "ai_ng_first_chapter_test_" + uuid4().hex
        self.client = AsyncMongoClient("mongodb://127.0.0.1:27017/?replicaSet=rs0", serverSelectionTimeoutMS=3000)
        await self.client.admin.command("ping")
        self.db = self.client[self.name]
        self.old_client = mongo.client
        self.old_settings = mongo._active_connection_settings
        mongo.client = self.client
        mongo._active_connection_settings = ("mongodb://127.0.0.1:27017/?replicaSet=rs0", self.name, 3000)
        self.env = patch.dict(os.environ, {"NOVEL_GENERATOR_MONGO_DATABASE_NAME": self.name})
        self.env.start()
        self.novel = str(ObjectId())
        self.data = {"novel": {"_id": ObjectId(self.novel), "version": 1}, "factions": [], "characters": [], "relations": [],
                     "rules": [], "items": [], "volumes": [], "arcs": [], "scenes": [], "chapters": [], "ready": {}}
        await self.db.novels.insert_one({"_id": ObjectId(self.novel), "is_deleted": False})
        await self.db.test_state.insert_one({"_id": "state", "data": self.data})
        self.generated = 0
        self.saved = 0
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.release.set()
        self.save_fail = False
        self.fail_after_commit = False

        async def snapshot(_novel, *, session=None):
            return (await self.db.test_state.find_one({"_id": "state"}, session=session))["data"]

        async def generate(task, request, **kwargs):
            self.generated += 1
            self.started.set()
            await self.release.wait()
            return {"candidate": {"value": "正文"}, "basis": {}, "context": {"identity": "将军"}}

        async def review(*args):
            return {"summary": "测试来源已核对", "issues": []}

        async def save(novel, task, result, key, *, session):
            if self.save_fail:
                self.save_fail = False
                raise DomainApiError("TEST_SAVE_FAILURE", "模拟保存失败", status_code=503)
            self.saved += 1
            data = deepcopy(self.data)
            volume, arc, scene = ObjectId(), ObjectId(), ObjectId()
            data.update(volumes=[{"_id": volume}], arcs=[{"_id": arc, "volume_id": volume}], scenes=[{"_id": scene, "arc_id": arc}])
            data["chapters"] = [{"_id": ObjectId(), "scene_id": scene, "content": "已有正文"}]
            await self.db.test_state.update_one({"_id": "state"}, {"$set": {"data": data}}, session=session)
            await self.db.test_saves.insert_one({"key": key, "content": result["candidate"]["value"]}, session=session)
            return {}

        self.patches = [patch.object(service.plan, "snapshot", snapshot), patch.object(service, "generate_task_flow", generate),
                        patch.object(service, "save_candidate", save), patch.object(service, "review_candidate", review)]
        for item in self.patches:
            item.start()

    async def asyncTearDown(self):
        self.release.set()
        await service.shutdown()
        for item in self.patches:
            item.stop()
        self.env.stop()
        mongo.client = self.old_client
        mongo._active_connection_settings = self.old_settings
        assert self.name.startswith("ai_ng_first_chapter_test_")
        await self.client.drop_database(self.name)
        await self.client.close()

    async def wait_status(self, *statuses):
        async with asyncio.timeout(15):
            while True:
                job = await service.latest(self.novel)
                if job and job["status"] in statuses:
                    worker = service._workers.get(job["id"])
                    if worker:
                        await asyncio.gather(worker, return_exceptions=True)
                    return job
                await asyncio.sleep(.02)

    async def begin(self, key="start"):
        return await service.start(self.novel, service.Start(include_optional=False), key)

    async def test_start_replay_and_repeated_run_do_not_regenerate_saved_body(self):
        first = await self.begin()
        same = await self.begin()
        self.assertEqual(first["id"], same["id"])
        await self.wait_status("completed")
        await self.begin("second")
        await self.wait_status("completed")
        self.assertEqual(self.generated, 1)
        self.assertEqual(await self.db.test_saves.count_documents({}), 1)

    async def test_pause_retains_candidate_then_resume_saves_without_generation(self):
        self.release.clear()
        job = await self.begin()
        await asyncio.wait_for(self.started.wait(), 5)
        await service.control(self.novel, job["id"], "pause")
        self.release.set()
        paused = await self.wait_status("paused")
        self.assertTrue(paused["has_saved_candidate"])
        self.assertEqual(self.saved, 0)
        await service.control(self.novel, job["id"], "resume")
        await self.wait_status("completed")
        self.assertEqual(self.generated, 1)

    async def test_save_failure_retains_candidate_and_original_request(self):
        self.save_fail = True
        job = await self.begin()
        failed = await self.wait_status("failed")
        self.assertTrue(failed["has_saved_candidate"])
        await service.control(self.novel, job["id"], "resume")
        await self.wait_status("completed")
        self.assertEqual(self.generated, 1)
        self.assertEqual(await self.db.test_saves.count_documents({}), 1)

    async def test_manual_edit_during_generation_blocks_save_and_preserves_candidate(self):
        self.release.clear()
        await self.begin()
        await asyncio.wait_for(self.started.wait(), 5)
        await self.db.test_state.update_one({"_id": "state"}, {"$set": {"data.novel.version": 2}})
        self.release.set()
        job = await self.wait_status("failed")
        self.assertEqual(job["error"]["code"], "FIRST_CHAPTER_SOURCE_CHANGED")
        self.assertTrue(job["has_saved_candidate"])
        self.assertEqual(await self.db.test_saves.count_documents({}), 0)

    async def test_shutdown_preserves_checkpoint_and_resume(self):
        self.release.clear()
        job = await self.begin()
        await asyncio.wait_for(self.started.wait(), 5)
        await service.shutdown()
        self.assertEqual((await service.latest(self.novel))["status"], "paused")
        self.release.set()
        await service.control(self.novel, job["id"], "resume")
        await self.wait_status("completed")
        self.assertEqual(await self.db.test_saves.count_documents({}), 1)

    async def test_active_volume_task_blocks_start_and_logs_are_scoped(self):
        await self.db.volume_writing_jobs.insert_one({"novel_id": ObjectId(self.novel), "active": True})
        with self.assertRaises(DomainApiError):
            await self.begin()
        await self.db.volume_writing_jobs.delete_many({"novel_id": ObjectId(self.novel)})
        job = await self.begin()
        await self.wait_status("completed")
        page = await service.logs(self.novel, job["id"])
        self.assertTrue(any(r.get("label") == "保存并核对" for r in page["items"]))
        with self.assertRaises(DomainApiError):
            await service.logs(str(ObjectId()), job["id"])

    async def test_timeline_uses_saved_checkpoint_and_keeps_superseded_history(self):
        job = await self.begin()
        await self.wait_status("completed")
        jid = ObjectId(job["id"])
        step = await self.db.first_chapter_steps.find_one({"job_id": jid})
        await service.log(jid, step["_id"], "old-failed-attempt", step["step"]["label"],
                          {"state": "failed", "label": "旧错误", "message": "断流"})
        old = {"_id": ObjectId(), "job_id": jid, "step": {"label": "被替换剧情"},
               "created_at": service.now(), "superseded": True, "result": {"candidate": {"secret": "never expose"}}}
        await self.db.first_chapter_steps.insert_one(old)
        page = await service.timeline(self.novel, job["id"])
        self.assertEqual(len(page["steps"]), 1)
        self.assertEqual(page["steps"][0]["state"], "saved")
        self.assertEqual(page["steps"][0]["failure_count"], 1)
        self.assertGreaterEqual(page["steps"][0]["elapsed_ms"], 0)
        self.assertEqual(page["history"][0]["state"], "superseded")
        self.assertNotIn("never expose", str(page))
        with self.assertRaises(DomainApiError):
            await service.timeline(str(ObjectId()), job["id"])

    async def test_step_logs_filter_scope_and_preserve_recorded_state(self):
        job = await self.begin()
        await self.wait_status("completed")
        jid = ObjectId(job["id"])
        step = await self.db.first_chapter_steps.find_one({"job_id": jid})
        await service.log(jid, step["_id"], "interrupted", step["step"]["label"],
                          {"state": "running", "label": "旧调用", "outputs": {"api_key": "secret"}})
        await service.log(jid, ObjectId(), "unrelated", "其他步骤", {"state": "done"})
        page = await service.logs(self.novel, job["id"], limit=100, step_id=str(step["_id"]))
        self.assertTrue(page["items"])
        self.assertTrue(all(row["title"] != "其他步骤" for row in page["items"]))
        self.assertNotIn("secret", str(page))
        self.assertNotIn("attempt", page["items"][0])
        with self.assertRaises(DomainApiError):
            await service.logs(self.novel, job["id"], step_id=str(ObjectId()))

    async def test_resumed_review_does_not_reactivate_an_old_attempt(self):
        reviewing, finish = asyncio.Event(), asyncio.Event()
        calls = 0
        async def review(*args):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise DomainApiError("TEST_REVIEW_INTERRUPTED", "审核连接中断", status_code=503)
            reviewing.set()
            await finish.wait()
            return {"summary": "已检查", "issues": []}
        with patch.object(service, "review_candidate", review):
            job = await self.begin()
            await self.wait_status("failed")
            jid = ObjectId(job["id"])
            old = await self.db.first_chapter_logs.find_one({"job_id": jid, "label": "核对剧情与事实一致性"})
            await service.control(self.novel, job["id"], "resume")
            try:
                await asyncio.wait_for(reviewing.wait(), 5)
                pending = (await self.db.first_chapter_jobs.find_one({"_id": jid}))["pending"]
                page = await service.logs(self.novel, job["id"], step_id=str(pending))
                old_public = next(row for row in page["items"] if row["id"] == str(old["_id"]))
                self.assertIs(old_public.get("is_live_attempt"), False)
                self.assertTrue(page.get("live_attempt_key"))
                self.assertNotEqual(old_public.get("attempt_key"), page["live_attempt_key"])
                self.assertEqual(old_public["state"], "running", "historical stored state must be preserved")
                self.assertTrue(any(row.get("is_live_attempt") and row.get("state") == "running" for row in page["items"]))
            finally:
                finish.set()
            await self.wait_status("completed")

    async def test_commit_response_lost_resumes_from_atomic_checkpoint_not_regeneration(self):
        original = service.run_mongo_write_unit
        lost = False
        async def uncertain(*args, **kwargs):
            nonlocal lost
            result = await original(*args, **kwargs)
            if not lost:
                lost = True
                raise DomainApiError("TEST_UNKNOWN_COMMIT", "模拟事务提交后响应丢失", status_code=503)
            return result
        with patch.object(service, "run_mongo_write_unit", uncertain):
            job = await self.begin()
            failed = await self.wait_status("failed")
            self.assertFalse(failed["has_saved_candidate"])
            self.assertEqual(await self.db.test_saves.count_documents({}), 1)
            self.assertEqual(await self.db.first_chapter_logs.count_documents({"job_id": ObjectId(job["id"]), "label": "保存并核对", "state": "done"}), 1)
            await service.control(self.novel, job["id"], "resume")
            await self.wait_status("completed")
        self.assertEqual(self.generated, 1)
        self.assertEqual(await self.db.test_saves.count_documents({}), 1)

    async def test_checkpoint_save_is_serialized_with_lease_heartbeat(self):
        original_perform = service.perform
        original_save = service.save_candidate
        observed = []
        async def perform(*args, **kwargs):
            lock = kwargs.get("checkpoint_lock")
            self.assertIsNotNone(lock, "heartbeat and save need the same checkpoint lock")
            observed.append(lock)
            return await original_perform(*args, **kwargs)
        async def save(*args, **kwargs):
            self.assertTrue(observed[-1].locked(), "formal save must hold the heartbeat lock")
            return await original_save(*args, **kwargs)
        with patch.object(service, "perform", perform), patch.object(service, "save_candidate", save):
            await self.begin()
            job = await self.wait_status("completed", "failed")
            self.assertEqual(job["status"], "completed", job.get("error"))
        self.assertEqual(await self.db.test_saves.count_documents({}), 1)

    async def test_expired_process_lease_is_paused_and_history_logs_paginate(self):
        self.release.clear()
        job = await self.begin()
        await asyncio.wait_for(self.started.wait(), 5)
        await service.shutdown()
        from datetime import timedelta
        await service.collection().update_one({"_id": ObjectId(job["id"])}, {"$set": {"status": "running", "lease_until": service.now() - timedelta(seconds=1)}})
        self.assertEqual((await service.latest(self.novel))["status"], "paused")
        for i in range(6):
            await service.log(ObjectId(job["id"]), "pagination", str(i), "测试", {"label": f"节点{i}", "state": "done", "outputs": {"api_key": "secret", "value": i}})
        page = await service.logs(self.novel, job["id"], limit=3)
        older = await service.logs(self.novel, job["id"], before=page["before"], limit=3)
        self.assertFalse({r["id"] for r in page["items"]} & {r["id"] for r in older["items"]})
        self.assertNotIn("secret", str(page))
        self.assertEqual((await service.history(self.novel))[0]["id"], job["id"])

    async def test_review_conflicts_have_bounded_repairs_and_preserve_all_candidates(self):
        async def conflict(*args):
            return {"summary": "存在冲突", "issues": [{"message": "身份冲突", "candidate_quote": "正文", "source_path": "identity", "source_quote": "身份"}]}
        with patch.object(service, "review_candidate", conflict):
            await self.begin()
            failed = await self.wait_status("failed")
        self.assertEqual(failed["error"]["code"], "FIRST_CHAPTER_CONSISTENCY_CONFLICT")
        self.assertEqual(self.generated, 3)
        self.assertEqual(await self.db.first_chapter_candidates.count_documents({}), 2)
        self.assertTrue(failed["has_saved_candidate"])
        self.assertEqual(await self.db.test_saves.count_documents({}), 0)

    async def test_simultaneous_first_and_volume_start_admit_only_one_active_job(self):
        from backend.services.novel import volume_writing_service as volume
        from backend.services.novel.volume_writing_contract import VolumeWritingStart
        async def catalog(*args):
            return {"scenes": [{}], "signature": "fixture", "summary": {}}
        with patch.object(service, "schedule"), patch.object(volume, "schedule"), patch.object(volume, "catalog", catalog):
            results = await asyncio.gather(self.begin(), volume.start(self.novel, str(ObjectId()), VolumeWritingStart(), "volume-start"), return_exceptions=True)
        count = await self.db.first_chapter_jobs.count_documents({"active": True}) + await self.db.volume_writing_jobs.count_documents({"active": True})
        self.assertEqual(count, 1, results)
        self.assertEqual(sum(isinstance(result, Exception) for result in results), 1)

    async def test_agent_continuation_is_reattached_to_resume(self):
        seen = []
        async def interrupted(task, request, *, on_event, **kwargs):
            seen.append(request.agent_continuation)
            if len(seen) == 1:
                await on_event({"event": "error", "data": {"details": {"agent_continuation": {"token": "a" * 64, "reason": "budget"}}}})
                raise DomainApiError("AGENT_ROUND_BUDGET_EXCEEDED", "整理轮次达到预算", status_code=409)
            return {"candidate": {"value": "正文"}, "basis": {}, "context": {"identity": "将军"}}
        with patch.object(service, "generate_task_flow", interrupted):
            job = await self.begin()
            await self.wait_status("failed")
            await service.control(self.novel, job["id"], "resume")
            await self.wait_status("completed")
        self.assertIsNone(seen[0])
        self.assertEqual(seen[1].token, "a" * 64)
        self.assertEqual(seen[1].additional_rounds, 2)

    async def test_source_loading_failure_preserves_generated_candidate_for_resume(self):
        async def generated(*args, **kwargs):
            self.generated += 1
            return {"candidate": {"value": "正文"}, "basis": {}}
        calls = 0
        async def sources(task, result, parameters):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise DomainApiError("TEST_SOURCE_READ_FAILURE", "模拟审核来源读取失败", status_code=503)
            return {**result, "context": {"identity": "将军"}}
        with patch.object(service, "generate_task_flow", generated), patch.object(service, "attach_review_sources", sources):
            job = await self.begin()
            failed = await self.wait_status("failed")
            self.assertTrue(failed["has_saved_candidate"])
            await service.control(self.novel, job["id"], "resume")
            await self.wait_status("completed")
        self.assertEqual(self.generated, 1)
        self.assertEqual(calls, 2)


if __name__ == "__main__":
    unittest.main()
