"""First-chapter orchestration regression tests; no paid model calls."""
import unittest
from copy import deepcopy
from bson import ObjectId


class PlannerTests(unittest.TestCase):
    def setUp(self):
        from backend.services.novel.first_chapter_plan import choose_step
        self.choose = choose_step
        self.novel = {"_id": ObjectId(), "version": 1, "number_of_chapters": 600, "words_per_chapter": 3000}
        self.data = {"novel": self.novel, "factions": [], "characters": [], "relations": [],
                     "rules": [], "items": [], "volumes": [], "arcs": [], "scenes": [], "chapters": [], "ready": {}}

    def initialized(self):
        self.data.update(factions=[{"faction_id": "fac_a"}], characters=[{"character_id": "char_a", "role_type": "protagonist"}],
                         rules=[{}], items=[{}])

    def test_starts_with_missing_factions(self):
        self.assertEqual(self.choose(self.data, {})["task"], "create_factions_by_ai")

    def test_resource_counts_are_explicitly_ai_decided_not_inherited(self):
        self.assertIn("faction_count", self.choose(self.data, {})["inputs"])
        self.assertIsNone(self.choose(self.data, {})["inputs"]["faction_count"])
        self.data["factions"] = [{"faction_id": "fac_a"}]
        self.assertIsNone(self.choose(self.data, {})["inputs"]["character_count"])
        self.data["characters"] = [{"character_id": "char_a", "role_type": "protagonist"}]
        self.assertIsNone(self.choose(self.data, {})["inputs"]["core_rule_count"])
        self.data["rules"] = [{}]
        self.assertIsNone(self.choose(self.data, {})["inputs"]["item_count"])

    def test_builtin_resource_forms_default_to_ai_counts(self):
        from backend.services.task_flows.defaults import builtin_flow
        for task, field in (("create_factions_by_ai", "faction_count"),
                            ("create_characters_by_ai", "character_count"),
                            ("create_world_rules_by_ai", "core_rule_count"),
                            ("create_items_by_ai", "item_count")):
            flow = builtin_flow(task)
            start = next(n for n in flow.nodes if n.id == "start")
            self.assertIsNone(start.outputs[field]["default"], task)

    def test_existing_first_body_is_terminal_without_generating_optional_material(self):
        volume, arc, scene = ObjectId(), ObjectId(), ObjectId()
        self.data.update(volumes=[{"_id": volume}], arcs=[{"_id": arc, "volume_id": volume}], scenes=[{"_id": scene, "arc_id": arc}])
        self.data["chapters"] = [{"_id": ObjectId(), "scene_id": scene, "content": "已保存正文"}]
        self.assertIsNone(self.choose(self.data, {}))

    def test_later_volume_body_does_not_finish_empty_first_volume(self):
        self.initialized()
        first, second = ObjectId(), ObjectId()
        self.data.update(volumes=[{"_id": first, "plot": "", "version": 1}, {"_id": second}], chapters=[{"content": "后卷正文", "scene_id": ObjectId()}])
        self.assertEqual(self.choose(self.data, {})["task"], "generate_volume_plot_by_ai")

    def test_reuses_existing_factions_and_requests_missing_protagonist(self):
        self.data["factions"] = [{"faction_id": "fac_a"}]
        self.assertEqual(self.choose(self.data, {})["task"], "create_characters_by_ai")

    def test_volume_budget_comes_from_novel_not_flow_defaults(self):
        self.initialized()
        step = self.choose(self.data, {})
        self.assertEqual(step["task"], "create_volume_skeleton_by_ai")
        self.assertEqual(step["inputs"]["total_chapter_budget"], 600)
        self.assertEqual(step["inputs"]["total_word_budget"], 1800000)

    def test_stale_existing_plot_is_blocked_not_overwritten(self):
        from backend.api.error_protocol import DomainApiError
        self.initialized()
        v = {"_id": ObjectId(), "title": "第一卷", "plot": "已有剧情", "version": 1}
        self.data.update(volumes=[v], ready={str(v["_id"]): {"plot_status": "stale"}})
        with self.assertRaises(DomainApiError) as error:
            self.choose(self.data, {})
        self.assertEqual(error.exception.code, "FIRST_CHAPTER_EXISTING_CONFLICT")
        self.assertEqual(v["plot"], "已有剧情")

    def test_preparation_order_and_existing_members_not_regenerated(self):
        self.initialized()
        v = {"_id": ObjectId(), "title": "卷", "plot": "剧情", "version": 2}
        ready = {"plot_status": "ready", "preparation": {"factions": True, "characters": False, "locations": False}}
        self.data.update(volumes=[v], ready={str(v["_id"]): ready})
        step = self.choose(self.data, {})
        self.assertEqual(step["task"], "create_characters_for_volume_by_ai")
        self.assertEqual(step["inputs"]["new_count"], 0)


