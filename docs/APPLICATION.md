# 参赛申请（表单填写内容）

## 记忆系统

**系统名称**
```
Memory Evidence Grid（MEG）
```

**版本名称**
```
v1.0
```

**Add API 地址**
```
https://memory.cuebridge.top/sch/add
```

**Search API 地址**
```
https://memory.cuebridge.top/sch/search
```

**认证方式**
```
Authorization: Token
```

**记忆系统 Key**
```
sch-memory
```

**公开 GitHub 仓库地址**
```
https://github.com/SCHqhm/Memory-Evidence-Grid
```

---

## 提交说明与运行指南

### 一、已部署服务

| 项 | 值 |
|---|---|
| Add | `POST https://memory.cuebridge.top/sch/add` |
| Search | `POST https://memory.cuebridge.top/sch/search` |
| Health | `GET  https://memory.cuebridge.top/sch/health` |
| 鉴权 | `Authorization: Token sch-memory`（另兼容 `X-Api-Key`）|
| 兼容路径 | `/v1/memories/add`、`/v1/memories/search` |
| 部署形态 | 单机 uvicorn + Cloudflare Tunnel 暴露，公网可直连 |

**契约合规**：
- Add 返回 `{success, request_id, user_id, session_id}`；同 `(user_id, request_id)` 幂等。
- Search 返回 `{data:[{id, content, score, created_at}]}`；`id` 唯一、`score` 单调不增、`top_k ≤ 100`；
  无匹配时返回单条哨兵记录（id=`sch_no_evidence`）。
- Search **只返回证据，不生成答案**；不针对任何题目硬编码。

### 二、容量与运行限制

| 项 | 现状 |
|---|---|
| 已验证规模 | 单用户 3,962 个 Add 请求 / 7,924 条记忆记录 |
| 存储 | SQLite（records / facts / vectors）+ 本地文件（图片 sha256 blob）|
| 索引 | 按 user 惰性构建、进程内缓存，新增数据后失效重建 |
| 写入延迟 | 单条 add 同步返回；图片仅落盘不做视觉理解 |
| 检索延迟 | 约 1~3s（含 1 次 query embedding）；启用支持度判定时 +2~8s |
| 并发 | 单进程 uvicorn，未做多 worker；评测建议串行或低并发 |
| 已知限制 | 无重排模型；图片内容未做视觉理解（只存引用）；事实抽取以规则为主 |

### 三、模型合规

| 用途 | 模型 | 说明 |
|---|---|---|
| Embedding | `text-embedding-v4` | 平台指定 |
| LLM（事实抽取 / 支持度判定）| `gpt-4o-mini` | 平台指定 |
| Reranker | 无 | 未使用任何 reranker |

### 四、原始方法作者与技术参考

本系统为**从零实现**（代码独立，未复用作者既有工程的代码路径），设计上参考了以下**公开工作**，特此披露：

| 参考 | 作者/团队 | 借鉴点 |
|---|---|---|
| MemGPT | Packer et al. | 分层记忆与管理 |
| A-Mem | Xu et al. | 关联式记忆组织 |
| Zep / Graphiti | Zep AI | 时间与关系结构化 |
| RAPTOR | Sarthi et al. | 分层摘要检索 |
| ColBERT | Khattab & Zaharia | 细粒度相关性 |
| CRAG | Yang et al. | 检索质量评估 |
| Self-RAG | Asai et al. | 证据反思 |

技术报告：仓库内 `docs/ARCHITECTURE.md`（设计）、`docs/API.md`（接口契约）、`docs/ROADMAP.md`（已知问题与后续计划）。

### 五、全部方法改动披露

相对上述参考方法，本系统的具体改动：

1. **双表示存储**：原始证据（`records`，永不改写）+ 原子事实（`facts`）。
   事实字段包含 `kind / subject / relation / object / value / unit / event_time / source_type / fact_status / confidence / evidence / record_id`。
2. **交易事实事件级化**：每笔金额单独成一条事实，**绝不合并**成"花了很多钱"这类摘要；
   金额解析支持 `$150`、`150 dollars`、`twenty-five bucks`、`150元` 等写法；
   客体抽取按语序分支（"定语+动词"取前、"动词+宾语"取后）。
3. **来源与状态显式化**：`source_type ∈ {user, assistant}`、`fact_status ∈ {current, estimate, advice, hypothetical}`；
   求和类问题在召回时**排除** assistant 陈述与建议/假设状态。
4. **集合级证据补全**：求和/多跳类问题按 `(金额, 日期, 客体)` 去重后**全量**给出事件清单，
   并显式标注"仅列事实，未做计算"——求和由平台 Answer 完成，符合"Search 不生成答案"的边界。
5. **拒答不依赖相关性阈值**：实测应拒答与非拒答样本的 top-dense 相似度严重重叠
   （0.39~0.70 vs 0.50~0.92），因此改用 **LLM 支持度判定**（判断"证据是否明确包含回答问题所需信息"），
   话题相关/部分相关/需外部推断一律判否。
6. **三路召回 + RRF**：dense（`text-embedding-v4`）+ BM25（中英混合分词 + 中文 2-gram）+ 结构化事实召回，
   以 RRF 融合；record 向量时拼接其事实摘要，缓解长消息主题稀释。
7. **批量灌库工具**：`tools/bulk_ingest.py` 直接写库 + 批量 embedding（25/批、4 并发、逐条兜底），
   相比逐条走 HTTP 快约 50 倍。

### 六、公开自测结果（如实披露）

对照组为**任何人都能复跑的官方基线**：`text-embedding-v4` 纯向量检索
（无 BM25 / 无结构化 / 无重排 / 无拒答，见仓库 `tools/baseline_eval.py`）。

Mem-Gallery 公开子集，20 用户 / 12 样本：

| 指标 | 基线 text-embedding-v4 | **MEG v1.0** | 增益 |
|---|---|---|---|
| Hit@1 | 0.167 | **0.250** | +8.3pp（+50%）|
| R@gold@10 | 0.292 | **0.612** | **+32.0pp（2.1×）** |
| any_gold@20 | 0.417 | **0.917** | **+50.0pp（2.2×）** |
| 拒答正确率 | —（基线不拒答）| 1.000 | — |

结论：相对官方 Embedding 基线，本系统召回覆盖提升 **2.2 倍**、R@10 提升 **2.1 倍**。
仍存在的短板是**没有重排层**（Hit@1 落后于带 reranker 的系统），以及拒答开关较敏感
（关闭 → 拒答率 0%，开启 → 拒答正确率 100% 但召回下降 25pp）。后续计划见 `docs/ROADMAP.md`。

### 七、复现方式

```bash
git clone https://github.com/SCHqhm/Memory-Evidence-Grid
cd Memory-Evidence-Grid
python -m pip install -r requirements.txt
export SCH_EMBED_KEY=<DashScope key>   # text-embedding-v4
export SCH_LLM_KEY=<gpt-4o-mini key>
export SCH_KEY=sch-memory
./run.sh                                # 监听 0.0.0.0:8100
python tools/smoke_test.py --base http://127.0.0.1:8100 --key sch-memory
```
