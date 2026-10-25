"""Automatic chapter-after fact closure contracts and helpers."""
import unittest
from copy import deepcopy
from unittest.mock import AsyncMock, patch

from pydantic import ValidationError

from backend.services.novel.chapter_fact_contract import (
    ChapterFactExtractionV1,
    ChapterFactReviewV1,
)
from backend.services.novel.chapter_fact_service import (
    merge_reviews,
    validate_extraction,
    current_facts_for_writing,
    public_current_facts,
    align_fact_evidence,
)
from backend.api.error_protocol import DomainApiError
from backend.services.novel import chapter_fact_service as service


PROPOSAL = {
    "subject_type": "character",
    "subject_id": "char_100001",
    "field": "psychological_state",
    "before": "戒备",
    "after": "决定继续调查三封粮信",
    "evidence": {"start": 2, "quote": "继续调查"},
    "risk": "normal",
}


class ChapterFactContractTests(unittest.TestCase):
    def test_extraction_rejects_duplicate_fact_keys(self):
        with self.assertRaises(ValidationError):
            ChapterFactExtractionV1.model_validate({
                "summary": "提取两次同一字段",
                "facts": [PROPOSAL, {**PROPOSAL, "after": "另一个值"}],
            })

    def test_extraction_rejects_noop_transition(self):
        with self.assertRaises(ValidationError):
            ChapterFactExtractionV1.model_validate({
                "summary": "没有变化",
                "facts": [{**PROPOSAL, "after": "戒备"}],
            })

    def test_review_requires_one_decision_for_every_fact(self):
        extraction = ChapterFactExtractionV1.model_validate({"summary": "一项", "facts": [PROPOSAL]})
        with self.assertRaises(ValidationError):
            ChapterFactReviewV1.model_validate({"summary": "漏审", "decisions": []}, context={"fact_count": len(extraction.facts)})

    def test_review_rejects_duplicate_or_out_of_range_indices(self):
        duplicate = {
            "summary": "重复",
            "decisions": [
                {"index": 0, "decision": "accept", "message": "正文明确支持"},
                {"index": 0, "decision": "omit", "message": "重复意见"},
            ],
        }
        with self.assertRaises(ValidationError):
            ChapterFactReviewV1.model_validate(duplicate, context={"fact_count": 1})
        with self.assertRaises(ValidationError):
            ChapterFactReviewV1.model_validate({
                "summary": "越界",
                "decisions": [{"index": 1, "decision": "accept", "message": "不存在"}],
            }, context={"fact_count": 1})


