"""FastAPI：AML 兼容的 Add / Search（挂在 /sch 前缀下）。"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from typing import Any

from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse

from . import embed as E
from .config import LLM_FACTS, EMBED_KEY, LLM_KEY, MAX_IMAGES_PER_ADD, MAX_IMAGE_BYTES
from .facts import extract_transactions, extract_with_llm, fact_index_text
from .index import Index
from .query import build_response
from .store import Store, now_iso, save_blob, ts_to_iso

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s sch: %(message)s")
log = logging.getLogger("sch")

app = FastAPI(title="SCH Memory", version="0.1.0")
store = Store()
index = Index(store)

from .config import API_KEYS as AUTH_KEYS


# ---------------- 鉴权 ----------------
def _check_auth(authorization: str | None, x_api_key: str | None) -> bool:
    if not AUTH_KEYS:
        return True
    tok = ""
    if authorization:
        parts = authorization.split()
        tok = parts[-1] if parts else ""
    if not tok and x_api_key:
        tok = x_api_key
    return tok in AUTH_KEYS


def _err(code: int, msg: str) -> JSONResponse:
    return JSONResponse(status_code=code,
                        content={"error": {"code": code, "message": msg, "type": "invalid_request_error"}})


# ---------------- 工具 ----------------
def _sniff_fmt(raw: bytes) -> str | None:
    if raw[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if raw[:2] == b"\xff\xd8":
        return "jpeg"
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "webp"
    if raw[:3] == b"GIF":
        return "gif"
    return None


def _decode_data_uri(url: str) -> tuple[bytes, str] | None:
    m = re.match(r"^data:image/(png|jpeg|jpg|webp|gif);base64,(.*)$", url or "", re.S | re.I)
    if not m:
        return None
    import base64
    try:
        return base64.b64decode(m.group(2)), ("jpeg" if m.group(1).lower() == "jpg" else m.group(1).lower())
    except Exception:
        return None


def _norm_content(content: Any) -> list[dict]:
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if isinstance(content, list):
        return [p for p in content if isinstance(p, dict)]
    return []


# ---------------- /sch/add ----------------
@app.post("/sch/add")
@app.post("/v1/memories/add")
async def add(request: Request, authorization: str | None = Header(default=None),
              x_api_key: str | None = Header(default=None, alias="X-Api-Key")):
    if not _check_auth(authorization, x_api_key):
        return _err(401, "invalid api key")
    try:
        body = await request.json()
    except Exception:
        return _err(400, "invalid json")
    user_id = str(body.get("user_id") or "").strip()
    session_id = str(body.get("session_id") or "").strip()
    request_id = str(body.get("request_id") or "").strip()
    messages = body.get("messages") or []
    if not user_id or not session_id or not request_id or not isinstance(messages, list) or not messages:
        return _err(422, "user_id / session_id / request_id / messages are required")

    if store.has_request(user_id, request_id):
        log.info("[add] replay user=%s req=%s", user_id[:12], request_id[:12])
        return {"success": True, "request_id": request_id, "user_id": user_id, "session_id": session_id}

    n_img_total = 0
    inserted = 0
    for mi, m in enumerate(messages):
        if not isinstance(m, dict):
            continue
        role = str(m.get("role") or "user")
        ts_ms = m.get("timestamp") or m.get("ts")
        created_at = ts_to_iso(ts_ms if isinstance(ts_ms, int) else None)
        parts = _norm_content(m.get("content"))
        texts: list[str] = []
        images: list[dict] = []
        for p in parts:
            typ = p.get("type")
            if typ == "image_url":
                url = (p.get("image_url") or {}).get("url") if isinstance(p.get("image_url"), dict) else p.get("image_url")
                dec = _decode_data_uri(url or "")
                if not dec:
                    continue
                raw, fmt = dec
                sniffed = _sniff_fmt(raw) or fmt
                if sniffed not in ("png", "jpeg", "webp"):
                    return _err(422, "image is invalid or unsupported")
                if len(raw) > MAX_IMAGE_BYTES:
                    return _err(422, "image too large")
                n_img_total += 1
                if n_img_total > MAX_IMAGES_PER_ADD:
                    return _err(413, "too many images")
                sha = save_blob(raw, sniffed)
                images.append({"sha256": sha, "fmt": sniffed, "bytes": len(raw)})
            elif typ == "text":
                texts.append(str(p.get("text") or ""))
        text = "\n".join(t for t in texts if t).strip()
        if not text and not images:
            continue
        rid = "r_" + hashlib.sha256(f"{user_id}|{request_id}|{mi}".encode()).hexdigest()[:24]
        ok = store.put_record({
            "id": rid, "user_id": user_id, "session_id": session_id, "request_id": request_id,
            "msg_index": mi, "role": role, "ts_ms": ts_ms if isinstance(ts_ms, int) else None,
            "created_at": created_at, "text": text, "images": images,
        })
        if not ok:
            continue
        inserted += 1
        # --- 原子事实 ---
        facts = extract_transactions(text, role, rid, user_id, created_at)
        if LLM_FACTS:
            facts += extract_with_llm(text, role, rid, user_id, created_at)
        for f in facts:
            if store.put_fact(f):
                ft = fact_index_text(f)
                try:
                    vec = E.embed([ft])[0]
                    store.put_vector("f_" + f["id"][2:], "fact", user_id, E.to_blob(vec), ft)
                except Exception:
                    log.warning("[add] fact embed failed: %s", f["id"][:10])
        # --- record 向量（正文 + 事实摘要，帮助长消息不被稀释）---
        try:
            rec_text_for_vec = text
            if facts:
                summar = " ; ".join(fact_index_text(f) for f in facts[:4])
                rec_text_for_vec = f"{text}\n\n[facts] {summar}"
            vec = E.embed([rec_text_for_vec])[0]
            store.put_vector(rid, "record", user_id, E.to_blob(vec), rec_text_for_vec)
        except Exception:
            log.warning("[add] record embed failed: %s", rid[:10])

    index.invalidate(user_id)
    log.info("[add] user=%s req=%s inserted=%d", user_id[:12], request_id[:12], inserted)
    return {"success": True, "request_id": request_id, "user_id": user_id, "session_id": session_id}


# ---------------- /sch/search ----------------
@app.post("/sch/search")
@app.post("/v1/memories/search")
async def search(request: Request, authorization: str | None = Header(default=None),
                 x_api_key: str | None = Header(default=None, alias="X-Api-Key")):
    if not _check_auth(authorization, x_api_key):
        return _err(401, "invalid api key")
    try:
        body = await request.json()
    except Exception:
        return _err(400, "invalid json")
    user_id = str(body.get("user_id") or "").strip()
    if not user_id:
        return _err(422, "user_id is required")
    q = body.get("query")
    if isinstance(q, list):
        text = " ".join(str(p.get("text") or "") for p in q if isinstance(p, dict) and p.get("type") == "text")
    else:
        text = str(q or "")
    text = text.strip()
    if not text:
        return _err(422, "query is required")
    top_k = int(body.get("top_k") or 10)
    t0 = time.time()
    try:
        resp = build_response(store, index, user_id, text, top_k)
    except Exception as exc:
        log.exception("[search] failed")
        return _err(500, f"search failed: {exc}")
    log.info("[search] user=%s n=%d %.2fs q=%s", user_id[:12], len(resp["data"]), time.time() - t0, text[:50])
    return resp


@app.get("/sch/health")
async def health():
    return {"status": "ok", "service": "sch-memory", "embed": E.EMBED_MODEL,
            "llm": __import__("os").getenv("SCH_LLM_MODEL", "gpt-4o-mini"),
            "records": store.conn.execute("SELECT COUNT(*) c FROM records").fetchone()["c"],
            "facts": store.conn.execute("SELECT COUNT(*) c FROM facts").fetchone()["c"]}
