"""Local-only ChatGPT authorization controls; never returns credentials."""
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict

from backend.llm import chatgpt_auth
from backend.runtime import get_frontend_port, get_backend_port


def local_request(request: Request):
    allowed = {f"http://{host}:{port}" for host in ("127.0.0.1", "localhost")
               for port in (get_frontend_port(), get_backend_port())}
    if request.client and request.client.host not in {"127.0.0.1", "::1", "testclient"}:
        raise HTTPException(403, "ChatGPT 授权仅供本机使用")
    if request.headers.get("origin") and request.headers["origin"] not in allowed:
        raise HTTPException(403, "不允许的授权请求来源")
    if request.headers.get("sec-fetch-site") == "cross-site":
        raise HTTPException(403, "不允许跨站授权请求")
    if request.method == "POST" and not request.headers.get("content-type", "").startswith("application/json"):
        raise HTTPException(415, "请使用 JSON 请求")


router = APIRouter(prefix="/api/config/chatgpt", tags=["chatgpt"], dependencies=[Depends(local_request)])


class AccountRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    account_id: str = ""


@router.get("/status")
def status():
    return chatgpt_auth.status()


@router.post("/start")
def start(body: AccountRequest):
    try:
        return {"url": chatgpt_auth.start(body.account_id)}
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None


@router.post("/cancel")
def cancel():
    chatgpt_auth.cancel()
    return {"ok": True}


@router.get("/models")
async def models(account_id: str):
    try:
        return {"models": await chatgpt_auth.models(account_id)}
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None


@router.post("/sign-out")
async def sign_out(body: AccountRequest):
    try:
        revoked = await chatgpt_auth.sign_out(body.account_id)
        return {"revoked": revoked, "message": "已退出" if revoked else "已在本机退出；未能确认远程撤销，请到 ChatGPT 设置中断开此应用。"}
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
