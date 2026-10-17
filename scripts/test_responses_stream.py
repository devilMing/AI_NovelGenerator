"""Offline regression tests for streamed output assembly; no inference requests."""
import asyncio
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from openai.types.responses import Response, ResponseOutputMessage, ResponseFunctionToolCall, ResponseReasoningItem
from backend.llm.config import LLMProviderConfig
from backend.llm.responses_client import ResponsesClient
from backend.llm.exceptions import LLMConnectionError, LLMResponseError, LLMSchemaError
from backend.llm.models import LLMRequest, LLMFunctionCallProbe, LLMToolTurn, LLMToolCall
from backend.llm.logger import LLMCall


class Events:
    def __init__(self, values): self.values = iter(values)
    async def __aenter__(self): return self
    async def __aexit__(self, *_args): pass
    def __aiter__(self): return self
    async def __anext__(self):
        try: return next(self.values)
        except StopIteration: raise StopAsyncIteration


def message(text):
    return ResponseOutputMessage(id="msg_test", type="message", role="assistant", status="completed",
        content=[{"type": "output_text", "text": text, "annotations": []}])


def terminal(output=None, status="completed"):
    return SimpleNamespace(type=f"response.{status}", response=Response.model_construct(
        id="resp_test", model="test-model", status=status, output=output or [], error=None,
        incomplete_details=SimpleNamespace(reason="max_output_tokens"), usage=None))


def done(index, item):
    return SimpleNamespace(type="response.output_item.done", output_index=index, item=item)


class StreamTests(unittest.TestCase):
    def collect(self, events):
        async def run():
            client = ResponsesClient(LLMProviderConfig(auth_mode="chatgpt", chatgpt_account_id="offline"))
            client._client.responses.create = AsyncMock(return_value=Events(events))
            try: return await client._create_response({"model": "test-model"}, stream=True)
            finally: await client._client.close()
        return asyncio.run(run())

    def test_json_survives_empty_terminal_output(self):
        response = self.collect([done(0, message('{"code":"ok"}')), terminal()])
        self.assertEqual(response.output_text, '{"code":"ok"}')

    def test_tools_and_final_reasoning_keep_output_order(self):
        reasoning = ResponseReasoningItem(id="reasoning", type="reasoning", summary=[], encrypted_content="final-test-cipher")
        tool = ResponseFunctionToolCall(id="tool", type="function_call", call_id="call_test", name="probe", arguments='{"ok":true}')
        response = self.collect([done(1, tool), done(0, reasoning), terminal()])
        self.assertEqual([item.type for item in response.output], ["reasoning", "function_call"])
        self.assertEqual(response.output[0].encrypted_content, "final-test-cipher")
        self.assertEqual(response.output[1].arguments, '{"ok":true}')

    def test_full_terminal_output_is_not_duplicated(self):
        item = message("ok")
        response = self.collect([done(0, item), terminal([item])])
        self.assertEqual(len(response.output), 1)
        self.assertEqual(response.output_text, "ok")

    def test_items_without_terminal_are_not_success(self):
        with self.assertRaises(LLMConnectionError):
            self.collect([done(0, message("partial"))])

    def test_incomplete_status_is_preserved(self):
        response = self.collect([done(0, message("partial")), terminal(status="incomplete")])
        client = ResponsesClient.__new__(ResponsesClient)
        client.provider_name = "offline"
        with self.assertRaises(LLMSchemaError): client._check_response(response, "test-model")

    def test_refusal_is_still_rejected(self):
        item = ResponseOutputMessage(id="msg_refusal", type="message", role="assistant", status="completed",
            content=[{"type": "refusal", "refusal": "test refusal"}])
        response = self.collect([done(0, item), terminal()])
        client = ResponsesClient.__new__(ResponsesClient)
        client.provider_name = "offline"
        with self.assertRaises(LLMResponseError): client._check_response(response, "test-model")

    def test_probe_followup_is_a_user_instruction_after_tool_results(self):
        async def run():
            client = ResponsesClient(LLMProviderConfig(auth_mode="chatgpt", chatgpt_account_id="offline"))
            client.tool_turn = AsyncMock(side_effect=[
                LLMToolTurn(calls=[LLMToolCall(id="call_test", name="probe", arguments={"ok": True})],
                    history=[{"role": "user", "content": "Call probe; do not answer."}]),
                LLMToolTurn(content="PROBE_OK", finish_reason="completed")])
            probe = LLMFunctionCallProbe(tool_name="probe", tool_description="test", parameters_schema={},
                expected_arguments={"ok": True}, tool_result={"ok": True},
                final_response_prompt="Now reply PROBE_OK", expected_final_text="PROBE_OK")
            try:
                result = await client.function_call_probe(LLMRequest(), probe)
                history = client.tool_turn.call_args_list[1].kwargs["history"]
                self.assertEqual(history[-2]["type"], "function_call_output")
                self.assertEqual(history[-1], {"role": "user", "content": "Now reply PROBE_OK"})
                self.assertEqual(result.content, "PROBE_OK")
            finally: await client._client.close()
        asyncio.run(run())


if __name__ == "__main__": unittest.main()
