"""text-embedding-v4 纯向量基线：与 MEG 在同一批样本上对比。

基线定义（最简、可复现）：
  - 只用 dense（text-embedding-v4, cosine）
  - 无 BM25、无结构化事实、无 RRF、无重排、无拒答
  - 一个"记忆记录"= 一条消息（与 MEG 的 records 同粒度）

用法：
  python tools/baseline_eval.py --dataset ../agent_memory/data/prepared/memgallery \
      --samples 12 --users 20
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import embed as E  # noqa: E402
from app.index import cos      # noqa: E402


def load_jsonl(p):
    with open(p, encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                yield json.loads(line)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", required=True)
    ap.add_argument("--samples", type=int, default=12)
    ap.add_argument("--users", type=int, default=20)
    ap.add_argument("--topk", type=int, default=20)
    args = ap.parse_args()

    adds = list(load_jsonl(os.path.join(args.dataset, "add.jsonl")))
    samples = list(load_jsonl(os.path.join(args.dataset, "samples.jsonl")))

    # 每个用户"每条消息"作为一条记忆
    per_user = defaultdict(list)          # uid -> [(record_key, text)]
    for r in adds:
        uid, rid = r.get("user_id"), r.get("request_id")
        for mi, m in enumerate(r.get("messages") or []):
            c = m.get("content")
            parts = [{"type": "text", "text": c}] if isinstance(c, str) else [p for p in (c or []) if isinstance(p, dict)]
            text = "\n".join(str(p.get("text") or "") for p in parts if p.get("type") == "text").strip()
            if text:
                per_user[uid].append((f"{rid}", text))

    users = sorted(per_user, key=lambda u: len(per_user[u]))[: args.users]
    print(f"  用户 {len(users)} | 记忆条数 {sum(len(per_user[u]) for u in users)}")

    picked = [s for s in samples if (s.get("request") or {}).get("user_id") in users][: args.samples]
    print(f"  样本 {len(picked)}")

    # 预编码
    vec_cache = {}
    for u in users:
        texts = [t for _, t in per_user[u]]
        vs = E.embed(texts)
        for (key, _t), v in zip(per_user[u], vs):
            vec_cache[(u, key)] = v

    hit1 = r10 = anyg = 0.0
    for s in picked:
        req = s.get("request") or {}
        uid = req.get("user_id")
        q = " ".join(str(p.get("text") or "") for p in req.get("query", []) if isinstance(p, dict)) or str(req.get("query"))
        gold = set(s.get("gold_request_ids") or [])
        qv = E.embed([q])[0]
        scored = [(key, cos(qv, vec_cache[(uid, key)])) for key, _ in per_user[uid]]
        scored.sort(key=lambda x: -x[1])
        reqs = [k for k, _ in scored[: args.topk]]
        hit1 += 1.0 if reqs and reqs[0] in gold else 0.0
        r10 += len(gold & set(reqs[:10])) / len(gold) if gold else 0.0
        anyg += 1.0 if (gold & set(reqs)) else 0.0

    n = max(1, len(picked))
    print(f"\n  === text-embedding-v4 纯向量基线 ===")
    print(f"  Hit@1      {hit1/n:.3f}")
    print(f"  R@gold@10  {r10/n:.3f}")
    print(f"  any_gold@{args.topk} {anyg/n:.3f}")
    print(f"  n          {n}")


if __name__ == "__main__":
    main()
