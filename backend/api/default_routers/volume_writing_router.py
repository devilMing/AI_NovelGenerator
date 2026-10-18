"""逐卷自动写作控制；只返回进度，不公开模型原始上下文。"""
from typing import Literal
from fastapi import APIRouter, Header, Query
from backend.api.contracts import ObjectIdString
from backend.api.default_routers.writing_router import EXPECTED_ERRORS, raise_writing_http_error
from backend.services.novel.volume_writing_contract import VolumeWritingStart
from backend.services.novel import volume_writing_service as service
from backend.services.novel import volume_writing_log

router = APIRouter(prefix="/api/novels/{novel_id}/volumes/{volume_id}/writing-job", tags=["volume-writing"])
overview_router = APIRouter(prefix="/api/novels/{novel_id}", tags=["volume-writing"])


@overview_router.get("/volume-writing-job")
async def overview(novel_id: ObjectIdString):
    try:
        return {"job": await service.overview(novel_id.lower())}
    except EXPECTED_ERRORS as exc:
        raise_writing_http_error(exc)


@router.get("/preview")
async def preview(novel_id: ObjectIdString, volume_id: ObjectIdString):
    try:
        return await service.preview(novel_id.lower(), volume_id.lower())
    except EXPECTED_ERRORS as exc:
        raise_writing_http_error(exc)


@router.get("")
async def status(novel_id: ObjectIdString, volume_id: ObjectIdString):
    try:
        return {"job": await service.latest(novel_id.lower(), volume_id.lower())}
    except EXPECTED_ERRORS as exc:
        raise_writing_http_error(exc)


@router.post("")
async def start(novel_id: ObjectIdString, volume_id: ObjectIdString, request: VolumeWritingStart, idempotency_key: str = Header(..., alias="Idempotency-Key")):
    try:
        return {"job": await service.start(novel_id.lower(), volume_id.lower(), request, idempotency_key)}
    except EXPECTED_ERRORS as exc:
        raise_writing_http_error(exc)


@router.get("/{job_id}/logs")
async def logs(novel_id: ObjectIdString, volume_id: ObjectIdString, job_id: ObjectIdString,
               before: ObjectIdString | None = None, limit: int = Query(50, ge=1, le=100)):
    try:
        await service.load_writing_parents(novel_id.lower(), "volume", volume_id.lower())
        return await volume_writing_log.read(novel_id.lower(), volume_id.lower(), job_id.lower(), before, limit)
    except EXPECTED_ERRORS as exc:
        raise_writing_http_error(exc)


@router.post("/{job_id}/{action}")
async def control(novel_id: ObjectIdString, volume_id: ObjectIdString, job_id: ObjectIdString, action: Literal["pause", "resume", "cancel"]):
    try:
        return {"job": await service.control(novel_id.lower(), volume_id.lower(), job_id.lower(), action)}
    except EXPECTED_ERRORS as exc:
        raise_writing_http_error(exc)