class ChapterFactServiceTests(unittest.TestCase):
    def test_unique_verbatim_quote_alignment_uses_unicode_offsets_without_mutating_original(self):
        content = "😀前文。\n继续调查。"
        original = self.extraction(evidence={"start": 1, "quote": "继续调查"})
        before = original.model_dump(mode="json")
        aligned, changes = align_fact_evidence(original, content)
        self.assertEqual(aligned.facts[0].evidence.start, 5)
        self.assertEqual(original.model_dump(mode="json"), before)
        self.assertEqual(changes, [{"index": 0, "original_start": 1, "start": 5, "quote_length": 4}])
        self.assertEqual(validate_extraction(aligned, content, {("character", "char_100001")},
            {("character", "char_100001", "psychological_state"): "戒备"})["facts"][0]["evidence"]["quote"], "继续调查")
        with self.assertRaises(DomainApiError):
            validate_extraction(aligned, content, set(), {})
        with self.assertRaises(DomainApiError):
            validate_extraction(aligned, content, {("character", "char_100001")}, {})

    def test_alignment_never_guesses_missing_repeated_or_normalized_quotes(self):
        for content, quote, start in (("甲继续调查乙继续调查", "继续调查", 0),
                                      ("继续\n调查", "继续调查", 0),
                                      ("原文没有这句话", "继续调查", 0),
                                      ("aaaa", "aa", 4)):
            extraction = self.extraction(evidence={"start": start, "quote": quote})
            aligned, changes = align_fact_evidence(extraction, content)
            self.assertEqual(changes, [])
            self.assertEqual(aligned, extraction)
            with self.assertRaises(DomainApiError):
                validate_extraction(aligned, content, {("character", "char_100001")},
                    {("character", "char_100001", "psychological_state"): "戒备"})
        valid = self.extraction(evidence={"start": 5, "quote": "调查"})
        aligned, changes = align_fact_evidence(valid, "调查。再来调查")
        self.assertEqual(changes, [])
        self.assertEqual(aligned, valid)

    def test_writing_facts_preserve_all_effective_values_and_source_ids_without_audit_quotes(self):
        facts = [{**deepcopy(PROPOSAL), "field": f"state_{index}",
                  "after": {"text": "完整事实" * 3000, "false": False, "zero": 0, "empty": [], "unknown": None},
                  "evidence": {"start": 0, "quote": "完整证据" * 2000},
                  "chapter_id": "chapter-one", "revision_id": "revision-one", "content_hash": "hash"}
                 for index in range(25)]
        before = deepcopy(facts)
        result = current_facts_for_writing(facts)
        self.assertEqual(len(result), 25)
        for original, projected in zip(facts, result):
            self.assertEqual(projected, {key: value for key, value in original.items() if key not in {"before", "evidence"}})
        self.assertEqual(facts, before)
        result[0]["after"]["text"] = "不能反向修改原始事实"
        self.assertEqual(facts, before)
        self.assertEqual(current_facts_for_writing([]), [])

    def test_writing_facts_keep_latest_state_including_false_and_retain_full_audit_in_fold(self):
        facts = [{**PROPOSAL, "after": False}, {**PROPOSAL, "field": "count", "after": 0}]
        batches = [{"chapter_id": "one", "revision_id": "r1", "content_hash": "h1", "facts": [PROPOSAL]},
                   {"chapter_id": "two", "revision_id": "r2", "content_hash": "h2", "facts": facts}]
        full = public_current_facts(batches)
        projected = current_facts_for_writing(full)
        self.assertEqual({row["field"]: row["after"] for row in projected}, {"psychological_state": False, "count": 0})
        self.assertTrue(all(row["chapter_id"] == "two" and row["revision_id"] == "r2" for row in projected))
        self.assertTrue(all(row["evidence"] == PROPOSAL["evidence"] for row in full))
        self.assertTrue(all("evidence" not in row and "before" not in row for row in projected))

    def extraction(self, **changes):
        row = {**PROPOSAL, **changes}
        return ChapterFactExtractionV1.model_validate({"summary": "提取", "facts": [row]})

    def review(self, decision="accept", message="正文和章前事实支持"):
        return ChapterFactReviewV1.model_validate({
            "summary": "审核",
            "decisions": [{"index": 0, "decision": decision, "message": message}],
        }, context={"fact_count": 1})

    def test_validate_extraction_requires_exact_quote_and_known_subject(self):
        content = "他决定继续调查三封粮信。"
        allowed = {("character", "char_100001")}
        current = {("character", "char_100001", "psychological_state"): "戒备"}
        valid = self.extraction(evidence={"start": 3, "quote": "继续调查"})
        self.assertEqual(validate_extraction(valid, content, allowed, current)["facts"][0]["after"], "决定继续调查三封粮信")
        with self.assertRaises(DomainApiError) as mismatch:
            validate_extraction(self.extraction(evidence={"start": 2, "quote": "继续调查"}), content, allowed, current)
        self.assertEqual(mismatch.exception.code, "CHAPTER_FACT_EVIDENCE_MISMATCH")
        with self.assertRaises(DomainApiError) as unknown:
            validate_extraction(self.extraction(subject_id="char_999999", evidence={"start": 3, "quote": "继续调查"}), content, allowed, current)
        self.assertEqual(unknown.exception.code, "CHAPTER_FACT_SUBJECT_UNKNOWN")

    def test_validate_extraction_requires_current_before_value(self):
        with self.assertRaises(DomainApiError) as stale:
            validate_extraction(
                self.extraction(evidence={"start": 3, "quote": "继续调查"}),
                "他决定继续调查三封粮信。",
                {("character", "char_100001")},
                {("character", "char_100001", "psychological_state"): "已经信任"},
            )
        self.assertEqual(stale.exception.code, "CHAPTER_FACT_BEFORE_CHANGED")

    def test_merge_reviews_omits_unsupported_and_blocks_conflicts(self):
        extraction = self.extraction(evidence={"start": 3, "quote": "继续调查"})
        accepted = merge_reviews(extraction, [self.review()])
        self.assertEqual(accepted["accepted_indices"], [0])
        omitted = merge_reviews(extraction, [self.review("omit", "只是推测")])
        self.assertEqual(omitted["omitted_indices"], [0])
        blocked = merge_reviews(extraction, [self.review("block", "与既有身份冲突")])
        self.assertTrue(blocked["blocking"])

    def test_high_risk_fact_requires_two_accepting_reviews(self):
        extraction = self.extraction(risk="high", evidence={"start": 3, "quote": "继续调查"})
        with self.assertRaises(DomainApiError) as missing:
            merge_reviews(extraction, [self.review()])
        self.assertEqual(missing.exception.code, "CHAPTER_FACT_SECOND_REVIEW_REQUIRED")
        accepted = merge_reviews(extraction, [self.review(), self.review()])
        self.assertEqual(accepted["accepted_indices"], [0])


