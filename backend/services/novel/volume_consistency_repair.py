"""Repair unsaved prose or hierarchy plans using their original review evidence."""
import json

from backend.api.error_protocol import DomainApiError, sanitize_json_value
from backend.llm.schemas.writing_pydantic import ChapterContentResultV1, WRITING_RESULTS
from backend.services.assistant.models import structured_advice
from backend.services.novel.first_chapter_review import Review, verify_review


MAX_CONSISTENCY_REPAIRS = 2
PLAN_REPAIR_FIELDS = {"plan_arcs_by_ai": "arcs", "plan_scenes_by_ai": "scenes", "plan_chapters_by_ai": "chapters"}


def repair_material(envelope, candidate, review):
    material = sanitize_json_value({key: envelope[key] for key in
        ("context", "prepared_context", "coverage_review") if envelope.get(key) is not None})
    report = Review.model_validate(review)
    # Also validate cached reviews from jobs created before automatic repair existed.
    verify_review(report, candidate, material)
    if not report.issues:
        raise DomainApiError("VOLUME_WRITING_REPAIR_NOT_NEEDED", "当前候选没有已确认的一致性冲突，无需修订。", status_code=409)
    if len(json.dumps(material, ensure_ascii=False, default=str)) > 220000:
        raise DomainApiError("VOLUME_WRITING_REPAIR_TOO_LARGE", "修订资料超过范围，候选和审核已保留，请缩小资料范围。", status_code=422)
    return material, report.model_dump(mode="json")


async def repair_chapter_candidate(envelope: dict, review: dict) -> tuple[dict, dict]:
    """Return text only; retain the original context/basis and never write book data."""
    candidate = ChapterContentResultV1.model_validate(envelope["candidate"]).model_dump(mode="json")
    material, report = repair_material(envelope, candidate, review)
    repaired, metadata = await structured_advice({},
        "你是小说正文修订编辑。只修订当前尚未保存的章节，修复给定审核中有原文引证的明确一致性冲突。"
        "material是生成和审核时实际读取的原始资料，review是待修复的问题，candidate是当前正文。"
        "以前文已发生事实、人物当前位置和知情范围、物品归属及父级规划边界为准；严格区分未来计划和已发生事件。"
        "不得修改前文或规划，不得为消除冲突编造折返、失忆、梦境、时间跳跃或新增能力；缺少依据时不得补成事实。"
        "尽量保留本章无冲突的情节、风格、视角和篇幅，只作必要修改，同时修正受影响的衔接与后续引用。"
        "返回完整修订后的正文content，不返回说明、差异、审核结论或书外文字。修订后会由独立请求重新审核。",
        {"candidate": candidate, "review": report, "material": material},
        ChapterContentResultV1)
    return repaired.model_dump(mode="json"), metadata


def empty_plan_pov_ids(envelope):
    """Only explicitly loaded characters with frozen source refs may fill a null POV."""
    read_ids = {row["id"] for row in envelope.get("basis", {}).get("source_refs", [])
                if row.get("resource_type") == "character"}
    selected = {row["id"] for row in envelope.get("context", {}).get("selected_entities", [])
                if row.get("resource_type") == "character"}
    return read_ids & selected


def validate_repaired_plan(task, original, repaired, *, empty_pov_ids=frozenset()):
    """Narrative corrections cannot delete, reorder or reallocate the batch."""
    schema, field = WRITING_RESULTS[task], PLAN_REPAIR_FIELDS[task]
    before = schema.model_validate(original).model_dump(mode="json")[field]
    result = schema.model_validate(repaired).model_dump(mode="json")
    after = result[field]
    if len(before) != len(after):
        raise DomainApiError("VOLUME_WRITING_REPAIR_SCOPE_CHANGED", "规划修订改变了候选数量，原候选与引证已保留，不会自动采用。", status_code=502)
    for old, new in zip(before, after):
        narrative = {"goal", "start_state", "end_state", "key_beats", "beats", "continuity_notes"}
        keys = set(old["planning"]) - narrative
        if (task == "plan_chapters_by_ai" and old["planning"].get("pov_character_id") is None
                and new["planning"].get("pov_character_id") in empty_pov_ids):
            keys.discard("pov_character_id")
        if (any(old[key] != new[key] for key in old if key not in {"summary", "planning"})
                or any(old["planning"].get(key) != new["planning"].get(key) for key in keys)):
            raise DomainApiError("VOLUME_WRITING_REPAIR_SCOPE_CHANGED", "规划修订改变了顺序、名称、预算或实体引用，原候选已保留，不会自动采用。", status_code=502)
    return result


async def repair_plan_candidate(task, envelope, review):
    schema = WRITING_RESULTS[task]
    candidate = schema.model_validate(envelope["candidate"]).model_dump(mode="json")
    material, report = repair_material(envelope, candidate, review)
    allowed_pov = empty_plan_pov_ids(envelope) if task == "plan_chapters_by_ai" else set()
    repaired, metadata = await structured_advice({},
        "你是小说规划修订编辑。只修订当前尚未保存的篇章、场景或章计划候选，解决review中有双方原文引证的明确冲突。"
        "material是生成时读取的原始剧情、前文与规划；candidate是本批候选。以已保存剧情的事件时序和边界为准，区分未来计划和已发生事实。"
        "必须保持候选数量、数组顺序、各条title、目标章节数、目标字数和所有实体ID引用完全不变。"
        "已明确的视角人物ID必须保留；仅章计划的pov_character_id原为null且审核要求补齐时，"
        "可依据原始材料从repair_contract.fillable_pov_ids中选择对应角色补填。不得换掉已有视角或引用列表外的角色。"
        "仅修改必要的summary以及planning中的目标、起止状态、节拍和衔接文字；不得通过删除候选、重复条目、缩减预算或换人物绕过问题。"
        "重点核对每个事件、选项、检查或计费项目首次出现的阶段，将提前出现的描述修回对应时段，且同步修正受影响的前后衔接。"
        "不得改写已保存的卷或篇章剧情、场景、章计划及正文，不得编造新事实来掩盖冲突。"
        "返回完整的修订后规划数组，不返回说明或审核结论；后续将独立复核，并按原有来源、引用和剩余预算校验后才保存。",
        {"task": task, "candidate": candidate, "review": report, "material": material,
         "repair_contract": {"fillable_pov_ids": sorted(allowed_pov)}}, schema)
    return validate_repaired_plan(task, candidate, repaired.model_dump(mode="json"), empty_pov_ids=allowed_pov), metadata
