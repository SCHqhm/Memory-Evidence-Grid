# 架构说明

## 写入路径

1. **解析**：`content` 可以是字符串或有序 ContentPart 数组；图片按 Base64 解码、校验格式（png/jpeg/webp）、落盘为 sha256 blob。
2. **原始证据**（`records`）：原文、时间戳、角色、会话、请求、图片引用。此表**永不改写**，用于事后追溯与答案侧引用。
3. **原子事实**（`facts`）：
   - **交易类**（规则法）：金额（支持 `$150` / `150 dollars` / `twenty-five bucks` / `150元`）、购买动词、客体（区分"定语+动词"与"动词+宾语"两种语序）、状态（current/estimate/advice/hypothetical）、来源（user/assistant）。
   - **非交易类**（可选 LLM）：偏好 / 事件 / 属性 / 计划。
   - 每条事实都带 `record_id` 与 `evidence` 原句，保证可回溯。
4. **向量**：record 文本 + 事实摘要一起编码，缓解"长消息主题稀释"（实测某条 742 字消息的购买句在第 364 字，单独编码时排名靠后，加入事实摘要后提升）。

## 检索路径

```
query
  → intent_of()  ── sum / time / visual / yes_no / choice / fact
  → index.search()
       ├─ dense_rec   (text-embedding-v4, cosine)
       ├─ bm25_rec    (中英混合分词 + 中文 2-gram)
       ├─ dense_fact  / bm25_fact / struct_fact
       └─ RRF 融合
  → 若 intent == sum: collect_events()
       过滤 source_type != user、fact_status ∈ {advice, hypothetical}
       按 (金额, 日期, 客体前 12 字) 去重
       → 生成【交易事件清单】证据（明确写"仅列事实，未做计算"）
  → assess()（LLM 支持度判定）→ 不足则回哨兵
  → 分数单调递减输出
```

## 关键取舍

| 取舍 | 理由 |
|---|---|
| 不在 Search 里求和 | 赛制禁止 Search 生成答案；求和由平台 Answer 完成 |
| 拒答用 LLM 判定而非阈值 | 实测 AR/非 AR 的相关性分布重叠，阈值必然误判 |
| 交易事实用规则而非 LLM | 确定性高、零成本、可解释；LLM 只补非交易事实 |
| 事实与记录分开存 | 记录保证可追溯，事实保证可计算 |
