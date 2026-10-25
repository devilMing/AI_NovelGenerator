"""Evidence and output boundaries for automatic chapter consistency repair."""
from copy import deepcopy
import unittest
from unittest.mock import AsyncMock, patch

from pydantic import ValidationError

from backend.api.error_protocol import DomainApiError
from backend.llm.schemas.writing_pydantic import ChapterContentResultV1
from backend.llm.schemas.writing_pydantic import WRITING_RESULTS
from backend.services.novel import volume_consistency_repair as repair


class ChapterRepairTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.envelope = {
            "candidate": {"content": "三人仍在原来的凹室，取走散页。"},
            "context": {"planning": "不得为补证折返。"},
            "prepared_context": {"previous_text": [{"excerpt": "三人离开凹室，来到黑石格旁。"}]},
            "basis": {"input_fingerprint": "original-source-basis"},
        }
        self.report = {"summary": "空间连续性冲突", "issues": [{
            "message": "前文已经离开凹室，本章没有依据返回。",
            "candidate_quote": "三人仍在原来的凹室", "source_path": "prepared_context.previous_text[0].excerpt",
            "source_quote": "三人离开凹室，来到黑石格旁。",
        }]}

    async def test_repairs_only_text_with_actual_sources_and_preserves_envelope(self):
        before = deepcopy(self.envelope)
        answer = ChapterContentResultV1(content="三人在黑石格旁整理已经拓录的纸页。")
        model = AsyncMock(return_value=(answer, {"model": "test", "usage": {"total_tokens": 32}}))
        with patch.object(repair, "structured_advice", model):
            candidate, metadata = await repair.repair_chapter_candidate(self.envelope, self.report)
        self.assertEqual(candidate, answer.model_dump())
        self.assertEqual(metadata["usage"]["total_tokens"], 32)
        self.assertEqual(self.envelope, before)
        args = model.call_args.args
        self.assertIs(args[3], ChapterContentResultV1)
        self.assertEqual(args[2]["material"]["prepared_context"], before["prepared_context"])
        self.assertEqual(args[2]["review"], self.report)
        self.assertNotIn("basis", args[2]["material"])

    async def test_rejects_unverifiable_cached_review_before_model_request(self):
        for field, invalid in (("source_quote", "不存在的来源原句"), ("candidate_quote", "不存在的候选原句"),
                               ("source_path", "prepared_context.previous_text[99].excerpt")):
            with self.subTest(field=field):
                report = deepcopy(self.report)
                report["issues"][0][field] = invalid
                model = AsyncMock()
                with patch.object(repair, "structured_advice", model), self.assertRaises(DomainApiError) as failure:
                    await repair.repair_chapter_candidate(self.envelope, report)
                self.assertEqual(failure.exception.code, "FIRST_CHAPTER_REVIEW_EVIDENCE")
                model.assert_not_awaited()

    async def test_no_conflict_does_not_send_repair_request(self):
        model = AsyncMock()
        with patch.object(repair, "structured_advice", model), self.assertRaises(DomainApiError) as failure:
            await repair.repair_chapter_candidate(self.envelope, {"summary": "核对通过", "issues": []})
        self.assertEqual(failure.exception.code, "VOLUME_WRITING_REPAIR_NOT_NEEDED")
        model.assert_not_awaited()

    async def test_oversized_sources_are_retained_without_sending(self):
        self.envelope["context"]["too_large"] = "字" * 220001
        model = AsyncMock()
        with patch.object(repair, "structured_advice", model), self.assertRaises(DomainApiError) as failure:
            await repair.repair_chapter_candidate(self.envelope, self.report)
        self.assertEqual(failure.exception.code, "VOLUME_WRITING_REPAIR_TOO_LARGE")
        model.assert_not_awaited()

    def test_output_contract_forbids_empty_body_and_source_changes(self):
        for value in ({"content": " \n "}, {"content": "修订正文", "basis": {}}, {"content": "修订正文", "title": "改章名"}):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                ChapterContentResultV1.model_validate(value)


