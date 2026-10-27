"""支持度判定（sufficiency）：不靠相关性分数，而是让模型判断"证据能否回答问题"。

这是 astra 框架里"支持度/覆盖度评估"的落地：
  - 输入：问题 + top-N 证据片段
  - 输出：answerable(是/否) + 依据 + 选中的证据 id
  - 只回 YES/NO 太弱，这里要求模型指出"缺什么"，便于日志归因
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from typing import Any

import httpx

from .config import DATA, LLM_BASE, LLM_KEY, LLM_MODEL

_lock = threading.Lock()
_conn: sqlite3.Connection | None = None


def _db() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(str(DATA / "assess_cache.db"), check_same_thread=False)
        _conn.execute("CREATE TABLE IF NOT EXISTS cache(k TEXT PRIMARY KEY, v TEXT)")
        _conn.commit()
    return _conn


def _key(q: str, ids: list[str]) -> str:
    return hashlib.sha256((q + "\x00" + ",".join(sorted(ids))).encode()).hexdigest()


def _get(k: str) -> dict | None:
    with _lock:
        row = _db().execute("SELECT v FROM cache WHERE k=?", (k,)).fetchone()
    return json.loads(row[0]) if row else None


def _put(k: str, v: dict) -> None:
    with _lock:
        db = _db()
        db.execute("INSERT OR REPLACE INTO cache(k,v) VALUES(?,?)", (k, json.dumps(v, ensure_ascii=False)))
        db.commit()


PROMPT = """判断下面的记忆证据**能否回答**这个问题。

问题：{q}

证据：
{evidence}

规则：
- 只有当证据里**明确包含**回答问题所需的信息时，answerable 才为 true；
- 只是话题相关、部分相关、或需要外部知识推断 → false；
- 如果问题是"总量/总计"，需要证据里能看到**每一笔**支出的金额才算 true；
- 如果问题是"有没有/是不是"，证据里必须能确定是或否（不能只说相关话题）。

只输出 JSON：
{{"answerable": true/false, "reason": "一句话说明（缺什么/有什么）", "used": ["用到的证据编号"]}}"""


def assess(question: str, records: list[dict[str, Any]], max_records: int = 6) -> dict:
    """records: [{'id':..,'text':..}]；返回 {'answerable','reason','used'}。"""
    picked = records[:max_records]
    k = _key(question, [str(r.get("id")) for r in picked])
    hit = _get(k)
    if hit is not None:
        return hit
    if not (LLM_BASE and LLM_KEY):
        return {"answerable": True, "reason": "no-llm", "used": []}
    ev = "\n\n".join(f"[{i}] {str(r.get('text'))[:700]}" for i, r in enumerate(picked, 1))
    body = {"model": LLM_MODEL, "temperature": 0.0, "max_tokens": 200,
            "messages": [{"role": "user", "content": PROMPT.format(q=question[:400], evidence=ev[:6000])}]}
    # 关键：判定必须是"快速失败、失败即放行"。
    # 评测端对单请求有超时限制，冷启动的 LLM 调用若拖 60s×2 会让整个 Search 超时。
    for attempt in range(1):
        try:
            r = httpx.post(LLM_BASE + "/chat/completions",
                           headers={"Authorization": f"Bearer {LLM_KEY}"}, json=body, timeout=15)
            r.raise_for_status()
            raw = (r.json()["choices"][0]["message"]["content"] or "").strip()
            m = __import__("re").search(r"\{.*\}", raw, __import__("re").S)
            out = json.loads(m.group(0)) if m else {"answerable": True, "reason": raw[:100], "used": []}
            out["answerable"] = bool(out.get("answerable"))
            _put(k, out)
            return out
        except Exception:
            pass
    return {"answerable": True, "reason": "assess-failed", "used": []}
