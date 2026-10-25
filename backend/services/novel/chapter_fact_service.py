"""Automatic chapter fact extraction, independent review, and append-only adoption."""
from __future__ import annotations

from copy import deepcopy
from typing import Any, Awaitable, Callable

from bson import ObjectId
from pydantic import ValidationError

from backend.api.error_protocol import DomainApiError
from backend.db.base import BaseRepository
from backend.db.transaction import run_mongo_write_unit
from backend.db.utils import get_utc_now
from backend.db.write_contracts import canonical_request_hash
from backend.services.assistant.models import structured_advice
from backend.services.novel.chapter_fact_contract import ChapterFactExtractionV1, ChapterFactReviewV1


EXTRACTION_INSTRUCTIONS = (
    "你是小说章后事实提取器。只提取本章正文明确造成、且下一章需要知道的状态变化。"
    "不得把计划、猜测、气氛、未来意图或未发生事件写成事实。每项必须引用正文中的连续原文，start按Unicode码点计算。"
    "subject只能使用资料中allowed_subjects列出的稳定身份；before必须等于current_facts中的当前值，未知时为null。"
)
REVIEW_INSTRUCTIONS = (
    "你是独立的小说事实审校。逐项核对章前事实、正文原文和拟更新值。"
    "明确支持选择accept；只是推测或不值得长期保存选择omit；若正文自身与既有事实形成会影响后续写作的冲突选择block。"
    "必须对extraction.facts的每个候选给出且只给出一个决定。index是从0开始的数组下标，"
    "必须恰好覆盖review_contract.expected_indices；两项候选必须返回index=0和index=1，不能使用1和2。"
    "即使决定omit或block也必须返回该项；空候选返回空decisions。"
    "source_quote如填写，只能复制正文中的一段连续原文，"
    "不得跨段拼接、改写、插入省略号或把多处引文合成一条。"
)
REPAIR_INSTRUCTIONS = (
    "你是小说事实候选修复器。根据确定性校验错误或独立审核的block意见重新提取整批事实。"
    "只能使用真实正文和allowed_subjects，删除无法证明的变化；不得为了通过审核而改写证据。"
)


def _fact_key(row: Any) -> tuple[str, str, str]:
    return row.subject_type, row.subject_id, row.field


def align_fact_evidence(extraction: ChapterFactExtractionV1, content: str) -> tuple[ChapterFactExtractionV1, list[dict]]:
    """Correct offsets only for unique, verbatim quotes in the fixed revision.

    Never normalize a quote, choose between repeated occurrences, or mutate the
    original proposal. Missing/ambiguous quotes remain for strict validation.
    """
    aligned = extraction.model_copy(deep=True)
    corrections = []
    for index, fact in enumerate(aligned.facts):
        evidence = fact.evidence
        if content[evidence.start:evidence.start + len(evidence.quote)] == evidence.quote:
            continue
        start = content.find(evidence.quote)
        if start < 0 or content.find(evidence.quote, start + 1) >= 0:
            continue
        corrections.append({"index": index, "original_start": evidence.start, "start": start,
                            "quote_length": len(evidence.quote)})
        evidence.start = start
    return aligned, corrections


def validate_extraction(
    extraction: ChapterFactExtractionV1,
    content: str,
    allowed_subjects: set[tuple[str, str]],
    current_facts: dict[tuple[str, str, str], Any],
) -> dict:
    """Apply deterministic evidence, identity, and before-value checks."""
    for index, row in enumerate(extraction.facts):
        if (row.subject_type, row.subject_id) not in allowed_subjects:
            raise DomainApiError(
                "CHAPTER_FACT_SUBJECT_UNKNOWN", "章后事实引用了本次资料以外的对象", status_code=422,
                details={"index": index, "subject_type": row.subject_type, "subject_id": row.subject_id},
            )
        evidence = row.evidence
        if content[evidence.start:evidence.start + len(evidence.quote)] != evidence.quote:
            raise DomainApiError(
                "CHAPTER_FACT_EVIDENCE_MISMATCH", "章后事实的正文引文与固定修订不一致", status_code=422,
                details={"index": index, "start": evidence.start, "quote": evidence.quote},
            )
        expected = current_facts.get(_fact_key(row))
        if row.before != expected:
            raise DomainApiError(
                "CHAPTER_FACT_BEFORE_CHANGED", "章后事实的章前值与当前事实快照不一致", status_code=409,
                details={"index": index, "expected": expected, "received": row.before},
            )
    return extraction.model_dump(mode="json")


