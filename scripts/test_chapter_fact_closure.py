"""Automatic chapter-after fact closure contracts and helpers."""
import unittest

from pydantic import ValidationError

from backend.services.novel.chapter_fact_contract import (
    ChapterFactExtractionV1,
    ChapterFactReviewV1,
)
from backend.services.novel.chapter_fact_service import (
    merge_reviews,
    validate_extraction,
)
from backend.api.error_protocol import DomainApiError


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


if __name__ == "__main__":
    unittest.main()
