"""Choose missing preparation within one volume; never replace existing content."""
from backend.api.error_protocol import DomainApiError
from backend.db.base import BaseRepository
from backend.db.write_contracts import canonical_request_hash
from backend.services.novel.writing_context import get_writing_readiness


PREPARATION_TASKS = (
    "generate_volume_plot_by_ai", "create_factions_for_volume_by_ai",
    "create_characters_for_volume_by_ai", "create_locations_for_volume_by_ai",
    "plan_arcs_by_ai", "generate_arc_plot_by_ai", "plan_scenes_by_ai",
)
STAGES = {
    "generate_volume_plot_by_ai": "volume_plot", "generate_arc_plot_by_ai": "arc_plot",
    "plan_arcs_by_ai": "arc_planning", "plan_scenes_by_ai": "scene_planning",
    "confirm_existing_preparation": "volume_preparing",
    **{task: "volume_preparing" for task in PREPARATION_TASKS if task.startswith("create_")},
}


def complete_mode(job):
    # Missing on persisted old jobs means the original, fixed scene scope.
    return job.get("settings", {}).get("complete_planning", False)


def require_missing_plan(task, result):
    field = {"plan_arcs_by_ai": "arcs", "plan_scenes_by_ai": "scenes"}.get(task)
    if field and not result["candidate"].get(field):
        raise DomainApiError("VOLUME_WRITING_EMPTY_PLAN", "当前部分没有下级规划，但模型未生成可采用的计划。结果已保留，请查看日志，调整流程后结束本轮重新开始。", status_code=409)


def structure_signature(volume, arcs, scenes, chapters):
    def fields(row, names):
        return {name: row.get(name) for name in names}
    return canonical_request_hash({
        "volume": fields(volume, ("_id", "title", "planning", "plot", "plot_source_fingerprint", "preparation", "location_ids")),
        "arcs": [fields(row, ("_id", "title", "summary", "planning", "plot", "plot_source_fingerprint", "order_index")) for row in arcs],
        "scenes": [fields(row, ("_id", "arc_id", "title", "summary", "planning", "order_index")) for row in scenes],
        "chapters": [fields(row, ("_id", "scene_id", "title", "summary", "planning", "order_index")) for row in chapters],
    })


def extra_summary(volume, arcs, scenes):
    return {"complete_planning": True, "arcs": len(arcs),
        "missing_arc_plots": sum(not row.get("plot", "").strip() for row in arcs),
        "arcs_without_scenes": sum(not any(scene["arc_id"] == row["_id"] for scene in scenes) for row in arcs),
        "volume_plot_missing": not bool(volume.get("plot", "").strip())}


def check_budgets(data):
    """Reject incompatible saved allocations instead of silently changing them."""
    for parent, children in [(data["volume"], data["arcs"]), *[
        (arc, [row for row in data["scenes"] if row["arc_id"] == arc["_id"]]) for arc in data["arcs"]]]:
        for field in ("target_chapter_count", "target_word_count"):
            budget = (parent.get("planning") or {}).get(field)
            minimum = 1 if field == "target_chapter_count" else 0
            def allocation(child):
                value = (child.get("planning") or {}).get(field) or minimum
                if field == "target_chapter_count":
                    descendants = [row for row in data["chapters"] if row.get("scene_id") == child["_id"] or row.get("arc_id") == child["_id"]]
                    value = max(value, len(descendants))
                    if parent["_id"] == data["volume"]["_id"]:
                        value = max(value, sum(max((scene.get("planning") or {}).get(field) or 1,
                            sum(chapter["scene_id"] == scene["_id"] for chapter in data["chapters"]))
                            for scene in data["scenes"] if scene["arc_id"] == child["_id"]))
                return value
            used = sum(allocation(child) for child in children)
            if budget is not None and used > budget:
                raise DomainApiError("VOLUME_WRITING_BUDGET_CONFLICT",
                    f"《{parent['title']}》已有下级规划超出预算，请核对后重新开始；已有内容保留。", status_code=409,
                    details={"field": field, "budget": budget, "allocated": used})


async def choose_preparation(data, arc=None):
    volume = data["volume"]
    target = arc or volume
    if not target.get("plot", "").strip():
        return ("generate_arc_plot_by_ai" if arc else "generate_volume_plot_by_ai", target, 0)
    ready = await get_writing_readiness(str(volume["novel_id"]), "arc" if arc else "volume", str(target["_id"]))
    if ready["plot_status"] != "ready":
        raise DomainApiError("VOLUME_WRITING_PLOT_STALE", f"《{target['title']}》已有详细剧情的来源已变化，请先核对并确认；不会自动覆盖。", status_code=409)
    if arc:
        if not any(scene["arc_id"] == arc["_id"] for scene in data["scenes"]):
            return "plan_scenes_by_ai", arc, 0
        return None
    for kind in ("factions", "characters", "locations"):
        if ready["preparation"][kind]:
            continue
        existing = bool(volume.get("location_ids")) if kind == "locations" else bool(await BaseRepository(
            "faction_volume_states" if kind == "factions" else "character_volume_states").collection.find_one({
                "novel_id": volume["novel_id"], "volume_id": volume["_id"], "is_deleted": False}))
        return ("confirm_existing_preparation" if existing else f"create_{kind}_for_volume_by_ai", {**volume, "preparation_kind": kind}, 0)
    if not data["arcs"]:
        return "plan_arcs_by_ai", volume, 0
    return None