class ChapterFactReviewOutputTests(unittest.IsolatedAsyncioTestCase):
    async def test_review_rejects_one_based_or_missing_indices_and_supplies_explicit_contract(self):
        extraction = {"facts": [PROPOSAL, {**PROPOSAL, "field": "other_state"}]}
        for indices in ([1, 2], [0]):
            report = ChapterFactReviewV1.model_validate({"summary": "返回错误编号",
                "decisions": [{"index": index, "decision": "accept", "message": "审核意见"} for index in indices]})
            advice = AsyncMock(return_value=(report, {}))
            with patch.object(service, "structured_advice", advice):
                with self.assertRaises(DomainApiError) as rejected:
                    await service.review_facts({}, extraction)
            self.assertEqual(rejected.exception.code, "CHAPTER_FACT_REVIEW_INVALID")
            self.assertEqual(rejected.exception.details["expected_indices"], [0, 1])
            self.assertEqual(rejected.exception.details["received_indices"], indices)
            self.assertEqual(rejected.exception.details["rejected_review"], report.model_dump(mode="json"))
            self.assertNotIn("pydantic", rejected.exception.message)
            payload = advice.call_args.args[2]
            self.assertEqual(payload["review_contract"]["expected_indices"], [0, 1])
            self.assertEqual(payload["review_contract"]["index_base"], 0)
            self.assertEqual([row["index"] for row in payload["review_contract"]["candidates"]], [0, 1])

    async def test_valid_reversed_indices_and_empty_candidates_are_not_rewritten(self):
        for indices in ([1, 0], []):
            extraction = {"facts": [PROPOSAL, {**PROPOSAL, "field": "other_state"}] if indices else []}
            report = ChapterFactReviewV1.model_validate({"summary": "完整审核",
                "decisions": [{"index": index, "decision": "omit", "message": "无须更新"} for index in indices]})
            with patch.object(service, "structured_advice", AsyncMock(return_value=(report, {"model": "test"}))):
                checked, usage = await service.review_facts({}, extraction)
            self.assertEqual(checked, report.model_dump(mode="json"))
            self.assertEqual(usage, {"model": "test"})

    async def test_invalid_schema_is_converted_without_exposing_raw_input(self):
        try:
            ChapterFactReviewV1.model_validate({"summary": "不应显示的模型原文", "decisions": [{"index": -1}]})
        except ValidationError as error:
            advice = AsyncMock(side_effect=error)
        with patch.object(service, "structured_advice", advice):
            with self.assertRaises(DomainApiError) as rejected:
                await service.review_facts({}, {"facts": [PROPOSAL]})
        self.assertEqual(rejected.exception.code, "CHAPTER_FACT_REVIEW_INVALID")
        self.assertNotIn("不应显示的模型原文", str(rejected.exception.details))
        self.assertTrue(rejected.exception.details["validation_errors"])


if __name__ == "__main__":
    unittest.main()
