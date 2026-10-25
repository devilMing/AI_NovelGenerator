"""Missing-work selection and saved budget protection without model calls."""
from copy import deepcopy
import unittest
from unittest.mock import AsyncMock, patch

from bson import ObjectId
from backend.api.error_protocol import DomainApiError
from backend.services.novel import volume_writing_preparation as prepare
from backend.services.novel.volume_writing_contract import VolumeWritingStart
from backend.services.novel import volume_writing_service as volume
from pymongo.errors import OperationFailure


class VolumePreparationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.volume = {"_id": ObjectId(), "novel_id": ObjectId(), "title": "本卷", "plot": "卷详细剧情", "planning": {"target_chapter_count": 4, "target_word_count": 12000}}
        self.arc = {"_id": ObjectId(), "volume_id": self.volume["_id"], "title": "北上履约", "plot": "", "planning": {"target_chapter_count": 2}}
        self.scene = {"_id": ObjectId(), "arc_id": self.arc["_id"], "title": "抵达", "planning": {"target_chapter_count": 2}}
        self.data = {"volume": self.volume, "arcs": [self.arc], "scenes": [], "chapters": []}
        self.ready = {"plot_status": "ready", "preparation": {"factions": True, "characters": True, "locations": True}}

    def test_new_requests_complete_planning_old_jobs_keep_fixed_scope(self):
        self.assertTrue(VolumeWritingStart().complete_planning)
        self.assertFalse(prepare.complete_mode({"settings": {}}))
        self.assertFalse(prepare.complete_mode({"settings": {"complete_planning": False}}))
        self.assertTrue(prepare.complete_mode({"settings": {"complete_planning": True}}))

    def test_empty_auto_plan_stops_instead_of_repeating_forever(self):
        for task, field in (("plan_arcs_by_ai", "arcs"), ("plan_scenes_by_ai", "scenes")):
            with self.subTest(task=task), self.assertRaises(DomainApiError) as error:
                prepare.require_missing_plan(task, {"candidate": {field: []}})
            self.assertEqual(error.exception.code, "VOLUME_WRITING_EMPTY_PLAN")
        prepare.require_missing_plan("plan_scenes_by_ai", {"candidate": {"scenes": [{"title": "新场景"}]}})

    async def test_missing_volume_plot_precedes_other_preparation(self):
        self.volume["plot"] = " "
        self.assertEqual((await prepare.choose_preparation(self.data))[0], "generate_volume_plot_by_ai")

    async def test_empty_volume_gets_arcs_once_ready(self):
        self.data["arcs"] = []
        with patch.object(prepare, "get_writing_readiness", AsyncMock(return_value=self.ready)):
            self.assertEqual((await prepare.choose_preparation(self.data))[0], "plan_arcs_by_ai")

    async def test_arc_gets_plot_then_scenes_and_preserves_existing_scenes(self):
        self.assertEqual((await prepare.choose_preparation(self.data, self.arc))[0], "generate_arc_plot_by_ai")
        self.arc["plot"] = "已保存的篇章详细剧情"
        with patch.object(prepare, "get_writing_readiness", AsyncMock(return_value=self.ready)):
            self.assertEqual((await prepare.choose_preparation(self.data, self.arc))[0], "plan_scenes_by_ai")
            self.data["scenes"] = [self.scene]
            before = deepcopy(self.data)
            self.assertIsNone(await prepare.choose_preparation(self.data, self.arc))
            self.assertEqual(self.data, before)

    async def test_stale_existing_plot_stops_without_generating_replacement(self):
        self.arc["plot"] = "作者已写好的剧情"
        with patch.object(prepare, "get_writing_readiness", AsyncMock(return_value={**self.ready, "plot_status": "stale"})), self.assertRaises(DomainApiError) as error:
            await prepare.choose_preparation(self.data, self.arc)
        self.assertEqual(error.exception.code, "VOLUME_WRITING_PLOT_STALE")
        self.assertEqual(self.arc["plot"], "作者已写好的剧情")

    def test_existing_allocations_cannot_exceed_volume_or_arc_budget(self):
        self.data["arcs"] = [{**self.arc, "planning": {"target_chapter_count": 5}}]
        with self.assertRaises(DomainApiError):
            prepare.check_budgets(self.data)
        self.data["arcs"] = [self.arc]
        self.data["scenes"] = [{**self.scene, "planning": {"target_chapter_count": 3}}]
        with self.assertRaises(DomainApiError):
            prepare.check_budgets(self.data)

    def test_existing_chapters_are_counted_even_without_allocated_scene_budget(self):
        self.arc["planning"] = {}
        self.scene["planning"] = {}
        self.volume["planning"]["target_chapter_count"] = 2
        self.data["scenes"] = [self.scene]
        self.data["chapters"] = [{"_id": ObjectId(), "arc_id": self.arc["_id"], "scene_id": self.scene["_id"]} for _ in range(3)]
        with self.assertRaises(DomainApiError):
            prepare.check_budgets(self.data)

    def test_signature_tracks_structure_not_body_saves_or_stats(self):
        chapter = {"_id": ObjectId(), "scene_id": self.scene["_id"], "content": "旧正文", "planning": {"goal": "核对信件"}}
        def signature():
            return prepare.structure_signature(self.volume, [self.arc], [self.scene], [chapter])
        original = signature()
        chapter.update(content="新正文", word_count=3000, version=2)
        self.volume.update(word_count=3000, chapter_count=1, version=3)
        self.assertEqual(original, signature())
        self.arc["title"] = "作者手动改后的篇章名"
        self.assertNotEqual(original, signature())

    async def test_aborted_checkpoint_write_conflict_retries_same_callback(self):
        error = OperationFailure("aborted write conflict", code=112, details={"errorLabels": ["TransientTransactionError"]})
        save = AsyncMock()
        writer = AsyncMock(side_effect=[error, {"saved": True}])
        with patch.object(volume, "run_mongo_write_unit", writer):
            result = await volume.run_preparation_checkpoint(save)
        self.assertEqual(result, {"saved": True})
        self.assertEqual(writer.await_count, 2)
        self.assertTrue(all(call.args[0] is save for call in writer.call_args_list))

    async def test_checkpoint_never_retries_unknown_commit_or_unrelated_failure(self):
        for labels, code in ((["UnknownTransactionCommitResult", "TransientTransactionError"],112),
                             ([],112),(["TransientTransactionError"],121)):
            error = OperationFailure("uncertain or unrelated", code=code, details={"errorLabels":labels})
            writer = AsyncMock(side_effect=error)
            with patch.object(volume, "run_mongo_write_unit", writer), self.assertRaises(OperationFailure):
                await volume.run_preparation_checkpoint(AsyncMock())
            self.assertEqual(writer.await_count,1)

    async def test_checkpoint_uses_same_lock_as_heartbeat(self):
        import asyncio
        lock = asyncio.Lock()
        async def writer(*args, **kwargs):
            self.assertTrue(lock.locked())
        with patch.object(volume, "run_mongo_write_unit", writer):
            await volume.run_preparation_checkpoint(AsyncMock(), lock)
        self.assertFalse(lock.locked())
