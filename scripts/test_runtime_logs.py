"""Runtime logs use temporary SQLite files; HTTP/model calls are stubbed."""
import asyncio
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.services import runtime_logs as logs
from backend.services import token_usage
from backend.llm.logger import LLMCall, log_http_request, log_http_response
from backend.llm.models import LLMResponse, TokenUsage
from backend.runtime import bind_log_context
from backend.api.default_routers import runtime_router
from backend.api.error_protocol import DomainApiError, domain_api_exception_handler


class RuntimeLogTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "events.sqlite3"
        self.store = logs.LogStore(self.path)
        self.store_patch = patch.object(logs, "get_store", return_value=self.store)
        self.store_patch.start()
        self.usage_store = token_usage.UsageStore(Path(self.directory.name) / "tokens.sqlite3")
        self.usage_patch = patch.object(token_usage, "get_store", return_value=self.usage_store)
        self.usage_patch.start()
        self.router_patch = patch.object(runtime_router, "get_store", return_value=self.store)
        self.router_patch.start()
        self.handler = logs.RuntimeLogHandler()
        self.logger = logging.getLogger("llm")
        self.old_level = self.logger.level
        self.logger.setLevel(logging.INFO)
        self.logger.addHandler(self.handler)
        self.addCleanup(self.cleanup)

    def cleanup(self):
        self.logger.removeHandler(self.handler)
        self.logger.setLevel(self.old_level)
        self.store_patch.stop()
        self.usage_patch.stop()
        self.usage_store.close()
        self.router_patch.stop()
        self.store.close()
        self.directory.cleanup()

    def emit(self, message, kind="system", level=logging.INFO, payload=None):
        self.logger.log(level, message, extra={"log_kind": kind, "log_payload": payload or message})
        self.store.flush()

    async def test_real_http_hooks_and_stream_buffer_preserve_text_and_redact_credentials(self):
        import httpx2
        from pydantic import BaseModel
        class Raw(BaseModel):
            access_token: str
            text: str
        with bind_log_context(request_id="request-123", task="write_chapter_by_ai"):
            with LLMCall("local", "model-one", "stream") as call:
                request = httpx2.Request("POST", "https://example.test/responses?api_key=hidden-query", json={"messages": [{"role": "user", "content": "实际提示词"}], "api_key": "hidden-key", "reasoning": {"effort": "high"}})
                await log_http_request(request)
                response = httpx2.Response(200, request=request, json={"content": "模型回复", "refresh_token": "hidden-refresh", "output": [{"type": "reasoning", "text": "private-thought"}]})
                await log_http_response(response)
                call.chunks.append("第一段")
                call.chunks.append("第二段")
                self.assertEqual("".join(call.chunks), "第一段第二段")
                call.result = LLMResponse(content="第一段第二段", model="model-one", raw_response={"credentials": Raw(access_token="hidden-nested", text="公开内容").model_dump()})
                call.usage = TokenUsage(input_tokens=10, output_tokens=20, total_tokens=30)
        self.store.flush()
        page = self.store.query(category="llm", q="request-123", limit=200)
        details = [self.store.detail(row["id"]) for row in page["items"]]
        serialized = json.dumps(details, ensure_ascii=False)
        for secret in ("hidden-query", "hidden-key", "hidden-refresh", "private-thought", "hidden-nested"):
            self.assertNotIn(secret, serialized)
        self.assertIn("实际提示词", serialized)
        self.assertIn("high", serialized)
        self.assertIn("第一段第二段", serialized)
        self.assertIn("total_tokens", serialized)
        self.assertTrue({"llm_request", "llm_delta", "llm_response"}.issubset({row["kind"] for row in details}))
        self.emit("schema request", kind="llm_request", payload={"schema": {"type": ["object", "null"]}})
        schema_row = self.store.query(q="schema request")["items"][0]
        self.assertIn('"object"', self.store.detail(schema_row["id"])["payload"])

    async def test_history_filters_keyset_pagination_and_reconnect_have_no_gaps(self):
        self.emit("initial")
        cursor = self.store.query()["cursor"]
        for index in range(7):
            self.emit(f"match-{index}", level=logging.ERROR)
        after = cursor
        collected = []
        while True:
            page = self.store.query(after=after, level="ERROR", q="match-", limit=2)
            collected.extend(row["id"] for row in page["items"])
            after = page["cursor"]
            if not page["has_more"]:
                break
        self.assertEqual(len(collected), 7)
        self.assertEqual(len(set(collected)), 7)
        self.assertEqual(self.store.query(after=after)["items"], [])
        first = self.store.query(q="match-", limit=3)
        second = self.store.query(q="match-", before=first["before"], limit=10)
        self.assertEqual(len(first["items"]) + len(second["items"]), 7)
        self.assertEqual(self.store.query(start="2099-01-01T00:00:00.000+00:00")["items"], [])
        self.assertEqual(self.store.query(q="' OR 1=1 --")["items"], [])

    async def test_restart_persists_details_and_legacy_import_is_idempotent(self):
        file = Path(self.directory.name) / "backend.out.log"
        file.write_text('2026-10-18 08:00:00 | ERROR: | llm | file.py:1 | request_id=abc | 旧错误\nTraceback: retained detail\napi_key="legacy-secret"\n', encoding="utf-8")
        self.store.import_legacy(Path(self.directory.name))
        self.store.import_legacy(Path(self.directory.name))
        self.assertEqual(len(self.store.query()["items"]), 1)
        self.store.close()
        reopened = logs.LogStore(self.path)
        try:
            row = reopened.query()["items"][0]
            detail = reopened.detail(row["id"])
            self.assertIn("retained detail", detail["payload"])
            self.assertNotIn("legacy-secret", detail["payload"])
        finally:
            reopened.close()

    async def test_long_payload_is_explicitly_bounded_and_queries_do_not_log_themselves(self):
        self.emit("long", payload={"text": "字" * (logs.MAX_TEXT + 100)})
        item = self.store.query()["items"][0]
        self.assertTrue(item["truncated"])
        self.assertLessEqual(len(self.store.detail(item["id"])["payload"]), logs.MAX_TEXT)
        self.emit("GET /api/runtime/logs?after=1")
        self.assertEqual(len(self.store.query()["items"]), 1)
        sensitive = 'mongodb://person:db-secret@example.test/db "token":"opaque-secret" "encrypted_content":"private-encrypted"'
        cleaned = logs.redact(sensitive)
        for secret in ("db-secret", "opaque-secret", "private-encrypted"):
            self.assertNotIn(secret, cleaned)

    async def test_query_http_contract_and_errors(self):
        from fastapi import FastAPI
        from httpx import AsyncClient, ASGITransport
        from backend.api.default_routers.runtime_router import router
        app = FastAPI()
        app.add_exception_handler(DomainApiError, domain_api_exception_handler)
        app.include_router(router)
        self.emit("查找这条", payload={"text": "详情"})
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            page = (await client.get("/api/runtime/logs", params={"q": "查找"})).json()
            identity = page["items"][0]["id"]
            self.assertEqual((await client.get(f"/api/runtime/logs/{identity}")).status_code, 200)
            self.assertEqual((await client.get("/api/runtime/logs", params={"limit": 1000})).status_code, 422)
            self.assertEqual((await client.get("/api/runtime/logs", params={"after": 2, "before": 3})).status_code, 422)
            self.assertEqual((await client.get("/api/runtime/logs/999999")).status_code, 404)

    async def test_failed_log_write_does_not_stop_following_records_and_reports_loss(self):
        self.store.submit((datetime.now(timezone.utc).isoformat(), "INFO", "test", "system", "invalid", "{}", {"invalid": True}, 0, None))
        self.store.flush()
        self.assertTrue(self.store.failure)
        self.emit("later valid record")
        page = self.store.query()
        self.assertEqual(page["dropped"], 1)
        self.assertEqual(page["items"][0]["message"], "later valid record")


if __name__ == "__main__":
    unittest.main()
