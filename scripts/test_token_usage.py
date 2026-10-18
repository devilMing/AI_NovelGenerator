"""Token accounting regression tests. No external model or formal novel data is used."""
import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import httpx2
from backend.services import token_usage as usage
from backend.llm.logger import LLMCall, log_http_request, log_http_response, observe_token_usage
from backend.llm.models import LLMResponse, TokenUsage
from backend.llm.provider_probe import observe_provider_response
from backend.runtime import bind_log_context
from backend.api.default_routers import token_usage_router


class UsageTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = usage.UsageStore(Path(self.tmp.name) / "tokens.sqlite3")
        self.patch = patch.object(usage, "get_store", return_value=self.store)
        self.patch.start()
        self.router_patch = patch.object(token_usage_router, "get_store", return_value=self.store)
        self.router_patch.start()
        self.addCleanup(self.cleanup)

    def cleanup(self):
        self.patch.stop(); self.router_patch.stop(); self.store.close(); self.tmp.cleanup()

    async def send(self, payload=None, code=200, stream=False, model="model-a"):
        request = httpx2.Request("POST", "https://example.test/v1/responses", json={"model": model, "input": "private prompt", "api_key": "secret"})
        await log_http_request(request)
        response = httpx2.Response(code, request=request, json=payload or {}, headers={"content-type": "text/event-stream" if stream else "application/json"})
        await log_http_response(response)

    def report(self, **kwargs):
        self.store.flush()
        return self.store.query(**kwargs)

    def test_native_counter_normalization_preserves_unknown_zero_and_subsets(self):
        self.assertEqual(usage.extract_usage({"usage": None}), {})
        self.assertEqual(usage.extract_usage({"usage": {"input_tokens": 0, "output_tokens": 0}}), {"input_tokens": 0, "output_tokens": 0})
        result = usage.extract_usage({"usage": {"prompt_tokens": 10, "completion_tokens": 8, "total_tokens": 18, "prompt_tokens_details": {"cached_tokens": 4}, "completion_tokens_details": {"reasoning_tokens": 5}}})
        self.assertEqual(result, dict(zip(usage.COUNTERS, [10, 8, 18, 4, 5])))
        result = usage.extract_usage({"usage": {"input_tokens": 10, "output_tokens": 8, "cache_read_input_tokens": 20, "cache_creation_input_tokens": 5}})
        self.assertEqual(usage.finalize_counts({**result, "usage_final": True})["total_tokens"], 43)
        result = usage.extract_usage({"usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 8, "thoughtsTokenCount": 5, "totalTokenCount": 23}})
        self.assertEqual(result["output_tokens"], 13)
        result = usage.extract_usage(NS(usage=NS(total_input_tokens=10, total_output_tokens=8, total_thought_tokens=5, total_tokens=23)))
        self.assertEqual(result["output_tokens"], 13)
        self.assertNotIn("input_tokens", usage.extract_usage({"usage": {"input_tokens": -1, "output_tokens": True}}))

    async def test_stream_cumulative_usage_and_final_summary_do_not_duplicate(self):
        with bind_log_context(novel_id="book", novel_title="小说", volume_id="vol", task="write_chapter_by_ai", node_label="正文"):
            with LLMCall("provider-a", "model-a", "stream") as call:
                await self.send(stream=True)
                observe_provider_response(NS(type="response.created", response=NS(usage=None)), "responses")
                self.assertEqual(self.report()["summary"]["pending"], 1)
                self.assertIsNone(self.report()["items"][0]["total_tokens"])
                for count in (2, 5, 5):
                    observe_token_usage({"type": "response.completed", "response": {"status": "completed", "usage": {"input_tokens": 10, "output_tokens": count, "total_tokens": 10 + count}}})
                call.usage = TokenUsage(input_tokens=10, output_tokens=5, total_tokens=15)
                call.result = LLMResponse(usage=call.usage)
        report = self.report(group="novel")
        self.assertEqual(report["summary"]["requests"], 1)
        self.assertEqual(report["summary"]["total_tokens"], 15)
        self.assertEqual(report["groups"][0]["name"], "小说")
        self.assertNotIn("private prompt", json.dumps(report))
        self.assertNotIn("secret", json.dumps(report))

    async def test_retries_and_tool_rounds_count_actual_sends_and_keep_known_failures(self):
        with LLMCall("provider-a", "model-a", "function_call"):
            await self.send({"error": "busy"}, code=429)
            await self.send(stream=True)
            observe_token_usage({"type": "response.completed", "response": {"status": "completed", "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}}})
            await self.send(stream=True)
            observe_token_usage({"type": "response.completed", "response": {"status": "completed", "usage": {"input_tokens": 20, "output_tokens": 6, "total_tokens": 26}}})
        report = self.report()
        self.assertEqual(report["summary"]["requests"], 3)
        self.assertEqual(report["summary"]["total_tokens"], 41)
        self.assertEqual(report["summary"]["unknown"], 1)
        self.assertEqual([row["status"] for row in report["items"]], ["completed", "completed", "failed"])
        with self.assertRaises(ValueError):
            with LLMCall("provider-a", "model-a", "schema"):
                await self.send({"usage": {"input_tokens": 3, "output_tokens": 4, "total_tokens": 7}})
                raise ValueError("invalid generated schema")
        report = self.report()
        self.assertEqual(report["summary"]["total_tokens"], 48)
        self.assertEqual(report["items"][0]["status"], "failed")

    async def test_transport_retries_cancelled_partial_and_restart(self):
        with self.assertRaises(asyncio.CancelledError):
            with LLMCall("provider-a", "model-a", "stream"):
                request = httpx2.Request("POST", "https://example.test/responses", json={})
                await log_http_request(request)  # transport failed before any response
                await self.send(stream=True)
                observe_token_usage({"type": "message_start", "message": {"usage": {"input_tokens": 9}}})
                raise asyncio.CancelledError()
        report = self.report()
        self.assertEqual([row["status"] for row in report["items"]], ["cancelled", "failed"])
        self.assertEqual(report["items"][0]["input_tokens"], 9)
        self.assertIsNone(report["items"][0]["total_tokens"])
        call = LLMCall("provider-a", "model-a", "stream")
        call.__enter__()
        await self.send(stream=True)
        self.store.flush()
        # Simulate abrupt process death by reopening the file with an unfinished request.
        self.store.close()
        self.store = usage.UsageStore(self.store.path)
        self.patch.stop(); self.patch = patch.object(usage, "get_store", return_value=self.store); self.patch.start()
        self.assertEqual(self.report()["items"][0]["status"], "interrupted")
        # Restore context without publishing a synthetic completion after the simulated restart.
        from backend.llm.logger import _current_call
        from backend.runtime import log_context
        _current_call.reset(call.call_token); log_context.reset(call.context_token)

    async def test_concurrent_contexts_filters_and_pagination(self):
        async def run(book, model, tokens):
            with bind_log_context(novel_id=book, task="test"):
                with LLMCall("provider-a", model, "text"):
                    await asyncio.sleep(0)
                    await self.send({"usage": {"input_tokens": tokens, "output_tokens": 1, "total_tokens": tokens + 1}}, model=model)
        await asyncio.gather(*(run(f"book-{index % 2}", f"m-{index % 2}", index) for index in range(6)))
        page = self.report(novel_id="book-1", limit=2)
        next_page = self.report(novel_id="book-1", before=page["before"], limit=2)
        self.assertEqual(page["summary"]["requests"], 3)
        self.assertEqual(page["summary"], next_page["summary"])
        self.assertEqual(len(page["items"]) + len(next_page["items"]), 3)
        self.assertTrue(all(row["model"] == "m-1" for row in page["items"]))
        self.assertEqual(self.report(model="' OR 1=1 --")["summary"]["requests"], 0)
        self.assertEqual(self.report(start="2099-01-01T00:00:00.000+00:00")["items"], [])

    async def test_anthropic_partial_events_and_real_zero(self):
        with LLMCall("claude", "claude", "stream"):
            await self.send(stream=True)
            observe_token_usage({"type": "message_start", "message": {"usage": {"input_tokens": 10, "output_tokens": 0, "cache_read_input_tokens": 20, "cache_creation_input_tokens": 5}}})
            observe_token_usage({"type": "message_delta", "usage": {"output_tokens": 8}, "delta": {"stop_reason": "end_turn"}})
        with LLMCall("p", "m", "text"):
            await self.send({"usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}})
        report = self.report()
        self.assertEqual(report["summary"]["total_tokens"], 43)
        self.assertEqual(report["summary"]["confirmed"], 2)
        self.assertEqual(report["items"][1]["total_source"], "sum")

    def test_history_import_is_idempotent_and_survives_log_deletion(self):
        path = Path(self.tmp.name) / "events.sqlite3"
        with sqlite3.connect(path) as db:
            db.execute("CREATE TABLE logs(id INTEGER PRIMARY KEY,time TEXT,kind TEXT,message TEXT,metadata TEXT,payload TEXT,source TEXT)")
            def add(identity, message, sequence=None, payload=None):
                meta = {"call_id": "old-call", "provider": "old", "model": "old-model"}
                if sequence:
                    meta["send"] = sequence
                db.execute("INSERT INTO logs VALUES(?,?,?,?,?,?,?)", (identity, "2026-10-17T16:00:00.000+00:00", "llm_request" if sequence else "system", message, json.dumps(meta), json.dumps(payload or {}), "llm"))
            add(1, "send=1", 1)
            add(2, "[LLM 结束] status=completed usage={'input_tokens': 3, 'output_tokens': 4, 'total_tokens': 7} finish=stop")
            # Console imports retain the full payload, but their list summary is truncated.
            db.execute("UPDATE logs SET kind='legacy',payload=message,message='truncated header' WHERE id=2")
        db.close()
        self.store.import_history(path); self.store.import_history(path)
        report = self.report(bucket="day", offset_minutes=480)
        self.assertEqual(report["summary"]["requests"], 1)
        self.assertEqual(report["summary"]["total_tokens"], 7)
        self.assertEqual(report["trend"][0]["period"], "2026-10-18")
        self.assertEqual(report["items"][0]["origin"], "history")
        path.unlink()
        self.store.close(); self.store = usage.UsageStore(self.store.path)
        self.assertEqual(self.report()["summary"]["total_tokens"], 7)

    async def test_interrupted_claude_initial_usage_is_not_a_final_total(self):
        with self.assertRaises(asyncio.CancelledError):
            with LLMCall("claude", "claude", "stream"):
                await self.send(stream=True)
                observe_token_usage({"type": "message_start", "message": {"usage": {"input_tokens": 10, "output_tokens": 0}}})
                raise asyncio.CancelledError()
        report = self.report()
        self.assertEqual(report["summary"]["input_tokens"], 10)
        self.assertIsNone(report["items"][0]["total_tokens"])
        self.assertEqual(report["summary"]["unknown"], 1)

    async def test_scope_follows_saved_hierarchy_without_prompt_inference(self):
        from bson import ObjectId
        from unittest.mock import AsyncMock
        from backend.db import mongo
        ids = {kind: str(ObjectId()) for kind in ("novel", "volume", "arc", "scene", "chapter")}
        rows = {
            "novels": {"title": "小说"}, "volumes": {"title": "卷一"},
            "arcs": {"title": "篇章", "volume_id": ObjectId(ids["volume"])},
            "scenes": {"title": "场景", "arc_id": ObjectId(ids["arc"])},
            "chapters": {"title": "第一章", "scene_id": ObjectId(ids["scene"])},
        }
        db = {name: NS(find_one=AsyncMock(return_value=row)) for name, row in rows.items()}
        with patch.object(mongo, "client", object()), patch.object(mongo, "get_database", return_value=db):
            result = await usage.resolve_scope({"novel_id": ids["novel"], "target_type": "chapter", "target_id": ids["chapter"]})
        self.assertEqual(result["volume_id"], ids["volume"])
        self.assertEqual(result["chapter_title"], "第一章")
        self.assertEqual(result["novel_title"], "小说")

    def test_history_multisend_aggregate_is_not_assigned_to_one_request(self):
        path = Path(self.tmp.name) / "events.sqlite3"
        db = sqlite3.connect(path)
        db.execute("CREATE TABLE logs(id INTEGER PRIMARY KEY,time TEXT,kind TEXT,message TEXT,metadata TEXT,payload TEXT,source TEXT)")
        for identity, kind, sequence, message in ((1, "llm_request", 1, "request"), (2, "llm_request", 2, "request"), (3, "system", None, "[LLM 结束] status=completed usage={'input_tokens': 30, 'output_tokens': 20, 'total_tokens': 50} finish=stop")):
            meta = {"call_id": "multi", "provider": "p", "model": "m"}
            if sequence: meta["send"] = sequence
            db.execute("INSERT INTO logs VALUES(?,?,?,?,?,?,?)", (identity, "2026-10-17T16:00:00.000+00:00", kind, message, json.dumps(meta), "{}", "llm"))
        db.commit(); db.close()
        self.store.import_history(path)
        report = self.report()
        self.assertEqual(report["summary"]["requests"], 2)
        self.assertEqual(report["summary"]["total_tokens"], 0)
        self.assertEqual(report["summary"]["unknown"], 2)

    async def test_route_validation_and_paging_contract(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from backend.api.default_routers.token_usage_router import router
        from backend.api.error_protocol import DomainApiError, domain_api_exception_handler
        app = FastAPI(); app.include_router(router); app.add_exception_handler(DomainApiError, domain_api_exception_handler)
        with TestClient(app) as client:
            for query in ("group=bad", "limit=0", "before=-1", "offset_minutes=900", "start=bad", "start=2026-10-19&end=2026-10-18"):
                self.assertEqual(client.get(f"/api/runtime/token-usage?{query}").status_code, 422, query)
            result = client.get("/api/runtime/token-usage")
            self.assertEqual(result.status_code, 200)
            self.assertEqual(result.json()["summary"]["requests"], 0)


if __name__ == "__main__":
    unittest.main()
