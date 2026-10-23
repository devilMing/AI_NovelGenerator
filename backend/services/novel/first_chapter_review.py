"""Independent evidence-bound consistency gate for automatically adopted candidates."""
import json
import re
from pydantic import Field
from backend.api.contracts import StrictRequestModel
from backend.api.error_protocol import DomainApiError, sanitize_json_value
from backend.services.assistant.models import structured_advice
from backend.services.novel.first_chapter_save import candidate_for_adoption


class Issue(StrictRequestModel):
    message: str = Field(min_length=1, max_length=1500)
    candidate_quote: str = Field(min_length=1, max_length=2000)
    source_path: str = Field(min_length=1, max_length=300)
    source_quote: str = Field(min_length=1, max_length=2000)


class Review(StrictRequestModel):
    summary: str = Field(min_length=1, max_length=3000)
    issues: list[Issue] = Field(max_length=20)


def strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for child in value.values():
            yield from strings(child)
    elif isinstance(value, list):
        for child in value:
            yield from strings(child)


def verify_review(report, candidate, material):
    for issue in report.issues:
        source = material
        try:
            # Both .0 and [0] identify an actual array index; never eval a path.
            for part in re.sub(r"\[(\d+)\]", r".\1", issue.source_path).split("."):
                source = source[int(part)] if isinstance(source, list) else source[part]
        except (KeyError, ValueError, IndexError, TypeError):
            source = None
        if not isinstance(source, str) or issue.source_quote not in source or not any(issue.candidate_quote in text for text in strings(candidate)):
            raise DomainApiError("FIRST_CHAPTER_REVIEW_EVIDENCE", "一致性检查的原文引证无效，已保留候选，请继续重试检查。", status_code=502)
    return report.model_dump(mode="json")


async def review_candidate(task, envelope, settings):
    material = sanitize_json_value({k: envelope[k] for k in ("context", "prepared_context", "coverage_review") if envelope.get(k) is not None})
    candidate = sanitize_json_value(candidate_for_adoption(task, envelope["candidate"]))
    # For design tasks use the original task's explicit source context, never whole-book future prose.
    if not material:
        raise DomainApiError("FIRST_CHAPTER_REVIEW_SOURCES_MISSING", "当前步骤缺少可核对的一致性材料，已保留候选，请检查默认流程。", status_code=409)
    if task == "create_items_by_ai":
        from backend.services.novel.item_fact_contract import ITEM_FACT_FIELDS
        material["adoption_contract"] = {
            "position": "book_start", "fact_fields": sorted(ITEM_FACT_FIELDS),
            "constraint": "本次物品整组状态将确认在书初，不是未定位的全书设计。允许补充合理的开局设定，但不得将未来才创制的物品登记为开局已存在；不得将未来取得写成当前持有。引用的null表示明确无引用，不表示未知。候选若明确尚未编成、尚未存在或状态只是后续计划，不能用available或held将其确认成书初事实。",
        }
    if task == "create_volume_skeleton_by_ai":
        if "book_start_character_facts" not in material.get("context", {}):
            raise DomainApiError("FIRST_CHAPTER_REVIEW_SOURCES_MISSING", "卷骨架缺少已确认的书初事实，候选已保留，不会自动保存。", status_code=409)
        material["adoption_contract"] = {"position": "book_start", "constraint":
            "首卷的实际开局必须与book_start_character_facts里已经确认的身份、年龄、伤势和能力相容。全书设计可以包含过去背景，但不能把已确认处于较晚人生阶段的书初人物无说明地倒回更早且状态不同的时期。明确的回忆或倒叙不改变当前位置事实，不能借此将整个首卷起止状态重置到过去。后卷未来计划不当作首卷已发生事实。"}
    if task == "create_factions_for_volume_by_ai":
        material["adoption_contract"] = {"position":"volume_start", "constraint":
            "已有势力只保存卷参与计划，不写入新的实力、资源、立场、控制范围或剧情状态事实。候选已剔除旧格式state中的事实占位值，不得把这些未采用字段当作将保存的事实报告冲突。新增势力仅按profile与显式initial_facts建立起点，不从兼容state补出未知实力或资源；参与计划中的未来目标不能当成已经发生。"}
    if len(json.dumps(material, ensure_ascii=False, default=str)) > 220000:
        raise DomainApiError("FIRST_CHAPTER_REVIEW_TOO_LARGE", "审核资料超过范围，已保留候选；请缩小自定义流程的资料范围。", status_code=422)
    report, _ = await structured_advice(settings,
        "你是小说一致性审校。只依据给定材料检查候选，重点检查身份、生死伤势、时间地点、物品归属、人物知情范围、叙述视角和父级剧情边界。"
        "必须区分未来设计与已经发生的事实，不把后卷计划当作本章已发生事件，不将未知值补成事实。"
        "若提供adoption_contract，必须按实际确认位置核对状态字段，不能仅因描述标注‘全书候选/未来规划’就放行将它保存为当前事实的冲突。"
        "只报告有双方逐字原句支持的明确冲突；风格偏好及未写到的未来情节不算冲突。"
        "每个问题给出候选原句、材料字符串字段的点分路径（含数组下标，路径从material内部起算，不带material前缀）及来源原句。无明确冲突返回空issues。"
        "candidate_quote和source_quote必须各自仅复制单个字符串字段内的一段连续原文，不得跨字段拼接，不得附加字段名、JSON片段、引号或省略号。"
        "需要同时说明状态字段时，在message中解释其字段和值，candidate_quote只引用描述中的实际原句。"
        "summary说明实际核对范围，不宣称整书绝无错误。",
        {"task": task, "candidate": candidate, "material": material}, Review)
    return verify_review(report, candidate, material)
