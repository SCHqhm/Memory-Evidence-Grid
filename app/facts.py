"""原子事实抽取。

原则（来自 astra）：
  - 一条事实 = 主体-关系-客体（数值+单位）+ 事件时间 + 来源类型 + 事实状态 + 证据原句
  - **交易类单独成事件**，绝不把多笔合并成"花了很多钱"这种摘要
  - 来源与状态必须显式：用户亲述 / 助手建议 / 假设 / 约数
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from .config import LLM_BASE, LLM_KEY, LLM_MODEL

# ---------------- 词表 ----------------
WORD_NUM = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
    "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14,
    "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19,
    "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70,
    "eighty": 80, "ninety": 90, "hundred": 100,
}
AMOUNT_RE = re.compile(
    r"\$\s?(?P<usd>\d[\d,]*(?:\.\d+)?)"
    r"|(?P<num>\d[\d,]*(?:\.\d+)?)\s?(?P<unit>dollars?|usd|bucks|元|块)"
    r"|(?P<cn>[一二三四五六七八九十百千]+)\s?(?P<cunit>元|块)"
    # 英文数字：只允许"数字词"组成，避免贪婪吞掉整句
    r"|\b(?P<words>(?:(?:one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|fifteen|"
    r"twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety|hundred)(?:[-\s]|\b))+?)"
    r"[\s]*(?P<wunit>bucks|dollars)\b",
    re.I,
)
BUY_RE = re.compile(
    r"\b(bought|buy|purchased?|picked up|grabbed|ordered|paid|spent|cost me|"
    r"hit my account|charged|scored|got a|invested in)\b|买了|买下|花了|付了|入手", re.I)
ADVICE_RE = re.compile(
    r"expect to spend|you should|budget|price range|sweet spot|entry-level|mid-range|"
    r"diminishing returns|i(?:'d| would) suggest|typically|on average|"
    r"the best|worth (?:it|the)|推荐|建议|一般要|大概要|行情", re.I)
HYPOTHETICAL_RE = re.compile(
    r"\bif\b|\bwhether\b|\bmight\b|\bcould\b|\bconsidering\b|\bthinking about\b|"
    r"on the fence|let's say|\bwould be\b|假设|如果|考虑", re.I)
ESTIMATE_RE = re.compile(r"\babout\b|\baround\b|\broughly\b|\balmost\b|\bapproximately\b|大约|差不多|左右", re.I)
OBJECT_STOP = re.compile(
    r"\b(at|for|from|in|on|with|because|and|but|so|which|that|when|while|after|before|"
    r"about|to|of|it|this|that|these|those)\b|，|。|,", re.I)
PRONOUN_OBJ = {"it", "that", "this", "one", "them", "those", "these"}


def _num_from_words(s: str) -> float | None:
    words = [w for w in re.split(r"[-\s]+", s.lower()) if w]
    total, cur = 0.0, 0.0
    for w in words:
        if w in ("and", ""):
            continue
        if w not in WORD_NUM:
            return None
        n = WORD_NUM[w]
        if n == 100:
            cur = max(cur, 1) * 100
        else:
            cur += n
    total += cur
    return total if total else None


def _cn_num(s: str) -> float | None:
    mp = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
    v = 0
    for ch in s:
        if ch in mp:
            v = v * 10 + mp[ch] if v else mp[ch]
        elif ch == "十":
            v = (v or 1) * 10
        elif ch == "百":
            v = (v or 1) * 100
    return float(v) if v else None


def _parse_amount(m: re.Match) -> tuple[float | None, str]:
    if m.group("usd"):
        return float(m.group("usd").replace(",", "")), "USD"
    if m.group("num"):
        v = float(m.group("num").replace(",", ""))
        unit = (m.group("unit") or "").lower()
        return v, ("CNY" if unit in ("元", "块") else "USD")
    if m.group("cn"):
        return _cn_num(m.group("cn")), "CNY"
    if m.group("words"):
        v = _num_from_words(m.group("words"))
        if v:
            return v, "USD"
    return None, ""


def _object_after(text: str, end: int) -> str:
    """取购买动词/金额之后的名词短语作为"买了什么"。"""
    tail = text[end:end + 90]
    tail = re.sub(r"^\s*(?:for|about|around|at|only|just|approximately)?\s*", "", tail, flags=re.I)
    m = OBJECT_STOP.search(tail)
    obj = (tail[:m.start()] if m else tail).strip(" .!?;:—-")
    obj = re.sub(r"^(?:a|an|the|my|some|very|really|new)\s+", "", obj, flags=re.I).strip()
    return obj[:80]


_OBJ_BEFORE_VERB = re.compile(
    r"(?P<obj>[A-Za-z][\w\-']*(?:\s+[\w\-']+){0,4}?)\s+(?:that\s+)?(?:I\s+)?(?:just\s+)?"
    r"(?:bought|purchased|picked up|grabbed|ordered)\b", re.I)


_LEAD_JUNK = re.compile(
    r"^(?:i|we|he|she|they|you|it|and|but|so|that|this|these|those|the|a|an|my|our|his|her|some|"
    r"saw|think|thinking|keep|about|of|for|to|in|on|at|with|from|was|were|is|are|had|have|"
    r"recently|just|actually|really|only)\b[\s,]*", re.I)


def _clean_obj(s: str) -> str:
    prev = None
    while prev != s:
        prev = s
        s = _LEAD_JUNK.sub("", s).strip(" .,-—")
    s = re.sub(r"\b(?:did|does|do|is|was|were|has|had|have|in|on|at|for|with|from|to)$", "", s, flags=re.I).strip(" .,-—")
    return s


def _head_noun(s: str, keep: int = 3) -> str:
    words = [w for w in re.split(r"\s+", s.strip()) if w]
    if len(words) > keep:
        words = words[-keep:]
    return " ".join(words)


def _object_by_grammar(sentence: str) -> str:
    """英文里定语在前：'the Nespresso machine I bought' → Nespresso machine。"""
    best = ""
    for m in _OBJ_BEFORE_VERB.finditer(sentence):
        cand = _clean_obj(_head_noun(m.group("obj")))
        if 1 < len(cand) <= 60 and cand.lower() not in PRONOUN_OBJ:
            best = cand
    return best


def _object_before(text: str, start: int) -> str:
    head = text[max(0, start - 90):start]
    m = None
    for m in re.finditer(r"\b(?:bought|purchased?|picked up|grabbed|ordered|got)\b", head, re.I):
        pass
    if m:
        seg = head[m.end():]
        mm = OBJECT_STOP.search(seg)
        seg = seg[:mm.start()] if mm else seg
        seg = re.sub(r"^(?:a|an|the|my|some|very|really|new)\s+", "", seg.strip(), flags=re.I)
        return seg.strip(" .!?;:—-")[:80]
    return ""


def classify_status(sentence: str, role: str) -> tuple[str, str, float]:
    """返回 (source_type, fact_status, confidence)。"""
    if role == "assistant":
        src = "assistant"
    else:
        src = "user"
    if ADVICE_RE.search(sentence):
        return src, "advice", 0.3
    if HYPOTHETICAL_RE.search(sentence):
        return src, "hypothetical", 0.35
    if role == "assistant":
        return src, "advice", 0.4
    if ESTIMATE_RE.search(sentence):
        return src, "estimate", 0.6
    return src, "current", 0.9


def _sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?。！？])\s+|\n+", text)
    return [p.strip() for p in parts if p and p.strip()]


def extract_transactions(text: str, role: str, record_id: str, user_id: str,
                         created_at: str) -> list[dict[str, Any]]:
    """规则法抽交易事实：金额 + 购买动词 + 客体 + 状态。"""
    out: list[dict[str, Any]] = []
    for sent in _sentences(text):
        amounts = list(AMOUNT_RE.finditer(sent))
        if not amounts:
            continue
        has_buy = bool(BUY_RE.search(sent))
        if not has_buy:
            continue
        src, status, conf = classify_status(sent, role)
        for m in amounts:
            value, unit = _parse_amount(m)
            if not value:
                continue
            # 语序判断：动词前若是 "I / that I / which I" → 定语从句，宾语在**前**；
            # 否则是 "动词+宾语" 语序，宾语在**后**。
            verb = None
            for vm in re.finditer(r"\b(bought|purchased|picked up|grabbed|ordered|got a|scored)\b", sent, re.I):
                if vm.start() <= m.start():
                    verb = vm
            obj = ""
            if verb:
                left = sent[max(0, verb.start() - 14):verb.start()]
                relative = bool(re.search(r"\b(i|we)\s*$", left.strip() + " ", re.I)) or \
                           bool(re.search(r"\b(that|which)\s+(i|we)\s*$", left, re.I))
                if relative:
                    obj = _object_by_grammar(sent)
                else:
                    obj = _clean_obj(_head_noun(_object_after(sent, verb.end())))
            if not obj:
                obj = _object_by_grammar(sent)
            if not obj:
                obj = _clean_obj(_object_before(sent, m.start())) or _clean_obj(_object_after(sent, m.end()))
            if not obj or obj.lower() in PRONOUN_OBJ or len(obj) < 2:
                obj = ""
            fid = "f_" + hashlib.sha256(
                f"{record_id}|{sent[:60]}|{m.group(0)}".encode("utf-8")).hexdigest()[:24]
            out.append({
                "id": fid, "user_id": user_id, "record_id": record_id, "kind": "transaction",
                "subject": "user" if role == "user" else role,
                "relation": "spent_on", "object": obj, "value": value, "unit": unit,
                "event_time": created_at[:10], "source_type": src, "fact_status": status,
                "confidence": conf, "evidence": sent[:300], "created_at": created_at,
            })
    return out


def extract_with_llm(text: str, role: str, record_id: str, user_id: str,
                     created_at: str) -> list[dict[str, Any]]:
    """用 gpt-4o-mini 抽"非交易类"原子事实（偏好/事件/属性/计划）。"""
    if not (LLM_BASE and LLM_KEY) or len(text) < 30:
        return []
    prompt = (
        "从下面这段对话里抽取**原子事实**。只输出 JSON 数组，每项字段：\n"
        '{"kind":"preference|event|attribute|plan","subject":"谁","relation":"动词短语",'
        '"object":"客体或内容","event_time":"YYYY-MM-DD 或空","evidence":"原文最短原句"}\n'
        "规则：\n"
        "- 只抽**明确陈述**的事实；推测/假设/建议不要抽成事实\n"
        "- 一段话最多 4 条\n"
        "- 没有事实就输出 []\n\n"
        f"[{role}] {text[:1200]}"
    )
    try:
        import httpx
        r = httpx.post(LLM_BASE + "/chat/completions",
                       headers={"Authorization": f"Bearer {LLM_KEY}"},
                       json={"model": LLM_MODEL, "temperature": 0.0, "max_tokens": 500,
                             "messages": [{"role": "user", "content": prompt}]}, timeout=60)
        r.raise_for_status()
        raw = (r.json()["choices"][0]["message"]["content"] or "").strip()
        m = re.search(r"\[.*\]", raw, re.S)
        if not m:
            return []
        items = json.loads(m.group(0))
    except Exception:
        return []
    out = []
    for it in items[:4]:
        if not isinstance(it, dict):
            continue
        ev = str(it.get("evidence") or "")[:300]
        fid = "f_" + hashlib.sha256(f"{record_id}|{ev}".encode("utf-8")).hexdigest()[:24]
        out.append({
            "id": fid, "user_id": user_id, "record_id": record_id,
            "kind": str(it.get("kind") or "event")[:20],
            "subject": str(it.get("subject") or "")[:40],
            "relation": str(it.get("relation") or "")[:40],
            "object": str(it.get("object") or "")[:120],
            "value": None, "unit": "",
            "event_time": str(it.get("event_time") or created_at[:10])[:10],
            "source_type": "user" if role == "user" else role,
            "fact_status": "current", "confidence": 0.7,
            "evidence": ev or text[:120], "created_at": created_at,
        })
    return out


def fact_index_text(fact: dict) -> str:
    """把事实变成可检索文本（给 dense / BM25 用）。"""
    bits = [fact.get("kind") or "", fact.get("subject") or "", fact.get("relation") or "",
            fact.get("object") or ""]
    if fact.get("value") is not None:
        bits.append(f"{fact['value']:g} {fact.get('unit') or ''}".strip())
    bits.append(f"[{fact.get('source_type')}/{fact.get('fact_status')}]")
    bits.append(fact.get("evidence") or "")
    return " ".join(b for b in bits if b).strip()
