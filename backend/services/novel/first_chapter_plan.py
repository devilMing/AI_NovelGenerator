"""Choose only missing work on the first active chapter's parent chain."""
from math import ceil

from bson import ObjectId

from backend.api.error_protocol import DomainApiError
from backend.db.base import BaseRepository
from backend.db.write_contracts import canonical_request_hash
from backend.services.novel.writing_context import load_writing_parents, get_writing_readiness


def conflict(message):
    raise DomainApiError("FIRST_CHAPTER_EXISTING_CONFLICT", message, status_code=409)


async def snapshot(novel_id, *, session=None):
    novel = (await load_writing_parents(novel_id, "novel", novel_id, session=session))[0][1]
    query = {"novel_id": ObjectId(novel_id), "is_deleted": False}
    data = {"novel": novel, "ready": {}}
    for name in ("factions", "characters", "character_relations", "world_rules", "items", "volumes", "arcs", "scenes", "chapters", "faction_volume_states", "character_volume_states"):
        data[{"character_relations": "relations", "world_rules": "rules", "faction_volume_states": "faction_states", "character_volume_states": "character_states"}.get(name, name)] = await BaseRepository(name).collection.find(query, session=session).sort([("order_index", 1), ("_id", 1)]).to_list(None)
    data["factions"] = [r for r in data["factions"] if r.get("level_type") == "core" and r.get("active_status") == "active"]
    data["characters"] = [r for r in data["characters"] if r.get("status") == "active"]
    data["relations"] = [r for r in data["relations"] if r.get("is_active")]
    # Recycled/orphaned parents never determine the first chapter.
    volumes = {r["_id"] for r in data["volumes"]}
    data["arcs"] = [r for r in data["arcs"] if r["volume_id"] in volumes]
    arcs = {r["_id"] for r in data["arcs"]}
    data["scenes"] = [r for r in data["scenes"] if r["arc_id"] in arcs]
    scenes = {r["_id"] for r in data["scenes"]}
    data["chapters"] = [r for r in data["chapters"] if r["scene_id"] in scenes]
    for name, parents, field in (("arcs", data["volumes"], "volume_id"), ("scenes", data["arcs"], "arc_id"), ("chapters", data["scenes"], "scene_id")):
        order = {r["_id"]: i for i, r in enumerate(parents)}
        data[name].sort(key=lambda r: (order[r[field]], r["order_index"], str(r["_id"])))
    first = first_chapter(data)
    if first and first.get("content", "").strip():
        return data
    if session is None:
        for kind in ("volume", "arc"):
            rows = data["volumes"] if kind == "volume" else [r for r in data["arcs"] if data["volumes"] and r["volume_id"] == data["volumes"][0]["_id"]]
            if rows:
                row = rows[0]
                data["ready"][str(row["_id"])] = await get_writing_readiness(novel_id, kind, str(row["_id"]))
    return data


def signature(data):
    """Exclude audit counters but retain all actual content and versions."""
    excluded = {"created_at", "updated_at", "writing_guard_revision", "dependency_guard_revision", "stats", "word_count", "chapter_count", "current_chapter_count", "current_word_count"}
    def clean(value):
        if isinstance(value, ObjectId):
            return str(value)
        if isinstance(value, dict):
            return {k: clean(v) for k, v in value.items() if k not in excluded}
        if isinstance(value, list):
            return [clean(v) for v in value]
        return value
    return canonical_request_hash(clean({k: v for k, v in data.items() if k != "ready"}))


def choose_step(data, settings):
    novel = data["novel"]
    if first_chapter(data) is not None and first_chapter(data).get("content", "").strip():
        return None
    def step(task, label, target=None, **inputs):
        return {"task": task, "label": label, "target": str(target["_id"]) if target else None, "inputs": inputs}
    if not data["factions"]:
        return step("create_factions_by_ai", "准备核心势力", existing_relation_targets=[], faction_count=None)
    if not any(r.get("role_type") == "protagonist" for r in data["characters"]):
        return step("create_characters_by_ai", "准备主角及核心角色", character_count=None)
    if not data["volumes"]:
        if settings.get("include_optional", True):
            if not data["relations"] and len(data["characters"]) >= 2:
                return step("create_character_relations_by_ai", "准备人物关系", character_ids=[r["character_id"] for r in data["characters"][:20]], allow_isolated_characters=True)
            if not data["rules"]:
                return step("create_world_rules_by_ai", "准备世界规则", core_rule_count=None)
            if not data["items"]:
                return step("create_items_by_ai", "准备故事物品", item_count=None)
        chapters = novel.get("number_of_chapters") or 60
        words = novel.get("words_per_chapter") or 3000
        return step("create_volume_skeleton_by_ai", "规划全书卷骨架", mode="create", target_volume_count=min(30, max(1, ceil(chapters / 50))),
                    total_chapter_budget=chapters, total_word_budget=chapters * words)
    volume = data["volumes"][0]
    ready = data["ready"].get(str(volume["_id"]), {})
    if volume.get("plot", "").strip():
        if ready.get("plot_status") != "ready":
            conflict("首卷已有剧情的来源已变化，请先核对并确认；一键写作不会覆盖已有剧情。")
    else:
        return step("generate_volume_plot_by_ai", "生成首卷详细剧情", volume)
    for kind, label in (("factions", "势力"), ("characters", "角色"), ("locations", "地点")):
        if not ready.get("preparation", {}).get(kind):
            states = data.get("faction_states" if kind == "factions" else "character_states", []) if kind != "locations" else []
            if any(r["volume_id"] == volume["_id"] for r in states) or kind == "locations" and volume.get("location_ids"):
                return step("confirm_existing_preparation", f"核对并复用首卷{label}", volume, kind=kind)
            return step(f"create_{kind}_for_volume_by_ai", f"准备首卷{label}", volume, selection_mode="auto", **({"new_count": 0} if kind != "locations" else {"location_count": 0}))
    arcs = [r for r in data["arcs"] if r["volume_id"] == volume["_id"]]
    if not arcs:
        return step("plan_arcs_by_ai", "规划首卷篇章", volume, count_mode="auto")
    arc = arcs[0]
    if arc.get("plot", "").strip():
        if data["ready"].get(str(arc["_id"]), {}).get("plot_status") != "ready":
            conflict("首篇章已有剧情需要复核，请确认后继续；已有内容不会覆盖。")
    else:
        return step("generate_arc_plot_by_ai", "生成首篇章详细剧情", arc)
    scenes = [r for r in data["scenes"] if r["arc_id"] == arc["_id"]]
    if not scenes:
        return step("plan_scenes_by_ai", "规划首篇章场景", arc, count_mode="auto")
    scene = scenes[0]
    chapters = [r for r in data["chapters"] if r["scene_id"] == scene["_id"]]
    if not chapters:
        return step("plan_chapters_by_ai", "生成首章计划", scene, count_mode="fixed", count=1)
    return step("write_chapter_by_ai", "撰写第一章正文", chapters[0], target_word_count=novel.get("words_per_chapter") or 3000)


def first_chapter(data):
    """A later volume's body must not complete an empty earlier parent chain."""
    if not data["volumes"]:
        return None
    arcs = [r for r in data["arcs"] if r["volume_id"] == data["volumes"][0]["_id"]]
    scenes = [r for r in data["scenes"] if arcs and r["arc_id"] == arcs[0]["_id"]]
    return next((r for r in data["chapters"] if scenes and r["scene_id"] == scenes[0]["_id"]), None)
