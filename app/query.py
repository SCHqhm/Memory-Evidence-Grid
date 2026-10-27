"""查询理解 + 事件补全 + 覆盖度判断 + 证据装配。

原则（来自 astra）：
  - Search 只交付**证据**，不交付结论（不写"总价 = X"）
  - 求和类问题要**集合级**证据：把所有相关事件按事件去重后全部给出
  - 证据不足 → 明确哨兵；证据充足 → 事件明细 + 来源记录
"""
from __future__ import annotations

import re
from typing import Any

from . import embed as E
from .config import TOPK_MAX
from .index import tokenize

# ---------------- 意图 ----------------
SUM_RE = re.compile(
    r"how much (total|altogether|in total)|total (amount|spent|cost)|\baltogether\b|"
    r"in total|combined|sum of|全部|总共|一共|加起来|合计|总价|总花费", re.I)
YESNO_RE = re.compile(
    r"^\s*(did|do|does|have|has|was|were|is|are|can|could|will|would)\b|"
    r"有没有|是不是|是否|会不会|能不能", re.I)
CHOICE_RE = re.compile(r"哪(一|个|天|次|种|类)|还是|\bwhich\b|\bor\b", re.I)
TIME_RE = re.compile(r"\bwhen\b|what (day|date|time)|哪(天|日|年|月)|几点|什么时候|多久", re.I)
VISUAL_RE = re.compile(r"\bimage\b|\bpicture\b|\bphoto\b|图(里|中|上)|照片|画面|什么颜色", re.I)


def intent_of(q: str) -> str:
    if SUM_RE.search(q):
        return "sum"
    if TIME_RE.search(q):
        return "time"
    if VISUAL_RE.search(q):
        return "visual"
    if YESNO_RE.search(q):
        return "yes_no"
    if CHOICE_RE.search(q):
        return "choice"
    return "fact"


# ---------------- 实体扩展 ----------------
SYNONYMS = [
    {"coffee maker", "coffee machine", "espresso", "nespresso", "moka", "pour-over", "pour over",
     "dripper", "coffee pot", "brewer", "咖啡机", "咖啡壶", "手冲", "摩卡壶"},
    {"kettle", "水壶", "电水壶"},
    {"grinder", "磨豆机"},
]


def expand_terms(q: str) -> set[str]:
    ql = q.lower()
    toks = set(tokenize(ql))
    for group in SYNONYMS:
        if any(g in ql for g in group):
            toks |= {t.lower() for t in group}
    return {t for t in toks if len(t) >= 2}


def _fact_matches(fact: dict, terms: set[str]) -> int:
    blob = " ".join(str(fact.get(k) or "") for k in ("object", "evidence", "subject", "relation")).lower()
    if not blob.strip():
        return 0
    hit = 0
    for t in terms:
        if t in blob:
            hit += 1
    return hit


def collect_events(store, user_id: str, query: str) -> list[dict[str, Any]]:
    """集合级事件召回：所有相关交易事实（排除建议/假设/助手陈述），按事件去重。"""
    terms = expand_terms(query)
    facts = store.facts_of(user_id, kind="transaction")
    picked = []
    for f in facts:
        if (f.get("source_type") or "user") != "user":
            continue                      # 助手说的一律不算消费
        if (f.get("fact_status") or "current") in ("advice", "hypothetical"):
            continue                      # 建议价 / 假设
        score = _fact_matches(f, terms)
        if score <= 0:
            continue
        picked.append((score, f))
    picked.sort(key=lambda x: -x[0])
    # 事件去重：(金额, 日期, 客体前 12 字)
    seen, out = set(), []
    for _s, f in picked:
        key = (round(float(f.get("value") or 0), 2), (f.get("event_time") or "")[:10],
               (f.get("object") or "")[:12].lower())
        if key in seen:
            continue
        seen.add(key)
        out.append(f)
    return out


