"""First chapter -> volume queue using original task graphs/saves in isolated Mongo."""
import asyncio
from collections import OrderedDict
import unittest
from unittest.mock import patch

from backend.api.error_protocol import DomainApiError
from backend.services.novel import first_chapter_service as first
from backend.services.novel import volume_writing_service as volume
from backend.services.novel import chapter_fact_service as chapter_facts
from backend.services.novel.chapter_fact_contract import ChapterFactExtractionV1, ChapterFactReviewV1
from backend.services.novel.volume_writing_contract import VolumeWritingStart
import scripts.test_first_chapter_flow as fixtures
from backend.services.task_flows import agent_continuation


class VolumeContinuationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fact_model = None
        self.recovery_store = patch.object(agent_continuation, "_continuations", OrderedDict())
        self.recovery_store.start()
        await fixtures.FullFlowTests.asyncSetUp(self)
        await first.start(self.novel, first.Start(include_optional=False), "fixture-first")
        job = await fixtures.FullFlowTests.wait_status(self, "completed", "failed")
        self.assertEqual(job["status"], "completed", job.get("error"))
        self.original = await self.db.chapters.find_one({})
        self.volume = str(self.original["volume_id"])
        self.calls.clear()
        self.reviewed_tasks.clear()
        async def fact_advice(settings, instructions, material, schema, **kwargs):
            if schema is ChapterFactExtractionV1:
                content = material["chapter_revision"]["content"]
                quote = content[:min(12, len(content))]
                prior = next((row["after"] for row in material["current_facts"]
                              if row["subject_type"] == "novel" and row["subject_id"] == self.novel
                              and row["field"] == "latest_chapter_state"), None)
                return schema.model_validate({
                    "summary": "提取一项可供下一章使用的事实",
                    "facts": [{
                        "subject_type": "novel", "subject_id": self.novel,
                        "field": "latest_chapter_state", "before": prior,
                        "after": f"章节 {material['chapter_revision']['chapter_id']} 已经发生并保存",
                        "evidence": {"start": 0, "quote": quote},
                        "risk": "normal",
                    }],
                }), {"model": "test-fact"}
            if schema is ChapterFactReviewV1:
                return schema.model_validate({
                    "summary": "证据明确",
                    "decisions": [{"index": 0, "decision": "accept", "message": "正文原文支持"}],
                }, context={"fact_count": len(material["extraction"]["facts"])}), {"model": "test-review"}
            raise AssertionError(schema)
        self.fact_model = patch.object(chapter_facts, "structured_advice", fact_advice)
        self.fact_model.start()

    async def cleanup(self):
        self.gate.set()
        try:
            await volume.shutdown()
            await fixtures.FullFlowTests.cleanup(self)
        finally:
            if self.fact_model is not None:
                self.fact_model.stop()
            self.recovery_store.stop()

    async def wait_volume(self, *states):
        async with asyncio.timeout(60):
            while True:
                job = await volume.latest(self.novel, self.volume)
                if job and job["status"] in states:
                    worker = volume._workers.get(job["id"])
                    if worker:
                        await asyncio.gather(worker, return_exceptions=True)
                    return job
                await asyncio.sleep(.02)

    async def begin(self, key="continue"):
        return await volume.start(self.novel, self.volume, VolumeWritingStart(), key)

    async def assert_original_untouched(self):
        actual = await self.db.chapters.find_one({"_id": self.original["_id"]})
        self.assertEqual(actual["content"], self.original["content"])
        self.assertEqual(actual["version"], self.original["version"])
        self.assertEqual(await self.db.chapter_revisions.count_documents({"chapter_id": actual["_id"]}), 2)

    async def test_continue_skips_first_body_reviews_new_work_and_repeat_is_noop(self):
        await self.begin()
        job = await self.wait_volume("completed", "failed")
        self.assertEqual(job["status"], "completed", job.get("error"))
        self.assertEqual(job["skipped_chapters"], 1)
        self.assertEqual(job["created_chapters"], 1)
        self.assertEqual(job["written_chapters"], 1)
        self.assertEqual(self.reviewed_tasks, {"plan_chapters_by_ai", "write_chapter_by_ai"})
        report = await self.db.volume_writing_reports.find_one({})
        self.assertIsNotNone(report)
        revision = await self.db.chapter_revisions.find_one({"_id": report["revision_id"]})
        self.assertEqual(revision["content_hash"], report["content_hash"])
        self.assertEqual(revision["chapter_id"], report["chapter_id"])
        fact_batch = await self.db.chapter_fact_batches.find_one({"chapter_id": report["chapter_id"]})
        self.assertIsNotNone(fact_batch)
        self.assertEqual(fact_batch["content_hash"], report["content_hash"])
        self.assertEqual(fact_batch["facts"][0]["field"], "latest_chapter_state")
        fact_logs = await self.db.volume_writing_logs.count_documents({
            "label": {"$in": ["提取章后事实", "独立审核章后事实", "更新事实库"]}, "state": "done",
        })
        self.assertEqual(fact_logs, 6)
        self.assertEqual(await self.db.chapter_fact_batches.count_documents({}), 2)
        self.assertIsNotNone(await self.db.chapter_fact_batches.find_one({"chapter_id": self.original["_id"]}))
        await self.assert_original_untouched()
        before = list(self.calls)
        await self.begin("repeat")
        repeated = await self.wait_volume("completed", "failed")
        self.assertEqual(repeated["status"], "completed", repeated.get("error"))
        self.assertEqual(self.calls, before)
        await self.assert_original_untouched()

    async def test_pause_retains_candidate_and_resume_does_not_regenerate_it(self):
        self.gate_task = "write_chapter_by_ai"
        self.gate.clear()
        started = await self.begin()
        await asyncio.wait_for(self.entered.wait(), 60)
        await volume.control(self.novel, self.volume, started["id"], "pause")
        self.gate.set()
        paused = await self.wait_volume("paused", "failed")
        self.assertEqual(paused["status"], "paused", paused.get("error"))
        self.assertTrue(paused["has_saved_candidate"])
        self.assertEqual(paused["written_chapters"], 0)
        calls = list(self.calls)
        await volume.control(self.novel, self.volume, started["id"], "resume")
        job = await self.wait_volume("completed", "failed")
        self.assertEqual(job["status"], "completed", job.get("error"))
        self.assertEqual(calls, self.calls)
        await self.assert_original_untouched()

    async def test_pause_after_model_node_stops_before_the_next_model_node(self):
        self.gate_task = "plan_chapters_by_ai"
        self.gate.clear()
        started = await self.begin("pause-between-model-nodes")
        await asyncio.wait_for(self.entered.wait(), 60)
        await volume.control(self.novel, self.volume, started["id"], "pause")
        self.gate.set()
        paused = await self.wait_volume("paused", "failed")
        self.assertEqual(paused["status"], "paused", paused.get("error"))
        first_pass = [node for task, node in self.calls if task == "plan_chapters_by_ai"]
        self.assertEqual(len(first_pass), 1, "pause must stop at the completed model-node boundary")

        stored = await self.db.volume_writing_jobs.find_one({"_id": volume.ObjectId(started["id"])})
        cached = (stored.get("pending") or {}).get("request", {}).get("cached_nodes", [])
        self.assertTrue(cached, "the completed model node must be durable before pausing")

        await volume.control(self.novel, self.volume, started["id"], "resume")
        completed = await self.wait_volume("completed", "failed")
        self.assertEqual(completed["status"], "completed", completed.get("error"))
        after_resume = [node for task, node in self.calls if task == "plan_chapters_by_ai"]
        self.assertEqual(after_resume.count(first_pass[0]), 1, "resume must reuse the cached model result")
        await self.assert_original_untouched()

    async def test_pause_resume_reuses_completed_agent_preparation(self):
        from backend.services.task_flows import agent as agent_module

        delegated = agent_module.execute_agent
        agent_calls = []

        async def counted_agent(*args, **kwargs):
            agent_calls.append(kwargs["models"].task)
            async for event in delegated(*args, **kwargs):
                yield event

        self.gate_task = "plan_chapters_by_ai"
        self.gate.clear()
        with patch.object(agent_module, "execute_agent", counted_agent):
            started = await self.begin("reuse-completed-agent")
            await asyncio.wait_for(self.entered.wait(), 60)
            self.assertEqual(agent_calls.count("plan_chapters_by_ai"), 1)
            await volume.control(self.novel, self.volume, started["id"], "pause")
            self.gate.set()
            paused = await self.wait_volume("paused", "failed")
            self.assertEqual(paused["status"], "paused", paused.get("error"))
            await volume.control(self.novel, self.volume, started["id"], "resume")
            completed = await self.wait_volume("completed", "failed")
        self.assertEqual(completed["status"], "completed", completed.get("error"))
        self.assertEqual(agent_calls.count("plan_chapters_by_ai"), 1,
                         "resume must reuse the completed agent preparation")
        await self.assert_original_untouched()

    async def test_next_chapter_reads_the_previous_confirmed_fact_batch(self):
        from backend.services.novel.chapter_service import ChapterService
        scene = await self.db.scenes.find_one({})
        plan = {
            "goal": "继续寻找信件", "start_state": "已有第一条线索", "end_state": "确认下一步调查方向",
            "beats": ["核对线索"], "must_include": [], "must_avoid": [], "target_word_count": 3000,
            "pov_character_id": None,
        }
        await ChapterService.create_chapter(str(scene["_id"]), {"title": "第二章", "planning": plan}, idempotency_key="fixture-second-chapter")
        await ChapterService.create_chapter(str(scene["_id"]), {"title": "第三章", "planning": plan}, idempotency_key="fixture-third-chapter")
        await self.db.scenes.update_one({"_id": scene["_id"]}, {
            "$set": {"planning.target_chapter_count": 3}, "$inc": {"version": 1},
        })
        await self.begin("three-chapters")
        job = await self.wait_volume("completed", "failed")
        self.assertEqual(job["status"], "completed", job.get("error"))
        batches = await self.db.chapter_fact_batches.find({}).sort("created_at", 1).to_list(None)
        self.assertEqual(len(batches), 3)
        self.assertIsNone(batches[0]["facts"][0]["before"])
        self.assertEqual(batches[1]["facts"][0]["before"], batches[0]["facts"][0]["after"])
        self.assertEqual(batches[2]["facts"][0]["before"], batches[1]["facts"][0]["after"])
        second = await self.db.chapters.find_one({"_id": batches[1]["chapter_id"]})
        third = await self.db.chapters.find_one({"_id": batches[2]["chapter_id"]})
        await ChapterService.update_chapter(str(second["_id"]), {
            "expected_version": second["version"], "content": second["content"] + "\n作者后来修改了正文。",
        })
        from backend.services.novel.writing_context import load_writing_context
        with self.assertRaises(DomainApiError) as stale:
            await load_writing_context("write_chapter_by_ai", {}, {
                "novel_id": self.novel, "target_type": "chapter", "target_id": str(third["_id"]),
                "expected_version": third["version"],
            })
        self.assertEqual(stale.exception.code, "CHAPTER_FACT_RECONCILIATION_REQUIRED")

    async def test_failed_review_keeps_candidate_and_only_retries_review(self):
        async def unavailable(*args):
            raise DomainApiError("TEST_REVIEW_NETWORK", "审核网络中断", status_code=503)
        with patch.object(volume, "review_candidate", unavailable):
            started = await self.begin()
            failed = await self.wait_volume("failed", "completed")
        self.assertEqual(failed["status"], "failed")
        self.assertTrue(failed["has_saved_candidate"])
        calls = list(self.calls)
        await volume.control(self.novel, self.volume, started["id"], "resume")
        result = await self.wait_volume("completed", "failed")
        self.assertEqual(result["status"], "completed", result.get("error"))
        # The retained plan is reviewed without regenerating it; only the subsequent body is generated.
        self.assertEqual([c for c in calls if c[0] == "plan_chapters_by_ai"], [c for c in self.calls if c[0] == "plan_chapters_by_ai"])
        await self.assert_original_untouched()

    async def test_fact_review_failure_resumes_without_reextracting_or_touching_body(self):
        delegated = chapter_facts.structured_advice
        calls = []

        async def fail_first_review(settings, instructions, material, schema, **kwargs):
            chapter_id = material["chapter_revision"]["chapter_id"]
            calls.append((chapter_id, schema))
            if schema is ChapterFactReviewV1 and chapter_id == str(self.original["_id"]):
                if sum(row == (chapter_id, ChapterFactReviewV1) for row in calls) == 1:
                    raise DomainApiError("TEST_FACT_REVIEW_NETWORK", "事实审核网络中断", status_code=503)
            return await delegated(settings, instructions, material, schema, **kwargs)

        with patch.object(chapter_facts, "structured_advice", fail_first_review):
            started = await self.begin("fact-review-resume")
            failed = await self.wait_volume("failed", "completed")
            self.assertEqual(failed["status"], "failed")
            self.assertEqual(failed["error"]["code"], "TEST_FACT_REVIEW_NETWORK")
            self.assertEqual(await self.db.chapter_fact_batches.count_documents({}), 0)
            await self.assert_original_untouched()
            await volume.control(self.novel, self.volume, started["id"], "resume")
            completed = await self.wait_volume("completed", "failed")
        self.assertEqual(completed["status"], "completed", completed.get("error"))
        original_id = str(self.original["_id"])
        self.assertEqual(sum(row == (original_id, ChapterFactExtractionV1) for row in calls), 1)
        self.assertEqual(sum(row == (original_id, ChapterFactReviewV1) for row in calls), 2)
        await self.assert_original_untouched()

    async def test_invalid_fact_review_quote_is_retried_without_reextracting(self):
        delegated = chapter_facts.structured_advice
        extraction_calls = 0
        review_calls = 0

        async def invalid_review_once(settings, instructions, material, schema, **kwargs):
            nonlocal extraction_calls, review_calls
            chapter_id = material["chapter_revision"]["chapter_id"]
            if chapter_id == str(self.original["_id"]) and schema is ChapterFactExtractionV1:
                extraction_calls += 1
            if chapter_id == str(self.original["_id"]) and schema is ChapterFactReviewV1:
                review_calls += 1
                if review_calls == 1:
                    facts = material["extraction"]["facts"]
                    return schema.model_validate({
                        "summary": "第一次错误地拼接了不连续引文",
                        "decisions": [{
                            "index": index,
                            "decision": "accept",
                            "message": "正文支持",
                            "source_quote": "正文开头……正文结尾（并非连续原文）",
                        } for index in range(len(facts))],
                    }, context={"fact_count": len(facts)}), {"model": "test-invalid-review-quote"}
            return await delegated(settings, instructions, material, schema, **kwargs)

        with patch.object(chapter_facts, "structured_advice", invalid_review_once):
            await self.begin("fact-review-quote-repair")
            completed = await self.wait_volume("completed", "failed")
        self.assertEqual(completed["status"], "completed", completed.get("error"))
        self.assertEqual(extraction_calls, 1, "an invalid review quote must not regenerate the fact proposal")
        self.assertEqual(review_calls, 2, "only the invalid independent review should be retried")
        repaired = await self.db.volume_writing_logs.find_one({
            "label": "重新审核章后事实", "state": "done",
        })
        self.assertIsNotNone(repaired)
        await self.assert_original_untouched()

    async def test_invalid_fact_evidence_is_repaired_automatically(self):
        delegated = chapter_facts.structured_advice
        extraction_calls = 0

        async def invalid_once(settings, instructions, material, schema, **kwargs):
            nonlocal extraction_calls
            if schema is ChapterFactExtractionV1:
                extraction_calls += 1
                if extraction_calls == 1:
                    return schema.model_validate({
                        "summary": "第一次返回错误引文",
                        "facts": [{
                            "subject_type": "novel", "subject_id": self.novel,
                            "field": "latest_chapter_state", "before": None,
                            "after": "错误候选", "evidence": {"start": 1, "quote": "不存在的原文"},
                            "risk": "normal",
                        }],
                    }), {"model": "test-invalid"}
            return await delegated(settings, instructions, material, schema, **kwargs)

        with patch.object(chapter_facts, "structured_advice", invalid_once):
            await self.begin("fact-evidence-repair")
            completed = await self.wait_volume("completed", "failed")
        self.assertEqual(completed["status"], "completed", completed.get("error"))
        self.assertGreaterEqual(extraction_calls, 3)
        repaired = await self.db.volume_writing_logs.find_one({"label": "修订章后事实候选", "state": "done"})
        self.assertIsNotNone(repaired)
        await self.assert_original_untouched()

    async def test_consistency_conflict_never_adopts_the_candidate(self):
        async def conflict(*args):
            return {"summary": "确认冲突", "issues": [{"message": "身份冲突", "candidate_quote": "已授官", "source_path": "context.identity", "source_quote": "尚未授官"}]}
        with patch.object(volume, "review_candidate", conflict):
            await self.begin()
            failed = await self.wait_volume("failed", "completed")
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["error"]["code"], "VOLUME_WRITING_CONSISTENCY_CONFLICT")
        self.assertTrue(failed["has_saved_candidate"])
        self.assertEqual(await self.db.chapters.count_documents({}), 1)
        audit_log = await self.db.volume_writing_logs.find_one({"label": "核对剧情与事实一致性", "state": "failed"})
        self.assertEqual(audit_log["stage"], "reviewing")
        await self.assert_original_untouched()

    async def test_manual_body_saved_during_generation_is_not_overwritten(self):
        from backend.services.novel.chapter_service import ChapterService
        self.gate_task = "write_chapter_by_ai"
        self.gate.clear()
        await self.begin()
        await asyncio.wait_for(self.entered.wait(), 60)
        target = await self.db.chapters.find_one({"_id": {"$ne": self.original["_id"]}})
        await ChapterService.update_chapter(str(target["_id"]), {"expected_version": target["version"], "content": "作者手动保存的后续正文"})
        self.gate.set()
        failed = await self.wait_volume("failed", "completed")
        self.assertEqual(failed["status"], "failed")
        self.assertEqual((await self.db.chapters.find_one({"_id": target["_id"]}))["content"], "作者手动保存的后续正文")
        await self.assert_original_untouched()

    async def test_pause_during_review_persists_it_without_saving_or_repeating(self):
        entered, release = asyncio.Event(), asyncio.Event()
        original_review = volume.review_candidate
        count = 0
        async def gated(*args):
            nonlocal count
            count += 1
            if count == 1:
                entered.set()
                await release.wait()
            return await original_review(*args)
        with patch.object(volume, "review_candidate", gated):
            started = await self.begin()
            try:
                await asyncio.wait_for(entered.wait(), 60)
                await volume.control(self.novel, self.volume, started["id"], "pause")
            finally:
                release.set()
            paused = await self.wait_volume("paused", "failed")
            self.assertEqual(paused["status"], "paused", paused.get("error"))
            self.assertEqual(await self.db.chapters.count_documents({}), 1)
            await volume.control(self.novel, self.volume, started["id"], "resume")
            completed = await self.wait_volume("completed", "failed")
            self.assertEqual(completed["status"], "completed", completed.get("error"))
        self.assertEqual(count, 2, "plan review is reused after pause; only the new body needs another review")
        await self.assert_original_untouched()

    async def test_review_report_failure_rolls_back_body_and_reuses_paid_candidate(self):
        async def broken(*args, **kwargs):
            raise DomainApiError("TEST_REPORT_WRITE", "审校记录写入失败", status_code=503)
        with patch.object(volume, "record_revision_review", broken, create=True):
            started = await self.begin()
            failed = await self.wait_volume("failed", "completed")
        self.assertEqual(failed["status"], "failed")
        self.assertTrue(failed["has_saved_candidate"])
        other = await self.db.chapters.find_one({"_id": {"$ne": self.original["_id"]}})
        self.assertEqual(other["content"], "")
        self.assertEqual(other["version"], 1)
        calls = list(self.calls)
        await volume.control(self.novel, self.volume, started["id"], "resume")
        completed = await self.wait_volume("completed", "failed")
        self.assertEqual(completed["status"], "completed", completed.get("error"))
        self.assertEqual(calls, self.calls)
        self.assertEqual(await self.db.volume_writing_reports.count_documents({}), 1)
        await self.assert_original_untouched()

    async def test_lost_commit_response_then_author_edit_keeps_original_revision_review(self):
        from backend.services.novel.chapter_service import ChapterService
        original_write = volume.run_mongo_write_unit
        lost = False
        async def uncertain(*args, **kwargs):
            nonlocal lost
            result = await original_write(*args, **kwargs)
            if not lost and await self.db.volume_writing_reports.count_documents({}):
                lost = True
                target = await self.db.chapters.find_one({"_id": {"$ne": self.original["_id"]}})
                await ChapterService.update_chapter(str(target["_id"]), {"expected_version": target["version"], "content": "提交后作者保存的新版本"})
                raise DomainApiError("TEST_UNKNOWN_COMMIT", "提交成功但响应丢失", status_code=503)
            return result
        with patch.object(volume, "run_mongo_write_unit", uncertain):
            started = await self.begin()
            failed = await self.wait_volume("failed", "completed")
        self.assertEqual(failed["status"], "failed")
        report = await self.db.volume_writing_reports.find_one({})
        revision = await self.db.chapter_revisions.find_one({"_id": report["revision_id"]})
        self.assertEqual(revision["content_hash"], report["content_hash"])
        self.assertNotEqual(revision["content"], "提交后作者保存的新版本")
        await volume.control(self.novel, self.volume, started["id"], "resume")
        resumed = await self.wait_volume("failed", "completed")
        self.assertEqual(resumed["error"]["code"], "VOLUME_WRITING_SAVED_CHANGED")
        target = await self.db.chapters.find_one({"_id": report["chapter_id"]})
        self.assertEqual(target["content"], "提交后作者保存的新版本")
        self.assertEqual(await self.db.volume_writing_reports.count_documents({}), 1)
        await self.assert_original_untouched()
