"""Repair book-start item candidates without overloading author guidance."""
from backend.api.error_protocol import DomainApiError
from backend.llm.schemas.item_pydantic import ItemsResultV1
from backend.services.assistant.models import structured_advice
from backend.services.novel.volume_consistency_repair import repair_material


async def repair_opening_items(envelope, review):
    candidate = ItemsResultV1.model_validate(envelope["candidate"]).model_dump(mode="json")
    material, report = repair_material(envelope, candidate, review)
    material["adoption_contract"] = {
        "position": "book_start",
        "constraint": "本批确认的是书初已存在物品及其真实归属，不是全书未来物品设计。未来才形成的物品不能以任何生命周期状态伪装成书初已存在。",
    }
    result, metadata = await structured_advice({},
        "你是小说书初物品修订编辑。依据完整review引证和material修订candidate，只解决明确冲突，尽量保留无冲突物品。"
        "书初是故事开局、后续主线事件发生之前。不得把未来制度、军械、文书或未来势力的持有事实提前登记。"
        "未来才形成、书初尚不存在的物品从本批移除；不能通过改叫草案、available/lost/destroyed/consumed、改归属、伪造过去经历来掩盖尚未存在。"
        "不得修改已保存资料、人物和势力。不得新增候选或编造物品弥补数量；保留原candidate_ref与相对顺序，允许删除无法在书初成立的候选。"
        "修正后只返回完整items数组；每件保留物品的描述、来源、生命周期、角色/势力/地点引用都必须符合书初，不能新增未读取的实体ID。"
        "返回结果会被独立重新审核并通过原保存来源校验后才采用，不输出说明或私有思考。",
        {"candidate": candidate, "review": report, "material": material}, ItemsResultV1)
    repaired = result.model_dump(mode="json")
    original = [row["candidate_ref"] for row in candidate["items"]]
    refs = [row["candidate_ref"] for row in repaired["items"]]
    if refs != [ref for ref in original if ref in set(refs)]:
        raise DomainApiError("FIRST_CHAPTER_ITEM_REPAIR_SCOPE", "物品修订新增或重排了候选，原候选与引证已保留。", status_code=502)
    return repaired, metadata
