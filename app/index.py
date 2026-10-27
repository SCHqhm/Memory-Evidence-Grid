"""三路召回 + RRF 融合：dense / BM25 / 结构化事实。"""
from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from typing import Any

from . import embed as E
from .config import BM25_TOP, DENSE_TOP, FACT_TOP, RRF_K


# ---------------- 分词 ----------------
_CJK = re.compile(r"[\u4e00-\u9fff]")
_TOK = re.compile(r"[a-z0-9]+|[\u4e00-\u9fff]")


def tokenize(text: str) -> list[str]:
    text = (text or "").lower()
    toks = _TOK.findall(text)
    out: list[str] = []
    for t in toks:
        out.append(t)
    # 中文 2-gram
    cjk = [t for t in toks if _CJK.match(t)]
    out += ["".join(cjk[i:i + 2]) for i in range(len(cjk) - 1)]
    return out


# ---------------- BM25 ----------------
class BM25:
    def __init__(self, docs: dict[str, str], k1: float = 1.2, b: float = 0.75):
        self.ids = list(docs.keys())
        self.tf: dict[str, Counter] = {}
        self.df: Counter = Counter()
        self.len: dict[str, int] = {}
        for i in self.ids:
            toks = tokenize(docs[i])
            c = Counter(toks)
            self.tf[i] = c
            self.len[i] = len(toks)
            for t in c:
                self.df[t] += 1
        self.N = max(1, len(self.ids))
        self.avg = max(1.0, sum(self.len.values()) / self.N)
        self.k1, self.b = k1, b

    def scores(self, query: str) -> dict[str, float]:
        q = tokenize(query)
        out: dict[str, float] = defaultdict(float)
        for t in q:
            if t not in self.df:
                continue
            idf = math.log(1 + (self.N - self.df[t] + 0.5) / (self.df[t] + 0.5))
            for i in self.ids:
                f = self.tf[i].get(t, 0)
                if not f:
                    continue
                denom = f + self.k1 * (1 - self.b + self.b * self.len[i] / self.avg)
                out[i] += idf * f * (self.k1 + 1) / denom
        return dict(out)


# ---------------- 向量 ----------------
def cos(a: list[float], b: list[float]) -> float:
    sa = sum(x * x for x in a) ** 0.5
    sb = sum(x * x for x in b) ** 0.5
    if not sa or not sb:
        return 0.0
    return sum(x * y for x, y in zip(a, b)) / (sa * sb)


class Index:
    """按 user 惰性构建的索引（records + facts）。"""

    def __init__(self, store):
        self.store = store
        self._cache: dict[str, dict[str, Any]] = {}

    def _build(self, user_id: str) -> dict[str, Any]:
        recs = self.store.records_of(user_id)
        facts = self.store.facts_of(user_id)
        vecs = {v["owner_id"]: E.from_blob(v["vec"]) for v in self.store.all_vectors(user_id)}
        rec_text = {r["id"]: (r["text"] or "") for r in recs}
        fact_text = {f["id"]: (f.get("evidence") or "") + " " + (f.get("object") or "") for f in facts}
        return {
            "records": {r["id"]: r for r in recs},
            "facts": {f["id"]: f for f in facts},
            "vecs": vecs,
            "bm25_rec": BM25(rec_text) if rec_text else None,
            "bm25_fact": BM25(fact_text) if fact_text else None,
        }

    def get(self, user_id: str) -> dict[str, Any]:
        if user_id not in self._cache:
            self._cache[user_id] = self._build(user_id)
        return self._cache[user_id]

    def invalidate(self, user_id: str) -> None:
        self._cache.pop(user_id, None)

    # ---------- 单路 ----------
    def dense(self, user_id: str, query: str, kind: str = "record", top: int = DENSE_TOP) -> list[tuple[str, float]]:
        idx = self.get(user_id)
        try:
            qv = E.embed([query])[0]
        except Exception:
            return []
        if not any(qv):                     # embedding 失败/超时 → 退化到 BM25
            return []
        scored = []
        for vec in self.store.all_vectors(user_id, kind):
            v = idx["vecs"].get(vec["owner_id"])
            if not v:
                continue
            scored.append((vec["owner_id"], cos(qv, v)))
        scored.sort(key=lambda x: -x[1])
        return scored[:top]

    def bm25(self, user_id: str, query: str, kind: str = "record", top: int = BM25_TOP) -> list[tuple[str, float]]:
        idx = self.get(user_id)
        bm = idx["bm25_rec"] if kind == "record" else idx["bm25_fact"]
        if not bm:
            return []
        s = bm.scores(query)
        return sorted(s.items(), key=lambda x: -x[1])[:top]

    # ---------- 结构化 ----------
    def structured_facts(self, user_id: str, query: str, kind: str = "transaction",
                         top: int = FACT_TOP) -> list[tuple[str, float]]:
        """按关键词命中事实字段（object/evidence），给固定分。"""
        idx = self.get(user_id)
        q = (query or "").lower()
        hits = []
        for fid, f in idx["facts"].items():
            if kind and f.get("kind") != kind:
                continue
            blob = " ".join(str(f.get(k) or "") for k in ("object", "evidence", "subject", "relation")).lower()
            score = 0.0
            for tok in set(tokenize(q)):
                if len(tok) >= 2 and tok in blob:
                    score += 1.0
            if score > 0:
                hits.append((fid, score))
        hits.sort(key=lambda x: -x[1])
        return hits[:top]

    # ---------- RRF ----------
    def search(self, user_id: str, query: str, want_facts: bool = True) -> dict[str, Any]:
        lists: dict[str, list[tuple[str, float]]] = {}
        lists["dense_rec"] = self.dense(user_id, query, "record")
        lists["bm25_rec"] = self.bm25(user_id, query, "record")
        if want_facts:
            lists["dense_fact"] = self.dense(user_id, query, "fact")
            lists["bm25_fact"] = self.bm25(user_id, query, "fact")
            lists["struct_fact"] = self.structured_facts(user_id, query)
        fused: dict[str, float] = defaultdict(float)
        for name, lst in lists.items():
            for rank, (oid, _s) in enumerate(lst, 1):
                fused[oid] += 1.0 / (RRF_K + rank)
        return {"lists": lists, "fused": dict(fused)}
