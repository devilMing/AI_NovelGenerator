"""Project-local Sign in with ChatGPT (official OSS OAuth + Responses API)."""
from __future__ import annotations

import asyncio
import base64
import ctypes
import hashlib
import json
import os
import secrets
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import jwt

from backend.storage import USER_DATA_DIR

ISSUER = "https://auth.openai.com"
RESOURCE = "https://api.openai.com/v1"
SCOPE = "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct"
STORE = USER_DATA_DIR / "auth" / "chatgpt.bin"
_disk_lock = threading.RLock()
_refresh_lock = asyncio.Lock()
_attempt: dict = {}


def _protect(data: bytes, *, decrypt: bool = False) -> bytes:
    """DPAPI binds Windows credentials to this OS user; Unix uses file permissions."""
    if os.name != "nt":
        return data
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_char))]

    buf = ctypes.create_string_buffer(data)
    src = Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    dest = Blob()
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    fn = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    if not fn(ctypes.byref(src), None, None, None, None, 1, ctypes.byref(dest)):
        raise OSError("无法访问本项目的 Windows 受保护凭据")
    try:
        return ctypes.string_at(dest.data, dest.size)
    finally:
        kernel.LocalFree(ctypes.cast(dest.data, ctypes.c_void_p))


def _read() -> dict:
    if STORE.exists():
        return json.loads(_protect(STORE.read_bytes(), decrypt=True))
    return {"host_id": f"urn:uuid:{uuid.uuid4()}", "accounts": {}}


def _write(data: dict) -> None:
    STORE.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temp = STORE.with_name(f".{uuid.uuid4().hex}.tmp")
    try:
        with temp.open("xb") as handle:
            os.chmod(temp, 0o600)
            handle.write(_protect(json.dumps(data).encode()))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, STORE)
    finally:
        temp.unlink(missing_ok=True)


def status() -> dict:
    with _disk_lock:
        data = _read()
        accounts = [{"id": k, "email": v.get("email", ""),
                     "label": f"{v.get('email') or 'ChatGPT'} · {k[:8]}",
                     "connected": bool(v.get("refresh_token") or v.get("access_token")),
                     "plan_enabled": "chatgpt.tokens.use.direct" in v.get("scopes", [])}
                    for k, v in data["accounts"].items()]
        phase = _attempt.get("status", "idle")
        if phase == "pending" and time.time() > _attempt.get("expires", 0):
            phase = "expired"
        return {"accounts": accounts, "status": phase, "message": _attempt.get("message", "")}


def _metadata() -> dict:
    with httpx.Client(timeout=30, trust_env=True) as client:
        result = client.get(f"{ISSUER}/.well-known/openid-configuration")
        result.raise_for_status()
        data = result.json()
    if data.get("issuer") != ISSUER:
        raise ValueError("OpenAI issuer 不匹配")
    for key in ("jwks_uri", "revocation_endpoint"):
        value = data.get(key)
        if value and (urlsplit(value).scheme != "https" or urlsplit(value).hostname != "auth.openai.com"):
            raise ValueError("OpenAI 授权端点无效")
    return data


def _validate_id(tokens: dict, client_id: str, nonce: str | None) -> dict:
    metadata = _metadata()
    with httpx.Client(timeout=30, trust_env=True) as client:
        response = client.get(metadata["jwks_uri"])
        response.raise_for_status()
        keys = jwt.PyJWKSet.from_dict(response.json())
    token = tokens["id_token"]
    header = jwt.get_unverified_header(token)
    key = next(k for k in keys.keys if k.key_id == header.get("kid"))
    claims = jwt.decode(token, key.key, algorithms=["RS256"], audience=client_id,
                        issuer=ISSUER, options={"require": ["exp", "iss", "aud", "sub"]})
    if nonce is not None and not secrets.compare_digest(str(claims.get("nonce", "")), nonce):
        raise ValueError("OpenAI 登录 nonce 不匹配")
    return claims


def _exchange(form: dict) -> dict:
    with httpx.Client(timeout=45, trust_env=True) as client:
        response = client.post(f"{ISSUER}/api/accounts/oauth/token", data=form)
    if response.status_code != 200:
        # Never propagate an HTTP body, authorization code, or token into logs.
        raise ValueError(f"OpenAI 授权未完成（HTTP {response.status_code}），请重新登录")
    return response.json()