class PlanningRepairTests(unittest.IsolatedAsyncioTestCase):
    def test_only_empty_chapter_pov_can_be_filled_from_explicitly_loaded_characters(self):
        original = {"chapters": [{"title": "名字与账法", "summary": "沈照核对账页。",
                    "planning": {"pov_character_id": None, "target_word_count": 3000}}]}
        changed = deepcopy(original)
        changed["chapters"][0]["planning"]["pov_character_id"] = "char_000004"
        envelope = {"basis": {"source_refs": [{"resource_type": "character", "id": "char_000004"}]},
                    "context": {"selected_entities": [{"resource_type": "character", "id": identity}
                                for identity in ("char_000004", "char_000005")]}}
        allowed = repair.empty_plan_pov_ids(envelope)
        self.assertEqual(allowed, {"char_000004"})
        self.assertEqual(repair.empty_plan_pov_ids({}), set())
        result = repair.validate_repaired_plan("plan_chapters_by_ai", original, changed, empty_pov_ids=allowed)
        self.assertEqual(result["chapters"][0]["planning"]["pov_character_id"], "char_000004")
        for bad in ("char_000005", "char_999999"):
            changed["chapters"][0]["planning"]["pov_character_id"] = bad
            with self.assertRaises(DomainApiError):
                repair.validate_repaired_plan("plan_chapters_by_ai", original, changed, empty_pov_ids=allowed)

    def test_pov_fill_never_allows_switching_existing_pov_or_changing_other_protected_fields(self):
        original = {"chapters": [{"title": "原章名", "planning": {"pov_character_id": None, "target_word_count": 3000}}]}
        for field, value in (("title", "改名"), ("target_word_count", 2000), ("existing_pov", "char_000005")):
            before, after = deepcopy(original), deepcopy(original)
            after["chapters"][0]["planning"]["pov_character_id"] = "char_000004"
            if field == "title": after["chapters"][0][field] = value
            elif field == "existing_pov": before["chapters"][0]["planning"]["pov_character_id"] = value
            else: after["chapters"][0]["planning"][field] = value
            with self.subTest(field=field), self.assertRaises(DomainApiError):
                repair.validate_repaired_plan("plan_chapters_by_ai", before, after,
                    empty_pov_ids={"char_000004", "char_000005"})

    async def test_chapter_repair_receives_pov_allowlist_and_preserves_original_source_basis(self):
        task = "plan_chapters_by_ai"
        envelope = {"candidate": {"chapters": [{"title": "名字与账法", "summary": "沈照核对账页。",
                    "planning": {"pov_character_id": None, "target_word_count": 3000}}]},
                    "context": {"plot": "采用沈照有限视角", "selected_entities": [{"resource_type": "character", "id": "char_000004"}]},
                    "basis": {"source_refs": [{"resource_type": "character", "id": "char_000004"}]}}
        before = deepcopy(envelope)
        report = {"summary": "视角缺项", "issues": [{"message": "补齐沈照视角字段", "candidate_quote": "沈照核对账页。",
                  "source_path": "context.plot", "source_quote": "采用沈照有限视角"}]}
        candidate = deepcopy(envelope["candidate"])
        candidate["chapters"][0]["planning"]["pov_character_id"] = "char_000004"
        model = AsyncMock(return_value=(WRITING_RESULTS[task].model_validate(candidate), {}))
        with patch.object(repair, "structured_advice", model):
            result, _ = await repair.repair_plan_candidate(task, envelope, report)
        self.assertEqual(result["chapters"][0]["planning"]["pov_character_id"], "char_000004")
        self.assertEqual(model.call_args.args[2]["repair_contract"]["fillable_pov_ids"], ["char_000004"])
        self.assertEqual(envelope, before)

    def setUp(self):
        self.task = "plan_scenes_by_ai"
        self.candidate = {"scenes": [{"title":"卸仓核算", "summary":"沈照在包销退出与自运之间选择。",
            "planning":{"goal":"核算人手与卸仓费用", "target_chapter_count":2,"target_word_count":6000}}]}
        self.envelope = {"candidate":self.candidate,"basis":{"input_fingerprint":"fixed"},
            "context":{"plot":"卸仓计费、人手核算之后，杜承裕才提出包销。"}}
        self.review = {"summary":"包销选项提前", "issues":[{"message":"包销在后续才提出", "candidate_quote":"包销退出",
            "source_path":"context.plot", "source_quote":"人手核算之后，杜承裕才提出包销。"}]}

    async def test_scene_repair_receives_exact_evidence_and_preserves_scope_and_basis(self):
        before = deepcopy(self.envelope)
        repaired = deepcopy(self.candidate)
        repaired["scenes"][0]["summary"] = "沈照先核算卸仓计费和人手，不提前讨论包销。"
        model = AsyncMock(return_value=(WRITING_RESULTS[self.task].model_validate(repaired), {"model":"test"}))
        with patch.object(repair,"structured_advice",model):
            result, metadata = await repair.repair_plan_candidate(self.task,self.envelope,self.review)
        self.assertEqual(result["scenes"][0]["summary"],repaired["scenes"][0]["summary"])
        self.assertEqual(self.envelope,before)
        self.assertEqual(model.call_args.args[2]["review"],self.review)
        self.assertEqual(model.call_args.args[2]["material"],{"context":self.envelope["context"]})

    def test_repair_cannot_remove_reorder_retitle_or_reallocate_candidates(self):
        candidate=deepcopy(self.candidate)
        candidate["scenes"].append({**deepcopy(candidate["scenes"][0]),"title":"正式查验"})
        for edit in ("remove","reorder","retitle","chapters","words","references"):
            changed=deepcopy(candidate)
            if edit=="remove": changed["scenes"].pop()
            elif edit=="reorder": changed["scenes"].reverse()
            elif edit=="retitle": changed["scenes"][0]["title"]="删除冲突"
            elif edit=="chapters": changed["scenes"][0]["planning"]["target_chapter_count"]=1
            elif edit=="words": changed["scenes"][0]["planning"]["target_word_count"]=3000
            else: changed["scenes"][0]["planning"]["pov_character_id"]="character_001"
            with self.subTest(edit=edit), self.assertRaises((DomainApiError,ValidationError)):
                repair.validate_repaired_plan(self.task,candidate,changed)

    def test_arc_and_chapter_plans_use_their_original_typed_contract(self):
        for task,field in repair.PLAN_REPAIR_FIELDS.items():
            candidate={field:[{"title":"原计划","summary":"原摘要","planning":{"goal":"核算费用","target_word_count":3000}}]}
            revised=deepcopy(candidate)
            revised[field][0]["planning"]["end_state"]="先核算费用，再等待报价"
            with self.subTest(task=task):
                result=repair.validate_repaired_plan(task,candidate,revised)
                self.assertEqual(result[field][0]["planning"]["end_state"],revised[field][0]["planning"]["end_state"])

    async def test_invalid_cached_plan_evidence_never_sends_request(self):
        self.review["issues"][0]["source_quote"]="不存在的原句"
        model=AsyncMock()
        with patch.object(repair,"structured_advice",model), self.assertRaises(DomainApiError):
            await repair.repair_plan_candidate(self.task,self.envelope,self.review)
        model.assert_not_awaited()
