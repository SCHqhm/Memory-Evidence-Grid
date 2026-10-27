"""Embedding 客户端：只调用官方指定的 text-embedding-v4，带本地缓存。"""
from __future__ import annotations

import hashlib
import sqlite3
import threading
from typing import Iterable

import httpx

from .config import DATA, EMBED_BASE, EMBED_DIM, EMBED_KEY, EMBED_MODEL

_lock = threading.Lock()
_conn: sqlite3.Connection | None = None


def _db() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(str(DATA / "embed_cache.db"), check_same_thread=False)
        _conn.execute("CREATE TABLE IF NOT EXISTS cache(k TEXT PRIMARY KEY, v TEXT)")
        _conn.commit()
    return _conn


def _key(text: str) -> str:
    return hashlib.sha256((EMBED_MODEL + "\x00" + text).encode("utf-8")).hexdigest()


def _get(text: str) -> list[float] | None:
    with _lock:
        row = _db().execute("SELECT v FROM cache WHERE k=?", (_key(text),)).fetchone()
    if not row:
        return None
    try:
        return [float(x) for x in row[0].split(",")]
    except Exception:
        return None


def _put(text: str, vec: list[float]) -> None:
    with _lock:
        db = _db()
        db.execute("INSERT OR REPLACE INTO cache(k,v) VALUES(?,?)",
                   (_key(text), ",".join(f"{x:.6f}" for x in vec)))
        db.commit()


def embed(texts: Iterable[str]) -> list[list[float]]:
    """批量取向量；命中缓存的不发请求。"""
    items = [t or "" for t in texts]
    out: list[list[float] | None] = [_get(t) if t else None for t in items]
    todo = [t for t, v in zip(items, out) if v is None and t]
    # 去重后请求
    uniq = list(dict.fromkeys(todo))
    # 单条截断，避免超长导致 400
    def _clip(t: str) -> str:
        return t[:2000]

    def _req(batch: list[str]) -> list[list[float]]:
        r = httpx.post(EMBED_BASE + "/embeddings",
                       headers={"Authorization": f"Bearer {EMBED_KEY}"},
                       json={"model": EMBED_MODEL, "input": [_clip(b) for b in batch],
                             "dimensions": EMBED_DIM, "encoding_format": "float"},
                       timeout=12)
        r.raise_for_status()
        data = r.json().get("data") or []
        return [[float(x) for x in d["embedding"]]
                for d in sorted(data, key=lambda x: x.get("index", 0))]

    if uniq:
        batches = [uniq[i:i + 25] for i in range(0, len(uniq), 25)]

        def _one(batch):
            # 失败**不再逐条重试**（会把 12s 放大成 120s）；直接返回零向量，
            # 由上层 dense() 识别为"无向量"，退化到 BM25/结构化召回。
            try:
                return batch, _req(batch)
            except Exception:
                return batch, [[0.0] * EMBED_DIM for _ in batch]

        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=4) as pool:
            for batch, vecs in pool.map(_one, batches):
                for t, v in zip(batch, vecs):
                    _put(t, v)
                    for j, orig in enumerate(items):
                        if orig == t and out[j] is None:
                            out[j] = v
    # 二次补齐（同一文本多次出现）
    cache: dict[str, list[float]] = {}
    for t, v in zip(items, out):
        if v is not None and t:
            cache[t] = v
    res: list[list[float]] = []
    for t, v in zip(items, out):
        if v is not None:
            res.append(v)
        else:
            res.append(cache.get(t) or [0.0] * EMBED_DIM)
    return res


def to_blob(vec: list[float]) -> bytes:
    import array
    return array.array("f", vec).tobytes()


def from_blob(b: bytes) -> list[float]:
    import array
    a = array.array("f")
    a.frombytes(b)
    return list(a)
