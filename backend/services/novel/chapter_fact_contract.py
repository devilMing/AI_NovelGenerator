"""Strict contracts for automatic, revision-bound chapter fact closure."""
from typing import Literal

from pydantic import Field, JsonValue, ValidationInfo, model_validator

from backend.api.contracts import StrictRequestModel


class ChapterFactEvidenceV1(StrictRequestModel):
    """A continuous code-point quote from the immutable chapter revision."""

    start: int = Field(ge=0, le=2**31 - 1)
    quote: str = Field(min_length=1, max_length=10000)


class ChapterFactProposalV1(StrictRequestModel):
    """One proposed state transition; models never write facts directly."""

    subject_type: Literal[
        "novel", "character", "faction", "character_relation", "item",
        "location", "world_rule", "foreshadowing",
    ]
    subject_id: str = Field(min_length=1, max_length=100)
    field: str = Field(pattern=r"^[a-z][a-z0-9_]{0,79}$")
    before: JsonValue | None = None
    after: JsonValue
    evidence: ChapterFactEvidenceV1
    risk: Literal["normal", "high"] = "normal"

    @model_validator(mode="after")
    def reject_noop(self) -> "ChapterFactProposalV1":
        if self.after is None:
            raise ValueError("章后事实不能把未知值写成 null")
        if self.before == self.after:
            raise ValueError("章后事实必须产生实际变化")
        return self


class ChapterFactExtractionV1(StrictRequestModel):
    """All fact proposals extracted from one exact chapter revision."""

    summary: str = Field(min_length=1, max_length=2000)
    facts: list[ChapterFactProposalV1] = Field(default_factory=list, max_length=50)

    @model_validator(mode="after")
    def unique_keys(self) -> "ChapterFactExtractionV1":
        keys = [(row.subject_type, row.subject_id, row.field) for row in self.facts]
        if len(keys) != len(set(keys)):
            raise ValueError("同一章不能重复修改同一个事实字段")
        return self


class ChapterFactDecisionV1(StrictRequestModel):
    """Independent decision for one proposal index."""

    index: int = Field(ge=0, le=49)
    decision: Literal["accept", "omit", "block"]
    message: str = Field(min_length=1, max_length=2000)
    source_quote: str | None = Field(default=None, min_length=1, max_length=10000)


class ChapterFactReviewV1(StrictRequestModel):
    """A complete independent review; every proposal gets one decision."""

    summary: str = Field(min_length=1, max_length=2000)
    decisions: list[ChapterFactDecisionV1] = Field(max_length=50)

    @model_validator(mode="after")
    def complete_decisions(self, info: ValidationInfo) -> "ChapterFactReviewV1":
        indices = [row.index for row in self.decisions]
        if len(indices) != len(set(indices)):
            raise ValueError("事实审核不能重复同一个候选")
        count = (info.context or {}).get("fact_count")
        if count is not None and set(indices) != set(range(count)):
            raise ValueError("事实审核必须覆盖全部候选且不能越界")
        return self
