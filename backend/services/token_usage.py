"""Durable token ledger. Only counters and explicit attribution are retained, never prompts."""
from __future__ import annotations

import atexit
from contextlib import contextmanager, closing
from datetime import datetime, timezone, timedelta
import json
from pathlib import Path
import queue
import sqlite3
import threading

from backend.storage import USER_DATA_DIR

COUNTERS = ("input_tokens", "output_tokens", "total_tokens", "cached_tokens", "reasoning_tokens")
SCOPES = ("novel_id", "novel_title", "volume_id", "volume_title", "scene_id", "scene_title", "chapter_id", "chapter_title", "task", "task_label", "node_id", "node_label", "stage", "request_id", "flow_id")


def now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def field(value, name, default=None):
    return value.get(name, default) if isinstance(value, dict) else getattr(value, name, default)


def number(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def extract_usage(response):
    """Normalize native counters; absent fields stay unknown, cumulative deltas are not added."""
    response = field(response, "response") or field(response, "message") or field(response, "interaction") or response
    usage = field(response, "usage")
    gemini = field(response, "usage_metadata") or field(response, "usageMetadata")
    usage = gemini if gemini is not None else usage
    if usage is None:
        return {}
    def pick(*names):
        for name in names:
            value = number(field(usage, name))
            if value is not None:
                return value
        return None
    incoming = pick("input_tokens", "prompt_tokens", "prompt_token_count", "promptTokenCount", "total_input_tokens")
    outgoing = pick("output_tokens", "completion_tokens", "candidates_token_count", "candidatesTokenCount", "total_output_tokens")
    total = pick("total_tokens", "total_token_count", "totalTokenCount")
    reasoning = pick("thoughts_token_count", "thoughtsTokenCount", "total_thought_tokens")
    cached = pick("cache_read_input_tokens", "cached_content_token_count", "cachedContentTokenCount", "total_cached_tokens", "prompt_cache_hit_tokens")
    for key in ("input_tokens_details", "prompt_tokens_details"):
        detail = field(usage, key)
        value = number(field(detail, "cached_tokens"))
        if value is not None:
            cached = value
    for key in ("output_tokens_details", "completion_tokens_details"):
        value = number(field(field(usage, key), "reasoning_tokens"))
        if value is not None:
            reasoning = value
    # Anthropic reports uncached input, cache creation and cache reads separately.
    # These are disjoint input categories; include all in input while keeping cache as a subset.
    creation = number(field(usage, "cache_creation_input_tokens"))
    if incoming is not None and (field(usage, "cache_read_input_tokens") is not None or creation is not None):
        incoming += (number(field(usage, "cache_read_input_tokens")) or 0) + (creation or 0)
    # Gemini candidates/output exclude thoughts. Unified output includes thoughts once.
    if outgoing is not None and reasoning is not None and (gemini is not None or field(usage, "total_thought_tokens") is not None):
        outgoing += reasoning
    result = dict(zip(COUNTERS, (incoming, outgoing, total, cached, reasoning)))
    return {key: value for key, value in result.items() if value is not None}


def finalize_counts(row):
    if row.get("total_tokens") is None and row.get("input_tokens") is not None and row.get("output_tokens") is not None and (row.get("usage_final") or row.get("origin") == "history"):
        row["total_tokens"] = row["input_tokens"] + row["output_tokens"]
        row["total_source"] = "sum"
    elif row.get("total_tokens") is not None:
        row.setdefault("total_source", "provider")
    return row


class UsageStore:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.failure = None
        self.dropped = 0
        self._closed = False
        self._queue = queue.Queue(maxsize=10000)
        self._lock = threading.Lock()
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
              CREATE TABLE IF NOT EXISTS usage (
                id INTEGER PRIMARY KEY AUTOINCREMENT, attempt_key TEXT UNIQUE NOT NULL,
                call_id TEXT NOT NULL, sequence INTEGER NOT NULL, time TEXT NOT NULL, updated_at TEXT NOT NULL,
                provider TEXT NOT NULL, model TEXT NOT NULL, task TEXT NOT NULL, novel_id TEXT NOT NULL,
                volume_id TEXT NOT NULL, status TEXT NOT NULL, origin TEXT NOT NULL,
                input_tokens INTEGER, output_tokens INTEGER, total_tokens INTEGER,
                cached_tokens INTEGER, reasoning_tokens INTEGER, usage_final INTEGER NOT NULL DEFAULT 0, data TEXT NOT NULL);
              CREATE INDEX IF NOT EXISTS usage_time ON usage(time);
              CREATE INDEX IF NOT EXISTS usage_novel ON usage(novel_id, time);
              CREATE INDEX IF NOT EXISTS usage_model ON usage(model, time);
              CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            """)
            if "usage_final" not in {row["name"] for row in db.execute("PRAGMA table_info(usage)")}:
                db.execute("ALTER TABLE usage ADD COLUMN usage_final INTEGER NOT NULL DEFAULT 0")
            db.execute("INSERT OR IGNORE INTO meta VALUES('started_at',?)", (now(),))
            db.execute("UPDATE usage SET status='interrupted', updated_at=? WHERE status='pending'", (now(),))
        self._thread = threading.Thread(target=self._write, name="token-usage-writer", daemon=True)
        self._thread.start()
        atexit.register(self.close)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def submit(self, row):
        with self._lock:
            if self._closed:
                return
            try:
                self._queue.put_nowait(dict(row))
            except queue.Full:
                self.dropped += 1

    @staticmethod
    def save(db, row):
        row = finalize_counts(dict(row))
        values = [row[key] for key in ("attempt_key", "call_id", "sequence", "time", "updated_at", "provider", "model", "task", "novel_id", "volume_id", "status", "origin")]
        values += [row.get(key) for key in COUNTERS]
        values.append(int(bool(row.get("usage_final", row["origin"] == "history"))))
        values.append(json.dumps(row, ensure_ascii=False))
        # Live evidence wins over a history import. A repeated cumulative event replaces the same row.
        db.execute("""INSERT INTO usage(attempt_key,call_id,sequence,time,updated_at,provider,model,task,novel_id,volume_id,status,origin,input_tokens,output_tokens,total_tokens,cached_tokens,reasoning_tokens,usage_final,data)
          VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(attempt_key) DO UPDATE SET
          updated_at=excluded.updated_at,model=excluded.model,status=excluded.status,
          input_tokens=excluded.input_tokens,output_tokens=excluded.output_tokens,total_tokens=excluded.total_tokens,
          cached_tokens=excluded.cached_tokens,reasoning_tokens=excluded.reasoning_tokens,usage_final=excluded.usage_final,data=excluded.data
          WHERE usage.origin=excluded.origin OR excluded.origin='live'""", values)

    def _write(self):
        while True:
            row = self._queue.get()
            try:
                if row is None:
                    return
                with self.connect() as db:
                    self.save(db, row)
                self.failure = None
            except Exception:
                self.failure = "Token usage storage failed"
                self.dropped += 1
            finally:
                self._queue.task_done()

    def flush(self):
        self._queue.join()

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self._queue.put(None)
        self._thread.join(timeout=10)

    @staticmethod
    def unpack(row):
        result = json.loads(row["data"])
        result.update({key: row[key] for key in ("id", "status", "updated_at", *COUNTERS)})
        result["coverage"] = "complete" if all(result[key] is not None for key in COUNTERS[:3]) else "partial" if any(result[key] is not None for key in COUNTERS[:3]) else "missing"
        if not row["usage_final"] and result["coverage"] == "complete":
            result["coverage"] = "partial"
        return result

    def query(self, *, start=None, end=None, provider=None, model=None, novel_id=None, volume_id=None, task=None, status=None,
              group="model", bucket="day", before=None, limit=50, offset_minutes=480):
        conditions, args = [], []
        for key, value in (("provider", provider), ("model", model), ("novel_id", novel_id), ("volume_id", volume_id), ("task", task), ("status", status)):
            if value is not None:
                conditions.append(f"{key}=?"); args.append("" if value == "__unassigned__" else value)
        if start:
            conditions.append("time>=?"); args.append(start)
        if end:
            conditions.append("time<?"); args.append(end)
        where = " AND ".join(conditions) or "1=1"
        aggregates = """COUNT(*) requests, SUM(status='pending') pending,
          SUM(total_tokens IS NOT NULL) confirmed, SUM(total_tokens IS NULL AND status!='pending') unknown,
          SUM((input_tokens IS NOT NULL OR output_tokens IS NOT NULL OR total_tokens IS NOT NULL)
              AND (usage_final=0 OR input_tokens IS NULL OR output_tokens IS NULL OR total_tokens IS NULL)) partial,
          COALESCE(SUM(input_tokens),0) input_tokens, COALESCE(SUM(output_tokens),0) output_tokens,
          COALESCE(SUM(total_tokens),0) total_tokens, COALESCE(SUM(cached_tokens),0) cached_tokens,
          COALESCE(SUM(reasoning_tokens),0) reasoning_tokens,
          SUM(cached_tokens IS NOT NULL) cached_reported, SUM(reasoning_tokens IS NOT NULL) reasoning_reported"""
        with self.connect() as db:
            # Bound potentially broad history queries without letting them block generation.
            import time
            deadline = time.monotonic() + 4
            db.set_progress_handler(lambda: int(time.monotonic() > deadline), 10000)
            db.execute("BEGIN")
            summary = dict(db.execute(f"SELECT {aggregates} FROM usage WHERE {where}", args).fetchone())
            dimension = {"model": "model", "provider": "provider", "novel": "novel_id", "volume": "volume_id", "task": "task"}[group]
            # Provider + model is one dimension: equal model names from different providers stay separate.
            expression = "provider || ' / ' || model" if group == "model" else dimension
            groups = [dict(row) for row in db.execute(f"SELECT {expression} label, {aggregates}, MIN(data) sample FROM usage WHERE {where} GROUP BY {expression} ORDER BY total_tokens DESC LIMIT 100", args)]
            for item in groups:
                sample = json.loads(item.pop("sample"))
                item["name"] = sample.get("scope", {}).get({"novel": "novel_title", "volume": "volume_title", "task": "task_label"}.get(group, "")) or item["label"]
            stamp = "substr(datetime(time, ?),1,13)" if bucket == "hour" else "substr(datetime(time, ?),1,10)"
            # For all-time, retain the latest 366 buckets and explicitly report the chart limit.
            trend = [dict(row) for row in db.execute(f"SELECT {stamp} period, {aggregates} FROM usage WHERE {where} GROUP BY period ORDER BY period DESC LIMIT 367", [f"{offset_minutes:+d} minutes", *args])]
            chart_limited = len(trend) > 366
            trend = list(reversed(trend[:366]))
            page_where, page_args = where, list(args)
            if before is not None:
                page_where += " AND id<?"; page_args.append(before)
            rows = db.execute(f"SELECT * FROM usage WHERE {page_where} ORDER BY id DESC LIMIT ?", [*page_args, limit + 1]).fetchall()
            options = {}
            for key in ("provider", "model", "novel_id", "volume_id", "task"):
                options[key] = [{"value": row[0] or "__unassigned__", "label": row[0], "sample": json.loads(row[1])} for row in db.execute(f"SELECT {key}, MIN(data) FROM usage GROUP BY {key} ORDER BY {key} LIMIT 500")]
            meta = dict(db.execute("SELECT key,value FROM meta"))
            oldest = db.execute("SELECT MIN(time) FROM usage").fetchone()[0]
        return {"summary": summary, "groups": groups, "trend": trend, "chart_limited": chart_limited,
                "items": [self.unpack(row) for row in rows[:limit]], "before": rows[min(limit, len(rows))-1]["id"] if len(rows)>limit else None,
                "options": options, "started_at": meta.get("started_at"), "oldest_at": oldest,
                "history": json.loads(meta.get("history", "{}")), "storage_error": self.failure, "dropped": self.dropped + _collection_errors}

    def import_history(self, path: Path):
        """Import only attributable per-send usage; aggregate fallback requires exactly one known send."""
        if not path.exists():
            return
        import ast
        import re
        with self.connect() as db:
            if db.execute("SELECT 1 FROM meta WHERE key='history'").fetchone():
                return
        with closing(sqlite3.connect(path, timeout=5)) as source:
            source.row_factory = sqlite3.Row
            source.execute("BEGIN")
            rows = source.execute("SELECT id,time,kind,message,metadata,payload FROM logs WHERE source='llm' ORDER BY id").fetchall()
        calls = {}
        for log in rows:
            meta = json.loads(log["metadata"])
            evidence = log["payload"] if log["kind"] == "legacy" else log["message"]
            header = evidence.splitlines()[0] if evidence else ""
            call_id = meta.get("call_id")
            if not call_id:
                continue
            call = calls.setdefault(call_id, {"sends": {}, "end": None, "meta": meta})
            sequence = meta.get("send")
            match = re.search(r"\bsend=(\d+)", header)
            if sequence is None and match:
                sequence = int(match[1])
            if sequence is not None:
                sequence = int(sequence)
                attempt = call["sends"].setdefault(sequence, {"time": log["time"], "usage": {}, "status": "interrupted"})
                try:
                    payload = json.loads(log["payload"])
                except (ValueError, TypeError):
                    payload = {}
                if log["kind"] == "llm_response" and isinstance(payload, dict):
                    attempt["usage"].update(extract_usage(payload.get("body", {})))
                    code = payload.get("status")
                    if isinstance(code, int):
                        attempt["status"] = "completed" if code < 400 else "failed"
                if log["kind"] == "legacy" and "[LLM 原始响应]" in header and "payload=\n" in evidence:
                    try:
                        attempt["usage"].update(extract_usage(json.loads(evidence.split("payload=\n", 1)[1])))
                    except (ValueError, TypeError):
                        pass
            if "[LLM 结束]" in header:
                call["end"] = log
        imported = ambiguous = 0
        with self.connect() as db:
            for call_id, call in calls.items():
                sends, end, meta = call["sends"], call["end"], call["meta"]
                if not sends:
                    ambiguous += 1; continue
                if end is not None:
                    end_text = end["payload"] if end["kind"] == "legacy" else end["message"]
                    status_match = re.search(r"\bstatus=(completed|failed|cancelled)", end_text)
                    sends[max(sends)]["status"] = status_match[1] if status_match else "interrupted"
                    if len(sends) == 1:
                        usage_match = re.search(r"\busage=(\{[^\n]+?\})\s+finish=", end_text)
                        if usage_match:
                            try:
                                usage = extract_usage({"usage": ast.literal_eval(usage_match[1])})
                                # Legacy TokenUsage defaulted missing usage to all zero. That is not evidence.
                                if any(usage.get(key, 0) > 0 for key in COUNTERS[:3]):
                                    sends[max(sends)]["usage"] = {**usage, **sends[max(sends)]["usage"]}
                            except (ValueError, SyntaxError):
                                pass
                for sequence, attempt in sends.items():
                    row = {"attempt_key": f"{call_id}:{sequence}", "call_id": call_id, "sequence": sequence,
                           "time": attempt["time"], "updated_at": end["time"] if end else attempt["time"],
                           "provider": str(meta.get("provider", "")), "model": str(meta.get("model", "")),
                           "task": str(meta.get("task", "")), "novel_id": str(meta.get("novel_id", "")),
                           "volume_id": str(meta.get("volume_id", "")), "scope": {key: meta[key] for key in SCOPES if meta.get(key)},
                           "status": attempt["status"], "origin": "history", **attempt["usage"]}
                    self.save(db, row); imported += 1
            db.execute("INSERT INTO meta VALUES('history',?)", (json.dumps({"imported_requests": imported, "unresolved_calls": ambiguous, "imported_at": now()}),))


_store = None
_lock = threading.Lock()
_collection_errors = 0


def collection_failed():
    global _collection_errors
    _collection_errors += 1


def get_store():
    global _store
    with _lock:
        if _store is None:
            _store = UsageStore(USER_DATA_DIR / "usage/tokens.sqlite3")
    return _store


def publish(row):
    try:
        get_store().submit(row)
    except Exception:
        # Usage collection is auxiliary. Its unavailable state is exposed by the query endpoint.
        collection_failed()


async def resolve_scope(scope):
    """Follow formal hierarchy IDs only; no inference from prompts or user text."""
    result = {key: str(scope[key]) for key in ("novel_id", "volume_id", "scene_id", "chapter_id") if scope.get(key)}
    try:
        from bson import ObjectId
        from backend.db import mongo
        if mongo.client is None:
            return result
        db = mongo.get_database()
        target_type, target_id = scope.get("target_type"), scope.get("target_id")
        if target_type in {"volume", "arc", "scene", "chapter"} and target_id:
            result[f"{target_type}_id"] = str(target_id)
        source = scope.get("source") or {}
        if source.get("chapter_id"):
            result["chapter_id"] = str(source["chapter_id"])
        for kind, collection, parent in (("chapter", "chapters", "scene"), ("scene", "scenes", "arc"), ("arc", "arcs", "volume"), ("volume", "volumes", None), ("novel", "novels", None)):
            identity = result.get(f"{kind}_id")
            if not identity or not ObjectId.is_valid(identity):
                continue
            query = {"_id": ObjectId(identity)}
            if kind != "novel" and result.get("novel_id"):
                query["novel_id"] = ObjectId(result["novel_id"])
            row = await db[collection].find_one(query, {"title": 1, "novel_id": 1, f"{parent}_id": 1} if parent else {"title": 1})
            if row:
                result[f"{kind}_title"] = row.get("title", "")
                if parent and row.get(f"{parent}_id"):
                    result[f"{parent}_id"] = str(row[f"{parent}_id"])
    except Exception:
        pass
    return {key: value for key, value in result.items() if key in SCOPES}
