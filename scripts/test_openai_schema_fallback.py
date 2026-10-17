"""Regression checks for free-form character data and strict OpenAI schemas."""
import asyncio
from copy import deepcopy
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pydantic import BaseModel, ConfigDict
from openai.types.responses import Response, ResponseOutputMessage
from backend.llm.config import LLMProviderConfig
from backend.llm.exceptions import LLMSchemaError
from backend.llm.models import LLMRequest
from backend.llm.openai_client import build_openai_parameters, requires_openai_json_object
from backend.llm.responses_client import ResponsesClient


class Profile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    extra: dict[str, object]


class ClosedProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str


class SchemaFallbackTests(unittest.TestCase):
    def config(self, mode="responses"):
        return LLMProviderConfig(api_key="offline-test", default_model="test-model", supports_json_schema=True, api_mode=mode)

    def test_dict_and_model_requests_use_json_object_in_both_apis(self):
        original = Profile.model_json_schema()
        before = deepcopy(original)
        for mode in ("responses", "chat_completions"):
            for explicit in (False, True):
                with self.subTest(mode=mode, explicit=explicit):
                    request = LLMRequest(system_prompt="Keep author instructions.",
                        json_output_schema=original, messages=[{"role": "user", "content": "Create a character."}])
                    params = build_openai_parameters(request, self.config(mode), schema=Profile if explicit else None)
                    fmt = params["text"]["format"] if mode == "responses" else params["response_format"]
                    self.assertEqual(fmt, {"type": "json_object"})
                    messages = params["input" if mode == "responses" else "messages"]
                    self.assertIn("Keep author instructions.", messages[0]["content"])
                    transmitted = json.loads(messages[0]["content"].split("JSON Schema:\n", 1)[1])
                    self.assertEqual(transmitted, original)
                    self.assertEqual(messages[-1]["content"], "Create a character.")
        self.assertEqual(original, before)

    def test_current_volume_character_flow_uses_fallback(self):
        path = Path(__file__).resolve().parents[1] / "backend/services/task_flows/defaults/create_characters_for_volume_by_ai.json"
        flow = json.loads(path.read_text(encoding="utf-8"))
        node = next(node for node in flow["nodes"] if node["id"] == "generate")
        schema = {"type": "object", "properties": node["outputs"]}
        params = build_openai_parameters(LLMRequest(json_output_schema=schema), self.config())
        self.assertEqual(params["text"]["format"], {"type": "json_object"})
        self.assertIn('"extra"', params["input"][0]["content"])

    def test_closed_schema_keeps_strict_native_output(self):
        for explicit in (True, False):
            params = build_openai_parameters(LLMRequest(json_output_schema=ClosedProfile.model_json_schema()),
                self.config(), schema=ClosedProfile if explicit else None)
            fmt = params["text"]["format"]
            self.assertEqual(fmt["type"], "json_schema")
            self.assertTrue(fmt["strict"])
            self.assertIs(fmt["schema"]["additionalProperties"], False)

    def test_nested_free_values_and_typed_maps_are_detected(self):
        for value in ({}, True, {"type": "object", "additionalProperties": True},
                      {"type": "object", "additionalProperties": {"type": "string"}},
                      {"type": "object", "patternProperties": {"^x": {"type": "number"}}}):
            with self.subTest(value=value):
                self.assertTrue(requires_openai_json_object({"type": "object", "properties": {
                    "profiles": {"type": "array", "items": {"anyOf": [value, {"type": "null"}]}}}}))

    def test_definition_refs_are_inspected_but_business_defaults_are_not(self):
        self.assertTrue(requires_openai_json_object({"$ref": "#/$defs/Free", "$defs": {"Free": {}}}))
        self.assertFalse(requires_openai_json_object({"type": "object", "properties": {},
            "default": {"additionalProperties": True}, "examples": [{}]}))

    def test_without_native_schema_capability_no_new_format_is_forced(self):
        params = build_openai_parameters(LLMRequest(json_output_schema=Profile.model_json_schema()),
            LLMProviderConfig(default_model="test-model", api_mode="responses"))
        self.assertNotIn("text", params)

    def test_json_fallback_preserves_extra_and_rejects_invalid_output(self):
        async def run(raw):
            client = ResponsesClient(self.config())
            message = ResponseOutputMessage(id="msg_test", type="message", role="assistant", status="completed",
                content=[{"type": "output_text", "text": raw, "annotations": []}])
            response = Response.model_construct(id="resp_test", model="test-model", status="completed",
                output=[message], error=None, usage=None)
            client._create_response = AsyncMock(return_value=response)
            try:
                return await client.schema_generate(LLMRequest(messages=[{"role": "user", "content": "Test"}]), Profile)
            finally:
                await client._client.close()
        raw = '{"name":"Test","extra":{"custom_fact":{"source":"author","tags":["test"]}}}'
        result = asyncio.run(run(raw))
        self.assertEqual(json.loads(result.content), json.loads(raw))
        with self.assertRaises(LLMSchemaError):
            asyncio.run(run('{"extra":{}}'))


if __name__ == "__main__":
    unittest.main()
