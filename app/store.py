"""存储层：原始证据 + 原子事实 + 图片。

设计要点（来自 astra）：
  - records：原始证据，**永不改写**，保留时间/说话人/模态
  - facts：原子事实（主体-关系-客体[数值+单位]），带来源类型、事实状态、置信度
  - 一个 record 可以对应多条 facts；一条 fact 必须能回到它的 record
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any

from .config import BLOB_DIR, DB_PATH

_SCHEMA = """
CREATE TABLE IF NOT EXISTS records (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  session_id TEXT NOT NULL,
  request_id TEXT NOT NULL,
  msg_index INTEGER NOT NULL,
  role TEXT NOT NULL,
  ts_ms INTEGER,
  created_at TEXT NOT NULL,
  text TEXT NOT NULL,
  has_image INTEGER DEFAULT 0,
  images_json TEXT DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS idx_records_user ON records(user_id);
CREATE INDEX IF NOT EXISTS idx_records_req ON records(user_id, request_id);

CREATE TABLE IF NOT EXISTS facts (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  record_id TEXT NOT NULL,
  kind TEXT NOT NULL,             -- transaction / preference / event / attribute / plan
  subject TEXT,                   -- 谁
  relation TEXT,                  -- 买了/偏好/计划/位于
  object TEXT,                    -- 买了什么
  value REAL,                     -- 数值（金额等）
  unit TEXT,                      -- USD / CNY / ""
  event_time TEXT,                -- 事件发生时间（尽量）
  source_type TEXT,               -- user / assistant / third_party
  fact_status TEXT,               -- current / updated / hypothetical / advice / estimate
  confidence REAL DEFAULT 0.8,
  evidence TEXT,                  -- 证据原句
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_facts_user ON facts(user_id);
CREATE INDEX IF NOT EXISTS idx_facts_kind ON facts(user_id, kind);
CREATE INDEX IF NOT EXISTS idx_facts_record ON facts(record_id);

CREATE TABLE IF NOT EXISTS vectors (
  owner_id TEXT PRIMARY KEY,      -- record:<id> 或 fact:<id>
  kind TEXT NOT NULL,             -- record / fact
  user_id TEXT NOT NULL,
  vec BLOB NOT NULL,
  text TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_vectors_user ON vectors(user_id, kind);
"""


class Store:
    def __init__(self, path: str = DB_PATH):
        self.path = path
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(_SCHEMA)
        self.conn.commit()

    # ---------------- 写入 ----------------
    def put_record(self, rec: dict[str, Any]) -> bool:
        with self.lock:
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO records(id,user_id,session_id,request_id,msg_index,role,"
                "ts_ms,created_at,text,has_image,images_json) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (rec["id"], rec["user_id"], rec["session_id"], rec["request_id"], rec["msg_index"],
                 rec["role"], rec.get("ts_ms"), rec["created_at"], rec["text"],
                 1 if rec.get("images") else 0, json.dumps(rec.get("images") or [], ensure_ascii=False)),
            )
            self.conn.commit()
            return cur.rowcount > 0

    def put_fact(self, fact: dict[str, Any]) -> bool:
        with self.lock:
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO facts(id,user_id,record_id,kind,subject,relation,object,value,"
                "unit,event_time,source_type,fact_status,confidence,evidence,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (fact["id"], fact["user_id"], fact["record_id"], fact["kind"], fact.get("subject"),
                 fact.get("relation"), fact.get("object"), fact.get("value"), fact.get("unit"),
                 fact.get("event_time"), fact.get("source_type") or "user",
                 fact.get("fact_status") or "current", float(fact.get("confidence") or 0.8),
                 fact.get("evidence"), fact["created_at"]),
            )
            self.conn.commit()
            return cur.rowcount > 0

    def put_vector(self, owner_id: str, kind: str, user_id: str, vec: bytes, text: str) -> None:
        with self.lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO vectors(owner_id,kind,user_id,vec,text) VALUES(?,?,?,?,?)",
                (owner_id, kind, user_id, vec, text))
            self.conn.commit()

    # ---------------- 读取 ----------------
    def has_request(self, user_id: str, request_id: str) -> bool:
        with self.lock:
            row = self.conn.execute(
                "SELECT 1 FROM records WHERE user_id=? AND request_id=? LIMIT 1",
                (user_id, request_id)).fetchone()
            return row is not None

    def count_records(self, user_id: str) -> int:
        with self.lock:
            return self.conn.execute(
                "SELECT COUNT(*) c FROM records WHERE user_id=?", (user_id,)).fetchone()["c"]

    def records_of(self, user_id: str, ids: list[str] | None = None) -> list[dict]:
        with self.lock:
            if ids:
                q = "SELECT * FROM records WHERE user_id=? AND id IN (%s)" % ",".join("?" * len(ids))
                rows = self.conn.execute(q, (user_id, *ids)).fetchall()
            else:
                rows = self.conn.execute("SELECT * FROM records WHERE user_id=?", (user_id,)).fetchall()
            return [dict(r) for r in rows]

    def all_vectors(self, user_id: str, kind: str | None = None) -> list[dict]:
        with self.lock:
            if kind:
                rows = self.conn.execute(
                    "SELECT * FROM vectors WHERE user_id=? AND kind=?", (user_id, kind)).fetchall()
            else:
                rows = self.conn.execute("SELECT * FROM vectors WHERE user_id=?", (user_id,)).fetchall()
            return [dict(r) for r in rows]

    def facts_of(self, user_id: str, kind: str | None = None) -> list[dict]:
        with self.lock:
            if kind:
                rows = self.conn.execute(
                    "SELECT * FROM facts WHERE user_id=? AND kind=?", (user_id, kind)).fetchall()
            else:
                rows = self.conn.execute("SELECT * FROM facts WHERE user_id=?", (user_id,)).fetchall()
            return [dict(r) for r in rows]

    def get_fact(self, fact_id: str) -> dict | None:
        with self.lock:
            row = self.conn.execute("SELECT * FROM facts WHERE id=?", (fact_id,)).fetchone()
            return dict(row) if row else None


def blob_path(sha: str, fmt: str):
    return BLOB_DIR / sha[:2] / f"{sha}.{fmt}"


def save_blob(raw: bytes, fmt: str) -> str:
    sha = hashlib.sha256(raw).hexdigest()
    p = blob_path(sha, fmt)
    p.parent.mkdir(parents=True, exist_ok=True)
    if not p.exists():
        p.write_bytes(raw)
    return sha


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def ts_to_iso(ms: int | None) -> str:
    if not ms:
        return now_iso()
    try:
        return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).isoformat()
    except Exception:
        return now_iso()
