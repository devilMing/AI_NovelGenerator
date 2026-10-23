"""Adopt missing first-chapter resources using their original transactional contracts."""
from copy import deepcopy
from backend.api.error_protocol import DomainApiError


def candidate_for_adoption(task, candidate):
    """Review and save the same effective fields, retaining original AI output."""
    result = deepcopy(candidate)
    if task == "create_factions_for_volume_by_ai":
        from backend.llm.schemas.faction_volume_state_pydantic import FactionVolumePlanFieldsV1
        for row in result["factions"]:
            row["state"] = {k:v for k,v in row["state"].items() if k in FactionVolumePlanFieldsV1.model_fields}
        # Legacy state fields are not explicit initial facts. In particular,
        # a coerced numeric placeholder must never establish unknown power.
    return result


async def save_candidate(novel_id, task, envelope, key, *, session):
    from backend.services.task_flows.registry import CONTENT_TASKS
    from backend.services.novel.plot_contract import PLOT_TASKS, PlotConfirmV1
    from backend.services.novel.plot_service import confirm_plot
    from backend.services.novel.writing_contract import WritingConfirmV1
    from backend.services.novel.writing_generation import confirm_writing
    if task == "confirm_existing_preparation":
        from backend.services.novel.plot_contract import VolumePreparationConfirmV1
        from backend.services.novel.plot_service import confirm_preparation
        candidate = envelope["candidate"]
        return await confirm_preparation(novel_id, candidate["volume_id"], candidate["kind"], VolumePreparationConfirmV1(expected_version=candidate["expected_version"]), idempotency_key=key, session=session)
    candidate, basis = candidate_for_adoption(task, envelope["candidate"]), envelope["basis"]
    inputs = basis.get("generation_input") or basis["flow_snapshot"]["business_input"]
    expected = {f"{r['resource_type']}:{r['id']}": r["version"] for r in basis["source_refs"]}
    common = {"idempotency_key": key, "session": session}
    position = {"version": 1, "intent": "book_start", "target_type": "novel", "target_id": novel_id}
    generation = {"basis": basis, "generation_input": inputs}
    if task in PLOT_TASKS:
        return await confirm_plot(task, inputs["target_id"], PlotConfirmV1.model_validate({"candidate": candidate, "basis": basis, "expected_version": inputs["expected_version"]}), **common)
    if task in CONTENT_TASKS:
        return await confirm_writing(task, inputs["target_id"], WritingConfirmV1.model_validate({"candidate": candidate, "basis": basis, "expected_version": inputs.get("expected_version")}), **common)
    if task == "create_factions_by_ai":
        from backend.services.novel.faction_fact_batch_service import create_faction_fact_batch
        from backend.services.novel.faction_fact_contract import FactionFactCreateDataV1, FACTION_FACT_PROFILE_FIELDS
        rows = [{"candidate_ref": r["faction_ref"], "profile": {**{k: v for k, v in r.items() if k in FactionFactCreateDataV1.model_fields}, "level_type": "core"},
                 "facts": {k: v for k, v in r.items() if k in FACTION_FACT_PROFILE_FIELDS}} for r in candidate["core_factions"]]
        return await create_faction_fact_batch(novel_id, {"position": position, "candidates": rows, "relations": candidate["faction_relations"], "generation": generation, "confirmed": True}, **common)
    if task == "create_characters_by_ai":
        from backend.services.novel.character_fact_batch_service import create_character_fact_batch
        from backend.services.novel.character_fact_contract import CharacterFactCreateDataV1, CHARACTER_FACT_PROFILE_FIELDS
        rows = []
        for row in candidate["core_characters"]:
            bindings = [{"faction_id": b["faction_id"], "faction_version": expected[f"faction:{b['faction_id']}"],
                         **{k: b[k] for k in ("membership_type", "role_title", "public_status", "loyalty_level")}, "notes": b["reason"]}
                        for b in candidate["binding_candidates"] if b["character_ref"] == row["character_ref"] and b["faction_id"] is not None]
            rows.append({"candidate_ref": row["character_ref"], "profile": {**{k: v for k, v in row.items() if k in CharacterFactCreateDataV1.model_fields}, "is_core_character": True},
                         "facts": {k: v for k, v in row.items() if k in CHARACTER_FACT_PROFILE_FIELDS}, "bindings": bindings})
        return await create_character_fact_batch(novel_id, {"position": position, "candidates": rows, "generation": generation, "confirmed": True}, **common)
    if task == "create_character_relations_by_ai":
        from backend.services.novel.character_fact_batch_service import create_character_relation_fact_batch
        from backend.services.novel.character_relation_fact_contract import CharacterRelationFactCreateDataV1
        rows = [{"candidate_ref": r["relation_ref"], "profile": {k: v for k, v in r.items() if k in CharacterRelationFactCreateDataV1.model_fields}, "facts": {"current_state": r["current_state"]}} for r in candidate["relations"]]
        return await create_character_relation_fact_batch(novel_id, {"position": position, "candidates": rows, "generation": generation, "confirmed": True}, **common)
    if task == "create_items_by_ai":
        from backend.services.novel.item_fact_service import preview_item_fact_batch, confirm_item_fact_batch
        batch = {"candidates": candidate["items"], "basis": basis, "expected_versions": expected}
        preview = await preview_item_fact_batch(novel_id, {"generation": batch, "position": position, "note": "一键首章写作：将本次新建物品明确建立在书初，不修改已有物品。"}, session=session)
        entries = [{"candidate_ref": r["candidate_ref"], "command": {"operation": "create", "basis": r["preview"]["basis"], "after": r["after"], "confirmed": True}} for r in preview["entries"]]
        return await confirm_item_fact_batch(novel_id, {"generation": batch, "entries": entries, "confirmed": True}, **common)
    if task in {"create_factions_for_volume_by_ai", "create_characters_for_volume_by_ai"}:
        from backend.llm.schemas.volume_entity_pydantic import VolumeFactionsPlanConfirmV1, VolumeCharactersInitialFactsConfirmV1
        from backend.services.llm.volume_entity_generation_service import confirm_volume_entities
        characters = task == "create_characters_for_volume_by_ai"
        rows = candidate["characters" if characters else "factions"]
        model = VolumeCharactersInitialFactsConfirmV1 if characters else VolumeFactionsPlanConfirmV1
        request = model.model_validate({"contract": envelope.get("contract") if characters else "participation_plan_v1", "candidates": rows, "basis": basis})
        return await confirm_volume_entities(task, novel_id, inputs["volume_id"], request, **common)
    from backend.llm.schemas.world_rule_pydantic import CoreWorldRulesConfirmV1
    from backend.llm.schemas.item_pydantic import ItemBulkGeneratedRequestV1
    from backend.llm.schemas.location_pydantic import LocationBulkGeneratedRequestV1
    from backend.llm.schemas.volume_planning_pydantic import VolumeBulkPlanRequestV1
    from backend.services.novel.world_rule_service import WorldRuleService
    from backend.services.novel.item_service import ItemService
    from backend.services.novel.location_service import LocationService
    from backend.services.novel.volume_service import VolumeService
    adapters = {
        "create_world_rules_by_ai": (CoreWorldRulesConfirmV1, "core_world_rules", WorldRuleService.bulk_create_core_world_rules),
        "create_items_by_ai": (ItemBulkGeneratedRequestV1, "items", ItemService.bulk_create_generated_items),
        "create_locations_for_volume_by_ai": (LocationBulkGeneratedRequestV1, "locations", LocationService.bulk_create_generated_locations),
        "create_volume_skeleton_by_ai": (VolumeBulkPlanRequestV1, "volumes", VolumeService.bulk_plan_volumes),
    }
    if task not in adapters:
        raise DomainApiError("FIRST_CHAPTER_TASK_INVALID", "不支持的首章任务", status_code=422)
    model, field, save = adapters[task]
    return await save(novel_id, model.model_validate({"candidates": candidate[field], "basis": basis, "expected_versions": expected}), **common)
