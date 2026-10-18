"""Persistent, redacted runtime and model communications, independent of MongoDB."""
from __future__ import annotations

import hashlib
import atexit
import json
import logging
import queue
import re
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from backend.storage import USER_DATA_DIR

MAX_TEXT = 262144
PRIVATE = {"apikey", "accesstoken", "refreshtoken", "idtoken", "authorization", "cookie", "setcookie", "password", "clientsecret", "token", "encryptedcontent", "reasoningcontent", "thinking", "thoughtsignature", "reasoningsignature"}


def redact(value):
    if hasattr(value, "model_dump"):
        return redact(value.model_dump(mode="json"))
    if isinstance(value, dict):
        if isinstance(value.get("type"), str) and value["type"] in {"reasoning", "thinking", "redacted_thinking"}:
            return {"type": value["type"], "content": "[hidden]"}
        return {str(key): "[redacted]" if re.sub(r"[^a-z]", "", str(key).lower()) in PRIVATE else redact(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(item) for item in value]
    if not isinstance(value, str):
        return value
    value = re.sub(r"(?i)(Bearer\s+)[A-Za-z0-9_.~+/=-]+", r"\1[redacted]", value)
    value = re.sub(r"\b(?:sk-|sess-)[A-Za-z0-9_-]{12,}", "[redacted]", value)
    value = re.sub(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", "[redacted]", value)
    value = re.sub(r"(?i)([?&](?:code|token|access_token|refresh_token|api_key|key|client_secret)=)[^&\s\"']+", r"\1[redacted]", value)
    value = re.sub(r"(?i)([a-z][a-z0-9+.-]*://)[^/@\s:]+:[^/@\s]+@", r"\1[redacted]@", value)
    value = re.sub(r'''(?i)(["']?(?:api[_-]?key|access[_-]?token|refresh[_-]?token|id[_-]?token|token|password|client[_-]?secret|authorization|(?:set[_-]?)?cookie|encrypted[_-]?content|reasoning[_-]?content|thought[_-]?signature)["']?\s*[:=]\s*)("(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')''', r'\1"[redacted]"', value)
    return value


def packed(value):
    text = json.dumps(redact(value), ensure_ascii=False, default=str, indent=2) if not isinstance(value, str) else redact(value)
    return text[:MAX_TEXT], len(text) > MAX_TEXT


class LogStore:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._queue = queue.Queue(maxsize=10000)
        self._closed = False
        self._lock = threading.Lock()
        self.failure = None
        self.dropped = 0
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS logs (
                  id INTEGER PRIMARY KEY AUTOINCREMENT, time TEXT NOT NULL, level TEXT NOT NULL,
                  source TEXT NOT NULL, kind TEXT NOT NULL, message TEXT NOT NULL,
                  metadata TEXT NOT NULL, payload TEXT NOT NULL, truncated INTEGER NOT NULL,
                  legacy_key TEXT UNIQUE);
                CREATE INDEX IF NOT EXISTS logs_time ON logs(time);
                CREATE INDEX IF NOT EXISTS logs_level ON logs(level, id);
                CREATE INDEX IF NOT EXISTS logs_kind ON logs(kind, id);
                CREATE TABLE IF NOT EXISTS imports (name TEXT PRIMARY KEY, fingerprint TEXT NOT NULL);
            """)
            self.prune(db)
        self._thread = threading.Thread(target=self._write, name="runtime-log-writer", daemon=True)
        self._thread.start()
        atexit.register(self.close)

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        return db

    @contextmanager
    def connect(self):
        db = self._connect()
        try:
            with db:
                yield db
        finally:
            db.close()

    def submit(self, row, *, wait=False):
        with self._lock:
            if self._closed:
                return
            try:
                self._queue.put(row, block=wait)
            except queue.Full:
                self.dropped += 1

    @staticmethod
    def prune(db):
        cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat(timespec="milliseconds")
        db.execute("DELETE FROM logs WHERE time < ? OR id < (SELECT id FROM logs ORDER BY id DESC LIMIT 1 OFFSET 99999)", (cutoff,))

    def _write(self):
        db = self._connect()
        count = 0
        try:
            while True:
                row = self._queue.get()
                try:
                    if row is None:
                        return
                    db.execute("INSERT OR IGNORE INTO logs(time,level,source,kind,message,metadata,payload,truncated,legacy_key) VALUES(?,?,?,?,?,?,?,?,?)", row)
                    count += 1
                    if count % 200 == 0:
                        self.prune(db)
                    db.commit()
                    self.failure = None
                except Exception as exc:
                    db.rollback()
                    self.failure = type(exc).__name__
                    self.dropped += 1
                finally:
                    self._queue.task_done()
        finally:
            db.close()

    def flush(self):
        self._queue.join()

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self._queue.put(None)
        self._thread.join(timeout=10)

    def query(self, *, after=None, before=None, start=None, end=None, level=None, category="all", q="", limit=50):
        conditions, values = [], []
        if after is not None and before is not None:
            raise ValueError("不能同时向前和向后查询")
        for field, operator, value in (("id", ">", after), ("id", "<", before), ("time", ">=", start), ("time", "<=", end), ("level", "=", level)):
            if value is not None:
                conditions.append(f"{field} {operator} ?"); values.append(value)
        if category == "llm":
            conditions.append("(source='llm' OR kind LIKE 'llm_%')")
        elif category == "system":
            conditions.append("(source<>'llm' AND kind NOT LIKE 'llm_%')")
        if q:
            conditions.append("instr(lower(message || metadata || payload), lower(?)) > 0"); values.append(q)
        with self.connect() as db:
            deadline = time.monotonic() + 3
            db.set_progress_handler(lambda: int(time.monotonic() > deadline), 10000)
            db.execute("BEGIN")
            high = db.execute("SELECT coalesce(max(id),0) FROM logs").fetchone()[0]
            conditions.append("id <= ?"); values.append(high)
            order = "ASC" if after is not None else "DESC"
            rows = db.execute(f"SELECT id,time,level,source,kind,message,metadata,truncated FROM logs WHERE {' AND '.join(conditions)} ORDER BY id {order} LIMIT ?", [*values, limit + 1]).fetchall()
        more = len(rows) > limit
        rows = rows[:limit]
        items = [{**dict(row), "metadata": json.loads(row["metadata"]), "truncated": bool(row["truncated"])} for row in rows]
        return {"items": items, "cursor": rows[-1]["id"] if after is not None and more else high,
                "before": min(row["id"] for row in rows) if more else None, "has_more": more,
                "retention_days": 30, "dropped": self.dropped, "storage_error": self.failure}

    def detail(self, identity):
        with self.connect() as db:
            row = db.execute("SELECT * FROM logs WHERE id=?", (identity,)).fetchone()
        if row is None:
            return None
        return {**{key: row[key] for key in row.keys() if key != "legacy_key"}, "metadata": json.loads(row["metadata"]), "truncated": bool(row["truncated"])}

    def import_legacy(self, directory: Path):
        """Import only known backend console files; no user-supplied paths."""
        for path in sorted({*directory.glob("backend*.out.log"), *directory.glob("backend*.err.log")}):
            if path.is_symlink() or path.resolve().parent != directory.resolve():
                continue
            stat = path.stat()
            fingerprint = f"{stat.st_mtime_ns}:{stat.st_size}"
            with self.connect() as db:
                previous = db.execute("SELECT fingerprint FROM imports WHERE name=?", (path.name,)).fetchone()
            if previous and previous[0] == fingerprint:
                continue
            with path.open("rb") as stream:
                if stat.st_size > 50 * 1024 * 1024:
                    stream.seek(-50 * 1024 * 1024, 2)
                    stream.readline()
                content = stream.read().decode("utf-8", errors="replace")
            blocks = re.split(r"(?m)(?=^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} \|)", content)
            for block in blocks:
                header = re.match(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) \|\s*(DEBUG|INFO|WARNING|ERROR|CRITICAL):?\s*\|", block)
                if not header or "/api/runtime/logs" in block:
                    continue
                stamp = datetime.strptime(header[1], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone(timedelta(hours=8))).astimezone(timezone.utc).isoformat(timespec="milliseconds")
                metadata = dict(re.findall(r"\b(request_id|call_id|task|flow_id|node_id|provider|model)=([^\s|]+)", block))
                payload, truncated = packed(block.strip())
                source = "llm" if "| llm |" in block else "legacy"
                self.submit((stamp, header[2], source, "legacy", payload.splitlines()[0][:500], json.dumps(redact(metadata)), payload, int(truncated), hashlib.sha256(block.encode()).hexdigest()), wait=True)
            self.flush()
            with self.connect() as db:
                db.execute("INSERT OR REPLACE INTO imports VALUES(?,?)", (path.name, fingerprint))
                self.prune(db)


_store = None
_store_lock = threading.Lock()


def get_store():
    global _store
    with _store_lock:
        if _store is None:
            _store = LogStore(USER_DATA_DIR / "runtime_logs/events.sqlite3")
    return _store


class RuntimeLogHandler(logging.Handler):
    def emit(self, record):
        try:
            from backend.runtime import log_context
            message = record.getMessage()
            if "/api/runtime/logs" in message or "/api/runtime/token-usage" in message:
                return
            metadata = {**log_context.get(), **getattr(record, "log_metadata", {})}
            details = getattr(record, "log_payload", message)
            if record.exc_info:
                details = {"message": message, "traceback": logging.Formatter().formatException(record.exc_info)}
            payload, truncated = packed(details)
            stamp = datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec="milliseconds")
            get_store().submit((stamp, record.levelname, record.name, getattr(record, "log_kind", "system"), redact(message)[:2000],
                json.dumps(redact(metadata), ensure_ascii=False, default=str), payload, int(truncated), None))
        except Exception:
            # 日志故障不打断生成，也不把未脱敏的 LogRecord 再写入 stderr。
            pass