def _finish(params: dict, pending: dict) -> None:
    if not secrets.compare_digest(params.get("state", ""), pending["state"]):
        raise ValueError("登录状态不匹配")
    if time.time() > pending["expires"]:
        raise ValueError("登录已超时，请重试")
    if params.get("error"):
        raise ValueError("授权已取消或未获批准，请重新登录并确认权限")
    client_id = params.get("client_id") or pending.get("client_id")
    if not client_id or client_id == "dynamic_agent_client":
        raise ValueError("OpenAI 未返回应用注册信息")
    if pending.get("client_id") and client_id != pending["client_id"]:
        raise ValueError("应用注册身份不匹配")
    if not params.get("code"):
        raise ValueError("OpenAI 未返回授权码")
    tokens = _exchange({"grant_type": "authorization_code", "client_id": client_id,
                        "code": params["code"], "code_verifier": pending["verifier"],
                        "redirect_uri": pending["redirect_uri"], "resource": RESOURCE})
    claims = _validate_id(tokens, client_id, pending["nonce"])
    with _disk_lock:
        if pending["status"] != "pending" or time.time() > pending["expires"]:
            raise ValueError("登录已取消或超时，请重试")
        data = _read()
        previous = data["accounts"].get(pending.get("account_id"))
        if previous and (previous["subject"] != claims["sub"] or previous["client_id"] != client_id):
            raise ValueError("返回账号与选择的账号不匹配")
        account_id = hashlib.sha256(f"{client_id}:{claims['sub']}".encode()).hexdigest()[:24]
        data["accounts"][account_id] = {
            "subject": claims["sub"], "email": claims.get("email", ""), "client_id": client_id,
            "id_token": tokens["id_token"], "access_token": tokens["access_token"],
            "refresh_token": tokens.get("refresh_token", ""),
            "scopes": tokens.get("scope", "").split(),
            "expires_at": time.time() + int(tokens.get("expires_in", 3600)),
        }
        _write(data)


