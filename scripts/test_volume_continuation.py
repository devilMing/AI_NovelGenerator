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

    async def begin(self, key="continue", *, complete=False):
        return await volume.start(self.novel, self.volume, VolumeWritingStart(complete_planning=complete), key)

    async def assert_original_untouched(self):
        actual = await self.db.chapters.find_one({"_id": self.original["_id"]})
        self.assertEqual(actual["content"], self.original["content"])
        self.assertEqual(actual["version"], self.original["version"])
        self.assertEqual(await self.db.chapter_revisions.count_documents({"chapter_id": actual["_id"]}), 2)

    async def empty_volume_fixture(self):
        """Only this test's random database: leave the saved volume skeleton and core resources."""
        for name in ("arcs", "scenes", "chapters", "chapter_revisions"):
            await self.db[name].delete_many({})
        await self.db.volumes.update_one({"_id": self.original["volume_id"]}, {"$set": {
            "plot": "", "plot_source_fingerprint": None, "preparation": {},
            "arcs_count": 0, "chapter_count": 0, "word_count": 0,
        }, "$inc": {"version": 1}})

    async def test_complete_volume_fills_missing_plot_preparation_arcs_scenes_and_bodies(self):
        await self.empty_volume_fixture()
        preview = await volume.preview(self.novel, self.volume)
        self.assertTrue(preview["volume_plot_missing"])
        self.assertEqual(preview["scenes"], 0)
        await self.begin("complete-empty-volume", complete=True)
        completed = await self.wait_volume("completed", "failed")
        self.assertEqual(completed["status"], "completed", completed.get("error"))
        self.assertTrue(completed["complete_planning"])
        self.assertEqual((completed["created_arcs"], completed["created_scenes"], completed["written_chapters"]), (1, 1, 2))
        self.assertEqual(completed["completed_arcs"], 1)
        self.assertEqual(await self.db.chapter_fact_batches.count_documents({}), 2)
        tasks = {row["task"] for row in await self.db.volume_writing_preparation_reports.find({}).to_list(None)}
        self.assertTrue({"generate_volume_plot_by_ai", "generate_arc_plot_by_ai", "plan_arcs_by_ai", "plan_scenes_by_ai", "plan_chapters_by_ai"} <= tasks, tasks)
        self.assertEqual(sum(bool(row["content"].strip()) for row in await self.db.chapters.find({}).to_list(None)), 2)
        self.assertFalse(completed["summary"]["volume_plot_missing"])
        self.assertEqual(completed["summary"]["arcs_without_scenes"], 0)
        calls = list(self.calls)
        await self.begin("complete-empty-volume-repeat", complete=True)
        repeated = await self.wait_volume("completed", "failed")
        self.assertEqual(repeated["status"], "completed", repeated.get("error"))
        self.assertEqual(repeated["written_chapters"], 0)
        self.assertEqual(repeated["skipped_chapters"], 2)
        self.assertEqual(self.calls, calls)

    async def test_complete_volume_adds_next_arc_without_overwriting_existing_body_or_plot(self):
        from backend.services.novel.arc_service import ArcService
        from backend.services.novel.plot_service import stamp_manual_plot
        volume_id = self.original["volume_id"]
        current = await self.db.volumes.find_one({"_id": volume_id})
        await self.db.volumes.update_one({"_id": volume_id}, {"$set": {"planning.target_chapter_count": 4, "planning.target_word_count": 12000}, "$inc": {"version": 1}})
        fields = {"plot": current["plot"]}
        await stamp_manual_plot(self.novel, "volume", self.volume, fields)
        await self.db.volumes.update_one({"_id": volume_id}, {"$set": fields})
        second = await ArcService.create_arc(self.volume, {"title": "北上履约", "summary": "继续查信", "planning": {
            "goal": "查明信件来源", "start_state": "已经入城", "end_state": "找到新的线索", "key_beats": ["调查信件"],
            "target_chapter_count": 2, "target_word_count": 6000}}, idempotency_key="fixture-next-arc")
        original_arc = await self.db.arcs.find_one({"_id": self.original["arc_id"]})
        fields = {"plot": original_arc["plot"]}
        await stamp_manual_plot(self.novel, "arc", str(original_arc["_id"]), fields)
        await self.db.arcs.update_one({"_id": original_arc["_id"]}, {"$set": fields})
        await self.begin("complete-next-arc", complete=True)
        completed = await self.wait_volume("completed", "failed")
        self.assertEqual(completed["status"], "completed", completed.get("error"))
        self.assertEqual(completed["completed_arcs"], 2)
        self.assertEqual(completed["written_chapters"], 3)
        self.assertEqual(completed["created_scenes"], 1)
        self.assertEqual((await self.db.arcs.find_one({"_id": original_arc["_id"]}))["plot"], original_arc["plot"])
        self.assertTrue((await self.db.arcs.find_one({"_id": second["_id"]}))["plot"].strip())
        self.assertFalse(any(task == "generate_volume_plot_by_ai" for task, node in self.calls))
        await self.assert_original_untouched()

    async def test_complete_preparation_lost_commit_response_resumes_without_duplicate_scenes(self):
        await self.empty_volume_fixture()
        original_write = volume.run_mongo_write_unit
        lost = False
        async def uncertain(*args, **kwargs):
            nonlocal lost
            result = await original_write(*args, **kwargs)
            if not lost and args[1] == "save_volume_preparation_checkpoint" and await self.db.scenes.count_documents({}):
                lost = True
                raise DomainApiError("TEST_PREPARATION_COMMIT_LOST", "提交成功，响应丢失", status_code=503)
            return result
        with patch.object(volume, "run_mongo_write_unit", uncertain):
            started = await self.begin("complete-lost-response", complete=True)
            failed = await self.wait_volume("failed", "completed")
        self.assertEqual(failed["error"]["code"], "TEST_PREPARATION_COMMIT_LOST")
        before_calls = [row for row in self.calls if row[0] == "plan_scenes_by_ai"]
        await volume.control(self.novel, self.volume, started["id"], "resume")
        completed = await self.wait_volume("completed", "failed")
        self.assertEqual(completed["status"], "completed", completed.get("error"))
        self.assertEqual([row for row in self.calls if row[0] == "plan_scenes_by_ai"], before_calls)
        self.assertEqual(await self.db.scenes.count_documents({}), 1)
        self.assertEqual(await self.db.chapters.count_documents({}), 2)

    async def test_complete_preparation_rejects_external_structure_edit_during_generation(self):
        await self.empty_volume_fixture()
        self.gate_task = "plan_scenes_by_ai"
        self.gate.clear()
        await self.begin("complete-manual-change", complete=True)
        try:
            await asyncio.wait_for(self.entered.wait(), 60)
            await self.db.volumes.update_one({"_id": self.original["volume_id"]}, {"$set": {"title": "作者改后的卷名"}, "$inc": {"version": 1}})
        finally:
            self.gate.set()
        failed = await self.wait_volume("failed", "completed")
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(await self.db.scenes.count_documents({}), 0)
        self.assertEqual((await self.db.volumes.find_one({"_id": self.original["volume_id"]}))["title"], "作者改后的卷名")

    async def test_complete_pause_keeps_scene_candidate_and_resume_adopts_it_once(self):
        await self.empty_volume_fixture()
        delegated_review = volume.review_candidate
        paused_once = False
        async def review(task, result, settings):
            nonlocal paused_once
            report = await delegated_review(task, result, settings)
            if task == "plan_scenes_by_ai" and not paused_once:
                paused_once = True
                current = await self.db.volume_writing_jobs.find_one({"active": True})
                await volume.control(self.novel, self.volume, str(current["_id"]), "pause")
            return report
        with patch.object(volume, "review_candidate", review):
            started = await self.begin("complete-pause-scenes", complete=True)
            paused = await self.wait_volume("paused", "failed")
            self.assertEqual(paused["status"], "paused", paused.get("error"))
            self.assertTrue(paused["has_saved_candidate"])
            self.assertEqual(await self.db.scenes.count_documents({}), 0)
            before_calls = [row for row in self.calls if row[0] == "plan_scenes_by_ai"]
            await volume.control(self.novel, self.volume, started["id"], "resume")
            completed = await self.wait_volume("completed", "failed")
        self.assertEqual(completed["status"], "completed", completed.get("error"))
        self.assertEqual(await self.db.scenes.count_documents({}), 1)
        self.assertEqual([row for row in self.calls if row[0] == "plan_scenes_by_ai"], before_calls)

    async def test_scene_conflict_repairs_and_independently_reviews_before_full_adoption(self):
        from copy import deepcopy
        await self.empty_volume_fixture()
        delegated = volume.review_candidate
        scene_reviews, repairs = [], []
        async def review(task, result, settings):
            if task == "plan_scenes_by_ai":
                scene_reviews.append(deepcopy(result))
                if len(scene_reviews) == 1:
                    return {"summary":"时序提前", "issues":[{"message":"先核算后报价", "candidate_quote":"提前报价",
                        "source_path":"context.plot", "source_quote":"先核算"}]}
            return await delegated(task, result, settings)
        async def repair(task, result, report):
            repairs.append(deepcopy(result))
            candidate=deepcopy(result["candidate"])
            candidate["scenes"][0]["summary"]="先核算卸仓和人手，后续场景再提出包销。"
            return candidate, {"model":"test-planning-repair"}
        with patch.object(volume,"review_candidate",review), patch.object(volume,"repair_plan_candidate",repair):
            await self.begin("repair-scenes-full",complete=True)
            completed=await self.wait_volume("completed","failed")
        self.assertEqual(completed["status"],"completed",completed.get("error"))
        self.assertEqual((len(repairs),len(scene_reviews)),(1,2))
        self.assertEqual(scene_reviews[0]["basis"],scene_reviews[1]["basis"])
        scene=await self.db.scenes.find_one({})
        self.assertEqual(scene["summary"],"先核算卸仓和人手，后续场景再提出包销。")
        report=await self.db.volume_writing_preparation_reports.find_one({"task":"plan_scenes_by_ai"})
        self.assertEqual(len(report["consistency_repairs"]),1)
        self.assertEqual(report["consistency_repairs"][0]["after_review"]["issues"],[])
        self.assertEqual(completed["written_chapters"],2)

    async def test_pause_after_scene_repair_resumes_review_without_repairing_again(self):
        from copy import deepcopy
        await self.empty_volume_fixture()
        delegated=volume.review_candidate
        review_calls, repair_calls=0,0
        async def review(task,result,settings):
            nonlocal review_calls
            if task=="plan_scenes_by_ai":
                review_calls+=1
                if review_calls==1:
                    return {"summary":"时序提前", "issues":[{"message":"先核算后报价", "candidate_quote":"提前报价",
                        "source_path":"context.plot", "source_quote":"先核算"}]}
            return await delegated(task,result,settings)
        async def repair(task,result,report):
            nonlocal repair_calls
            repair_calls+=1
            current=await self.db.volume_writing_jobs.find_one({"active":True})
            await volume.control(self.novel,self.volume,str(current["_id"]),"pause")
            candidate=deepcopy(result["candidate"])
            candidate["scenes"][0]["summary"]="修正后的完整场景候选。"
            return candidate,{}
        with patch.object(volume,"review_candidate",review),patch.object(volume,"repair_plan_candidate",repair):
            started=await self.begin("repair-scenes-pause",complete=True)
            paused=await self.wait_volume("paused","failed")
            self.assertEqual(paused["status"],"paused",paused.get("error"))
            self.assertEqual(await self.db.scenes.count_documents({}),0)
            stored=await self.db.volume_writing_jobs.find_one({"_id":volume.ObjectId(started["id"])})
            self.assertIsNone(stored["pending"]["review"])
            self.assertIn("after_candidate",stored["pending"]["consistency_repairs"][0])
            await volume.control(self.novel,self.volume,started["id"],"resume")
            completed=await self.wait_volume("completed","failed")
        self.assertEqual(completed["status"],"completed",completed.get("error"))
        self.assertEqual((repair_calls,review_calls),(1,2))
        self.assertEqual(await self.db.scenes.count_documents({}),1)

    async def test_chapter_plan_conflict_repairs_without_overwriting_existing_body(self):
        from copy import deepcopy
        delegated=volume.review_candidate
        reviews=0
        async def review(task,result,settings):
            nonlocal reviews
            if task=="plan_chapters_by_ai":
                reviews+=1
                if reviews==1:
                    return {"summary":"时序提前", "issues":[{"message":"先核算后报价", "candidate_quote":"提前报价",
                        "source_path":"context.plot", "source_quote":"先核算"}]}
            return await delegated(task,result,settings)
        async def repair(task,result,report):
            candidate=deepcopy(result["candidate"])
            candidate["chapters"][0]["summary"]="核算完成后再讨论包销。"
            return candidate,{}
        with patch.object(volume,"review_candidate",review),patch.object(volume,"repair_plan_candidate",repair):
            await self.begin("repair-chapter-plan")
            completed=await self.wait_volume("completed","failed")
        self.assertEqual(completed["status"],"completed",completed.get("error"))
        self.assertEqual(reviews,2)
        self.assertEqual(completed["written_chapters"],1)
        await self.assert_original_untouched()

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

    async def test_empty_chapter_pov_is_filled_then_independently_reviewed_before_adoption(self):
        from copy import deepcopy
        from backend.services.novel import volume_consistency_repair as repair_module
        actor = await self.db.characters.find_one({"is_deleted": False})
        await self.db.scenes.update_one({"_id": self.original["scene_id"]},
            {"$set": {"planning.character_ids": [actor["character_id"]]}, "$inc": {"version": 1}})
        delegated = volume.review_candidate
        reviews, chosen = [], []
        async def review(task, result, settings):
            if task == "plan_chapters_by_ai":
                reviews.append(deepcopy(result["candidate"]))
                if result["candidate"]["chapters"][0]["planning"]["pov_character_id"] is None:
                    selected = result["context"]["selected_entities"]
                    index = next(i for i, row in enumerate(selected) if row["resource_type"] == "character")
                    chosen.append(selected[index]["id"])
                    return {"summary": "补齐视角", "issues": [{"message": "应填写已读人物的视角字段",
                        "candidate_quote": result["candidate"]["chapters"][0]["title"],
                        "source_path": f"context.selected_entities.{index}.name", "source_quote": selected[index]["name"]}]}
            return await delegated(task, result, settings)
        async def fill(settings, instructions, payload, schema, **kwargs):
            self.assertIn(chosen[0], payload["repair_contract"]["fillable_pov_ids"])
            candidate = deepcopy(payload["candidate"])
            candidate["chapters"][0]["planning"]["pov_character_id"] = chosen[0]
            return schema.model_validate(candidate), {"model": "fixture-pov-fill"}
        original_confirm = volume.confirm_writing
        async def pause_after_plan(task, *args, **kwargs):
            saved = await original_confirm(task, *args, **kwargs)
            if task == "plan_chapters_by_ai":
                await self.db.volume_writing_jobs.update_one({"novel_id": volume.ObjectId(self.novel), "status": "running"},
                    {"$set": {"pause_requested": True}})
            return saved
        with patch.object(volume, "review_candidate", review), patch.object(repair_module, "structured_advice", fill), patch.object(volume, "confirm_writing", pause_after_plan):
            await self.begin("repair-empty-chapter-pov")
            completed = await self.wait_volume("paused", "failed")
        self.assertEqual(completed["status"], "paused", completed.get("error"))
        self.assertEqual(len(reviews), 2)
        created = await self.db.chapters.find_one({"_id": {"$ne": self.original["_id"]}})
        self.assertEqual(created["planning"]["pov_character_id"], chosen[0])
        self.assertEqual(created["title"], reviews[0]["chapters"][0]["title"])
        self.assertEqual(created["planning"]["target_word_count"], reviews[0]["chapters"][0]["planning"]["target_word_count"])
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

    async def test_long_fact_evidence_does_not_block_existing_body_closure_or_change_source_basis(self):
        import json
        from copy import deepcopy
        from backend.db.write_contracts import canonical_request_hash
        from backend.services.novel.chapter_service import ChapterService
        from backend.services.novel.writing_context import load_writing_context

        # Build valid long evidence in this test's isolated, random database.
        original = await ChapterService.update_chapter(str(self.original["_id"]), {
            "expected_version": self.original["version"], "content": "核对原始证据。" * 1500})
        revision = await self.db.chapter_revisions.find_one({"chapter_id": original["_id"], "version": original["version"]})
        extraction = ChapterFactExtractionV1.model_validate({"summary": "历史已确认事实", "facts": [
            {"subject_type": "novel", "subject_id": self.novel, "field": f"state_{index}",
             "before": None, "after": {"state": f"有效状态{index}", "count": index},
             "evidence": {"start": 0, "quote": original["content"][:10000]}, "risk": "normal"}
            for index in range(25)]})
        validated = chapter_facts.validate_extraction(extraction, original["content"], {("novel", self.novel)}, {})
        batch = await chapter_facts.adopt_fact_batch(self.novel, original, revision, validated, [],
            {"accepted_indices": list(range(25)), "omitted_indices": []}, [])
        batch = await self.db.chapter_fact_batches.find_one({"_id": batch["_id"]})
        second = await ChapterService.create_chapter(str(original["scene_id"]), {
            "title": "已有第二章", "planning": original["planning"], "content": "第二章的正文已经保存，不得重写。"},
            idempotency_key="long-evidence-second")
        scope = {"novel_id": self.novel, "target_type": "chapter", "target_id": str(second["_id"]), "expected_version": second["version"]}
        guidance = "作者要求必须完整保留。" * 50
        data = await load_writing_context("write_chapter_by_ai", {"user_guidance": guidance}, scope, check_ready=False)
        facts = data["context"]["confirmed_chapter_facts"]
        self.assertEqual(len(facts), 25)
        self.assertEqual([row["after"] for row in facts], [row["after"] for row in chapter_facts.public_current_facts([batch])])
        self.assertTrue(all("evidence" not in row and "before" not in row for row in facts))
        self.assertEqual(data["context"]["user_guidance"], guidance)
        full = chapter_facts.public_current_facts([batch])
        old = {**data["context"], "confirmed_chapter_facts": full}
        self.assertGreater(len(json.dumps(old, ensure_ascii=False)), 200000)
        self.assertLess(len(json.dumps(data["context"], ensure_ascii=False)), 200000)
        read = next(row for row in data["context_basis"]["reads"] if row["kind"] == "chapter_fact_batches")
        self.assertEqual(read["fingerprint"], canonical_request_hash(full))

        model = chapter_facts.structured_advice
        seen = []
        async def inspect_material(settings, instructions, material, schema, **kwargs):
            if schema is ChapterFactExtractionV1:
                seen.append(deepcopy(material["current_facts"]))
            return await model(settings, instructions, material, schema, **kwargs)
        with patch.object(chapter_facts, "structured_advice", inspect_material):
            await self.begin("close-after-long-history")
            completed = await self.wait_volume("completed", "failed")
        self.assertEqual(completed["status"], "completed", completed.get("error"))
        self.assertEqual(completed["written_chapters"], 0)
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0], facts)
        self.assertFalse(self.calls, "Existing chapter bodies must not invoke generation")
        self.assertEqual(await self.db.chapter_fact_batches.find_one({"_id": batch["_id"]}), batch)
        for document in (original, second):
            current = await self.db.chapters.find_one({"_id": document["_id"]})
            self.assertEqual((current["content"], current["version"]), (document["content"], document["version"]))

        # Even omitted audit text is still fingerprinted for source revalidation.
        await self.db.chapter_fact_batches.update_one({"_id": batch["_id"]}, {"$set": {"facts.0.evidence.quote": original["content"][:9999]}})
        changed = await load_writing_context("write_chapter_by_ai", {"user_guidance": guidance}, scope, check_ready=False)
        self.assertEqual(changed["context"]["confirmed_chapter_facts"], facts)
        self.assertNotEqual(changed["context_basis"], data["context_basis"])

        # Effective values are never silently shortened to satisfy the budget.
        await self.db.chapter_fact_batches.update_one({"_id": batch["_id"]}, {"$set": {"facts.0.after": "完整事实" * 60000}})
        with self.assertRaises(DomainApiError) as oversized:
            await load_writing_context("write_chapter_by_ai", {}, scope, check_ready=False)
        self.assertEqual(oversized.exception.code, "WRITING_CONTEXT_TOO_LARGE")

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

    async def test_fact_total_timeout_keeps_body_and_resume_retries_only_unfinished_call(self):
        delegated = chapter_facts.structured_advice
        real_call = volume.model_call
        attempts = []
        cancelled = asyncio.Event()

        async def advice(settings, instructions, material, schema, **kwargs):
            attempts.append(schema)
            if len(attempts) == 1:
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()
            return await delegated(settings, instructions, material, schema, **kwargs)

        async def short_call(*args, **kwargs):
            if kwargs.get("total_seconds", 300) is not None:
                kwargs["total_seconds"] = .05
            return await real_call(*args, **kwargs)

        with patch.object(chapter_facts, "structured_advice", advice), patch.object(volume, "model_call", short_call):
            started = await self.begin("fact-total-timeout")
            failed = await self.wait_volume("failed", "completed")
            self.assertEqual(failed["status"], "failed")
            self.assertEqual(failed["error"]["code"], "VOLUME_WRITING_MODEL_TIMEOUT")
            self.assertTrue(cancelled.is_set())
            await self.assert_original_untouched()
            stored = await self.db.volume_writing_jobs.find_one({"_id": volume.ObjectId(started["id"])})
            self.assertNotIn("extraction", stored["pending"]["fact_closure"])
            await volume.control(self.novel, self.volume, started["id"], "resume")
            completed = await self.wait_volume("completed", "failed")
        self.assertEqual(completed["status"], "completed", completed.get("error"))
        self.assertEqual(await self.db.chapter_fact_batches.count_documents({"chapter_id": self.original["_id"]}), 1)
        await self.assert_original_untouched()

    async def test_pause_hanging_fact_review_keeps_extraction_and_resumes_review(self):
        from backend.services.novel import model_wait
        delegated = chapter_facts.structured_advice
        entered = asyncio.Event()
        cancelled = asyncio.Event()
        calls = []

        async def advice(settings, instructions, material, schema, **kwargs):
            identity = material["chapter_revision"]["chapter_id"]
            calls.append((identity, schema))
            if schema is ChapterFactReviewV1 and not entered.is_set():
                entered.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()
            return await delegated(settings, instructions, material, schema, **kwargs)

        with patch.object(chapter_facts, "structured_advice", advice), \
             patch.object(model_wait, "STOP_GRACE_SECONDS", .03), \
             patch.object(model_wait, "POLL_SECONDS", .01):
            started = await self.begin("pause-hanging-fact-review")
            await asyncio.wait_for(entered.wait(), 60)
            await volume.control(self.novel, self.volume, started["id"], "pause")
            paused = await self.wait_volume("paused", "failed")
            self.assertEqual(paused["status"], "paused", paused.get("error"))
            self.assertIsNone(paused["error"])
            self.assertTrue(cancelled.is_set())
            stored = await self.db.volume_writing_jobs.find_one({"_id": volume.ObjectId(started["id"])})
            extraction = stored["pending"]["fact_closure"]["extraction"]
            self.assertTrue(extraction["facts"])
            self.assertEqual(stored["pending"]["fact_closure"]["reviews"], [])
            await self.assert_original_untouched()
            await volume.control(self.novel, self.volume, started["id"], "resume")
            completed = await self.wait_volume("completed", "failed")
        self.assertEqual(completed["status"], "completed", completed.get("error"))
        original_id = str(self.original["_id"])
        self.assertEqual(calls.count((original_id, ChapterFactExtractionV1)), 1)
        self.assertEqual(calls.count((original_id, ChapterFactReviewV1)), 2)
        self.assertEqual(await self.db.chapter_fact_batches.count_documents({"chapter_id": self.original["_id"]}), 1)
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

    def fact_index_model(self, bad_reviews):
        from copy import deepcopy
        delegated = chapter_facts.structured_advice
        counts = {"extract": 0, "review": 0}
        async def advice(settings, instructions, material, schema, **kwargs):
            if material["chapter_revision"]["chapter_id"] != str(self.original["_id"]):
                return await delegated(settings, instructions, material, schema, **kwargs)
            if schema is ChapterFactExtractionV1:
                counts["extract"] += 1
                extracted, usage = await delegated(settings, instructions, material, schema, **kwargs)
                result = extracted.model_dump(mode="json")
                second = deepcopy(result["facts"][0])
                second.update(field="other_state", after="另一项已保存事实")
                result["facts"].append(second)
                return schema.model_validate(result), usage
            counts["review"] += 1
            self.assertEqual(material["review_contract"]["expected_indices"], [0, 1])
            if counts["review"] > 1 and counts["review"] <= bad_reviews + 1:
                self.assertEqual(material["previous_review_error"]["code"], "CHAPTER_FACT_REVIEW_INVALID")
            indices = [1, 2] if counts["review"] <= bad_reviews else [0, 1]
            return schema.model_validate({"summary": "检查两项事实",
                "decisions": [{"index": index, "decision": "accept", "message": "原文支持"} for index in indices]}), {"model": "test-index-review"}
        return advice, counts

    async def test_bad_fact_review_indices_retry_twice_without_reextracting_or_rewriting(self):
        import json
        advice, counts = self.fact_index_model(2)
        with patch.object(chapter_facts, "structured_advice", advice):
            await self.begin("fact-indices-auto-retry")
            completed = await self.wait_volume("completed", "failed")
        self.assertEqual(completed["status"], "completed", completed.get("error"))
        self.assertEqual(counts, {"extract": 1, "review": 3})
        batch = await self.db.chapter_fact_batches.find_one({"chapter_id": self.original["_id"]})
        self.assertEqual(len(batch["facts"]), 2)
        logs = await self.db.volume_writing_logs.find({"label": "重新审核章后事实"}).to_list(None)
        self.assertEqual(len(logs), 2)
        self.assertTrue(all(json.loads(row["output"])["validation_error"]["expected_indices"] == [0, 1] for row in logs))
        await self.assert_original_untouched()

    async def test_persistent_bad_fact_indices_stop_then_resume_only_review(self):
        advice, counts = self.fact_index_model(3)
        with patch.object(chapter_facts, "structured_advice", advice):
            started = await self.begin("fact-indices-retry-limit")
            failed = await self.wait_volume("failed", "completed")
            self.assertEqual(failed["error"]["code"], "CHAPTER_FACT_REVIEW_INVALID")
            self.assertNotIn("pydantic", failed["error"]["message"])
            self.assertEqual(counts, {"extract": 1, "review": 3})
            stored = await self.db.volume_writing_jobs.find_one({"_id": volume.ObjectId(started["id"])})
            state = stored["pending"]["fact_closure"]
            self.assertEqual(len(state["extraction"]["facts"]), 2)
            self.assertEqual(len(state["review_attempts"]), 2)
            self.assertEqual(state["reviews"], [])
            self.assertEqual(await self.db.chapter_fact_batches.count_documents({}), 0)
            await self.assert_original_untouched()
            await volume.control(self.novel, self.volume, started["id"], "resume")
            completed = await self.wait_volume("completed", "failed")
        self.assertEqual(completed["status"], "completed", completed.get("error"))
        self.assertEqual(counts, {"extract": 1, "review": 4})
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

    async def test_unique_quote_offset_checkpoint_resumes_with_exhausted_repairs_without_reextracting(self):
        delegated = chapter_facts.structured_advice
        extracts = 0
        async def wrong_offset(settings, instructions, material, schema, **kwargs):
            nonlocal extracts
            result, usage = await delegated(settings, instructions, material, schema, **kwargs)
            if schema is ChapterFactExtractionV1 and material["chapter_revision"]["chapter_id"] == str(self.original["_id"]):
                extracts += 1
                data = result.model_dump(mode="json")
                data["facts"][0]["evidence"] = {"start": 1, "quote": material["chapter_revision"]["content"]}
                await self.db.volume_writing_jobs.update_one({"novel_id": volume.ObjectId(self.novel), "status": "running"},
                    {"$set": {"pause_requested": True}})
                return schema.model_validate(data), usage
            return result, usage
        with patch.object(chapter_facts, "structured_advice", wrong_offset):
            started = await self.begin("unique-evidence-offset")
            paused = await self.wait_volume("paused", "failed")
            self.assertEqual(paused["status"], "paused", paused.get("error"))
            stored = await self.db.volume_writing_jobs.find_one({"_id": volume.ObjectId(started["id"])})
            state = stored["pending"]["fact_closure"]
            self.assertEqual(state["extraction"]["facts"][0]["evidence"]["start"], 0)
            self.assertEqual(state["evidence_alignments"][0]["original_extraction"]["facts"][0]["evidence"]["start"], 1)
            self.assertEqual(state["reviews"], [])
            # Emulate the two historical model repairs in the user's cached task.
            attempts = [{"validation_error": {"code": "CHAPTER_FACT_EVIDENCE_MISMATCH"}} for _ in range(2)]
            await self.db.volume_writing_jobs.update_one({"_id": volume.ObjectId(started["id"])},
                {"$set": {"pending.fact_closure.attempts": attempts}})
            await volume.control(self.novel, self.volume, started["id"], "resume")
            completed = await self.wait_volume("completed", "failed")
        self.assertEqual(completed["status"], "completed", completed.get("error"))
        self.assertEqual(extracts, 1)
        batch = await self.db.chapter_fact_batches.find_one({"chapter_id": self.original["_id"]})
        self.assertEqual(batch["facts"][0]["evidence"]["start"], 0)
        self.assertEqual(batch["attempts"], attempts)
        self.assertEqual(len(batch["evidence_alignments"]), 1)
        self.assertEqual(len(batch["reviews"]), 1)
        await self.assert_original_untouched()

    async def test_consistency_conflict_never_adopts_the_candidate(self):
        from copy import deepcopy
        repair_calls = 0
        async def conflict(*args):
            return {"summary": "确认冲突", "issues": [{"message": "身份冲突", "candidate_quote": "已授官", "source_path": "context.identity", "source_quote": "尚未授官"}]}
        async def repair(task, result, report):
            nonlocal repair_calls
            repair_calls += 1
            return deepcopy(result["candidate"]), {}
        with patch.object(volume, "review_candidate", conflict), patch.object(volume,"repair_plan_candidate",repair):
            await self.begin()
            failed = await self.wait_volume("failed", "completed")
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["error"]["code"], "VOLUME_WRITING_CONSISTENCY_CONFLICT")
        self.assertEqual(repair_calls,2)
        self.assertTrue(failed["has_saved_candidate"])
        self.assertEqual(await self.db.chapters.count_documents({}), 1)
        audit_log = await self.db.volume_writing_logs.find_one({"label": "核对剧情与事实一致性", "state": "failed"})
        self.assertEqual(audit_log["stage"], "reviewing")
        await self.assert_original_untouched()

    async def test_body_repairs_twice_then_independently_reviews_and_saves_only_final_text(self):
        from copy import deepcopy
        delegated_review = volume.review_candidate
        reviews, repairs = [], []
        async def review(task, result, settings):
            if task != "write_chapter_by_ai":
                return await delegated_review(task, result, settings)
            reviews.append(deepcopy(result))
            return {"summary": "已核对前文", "issues": [] if len(reviews) == 3 else [{
                "message": "空间连续性冲突", "candidate_quote": result["candidate"]["content"][:6],
                "source_path": "context.previous_text", "source_quote": "已经离开",
            }]}
        async def repair(result, report):
            repairs.append(deepcopy(result))
            return {"content": f"第{len(repairs)}次修订：三人已离开凹室，在黑石格旁整理证据。"}, {"model": "test-repair"}
        with patch.object(volume, "review_candidate", review), patch.object(volume, "repair_chapter_candidate", repair):
            await self.begin("repair-body-success")
            completed = await self.wait_volume("completed", "failed")
        self.assertEqual(completed["status"], "completed", completed.get("error"))
        self.assertEqual((len(repairs), len(reviews)), (2, 3))
        target = await self.db.chapters.find_one({"_id": {"$ne": self.original["_id"]}})
        self.assertEqual(target["content"], reviews[-1]["candidate"]["content"])
        self.assertTrue(target["content"].startswith("第2次修订"))
        self.assertTrue(all(row["basis"] == reviews[0]["basis"] for row in reviews + repairs))
        report = await self.db.volume_writing_reports.find_one({"chapter_id": target["_id"]})
        self.assertEqual(len(report["consistency_repairs"]), 2)
        self.assertEqual(report["consistency_repairs"][0]["before_candidate"], reviews[0]["candidate"])
        self.assertEqual(report["consistency_repairs"][-1]["after_review"]["issues"], [])
        self.assertEqual(await self.db.chapter_revisions.count_documents({"chapter_id": target["_id"]}), 2)
        self.assertIsNotNone(await self.db.chapter_fact_batches.find_one({"chapter_id": target["_id"]}))
        for index in (1, 2):
            self.assertIsNotNone(await self.db.volume_writing_logs.find_one({
                "instance_path": f"_consistency_repair_{index}", "state": "done"}))
        await self.assert_original_untouched()

    async def test_legacy_cached_body_conflict_repairs_at_most_twice_across_resumes(self):
        delegated_review = volume.review_candidate
        repairs = []
        async def conflict(task, result, settings):
            if task != "write_chapter_by_ai":
                return await delegated_review(task, result, settings)
            return {"summary": "仍有冲突", "issues": [{"message": "空间连续性冲突",
                "candidate_quote": result["candidate"]["content"][:6], "source_path": "context.previous_text", "source_quote": "已经离开"}]}
        async def unavailable(*args):
            raise DomainApiError("TEST_REPAIR_NETWORK", "修订网络中断", status_code=503)
        async def repair(result, report):
            repairs.append(result["candidate"]["content"])
            return {"content": f"修订{len(repairs)}：三人在黑石格旁整理证据。"}, {}
        with patch.object(volume, "review_candidate", conflict), patch.object(volume, "repair_chapter_candidate", unavailable):
            started = await self.begin("repair-legacy-cap")
            failed = await self.wait_volume("failed", "completed")
        self.assertEqual(failed["error"]["code"], "TEST_REPAIR_NETWORK")
        # Simulate the persisted shape of an older job: candidate + failed review, no repair history.
        from bson import ObjectId
        await self.db.volume_writing_jobs.update_one({"_id": ObjectId(started["id"])}, {"$unset": {"pending.consistency_repairs": ""}})
        calls = list(self.calls)
        with patch.object(volume, "review_candidate", conflict), patch.object(volume, "repair_chapter_candidate", repair):
            await volume.control(self.novel, self.volume, started["id"], "resume")
            failed = await self.wait_volume("failed", "completed")
            self.assertEqual(failed["error"]["code"], "VOLUME_WRITING_CONSISTENCY_CONFLICT")
            self.assertIn("2次", failed["error"]["message"])
            await volume.control(self.novel, self.volume, started["id"], "resume")
            again = await self.wait_volume("failed", "completed")
        self.assertEqual(again["error"]["code"], "VOLUME_WRITING_CONSISTENCY_CONFLICT")
        self.assertEqual(len(repairs), 2)
        self.assertEqual(self.calls, calls, "cached failed body is repaired without regenerating its task graph")
        target = await self.db.chapters.find_one({"_id": {"$ne": self.original["_id"]}})
        self.assertEqual(target["content"], "")
        self.assertEqual(await self.db.volume_writing_reports.count_documents({}), 0)
        saved = await self.db.volume_writing_jobs.find_one({"_id": ObjectId(started["id"])})
        self.assertEqual(len(saved["pending"]["consistency_repairs"]), 2)
        self.assertTrue(all(row["after_review"]["issues"] for row in saved["pending"]["consistency_repairs"]))
        await self.assert_original_untouched()

    async def test_pause_after_repair_keeps_candidate_and_resume_only_rereviews_it(self):
        entered, release = asyncio.Event(), asyncio.Event()
        delegated_review = volume.review_candidate
        repair_calls, body_reviews = 0, 0
        async def review(task, result, settings):
            nonlocal body_reviews
            if task == "write_chapter_by_ai":
                body_reviews += 1
                if body_reviews == 1:
                    return {"summary": "冲突", "issues": [{"message": "空间倒退", "candidate_quote": "城门初开",
                        "source_path": "context.previous_text", "source_quote": "已经离开"}]}
            return await delegated_review(task, result, settings)
        async def repair(result, report):
            nonlocal repair_calls
            repair_calls += 1
            entered.set()
            await release.wait()
            return {"content": "三人在黑石格旁整理证据，没有返回凹室。"}, {}
        with patch.object(volume, "review_candidate", review), patch.object(volume, "repair_chapter_candidate", repair):
            started = await self.begin("repair-pause")
            try:
                await asyncio.wait_for(entered.wait(), 60)
                await volume.control(self.novel, self.volume, started["id"], "pause")
            finally:
                release.set()
            paused = await self.wait_volume("paused", "failed")
            self.assertEqual(paused["status"], "paused", paused.get("error"))
            from bson import ObjectId
            job = await self.db.volume_writing_jobs.find_one({"_id": ObjectId(started["id"])})
            self.assertIsNone(job["pending"]["review"])
            self.assertEqual(job["pending"]["result"]["candidate"]["content"], "三人在黑石格旁整理证据，没有返回凹室。")
            target = await self.db.chapters.find_one({"_id": {"$ne": self.original["_id"]}})
            self.assertEqual(target["content"], "")
            await volume.control(self.novel, self.volume, started["id"], "resume")
            completed = await self.wait_volume("completed", "failed")
        self.assertEqual(completed["status"], "completed", completed.get("error"))
        self.assertEqual((repair_calls, body_reviews), (1, 2))
        self.assertEqual((await self.db.chapters.find_one({"_id": target["_id"]}))["content"], "三人在黑石格旁整理证据，没有返回凹室。")
        await self.assert_original_untouched()

    async def test_repair_timeout_reuses_slot_and_resumes_without_regenerating_body(self):
        delegated_review, delegated_call = volume.review_candidate, volume.model_call
        repair_calls, body_reviews = 0, 0
        cancelled = asyncio.Event()
        async def review(task, result, settings):
            nonlocal body_reviews
            if task == "write_chapter_by_ai":
                body_reviews += 1
                if body_reviews == 1:
                    return {"summary": "冲突", "issues": [{"message": "空间倒退", "candidate_quote": "城门初开",
                        "source_path": "context.previous_text", "source_quote": "已经离开"}]}
            return await delegated_review(task, result, settings)
        async def repair(result, report):
            nonlocal repair_calls
            repair_calls += 1
            if repair_calls == 1:
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()
            return {"content": "超时后修订成功：三人在黑石格旁整理证据。"}, {}
        async def short_call(*args, **kwargs):
            if kwargs.get("path", "").startswith("_consistency_repair_"):
                kwargs["total_seconds"] = .05
            return await delegated_call(*args, **kwargs)
        with patch.object(volume, "review_candidate", review), patch.object(volume, "repair_chapter_candidate", repair), patch.object(volume, "model_call", short_call):
            started = await self.begin("repair-timeout")
            failed = await self.wait_volume("failed", "completed")
            self.assertEqual(failed["error"]["code"], "VOLUME_WRITING_MODEL_TIMEOUT")
            self.assertTrue(cancelled.is_set())
            job = await self.db.volume_writing_jobs.find_one({"_id": volume.ObjectId(started["id"])})
            self.assertEqual(len(job["pending"]["consistency_repairs"]), 1)
            self.assertNotIn("after_candidate", job["pending"]["consistency_repairs"][0])
            target = await self.db.chapters.find_one({"_id": {"$ne": self.original["_id"]}})
            self.assertEqual(target["content"], "")
            calls = list(self.calls)
            await volume.control(self.novel, self.volume, started["id"], "resume")
            completed = await self.wait_volume("completed", "failed")
        self.assertEqual(completed["status"], "completed", completed.get("error"))
        self.assertEqual(self.calls, calls)
        self.assertEqual((repair_calls, body_reviews), (2, 2))
        report = await self.db.volume_writing_reports.find_one({"chapter_id": target["_id"]})
        self.assertEqual(len(report["consistency_repairs"]), 1, "unfinished request does not consume another repair slot")
        await self.assert_original_untouched()

    async def test_author_body_edit_during_repair_is_never_overwritten(self):
        from backend.services.novel.chapter_service import ChapterService
        delegated_review = volume.review_candidate
        body_reviews = 0
        async def review(task, result, settings):
            nonlocal body_reviews
            if task == "write_chapter_by_ai":
                body_reviews += 1
                if body_reviews == 1:
                    return {"summary": "冲突", "issues": [{"message": "空间倒退", "candidate_quote": "城门初开",
                        "source_path": "context.previous_text", "source_quote": "已经离开"}]}
            return await delegated_review(task, result, settings)
        async def repair(result, report):
            target = await self.db.chapters.find_one({"_id": {"$ne": self.original["_id"]}})
            await ChapterService.update_chapter(str(target["_id"]), {"expected_version": target["version"], "content": "作者在修订过程中手动保存的新正文。"})
            return {"content": "自动修订后的候选。"}, {}
        with patch.object(volume, "review_candidate", review), patch.object(volume, "repair_chapter_candidate", repair):
            await self.begin("repair-author-edit")
            failed = await self.wait_volume("failed", "completed")
        self.assertEqual(failed["status"], "failed")
        target = await self.db.chapters.find_one({"_id": {"$ne": self.original["_id"]}})
        self.assertEqual(target["content"], "作者在修订过程中手动保存的新正文。")
        self.assertEqual(await self.db.volume_writing_reports.count_documents({}), 0)
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
