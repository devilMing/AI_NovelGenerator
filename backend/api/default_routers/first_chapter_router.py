"""Novel-level first-chapter background job controls and persistent logs."""
from typing import Literal
from fastapi import APIRouter, Header, Query
from backend.api.contracts import ObjectIdString
from backend.api.default_routers.writing_router import EXPECTED_ERRORS, raise_writing_http_error
from backend.services.novel import first_chapter_service as service

router = APIRouter(prefix="/api/novels/{novel_id}/first-chapter-job", tags=["first-chapter"])


@router.get("")
async def status(novel_id: ObjectIdString):
    try:
        return {"job": await service.latest(novel_id.lower())}
    except EXPECTED_ERRORS as exc:
        raise_writing_http_error(exc)


@router.post("")
async def start(novel_id: ObjectIdString, request: service.Start, idempotency_key: str = Header(..., alias="Idempotency-Key")):
    try:
        return {"job": await service.start(novel_id.lower(), request, idempotency_key)}
    except EXPECTED_ERRORS as exc:
        raise_writing_http_error(exc)


@router.get("/history")
async def history(novel_id: ObjectIdString):
    try:
        await service.plan.load_writing_parents(novel_id.lower(), "novel", novel_id.lower())
        return {"jobs": await service.history(novel_id.lower())}
    except EXPECTED_ERRORS as exc:
        raise_writing_http_error(exc)


@router.get("/{job_id}/logs")
async def logs(novel_id: ObjectIdString, job_id: ObjectIdString, before: ObjectIdString | None = None, limit: int = Query(50, ge=1, le=100), step_id: ObjectIdString | None = None):
    try:
        await service.plan.load_writing_parents(novel_id.lower(), "novel", novel_id.lower())
        return await service.logs(novel_id.lower(), job_id.lower(), before, limit, step_id.lower() if step_id else None)
    except EXPECTED_ERRORS as exc:
        raise_writing_http_error(exc)


@router.get("/{job_id}/timeline")
async def timeline(novel_id: ObjectIdString, job_id: ObjectIdString):
    try:
        await service.plan.load_writing_parents(novel_id.lower(), "novel", novel_id.lower())
        return await service.timeline(novel_id.lower(), job_id.lower())
    except EXPECTED_ERRORS as exc:
        raise_writing_http_error(exc)


@router.post("/{job_id}/{action}")
async def control(novel_id: ObjectIdString, job_id: ObjectIdString, action: Literal["pause", "resume", "cancel"]):
    try:
        return {"job": await service.control(novel_id.lower(), job_id.lower(), action)}
    except EXPECTED_ERRORS as exc:
        raise_writing_http_error(exc)