def start(account_id: str = "") -> str:
    global _attempt
    with _disk_lock:
        if _attempt.get("status") == "pending" and time.time() < _attempt.get("expires", 0):
            raise ValueError("已有登录窗口等待授权，请先完成或取消本次登录")
        data = _read()
        _write(data)  # Persist the host before opening the browser.
        account = data["accounts"].get(account_id) if account_id else None
        if account_id and account is None:
            raise ValueError("请选择已保存的账号")
        pending = {"state": secrets.token_urlsafe(32), "nonce": secrets.token_urlsafe(32),
                   "verifier": secrets.token_urlsafe(64), "expires": time.time() + 600,
                   "status": "pending", "account_id": account_id,
                   "client_id": account["client_id"] if account else None}

        class Callback(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass  # OAuth callback queries must never be logged.

            def do_GET(self):
                url = urlsplit(self.path)
                if url.path != "/auth/callback":
                    self.send_error(404)
                    return
                params = {k: v[0] for k, v in parse_qs(url.query).items()}
                if not secrets.compare_digest(params.get("state", ""), pending["state"]):
                    self.send_error(400)
                    return
                try:
                    _finish(params, pending)
                    pending.update(status="completed", message="授权完成，请返回小说生成器选择账号和模型")
                except Exception:
                    pending.update(status="error", message="授权未完成。请确认网络、账号资格及授权权限后重试。")
                body = f"<!doctype html><meta charset='utf-8'><title>ChatGPT 授权</title><p>{pending['message']}</p>".encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        server = HTTPServer(("127.0.0.1", 0), Callback)
        server.timeout = 1
        pending["redirect_uri"] = f"http://127.0.0.1:{server.server_port}/auth/callback"
        _attempt = pending

        def listen():
            try:
                while pending["status"] == "pending" and time.time() < pending["expires"]:
                    server.handle_request()
                    if _attempt.get("state") == pending["state"]:
                        _attempt.update(pending)
            finally:
                server.server_close()
                if _attempt.get("state") == pending["state"]:
                    _attempt.update(pending)

        threading.Thread(target=listen, daemon=True).start()
        query = {"client_id": pending["client_id"] or "dynamic_agent_client",
                 "ext_agent_host_id": data["host_id"], "response_type": "code",
                 "redirect_uri": pending["redirect_uri"], "scope": SCOPE, "resource": RESOURCE,
                 "state": pending["state"], "nonce": pending["nonce"], "code_challenge_method": "S256",
                 "code_challenge": base64.urlsafe_b64encode(hashlib.sha256(pending["verifier"].encode()).digest()).rstrip(b"=").decode()}
        if not account:
            query["agent_name_hint"] = "AI NovelGenerator"
        elif account.get("id_token"):
            query["id_token_hint"] = account["id_token"]
        return f"{ISSUER}/api/accounts/authorize?{urlencode(query)}"


def cancel() -> None:
    _attempt["expires"] = 0
    _attempt["status"] = "cancelled"


async def access_token(account_id: str) -> str:
    async with _refresh_lock:
        with _disk_lock:
            data = _read()
            account = data["accounts"].get(account_id)
        if not account or not account.get("access_token"):
            raise ValueError("请先在设置中登录 ChatGPT")
        if "chatgpt.tokens.use.direct" not in account.get("scopes", []):
            raise ValueError("此账号尚未授权使用 ChatGPT 套餐，请重新授权")
        if account["expires_at"] < time.time() + 120:
            if not account.get("refresh_token"):
                raise ValueError("ChatGPT 登录已过期，请重新登录")
            tokens = await asyncio.to_thread(_exchange, {"grant_type": "refresh_token",
                "client_id": account["client_id"], "refresh_token": account["refresh_token"], "resource": RESOURCE})
            if "id_token" in tokens:
                claims = await asyncio.to_thread(_validate_id, tokens, account["client_id"], None)
                if claims["sub"] != account["subject"]:
                    raise ValueError("ChatGPT 账号身份不匹配，请重新登录")
            with _disk_lock:
                # Sign-out may have happened while awaiting the network.
                data = _read()
                current = data["accounts"].get(account_id)
                if not current or current.get("refresh_token") != account.get("refresh_token"):
                    raise ValueError("ChatGPT 账号已退出，请重新登录")
                current.update(access_token=tokens["access_token"],
                    refresh_token=tokens.get("refresh_token", account["refresh_token"]),
                    id_token=tokens.get("id_token", account.get("id_token", "")),
                    scopes=tokens.get("scope", " ".join(account["scopes"])).split(),
                    expires_at=time.time() + int(tokens.get("expires_in", 3600)))
                _write(data)
                account = current
            if "chatgpt.tokens.use.direct" not in account["scopes"]:
                raise ValueError("ChatGPT 套餐使用权限已撤销")
        return account["access_token"]


async def models(account_id: str) -> list[dict]:
    token = await access_token(account_id)
    async with httpx.AsyncClient(timeout=30, trust_env=True) as client:
        response = await client.get(f"{RESOURCE}/models", headers={"Authorization": f"Bearer {token}"})
    if response.status_code != 200:
        raise ValueError(f"无法读取 ChatGPT 模型（HTTP {response.status_code}）")
    return [{"id": m["slug"], "name": m.get("display_name", m["slug"])}
            for m in response.json().get("models", []) if m.get("visibility") == "list"]


async def sign_out(account_id: str) -> bool:
    async with _refresh_lock:
        with _disk_lock:
            data = _read()
            account = data["accounts"].get(account_id)
        if not account:
            raise ValueError("账号不存在")
        revoked = not bool(account.get("refresh_token"))
        if not revoked:
            try:
                metadata = await asyncio.to_thread(_metadata)
                async with httpx.AsyncClient(timeout=20, trust_env=True) as client:
                    for attempt in range(3):
                        response = await client.post(metadata["revocation_endpoint"], data={
                            "token": account["refresh_token"], "token_type_hint": "refresh_token", "client_id": account["client_id"]})
                        if response.status_code == 200:
                            revoked = True
                            break
                        if response.status_code < 500:
                            break
                        await asyncio.sleep(2 ** attempt)
            except Exception:
                pass
        with _disk_lock:
            data = _read()
            record = data["accounts"][account_id]
            for key in ("access_token", "refresh_token", "id_token"):
                record.pop(key, None)
            _write(data)
        return revoked
