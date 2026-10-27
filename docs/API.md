# 接口契约

## POST /sch/add

请求体：
```json
{
  "request_id": "string",
  "user_id": "string",
  "session_id": "string",
  "messages": [
    {
      "role": "user | assistant",
      "timestamp": 1715040000000,
      "content": "string 或 ContentPart[]"
    }
  ]
}
```

ContentPart：
```json
{"type": "text", "text": "..."}
{"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,..."}}
```

响应：
```json
{"success": true, "request_id": "...", "user_id": "...", "session_id": "..."}
```

- 同一 `(user_id, request_id)` 重复写入幂等返回 `success: true`
- 图片格式仅 png/jpeg/webp，单张 ≤10 MiB

## POST /sch/search

请求体：
```json
{"user_id": "string", "query": [{"type": "text", "text": "..."}], "top_k": 20}
```

响应：
```json
{"data": [
  {"id": "r_xxx",
   "content": [{"type": "text", "text": "..."}],
   "score": 0.98,
   "created_at": "2024-05-07T00:00:00+00:00"}
]}
```

约束：
- `data[].id` 唯一
- `score` 单调不增
- 无匹配 → 单条哨兵 `{"id": "sch_no_evidence", ...}`
- `top_k` ≤ 100

## GET /sch/health

```json
{"status":"ok","service":"sch-memory","embed":"text-embedding-v4",
 "llm":"gpt-4o-mini","records":7924,"facts":0}
```
