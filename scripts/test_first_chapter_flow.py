"""End-to-end original graphs and saves with deterministic AI output in a temporary Mongo DB."""
import asyncio
import unittest
from copy import deepcopy
from unittest.mock import patch
from uuid import uuid4

from pymongo import AsyncMongoClient

from backend.db import mongo
from backend.services.novel import first_chapter_service as service
from backend.services.novel.novel_service import NovelService
from backend.api.default_routers.novel_router import NovelManualCreateRequestV1
from backend.services.task_flows.llm import FlowModels
from backend.services.task_flows.agent_contract import PreparedContextV1
from backend.services.task_flows.book_tools import normalize_agent_scope
from backend.services.novel.first_chapter_review import Review


def sample(schema, root=None):
    root = root or schema
    if "$ref" in schema:
        return sample(root["$defs"][schema["$ref"].split("/")[-1]], root)
    if "default" in schema:
        return deepcopy(schema["default"])
    if "const" in schema:
        return schema["const"]
    if "enum" in schema:
        return schema["enum"][0]
    if "anyOf" in schema:
        if any(v.get("type") == "null" for v in schema["anyOf"]):
            return None
        return sample(schema["anyOf"][0], root)
    kind = schema.get("type")
    if kind == "object" or "properties" in schema:
        return {k: sample(v, root) for k, v in schema.get("properties", {}).items()}
    if kind == "array":
        return [sample(schema["items"], root) for _ in range(schema.get("minItems", 0))]
    if kind == "integer" or kind == "number":
        return schema.get("minimum", 1)
    if kind == "boolean":
        return True
    if kind == "null":
        return None
    pattern = schema.get("pattern", "")
    if "faction_" in pattern:
        return "faction_001"
    if "relation_" in pattern:
        return "relation_001"
    if "character_" in pattern:
        return "character_01"
    return ("测试内容" * max(1, (schema.get("minLength", 1) + 3) // 4))[:schema.get("maxLength", 1000)]


class FullFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        asyncio.get_running_loop().set_debug(False)
        self.database = "ai_ng_first_chapter_flow_test_" + uuid4().hex
        self.client = AsyncMongoClient("mongodb://127.0.0.1:27017/?replicaSet=rs0", serverSelectionTimeoutMS=3000)
        await self.client.admin.command("ping")
        self.db = self.client[self.database]
        self.previous = mongo.client, mongo._active_connection_settings
        mongo.client, mongo._active_connection_settings = self.client, ("mongodb://127.0.0.1:27017/?replicaSet=rs0", self.database, 3000)
        self.calls = []
        self.reviewed_tasks = set()
        self.gate = asyncio.Event()
        self.entered = asyncio.Event()
        self.gate.set()
        self.gate_task = None
        self.pause_once = False
        self.fail_task = None
        self.failed_once = False
        self.patches = []
        self.addAsyncCleanup(self.cleanup)
        novel = await NovelService.create_manual_novel(NovelManualCreateRequestV1(title="一键首章测试", genre="架空历史", plot="主角入城寻找信件，查明旧案。", summary="旧案调查", worldview="架空王朝，普通人没有超能力。", core_seed="寻找信件", narrative_pov="第三人称限知", number_of_chapters=2, words_per_chapter=3000), idempotency_key="fixture-novel-create")
        self.novel = str(novel["_id"])

        async def execute(models, node, inputs, instance_path):
            task = models.task
            self.calls.append((task, node.id))
            if task == self.fail_task and not self.failed_once:
                self.failed_once = True
                from backend.api.error_protocol import DomainApiError
                raise DomainApiError("TEST_NETWORK", "模拟本步网络中断", status_code=503)
            if task == self.gate_task and not self.pause_once:
                self.pause_once = True
                self.entered.set()
                await self.gate.wait()
            output = sample(models.output_models[id(node)].model_json_schema())
            if task == "create_factions_by_ai":
                row = output["core_factions"][0]
                output["core_factions"] = [{**deepcopy(row), "faction_ref": f"faction_{i:03}", "name": f"测试势力{i}"} for i in range(1, 4)]
                from backend.llm.schemas.novel_pydantic import CoreFactionRelationSchema
                relation = sample(CoreFactionRelationSchema.model_json_schema())
                output["faction_relations"] = [{**deepcopy(relation), "relation_ref": f"relation_{i:03}", "source": {"kind": "candidate", "faction_ref": f"faction_{i:03}"}, "target": {"kind": "candidate", "faction_ref": f"faction_{i+1:03}"}} for i in (1, 2)]
            elif task == "create_characters_by_ai":
                row = output["core_characters"][0]
                output["core_characters"] = [{**deepcopy(row), "character_ref": f"character_{i:02}", "name": f"测试人物{i}", "role_type": "protagonist" if i == 1 else "supporting"} for i in range(1, 4)]
                output["binding_candidates"] = [{"character_ref": r["character_ref"], "faction_id": None, "membership_type": None, "role_title": None, "public_status": None, "loyalty_level": None, "reason": None} for r in output["core_characters"]]
            elif task == "create_volume_skeleton_by_ai":
                row = output["volumes"][0]
                row.update(candidate_ref="volume_one", title="第一卷", order_index=1)
                row["planning"].update(target_chapter_count=2, target_word_count=6000, key_turning_points=["找到信件"])
            elif task == "create_character_relations_by_ai":
                schema = models.output_models[id(node)].model_json_schema()
                row = sample(schema["properties"]["relations"]["items"], schema)
                chars = await self.db.characters.find({}).sort("character_id", 1).to_list(None)
                output["relations"] = [{**deepcopy(row), "relation_ref": f"relation_{i+1:03}", "source_character_id": chars[i]["character_id"], "target_character_id": chars[i+1]["character_id"]} for i in range(2)]
            elif task in {"create_world_rules_by_ai", "create_items_by_ai"}:
                field = "core_world_rules" if task == "create_world_rules_by_ai" else "items"
                schema = models.output_models[id(node)].model_json_schema()
                row = sample(schema["properties"][field]["items"], schema)
                count = 5 if field == "core_world_rules" else 6
                output[field] = [{**deepcopy(row), "name": f"测试{field}{i}", "candidate_ref": f"rule_candidate_{i}" if field == "core_world_rules" else f"item_candidate_{i}"} for i in range(1, count + 1)]
                if field == "core_world_rules":
                    for i, rule in enumerate(output[field], 1):
                        rule["statement"] = f"第{i}条世界规则：架空王朝禁止第{i}类行为。"
            elif task in {"generate_volume_plot_by_ai", "generate_arc_plot_by_ai"}:
                if "plot" in output:
                    output["plot"] = "主角入城寻找信件，发现旧案的第一条线索。"
                if "planning" in output:
                    if task == "generate_volume_plot_by_ai":
                        output["planning"].update(target_chapter_count=2, target_word_count=6000, key_turning_points=["找到信件"])
                    else:
                        output["planning"].update(target_chapter_count=2, target_word_count=6000)
                    output["source_conflicts"] = []
            elif task in {"plan_arcs_by_ai", "plan_scenes_by_ai", "plan_chapters_by_ai"}:
                if "review" in output:
                    output["review"] = {"schema_version": 1, "summary": "测试覆盖检查", "issues": []}
                else:
                    field = {"plan_arcs_by_ai": "arcs", "plan_scenes_by_ai": "scenes", "plan_chapters_by_ai": "chapters"}[task]
                    # Array defaults may be empty, construct the actual candidate item schema.
                    schema = models.output_models[id(node)].model_json_schema()
                    row = sample(schema["properties"][field]["items"], schema)
                    row["title"] = "首篇章" if field == "arcs" else "首场景" if field == "scenes" else "第一章 入城"
                    row["planning"].update(goal="寻找信件", start_state="尚未入城", end_state="找到第一条线索", target_word_count=3000 if field == "chapters" else 6000)
                    if field != "chapters":
                        row["planning"]["target_chapter_count"] = 2
                    output[field] = [row]
            elif task == "write_chapter_by_ai":
                output = {"content": "城门初开，主角走入城中。\n\n他循着旧案的线索，前去寻找那封信。"}
            yield {"status": "done", "outputs": output}

        async def agent(node, inputs, instance, *, models, scope, **kwargs):
            report = PreparedContextV1(target=normalize_agent_scope(scope, task=models.task), boundaries="只推进当前层级和第一章，不泄露后续剧情。", coverage="尚无已保存下级，准备首次生成。", remaining=True, suggested_count=1,
                chapter_sources={"parent_excerpts": [], "previous_chapter_ids": [], "relation_ids": []} if scope.get("chapter_context_version") else None)
            yield {"status": "done", "outputs": {"prepared_context": report.model_dump(mode="json")}}

        async def advice(settings, instructions, material, schema, **kwargs):
            self.assertIs(schema, Review)
            self.assertTrue(material["material"], material["task"])
            self.reviewed_tasks.add(material["task"])
            return Review(summary="已核对本步提供的规划和当前位置资料。", issues=[]), {}
        self.patches = [patch.object(FlowModels, "execute", execute), patch("backend.services.task_flows.agent.execute_agent", agent),
                        patch("backend.services.novel.first_chapter_review.structured_advice", advice)]
        for item in self.patches:
            item.start()

    async def cleanup(self):
        self.gate.set()
        await service.shutdown()
        for item in self.patches:
            item.stop()
        assert self.database.startswith("ai_ng_first_chapter_flow_test_")
        await self.client.drop_database(self.database)
        await self.client.close()
        mongo.client, mongo._active_connection_settings = self.previous

    async def wait_status(self, *states):
        async with asyncio.timeout(90):
            while True:
                job = await service.latest(self.novel)
                if job and job["status"] in states:
                    worker = service._workers.get(job["id"])
                    if worker:
                        await asyncio.gather(worker, return_exceptions=True)
                    return job
                await asyncio.sleep(.05)

    async def test_real_flows_save_every_stage_pause_resume_and_never_overwrite(self):
        self.gate_task = "write_chapter_by_ai"
        self.gate.clear()
        started = await service.start(self.novel, service.Start(include_optional=False), "full-flow")
        async with asyncio.timeout(90):
            while not self.entered.is_set():
                job = await service.latest(self.novel)
                self.assertNotEqual(job["status"], "failed", job.get("error"))
                await asyncio.sleep(.05)
        await service.control(self.novel, started["id"], "pause")
        self.gate.set()
        job = await self.wait_status("paused", "failed")
        self.assertEqual(job["status"], "paused", job["error"])
        self.assertTrue(job["has_saved_candidate"])
        self.assertEqual(await self.db.chapters.count_documents({"content": {"$ne": ""}}), 0)
        original_calls = len(self.calls)
        await service.control(self.novel, started["id"], "resume")
        job = await self.wait_status("completed", "failed")
        self.assertEqual(job["status"], "completed", job["error"])
        self.assertEqual(len(self.calls), original_calls)
        self.assertEqual(await self.db.chapters.count_documents({}), 1)
        self.assertEqual(await self.db.first_chapter_reports.count_documents({}), 1)
        chapter = await self.db.chapters.find_one({})
        self.assertTrue(chapter["content"].strip())
        for name in ("factions", "characters", "volumes", "arcs", "scenes", "chapter_revisions"):
            self.assertGreater(await self.db[name].count_documents({}), 0, name)
        page = await service.logs(self.novel, started["id"], limit=100)
        self.assertTrue(any(r.get("label") == "核对剧情与事实一致性" for r in page["items"]))
        self.assertTrue(any(r.get("label") == "保存并核对" and r["state"] == "done" for r in page["items"]))
        await service.start(self.novel, service.Start(include_optional=False), "repeat")
        repeated = await self.wait_status("completed", "failed")
        self.assertEqual(repeated["status"], "completed", repeated["error"])
        self.assertEqual(len(self.calls), original_calls)
        self.assertEqual((await self.db.chapters.find_one({}))["content"], chapter["content"])

    async def test_optional_resources_and_failed_step_resume_do_not_repeat_saved_stages(self):
        self.fail_task = "plan_scenes_by_ai"
        started = await service.start(self.novel, service.Start(include_optional=True), "full-flow-with-optional")
        failed = await self.wait_status("failed", "completed")
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["error"]["code"], "TEST_NETWORK", failed["error"])
        counts = {name: await self.db[name].count_documents({}) for name in ("factions", "characters", "character_relations", "world_rules", "items", "volumes", "arcs")}
        self.assertGreater(counts["character_relations"], 0)
        self.assertEqual(counts["world_rules"], 5)
        self.assertEqual(counts["items"], 6)
        core_calls = [call for call in self.calls if call[0] in {"create_factions_by_ai", "create_characters_by_ai"}]
        await service.control(self.novel, started["id"], "resume")
        completed = await self.wait_status("completed", "failed")
        self.assertEqual(completed["status"], "completed", completed["error"])
        self.assertEqual(core_calls, [call for call in self.calls if call[0] in {"create_factions_by_ai", "create_characters_by_ai"}])
        self.assertEqual(counts, {name: await self.db[name].count_documents({}) for name in counts})
        for task in {call[0] for call in self.calls}:
            self.assertIn(task, self.reviewed_tasks)


if __name__ == "__main__":
    unittest.main()