def fact_sheet(events: list[dict[str, Any]], query: str) -> str:
    """把事件整理成一条"事实清单"证据（不求和、不下结论）。"""
    if not events:
        return ""
    lines = [f"【交易事件清单】与“{query[:40]}”相关的支出共 {len(events)} 笔（仅列事实，未做计算）："]
    for f in events:
        val = f.get("value")
        unit = f.get("unit") or ""
        money = f"{val:g} {unit}".strip() if val is not None else "金额未写"
        status = f.get("fact_status") or "current"
        obj = f.get("object") or "（未写明）"
        lines.append(f"- {f.get('event_time') or '日期不详'}｜{obj}｜{money}｜状态={status}"
                     f"｜原句：{(f.get('evidence') or '')[:110]}")
    return "\n".join(lines)


def is_insufficient(fused_top: float, n_events: int, intent: str) -> bool:
    """覆盖度判断：求和题看事件数，其它看融合分。"""
    if intent == "sum":
        return n_events == 0
    return fused_top < 1e-4


SENTINEL_TEXT = ("Not enough evidence to answer. No matching memory record supports this question; "
                 "the memory does not contain the information needed. 记忆中缺少回答该问题所需的信息。")


def build_response(store, index, user_id: str, query: str, top_k: int) -> dict[str, Any]:
    top_k = max(1, min(int(top_k or 10), TOPK_MAX))
    intent = intent_of(query)
    res = index.search(user_id, query, want_facts=True)
    fused: dict[str, float] = res["fused"]
    idx = index.get(user_id)

    # 事件清单（求和类必须集合级）
    events = collect_events(store, user_id, query) if intent == "sum" else []

    out: list[dict[str, Any]] = []
    score_floor = 0.0

    if intent == "sum" and events:
        sheet = fact_sheet(events, query)
        out.append({
            "id": "sch_fact_sheet",
            "content": [{"type": "text", "text": sheet}],
            "score": 1.0, "created_at": "",
        })
        for f in events:
            rec = idx["records"].get(f["record_id"])
            if not rec:
                continue
            out.append({
                "id": rec["id"],
                "content": [{"type": "text", "text": rec["text"]}],
                "score": 0.99, "created_at": rec["created_at"],
            })
        score_floor = 0.9

    # 融合结果补齐（facts → 它们所属 records；records 直接给）
    ranked = sorted(fused.items(), key=lambda x: -x[1])
    have = {r["id"] for r in out}
    for oid, sc in ranked:
        if len(out) >= top_k:
            break
        if oid.startswith("f_"):
            f = idx["facts"].get(oid)
            if not f:
                continue
            rid = f["record_id"]
            if rid in have:
                continue
            rec = idx["records"].get(rid)
            if not rec:
                continue
            out.append({
                "id": rec["id"],
                "content": [{"type": "text", "text": rec["text"]}],
                "score": max(score_floor, min(0.95, sc * 40)),
                "created_at": rec["created_at"],
            })
            have.add(rid)
        else:
            rec = idx["records"].get(oid)
            if not rec or oid in have:
                continue
            out.append({
                "id": rec["id"],
                "content": [{"type": "text", "text": rec["text"]}],
                "score": max(score_floor, min(0.95, sc * 40)),
                "created_at": rec["created_at"],
            })
            have.add(oid)

    # 覆盖度判断
    fused_top = ranked[0][1] if ranked else 0.0
    if not out or is_insufficient(fused_top, len(events), intent):
        return {"data": [{"id": "sch_no_evidence",
                          "content": [{"type": "text", "text": SENTINEL_TEXT}],
                          "score": 0.0, "created_at": ""}]}

    # --- 支持度判定：不靠分数阈值，让模型判断"证据能否回答" ---
    from .config import ASSESS as _assess_on
    try:
        if not _assess_on:
            raise RuntimeError("assess disabled")
        from .assess import assess
        verdict = assess(query, [{"id": r["id"], "text": (r["content"][0].get("text") or "")} for r in out])
        if not verdict.get("answerable"):
            import logging as _lg
            _lg.getLogger("sch").info("[assess] 判不足：%s", str(verdict.get("reason"))[:80])
            return {"data": [{"id": "sch_no_evidence",
                              "content": [{"type": "text", "text": SENTINEL_TEXT}],
                              "score": 0.0, "created_at": ""}]}
    except Exception:
        pass

    # 分数单调递减
    out = out[:top_k]
    cur = 1.0
    for r in out:
        r["score"] = round(min(cur, max(0.0, float(r["score"] or 0.0))), 6)
        cur = r["score"]
    return {"data": out}
