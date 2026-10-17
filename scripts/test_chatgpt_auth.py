"""Offline regression checks: no real account, tokens, or inference requests."""
import asyncio
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch, AsyncMock
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.llm import chatgpt_auth as auth
from backend.llm.config import LLMProviderConfig
from backend.llm.factory import create_llm_client_from_config
from backend.llm.base_client import apply_request_defaults
from backend.llm.models import LLMRequest
from backend.llm.openai_client import build_openai_parameters
from backend.api.default_routers.chatgpt_auth_router import router
from fastapi import FastAPI
from fastapi.testclient import TestClient
from cryptography.hazmat.primitives.asymmetric import rsa
import jwt


class AuthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = patch.object(auth, "STORE", Path(self.temp.name) / "chatgpt.bin")
        self.store.start()
        auth.cancel()

    def tearDown(self):
        auth.cancel()
        self.store.stop()
        self.temp.cleanup()

    def test_encrypted_project_store_roundtrip(self):
        record = {"host_id": "test", "accounts": {"x": {"access_token": "secret-placeholder"}}}
        auth._write(record)
        self.assertEqual(auth._read(), record)
        if sys.platform == "win32":
            self.assertNotIn(b"secret-placeholder", auth.STORE.read_bytes())

    def test_pkce_state_and_stable_host(self):
        first = parse_qs(urlsplit(auth.start()).query)
        self.assertEqual(first["client_id"], ["dynamic_agent_client"])
        self.assertEqual(first["resource"], [auth.RESOURCE])
        self.assertEqual(first["code_challenge_method"], ["S256"])
        self.assertTrue(first["redirect_uri"][0].startswith("http://127.0.0.1:"))
        auth.cancel()
        second = parse_qs(urlsplit(auth.start()).query)
        self.assertEqual(first["ext_agent_host_id"], second["ext_agent_host_id"])
        self.assertNotEqual(first["state"], second["state"])
        self.assertNotEqual(first["code_challenge"], second["code_challenge"])

    def test_callback_rejects_state_before_exchange(self):
        with patch.object(auth, "_exchange") as exchange:
            with self.assertRaises(ValueError):
                auth._finish({"state": "wrong"}, {"state": "right"})
            exchange.assert_not_called()

    def test_validate_signature_audience_and_nonce(self):
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
        jwk["kid"] = "unit-test"
        claims = {"iss": auth.ISSUER, "aud": "client-test", "sub": "test-subject",
                  "nonce": "correct", "exp": int(time.time()) + 600}
        token = jwt.encode(claims, key, algorithm="RS256", headers={"kid": "unit-test"})
        fake_response = unittest.mock.Mock()
        fake_response.json.return_value = {"keys": [jwk]}
        with patch.object(auth, "_metadata", return_value={"jwks_uri": "https://auth.openai.com/test"}), \
             patch.object(auth.httpx.Client, "get", return_value=fake_response):
            self.assertEqual(auth._validate_id({"id_token": token}, "client-test", "correct")["sub"], "test-subject")
            with self.assertRaises(ValueError):
                auth._validate_id({"id_token": token}, "client-test", "wrong")
            with self.assertRaises(jwt.InvalidAudienceError):
                auth._validate_id({"id_token": token}, "wrong-client", "correct")

    def test_factory_pins_api_host_and_uses_lazy_token(self):
        config = LLMProviderConfig(type="openai", auth_mode="chatgpt", chatgpt_account_id="test",
                                   base_url="https://untrusted.invalid", api_mode="chat_completions", enabled=True)
        client = create_llm_client_from_config(config)
        self.assertEqual(str(client._client.base_url), "https://api.openai.com/v1/")
        self.assertEqual(client.config.resolved_api_mode, "responses")
        self.assertIsNotNone(client._client._api_key_provider)
        asyncio.run(client._client.close())

    def test_plan_permission_required(self):
        auth._write({"host_id": "test", "accounts": {"a": {"access_token": "not-a-real-token", "scopes": [], "expires_at": time.time()+600}}})
        with self.assertRaisesRegex(ValueError, "尚未授权"):
            asyncio.run(auth.access_token("a"))

    def test_default_reasoning_is_not_forced(self):
        config = LLMProviderConfig(type="openai", default_model="gpt-5.6-terra", auth_mode="chatgpt")
        request = apply_request_defaults(LLMRequest(messages=[{"role": "user", "content": "offline test"}]), config)
        params = build_openai_parameters(request, config)
        self.assertNotIn("reasoning", params)

    def test_reasoning_default_and_request_override_reach_responses(self):
        config = LLMProviderConfig(type="openai", default_model="gpt-5.6-terra", auth_mode="chatgpt", reasoning_effort="high")
        request = apply_request_defaults(LLMRequest(), config)
        self.assertEqual(build_openai_parameters(request, config)["reasoning"], {"effort": "high"})
        overridden = apply_request_defaults(LLMRequest(reasoning_effort="low"), config)
        self.assertEqual(build_openai_parameters(overridden, config)["reasoning"], {"effort": "low"})

    def test_oauth_requires_stream_and_no_storage(self):
        config = LLMProviderConfig(type="openai", auth_mode="chatgpt", chatgpt_account_id="test", enabled=True)
        client = create_llm_client_from_config(config)
        class Events:
            async def __aenter__(self): return self
            async def __aexit__(self, *_args): pass
            def __aiter__(self): return self
            async def __anext__(self): raise StopAsyncIteration
        create = AsyncMock(return_value=Events())
        client._client.responses.create = create
        async def run():
            try:
                await client._create_response({"model": "test"}, stream=False)
            except Exception:
                pass  # Empty stream must not be reported as completed inference.
            self.assertTrue(create.call_args.kwargs["stream"])
            self.assertFalse(create.call_args.kwargs["store"])
            await client._client.close()
        asyncio.run(run())

    def test_credentials_not_in_status_and_signout_clears_tokens(self):
        auth._write({"host_id": "test", "accounts": {"a": {"email": "test@example.invalid", "client_id": "c", "subject": "s", "access_token": "secret-placeholder", "scopes": []}}})
        self.assertNotIn("secret-placeholder", json.dumps(auth.status()))
        asyncio.run(auth.sign_out("a"))
        account = auth._read()["accounts"]["a"]
        self.assertNotIn("access_token", account)
        self.assertEqual(account["client_id"], "c")

    def test_routes_reject_cross_site_requests(self):
        app = FastAPI()
        app.include_router(router)
        with TestClient(app) as client:
            self.assertEqual(client.get("/api/config/chatgpt/status").status_code, 200)
            self.assertEqual(client.post("/api/config/chatgpt/start", json={}, headers={"Origin": "https://untrusted.invalid"}).status_code, 403)
            self.assertEqual(client.post("/api/config/chatgpt/start", data={}).status_code, 415)


if __name__ == "__main__":
    unittest.main()