class ErrorMappingTests(unittest.TestCase):
    def test_interrupted_stream_is_not_reported_as_invalid_content(self):
        from backend.api.error_protocol import exception_to_error
        from backend.llm.exceptions import LLMConnectionError
        result = exception_to_error(LLMConnectionError("private-credential-and-network-detail"))
        self.assertEqual(result[1], "LLM_CONNECTION_INTERRUPTED")
        self.assertNotIn("private-credential", str(result))


class ReviewTests(unittest.TestCase):
    def test_bracket_array_paths_still_verify_actual_exact_evidence(self):
        from backend.services.novel.first_chapter_review import Review, verify_review
        report = Review(summary="冲突", issues=[{"message":"状态冲突", "candidate_quote":"确定实力一级",
            "source_path":"context.profiles[0].facts.unknown.power_level", "source_quote":"unclaimed"}])
        self.assertEqual(verify_review(report, {"text":"确定实力一级"}, {"context":{"profiles":[{"facts":{"unknown":{"power_level":"unclaimed"}}}]}})["issues"][0]["source_quote"], "unclaimed")

    def test_invented_conflict_evidence_is_rejected(self):
        from backend.services.novel.first_chapter_review import Review, verify_review
        from backend.api.error_protocol import DomainApiError
        report = Review(summary="冲突", issues=[{"message": "身份矛盾", "candidate_quote": "不存在的原句",
                                                "source_path": "context.identity", "source_quote": "将军"}])
        with self.assertRaises(DomainApiError):
            verify_review(report, {"content": "主角进城"}, {"context": {"identity": "将军"}})

    def test_real_conflict_preserves_exact_sources(self):
        from backend.services.novel.first_chapter_review import Review, verify_review
        report = Review(summary="冲突", issues=[{"message": "身份矛盾", "candidate_quote": "农夫",
                                                "source_path": "context.identity", "source_quote": "将军"}])
        self.assertEqual(verify_review(report, {"content": "他是农夫"}, {"context": {"identity": "将军"}}), report.model_dump())

    def test_no_issues_is_not_a_guarantee_of_all_book_consistency(self):
        from backend.services.novel.first_chapter_review import Review, verify_review
        report = Review(summary="已核对当前提供的材料", issues=[])
        self.assertEqual(verify_review(report, {"content": "开篇"}, {})["issues"], [])


class ReviewTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_faction_review_sees_only_the_fields_that_will_be_adopted(self):
        from copy import deepcopy
        from unittest.mock import patch
        from backend.services.novel.first_chapter_review import Review, review_candidate
        original = {"candidate":{"factions":[{"faction_id":"fac_000004", "profile":None, "state":{
            "participation_scope":"primary", "current_goal":"核账", "start_state":"实力未知，旧格式0为占位", "power_level":1,
            "resources":[], "public_stance":"旧值", "active_status":"active"}},
            {"faction_id":None,"profile":{"name":"户部"},"state":{"participation_scope":"primary","current_goal":"核验",
             "start_state":"未量化", "power_level":1,"resources":[]}}]},"context":{"scope":"首卷参与计划"}}
        saved = deepcopy(original)
        async def advice(settings, instructions, material, schema):
            for row in material["candidate"]["factions"]:
                self.assertNotIn("power_level",row["state"])
                self.assertNotIn("resources",row["state"])
                self.assertNotIn("initial_facts",row)
            return Review(summary="只核对实际采用的计划字段",issues=[]),{}
        with patch("backend.services.novel.first_chapter_review.structured_advice",advice):
            await review_candidate("create_factions_for_volume_by_ai",original,{})
        self.assertEqual(original,saved)

    async def test_volume_review_cannot_treat_book_start_as_unlocated_design(self):
        from unittest.mock import patch
        from backend.services.novel.first_chapter_review import Review, review_candidate
        async def advice(settings, instructions, material, schema):
            self.assertIn("adoption_contract", material["material"])
            self.assertEqual(material["material"]["adoption_contract"]["position"], "book_start")
            self.assertIn("首卷", material["material"]["adoption_contract"]["constraint"])
            return Review(summary="检查首卷与书初事实", issues=[]), {}
        with patch("backend.services.novel.first_chapter_review.structured_advice", advice):
            await review_candidate("create_volume_skeleton_by_ai", {"candidate": {}, "context": {
                "book_start_character_facts": [{"values": {"identity": "隐姓十年"}}]}}, {})

    async def test_volume_outline_review_reads_confirmed_book_start_facts(self):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, patch
        from backend.services.novel.first_chapter_service import attach_review_sources
        nid = str(ObjectId())
        facts = AsyncMock(return_value={"characters": [{"character_id": "char_000004", "values": {"identity": "隐姓十年"}}]})
        envelope = {"candidate": {}, "basis": {"source_refs": [{"resource_type": "character", "id": "char_000004", "version": 1}],
            "flow_snapshot": {"business_input": {}, "scope": {"novel_id": nid}}}}
        context = AsyncMock(return_value=SimpleNamespace(ports={"novel_profile": "全书设计"}))
        with patch("backend.services.task_flows.domain.load_task_context", context), patch("backend.services.novel.character_fact_service.read_character_facts", facts):
            result = await attach_review_sources("create_volume_skeleton_by_ai", envelope, {})
        self.assertIn("book_start_character_facts", result["context"])
        self.assertEqual(result["context"]["book_start_character_facts"][0]["values"]["identity"], "隐姓十年")
        self.assertEqual(facts.await_args.args[2]["reading_intent"], "book_start")

    async def test_item_review_is_bound_to_actual_book_start_fact_adoption(self):
        from unittest.mock import patch
        from backend.services.novel.first_chapter_review import Review, review_candidate
        async def advice(settings, instructions, material, schema):
            self.assertIn("adoption_contract", material["material"])
            self.assertIn("不得跨字段拼接", instructions)
            contract = material["material"]["adoption_contract"]
            self.assertEqual(contract["position"], "book_start")
            self.assertIn("lifecycle_status", contract["fact_fields"])
            return Review(summary="后期才编制的物品不能确认在书初已存在", issues=[{
                "message": "候选明确尚未编制，不能保存为书初可用物品",
                "candidate_quote": "属于后续行动工具，不预设已经编成",
                "source_path": "adoption_contract.constraint",
                "source_quote": contract["constraint"],
            }]), {}
        with patch("backend.services.novel.first_chapter_review.structured_advice", advice):
            result = await review_candidate("create_items_by_ai", {
                "candidate": {"items": [{"description": "属于后续行动工具，不预设已经编成", "lifecycle_status": "available"}]},
                "context": {"plot": "查案后期才整理证物交接册"}}, {})
        self.assertEqual(len(result["issues"]), 1)

    async def test_existing_bson_state_can_be_reviewed_without_overwriting(self):
        import json
        from unittest.mock import patch
        from backend.services.novel.first_chapter_review import Review, review_candidate
        async def advice(settings, instructions, material, schema):
            json.dumps(material, ensure_ascii=False)
            return Review(summary="复用已有资料", issues=[]), {}
        with patch("backend.services.novel.first_chapter_review.structured_advice", advice):
            result = await review_candidate("confirm_existing_preparation", {"candidate": {"id": ObjectId(), "name": "旧人物"}, "context": {"state_id": ObjectId(), "name": "旧人物"}}, {})
        self.assertEqual(result["issues"], [])


if __name__ == "__main__":
    unittest.main()