def merge_reviews(extraction: ChapterFactExtractionV1, reviews: list[ChapterFactReviewV1]) -> dict:
    """Merge independent decisions, requiring two approvals for high-risk facts."""
    if any(row.risk == "high" for row in extraction.facts) and len(reviews) < 2:
        raise DomainApiError(
            "CHAPTER_FACT_SECOND_REVIEW_REQUIRED", "高风险事实需要第二次独立审核", status_code=409,
        )
    decisions = [{row.index: row for row in review.decisions} for review in reviews]
    accepted, omitted, blocked = [], [], []
    for index, fact in enumerate(extraction.facts):
        required = 2 if fact.risk == "high" else 1
        selected = [review[index] for review in decisions[:required]]
        if any(row.decision == "block" for row in selected):
            blocked.append(index)
        elif any(row.decision == "omit" for row in selected):
            omitted.append(index)
        else:
            accepted.append(index)
    return {
        "accepted_indices": accepted,
        "omitted_indices": omitted,
        "blocked_indices": blocked,
        "blocking": bool(blocked),
    }


async def extract_facts(material: dict, *, repair: dict | None = None,
                        before_send: Callable[[], Awaitable[Any]] | None = None) -> tuple[dict, dict]:
    """Request a fresh extraction or a bounded repair using the configured default model."""
    schema, usage = await structured_advice(
        {}, REPAIR_INSTRUCTIONS if repair else EXTRACTION_INSTRUCTIONS,
        {**material, **({"repair": repair} if repair else {})}, ChapterFactExtractionV1,
        before_send=before_send,
    )
    return schema.model_dump(mode="json"), usage


async def review_facts(material: dict, extraction: dict, *,
                       retry: dict | None = None,
                       before_send: Callable[[], Awaitable[Any]] | None = None) -> tuple[dict, dict]:
    """Independently review every extracted proposal."""
    expected = list(range(len(extraction["facts"])))
    payload = {**material, "extraction": extraction,
               "review_contract": {"index_base": 0, "expected_indices": expected,
                   "candidates": [{"index": index, **{key: fact[key] for key in ("subject_type", "subject_id", "field")}}
                                  for index, fact in enumerate(extraction["facts"])]}}
    if retry:
        payload["previous_review_error"] = {key: retry[key] for key in ("code", "message", "expected_indices", "received_indices") if key in retry}
    report = None
    try:
        schema, usage = await structured_advice(
            {}, REVIEW_INSTRUCTIONS, payload, ChapterFactReviewV1, before_send=before_send,
        )
        report = schema.model_dump(mode="json")
        checked = ChapterFactReviewV1.model_validate(report, context={"fact_count": len(expected)})
    except ValidationError as exc:
        details = {"expected_indices": expected,
                   "validation_errors": [{"loc": list(row["loc"]), "type": row["type"], "message": row["msg"]}
                                         for row in exc.errors(include_input=False, include_url=False)]}
        if report is not None:
            details.update(received_indices=[row["index"] for row in report["decisions"]], rejected_review=report)
        raise DomainApiError("CHAPTER_FACT_REVIEW_INVALID",
            "事实审核未完整对应候选，或返回格式无效；正文和事实候选已保留。",
            status_code=502, details=details) from exc
    return checked.model_dump(mode="json"), usage


