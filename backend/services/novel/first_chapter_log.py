"""Read-only business-step projection; never expose candidates or request credentials."""
from datetime import timezone

from bson import ObjectId

from backend.api.error_protocol import DomainApiError
from backend.db.base import BaseRepository


def stamp(value):
    return value.replace(tzinfo=timezone.utc).isoformat() if value else None


async def timeline(novel_id, job_id):
    jid = ObjectId(job_id)
    job = await BaseRepository("first_chapter_jobs").collection.find_one({"_id": jid, "novel_id": ObjectId(novel_id)})
    if not job:
        raise DomainApiError("FIRST_CHAPTER_NOT_FOUND", "一键写作任务不存在", status_code=404)
    rows = await BaseRepository("first_chapter_steps").collection.find({"job_id": jid},
        {"step.label": 1, "saved": 1, "superseded": 1, "created_at": 1, "saved_at": 1,
         "review.summary": 1, "repairs": 1}).sort([("created_at", 1), ("_id", 1)]).to_list(None)
    counts = {}
    cursor = await BaseRepository("first_chapter_logs").collection.aggregate([
        {"$match": {"job_id": jid}},
        {"$group": {"_id": "$operation", "node_count": {"$sum": 1},
                    "failure_count": {"$sum": {"$cond": [{"$eq": ["$state", "failed"]}, 1, 0]}}}},
    ])
    async for count in cursor:
        counts[count["_id"]] = count
    steps, history = [], []
    for row in rows:
        saved = bool(row.get("saved"))
        pending = str(job.get("pending")) == str(row["_id"])
        state = "saved" if saved else "superseded" if row.get("superseded") else job["status"] if pending else "stopped"
        start, end = row.get("created_at"), row.get("saved_at")
        count = counts.get(str(row["_id"]), {})
        item = {"id": str(row["_id"]), "title": row["step"]["label"], "state": state,
                "started_at": stamp(start), "saved_at": stamp(end),
                "elapsed_ms": max(0, (end - start).total_seconds() * 1000) if start and end else None,
                "node_count": count.get("node_count", 0), "failure_count": count.get("failure_count", 0),
                "repairs": row.get("repairs", 0), "review_summary": (row.get("review") or {}).get("summary", "")[:16000],
                "candidate_retained": bool(pending and job.get("has_saved_candidate"))}
        (history if row.get("superseded") else steps).append(item)
    return {"steps": steps, "history": history, "status": job["status"],
            "started_at": stamp(job["created_at"]),
            "finished_at": stamp(job["updated_at"]) if job["status"] in {"completed", "cancelled"} else None}
