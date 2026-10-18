"""逐卷写作只追加缺少的章计划，并写入空正文。"""
from pydantic import Field, field_validator

from backend.api.contracts import StrictRequestModel
from backend.services.task_flows.contracts import FlowRef
from backend.services.task_flows.registry import GENERATION_FIELDS


class VolumeWritingStart(StrictRequestModel):
    user_guidance: str = Field(default="", max_length=5000)
    plan_flow_ref: FlowRef | None = None
    writing_flow_ref: FlowRef | None = None
    generation_params: dict = Field(default_factory=dict)

    @field_validator("generation_params")
    @classmethod
    def validate_parameters(cls, value: dict) -> dict:
        if set(value) - GENERATION_FIELDS:
            raise ValueError("生成参数包含未声明字段")
        return value