def public_current_facts(rows: list[dict]) -> list[dict]:
    """Fold append-only accepted facts into the effective state exposed to later writing."""
    current: dict[tuple[str, str, str], dict] = {}
    for batch in rows:
        for fact in batch.get("facts", []):
            key = (fact["subject_type"], fact["subject_id"], fact["field"])
            current[key] = {
                **deepcopy(fact),
                "chapter_id": str(batch["chapter_id"]),
                "revision_id": str(batch["revision_id"]),
                "content_hash": batch["content_hash"],
            }
    return [current[key] for key in sorted(current)]


def current_facts_for_writing(facts: list[dict]) -> list[dict]:
    """Expose complete effective values and provenance, without replaying audit evidence.

    The full facts (including before values and exact quotes) remain stored and
    fingerprinted. Later writing needs the effective after value, not the old
    transition or several thousand characters of evidence for each field.
    """
    fields = ("subject_type", "subject_id", "field", "after", "risk",
              "chapter_id", "revision_id", "content_hash")
    return [{key: deepcopy(fact[key]) for key in fields if key in fact} for fact in facts]


async def adopt_fact_batch(
    novel_id: str,
    chapter: dict,
    revision: dict,
    extraction: dict,
    reviews: list[dict],
    merged: dict,
    attempts: list[dict],
    *,
    evidence_alignments: list[dict] | None = None,
    session=None,
) -> dict:
    """Commit one immutable, idempotent fact batch bound to the saved revision."""
    collection = BaseRepository("chapter_fact_batches").collection
    await collection.create_index(
        [("novel_id", 1), ("chapter_id", 1), ("chapter_version", 1), ("status", 1)],
        name="chapter_fact_batches_current_revision",
    )
    accepted = [deepcopy(extraction["facts"][index]) for index in merged["accepted_indices"]]
    payload = {
        "schema_version": 1,
        "novel_id": ObjectId(novel_id),
        "chapter_id": chapter["_id"],
        "revision_id": revision["_id"],
        "chapter_version": revision["version"],
        "content_hash": revision["content_hash"],
        "facts": accepted,
        "omitted_indices": merged["omitted_indices"],
        "extraction": deepcopy(extraction),
        "reviews": deepcopy(reviews),
        "attempts": deepcopy(attempts),
    }
    if evidence_alignments:
        payload["evidence_alignments"] = deepcopy(evidence_alignments)
    fingerprint = canonical_request_hash(payload)
    identity = f"auto-facts:{revision['_id']}"

    async def save(active_session):
        current_chapter = await BaseRepository("chapters").collection.find_one({
            "_id": chapter["_id"], "novel_id": ObjectId(novel_id), "is_deleted": False,
        }, session=active_session)
        current_revision = await BaseRepository("chapter_revisions").collection.find_one({
            "_id": revision["_id"], "chapter_id": chapter["_id"], "content_hash": revision["content_hash"],
        }, session=active_session)
        if current_chapter is None or current_revision is None or current_chapter["version"] != revision["version"]:
            raise DomainApiError(
                "CHAPTER_FACT_REVISION_CHANGED", "章后事实所依据的正文已经变化", status_code=409,
            )
        existing = await collection.find_one({"_id": identity}, session=active_session)
        if existing is not None:
            if existing["request_fingerprint"] != fingerprint:
                raise DomainApiError(
                    "CHAPTER_FACT_IDEMPOTENCY_CONFLICT", "同一正文修订已有不同的事实批次", status_code=409,
                )
            return existing
        document = {
            "_id": identity, **payload, "request_fingerprint": fingerprint,
            "status": "confirmed", "created_at": get_utc_now().replace(tzinfo=None),
        }
        await collection.insert_one(document, session=active_session)
        return document

    if session is not None:
        return await save(session)
    return await run_mongo_write_unit(save, "adopt_automatic_chapter_facts", require_transaction=True)
