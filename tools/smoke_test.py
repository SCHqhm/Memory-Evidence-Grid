"""冒烟测试：验证 Add / Search 契约。"""
from __future__ import annotations

import argparse
import json
import sys
import time

import httpx


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8100")
    ap.add_argument("--key", default="sch-memory")
    args = ap.parse_args()
    H = {"Authorization": f"Token {args.key}"}
    uid = f"smoke:{int(time.time())}"
    ok = True

    r = httpx.get(f"{args.base}/sch/health", timeout=20)
    print(f"  health        -> {r.status_code} {r.text[:80]}")
    ok &= r.status_code == 200

    msgs = [
        {"role": "user", "timestamp": 1714780800000,
         "content": "I recently picked up a very basic pour-over kit for my slow mornings "
                    "that was only about twenty-five bucks."},
        {"role": "user", "timestamp": 1715040000000,
         "content": "I just checked my banking app and saw that the Nespresso machine I bought "
                    "last week did in fact hit my account for exactly $150.00."},
    ]
    r = httpx.post(f"{args.base}/sch/add", headers=H, timeout=120,
                   json={"request_id": f"{uid}:r1", "user_id": uid, "session_id": f"{uid}:s", "messages": msgs})
    print(f"  add           -> {r.status_code} {str(r.text)[:100]}")
    ok &= r.status_code == 200

    r = httpx.post(f"{args.base}/sch/search", headers=H, timeout=180,
                   json={"user_id": uid, "top_k": 20,
                         "query": [{"type": "text", "text": "How much total have I spent on coffee makers?"}]})
    data = (r.json() or {}).get("data") or []
    print(f"  search(sum)   -> {r.status_code} n={len(data)}")
    if data:
        first = data[0]["content"][0].get("text", "")[:160]
        print(f"    [1] {first}")
        ok &= "150" in json.dumps(data) or "25" in json.dumps(data)

    scores = [d.get("score") for d in data]
    ok &= all(scores[i] >= scores[i + 1] for i in range(len(scores) - 1))
    ids = [d.get("id") for d in data]
    ok &= len(ids) == len(set(ids))

    r = httpx.post(f"{args.base}/sch/search", headers=H, timeout=180,
                   json={"user_id": uid, "top_k": 5,
                         "query": [{"type": "text", "text": "What is my blood type?"}]})
    d2 = (r.json() or {}).get("data") or []
    print(f"  search(absent)-> n={len(d2)} id={d2[0]['id'] if d2 else '-'}")
    ok &= bool(d2) and d2[0]["id"] == "sch_no_evidence"

    r = httpx.post(f"{args.base}/sch/search", headers={"Authorization": "Token wrong"}, timeout=20,
                   json={"user_id": uid, "query": "x", "top_k": 1})
    print(f"  auth(bad key) -> {r.status_code}")
    ok &= r.status_code == 401

    print(f"\n  {'PASSED' if ok else 'FAILED'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
