"""批量灌库：直接写 SQLite + 批量 embedding（比走 HTTP 快 50 倍）。

用法：
  python bulk_ingest.py --dataset ../data/prepared/memgallery --llm-facts 0
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from app import embed as E                      # noqa: E402
from app.facts import extract_transactions, extract_with_llm, fact_index_text  # noqa: E402
from app.store import Store, ts_to_iso          # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", required=True)
    ap.add_argument("--db", default="data/sch.db")
    ap.add_argument("--llm-facts", type=int, default=0)
    args = ap.parse_args()

    store = Store(args.db)
    reqs = [json.loads(l) for l in open(os.path.join(args.dataset, "add.jsonl"), encoding="utf-8")]
    print(f"  待处理 {len(reqs)} 个请求")

    recs, facts = [], []
    t0 = time.time()
    for r in reqs:
        uid = r.get("user_id")
        rid_req = r.get("request_id")
        sid = r.get("session_id")
        if store.has_request(uid, rid_req):
            continue
        for mi, m in enumerate(r.get("messages") or []):
            content = m.get("content")
            parts = [{"type": "text", "text": content}] if isinstance(content, str) else [
                p for p in (content or []) if isinstance(p, dict)]
            texts = [str(p.get("text") or "") for p in parts if p.get("type") == "text"]
            text = "\n".join(t for t in texts if t).strip()
            if not text:
                continue
            ts = m.get("timestamp")
            created = ts_to_iso(ts if isinstance(ts, int) else None)
            rid = "r_" + hashlib.sha256(f"{uid}|{rid_req}|{mi}".encode()).hexdigest()[:24]
            recs.append({"id": rid, "user_id": uid, "session_id": sid, "request_id": rid_req,
                         "msg_index": mi, "role": str(m.get("role") or "user"),
                         "ts_ms": ts if isinstance(ts, int) else None,
                         "created_at": created, "text": text, "images": []})
            fs = extract_transactions(text, str(m.get("role") or "user"), rid, uid, created)
            if args.llm_facts:
                fs += extract_with_llm(text, str(m.get("role") or "user"), rid, uid, created)
            facts += fs
    print(f"  解析完成：记录 {len(recs)} | 事实 {len(facts)} | {time.time()-t0:.1f}s")

    # 批量向量
    t1 = time.time()
    rec_texts = []
    for rec in recs:
        fs = [f for f in facts if f["record_id"] == rec["id"]][:4]
        rec_texts.append(rec["text"] + ("\n\n[facts] " + " ; ".join(fact_index_text(f) for f in fs) if fs else ""))
    fact_texts = [fact_index_text(f) for f in facts]
    all_texts = rec_texts + fact_texts
    print(f"  批量 embedding {len(all_texts)} 条 ...")
    vecs = E.embed(all_texts) if all_texts else []
    print(f"  embedding 完成 {time.time()-t1:.1f}s")

    # 写库
    t2 = time.time()
    for rec, vt, vec in zip(recs, rec_texts, vecs[: len(recs)]):
        store.put_record(rec)
        store.put_vector(rec["id"], "record", rec["user_id"], E.to_blob(vec), vt)
    for f, vt, vec in zip(facts, fact_texts, vecs[len(recs):]):
        if store.put_fact(f):
            store.put_vector("f_" + f["id"][2:], "fact", f["user_id"], E.to_blob(vec), vt)
    print(f"  写库完成 {time.time()-t2:.1f}s | 总耗时 {time.time()-t0:.1f}s")
    print("  ", store.conn.execute("select count(*) from records").fetchone()[0], "records /",
          store.conn.execute("select count(*) from facts").fetchone()[0], "facts")


if __name__ == "__main__":
    main()
