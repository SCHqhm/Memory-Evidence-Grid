# Memory Evidence Grid

> 参赛系统：**Memory Evidence Grid (MEG)** — 面向 Agent Memory Leaderboard / Agent Memory Challenge 的
> Add / Search 记忆服务。只负责**写入**与**检索**，不生成最终答案。

---

## 1. 设计目标

平台侧统一用 Answer 模型与 Eval 裁判评分，因此本系统的目标不是"答得准"，而是：

> **让统一 Answer 模型能稳定拿到可核验、覆盖完整、时间与来源清晰的证据。**

由此确定三条设计原则：

| 原则 | 落地 |
|---|---|
| **证据优先** | Search 只返回证据，绝不输出"总价为 X"这类结论 |
| **双表示** | 原始证据（`records`，永不改写）+ 原子事实（`facts`，结构化） |
| **集合级召回** | 求和/多跳类问题按"事件"去重后**全部**给出，而不是只取 top-k |

---

## 2. 架构

```
写入 /sch/add
  ├─ 有序 content 数组解析（文本 + Base64 图片）
  ├─ records：原文 / 时间戳 / 说话人 / 角色 / 图片引用   ← 原始证据，永不改写
  └─ facts：原子事实
        kind ∈ {transaction, preference, event, attribute, plan}
        subject / relation / object / value / unit / event_time
        source_type ∈ {user, assistant}          ← 谁说的
        fact_status ∈ {current, estimate, advice, hypothetical}
        confidence / evidence(原句) / record_id  ← 可回溯

检索 /sch/search
  ├─ 意图识别：sum / time / visual / yes_no / choice / fact
  ├─ 三路召回：dense(text-embedding-v4) + BM25 + 结构化事实
  ├─ RRF 融合
  ├─ 集合级补全：sum 类问题 → collect_events() 按 (金额,日期,客体) 去重
  ├─ 支持度判定：LLM 判断"证据能否回答问题"（不靠相关性分数）
  └─ 输出：证据记录 或 单条"证据不足"哨兵
```

### 为什么不用"相关性分数阈值"拒绝

实测同一批样本上，AR（应拒答）与非 AR 的 top-dense 相似度**严重重叠**
（AR 0.39~0.70，非 AR 0.50~0.92），任何单阈值都会大量误判。
因此拒答改由 **LLM 支持度判定** 决定：

```
只有当证据里明确包含回答问题所需的信息时才算 answerable；
话题相关、部分相关、需要外部推断 → false；
"总量"类问题必须能看到每一笔金额；"有没有"类必须能确定是或否。
```

---

## 2.1 已部署实例（参赛用）

| 项 | 值 |
|---|---|
| Add | `https://memory.cuebridge.top/sch/add` |
| Search | `https://memory.cuebridge.top/sch/search` |
| Health | `https://memory.cuebridge.top/sch/health` |
| 鉴权 | `Authorization: Token sch-memory` |
| 仓库 | https://github.com/SCHqhm/Memory-Evidence-Grid |

> 部署方式：本机 uvicorn(127.0.0.1:8100) + Cloudflare Tunnel 暴露。
> 兼容路径 `/v1/memories/add`、`/v1/memories/search` 亦可使用。

---

## 3. 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/sch/add` | 同 AML `/add` 语义 |
| POST | `/sch/search` | 同 AML `/v1/memories/search` 语义 |
| GET | `/sch/health` | 服务状态、记录/事实计数 |

兼容路径：`/v1/memories/add`、`/v1/memories/search`

### 鉴权
```
Authorization: Token <SCH_KEY>
```
`SCH_KEY` 支持逗号分隔多个 key。

### Add 请求
```json
{
  "request_id": "r-1",
  "user_id": "u-1",
  "session_id": "s-1",
  "messages": [
    {"role": "user", "timestamp": 1715040000000,
     "content": [
       {"type": "text", "text": "I bought a Nespresso machine for $150."},
       {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,..."}}
     ]}
  ]
}
```
响应：`{"success": true, "request_id": "...", "user_id": "...", "session_id": "..."}`

### Search 请求
```json
{"user_id": "u-1", "query": [{"type": "text", "text": "How much total ..."}], "top_k": 20}
```
响应：
```json
{"data": [{"id": "r_xxx", "content": [{"type": "text", "text": "..."}],
           "score": 0.98, "created_at": "2024-05-07T00:00:00+00:00"}]}
```
- `id` 全局唯一；`score` 单调不增；无匹配时返回**单条哨兵**
- `top_k` 上限 100

---

## 4. 部署

```bash
git clone https://github.com/SCHqhm/Memory-Evidence-Grid
cd Memory-Evidence-Grid
python -m pip install -r requirements.txt

export SCH_EMBED_KEY=<DashScope key>       # text-embedding-v4
export SCH_LLM_KEY=<gpt-4o-mini key>       # OpenAI 兼容
export SCH_KEY=sch-memory
./run.sh                                    # 监听 0.0.0.0:8100
```

冒烟：
```bash
python tools/smoke_test.py --base http://127.0.0.1:8100 --key sch-memory
```

### 环境变量
| 变量 | 默认 | 说明 |
|---|---|---|
| `SCH_KEY` | `sch-memory` | 鉴权 key，可逗号分隔 |
| `SCH_PORT` | `8100` | 监听端口 |
| `SCH_LLM_FACTS` | `1` | 是否用 LLM 抽非交易事实 |
| `SCH_ASSESS` | `1` | 是否启用支持度判定（拒答）|
| `SCH_EMBED_MODEL` | `text-embedding-v4` | **官方指定** |
| `SCH_LLM_MODEL` | `gpt-4o-mini` | **官方指定** |

---

## 5. 容量与限制

| 项 | 现状 |
|---|---|
| 写入 | 单条 add 同步完成；图片存本地 blob（sha256 去重）|
| 索引 | 按 user 惰性构建，内存缓存；新增数据后失效重建 |
| 规模 | 实测单用户 3,962 请求 / 7,924 记录可正常服务 |
| 延迟 | 检索 ~1~3s（含 1 次 query embedding）；开启支持度判定 +2~8s |
| 限制 | 未做图片视觉理解（仅存引用）；未接重排模型 |

---

## 6. 依赖模型（合规）

| 用途 | 模型 | 说明 |
|---|---|---|
| Embedding | `text-embedding-v4` | 官方指定 |
| LLM（事实抽取 / 支持度判定）| `gpt-4o-mini` | 官方指定 |
| Reranker | 无 | 未使用 |

---

## 7. 评测结果（公开子集自测）

Mem-Gallery 公开子集，20 用户、12 样本。对照组为**可复现的官方基线**：
`text-embedding-v4` 纯向量检索（无 BM25 / 无结构化 / 无重排 / 无拒答，
脚本 `tools/baseline_eval.py`，任何人可复跑）。

| 指标 | **基线：text-embedding-v4** | **MEG（本系统）** | 增益 |
|---|---|---|---|
| Hit@1 | 0.167 | **0.250** | +8.3pp（+50%）|
| R@gold@10 | 0.292 | **0.612** | **+32.0pp（2.1×）** |
| any_gold@20 | 0.417 | **0.917** | **+50.0pp（2.2×）** |
| 拒答正确率 | —（基线不拒答）| 1.000 | — |

**结论**：相对官方 Embedding 基线，MEG 在召回覆盖上提升 **2.2 倍**、R@10 提升 **2.1 倍**。
本系统当前主要短板是**没有重排层**（Hit@1 仍低于带 reranker 的系统），后续计划见 `docs/ROADMAP.md`。

---

## 8. 目录

```
app/
  config.py    配置（仅官方允许的模型）
  store.py     records / facts / vectors（SQLite）
  facts.py     原子事实抽取（规则；LLM 可选）
  embed.py     text-embedding-v4 客户端（批量 + 缓存）
  index.py     dense / BM25 / 结构化 → RRF
  query.py     意图识别、事件补全、证据装配
  assess.py    支持度判定（LLM）
  api.py       FastAPI 路由
tools/
  bulk_ingest.py  批量灌库（批量 embedding，比走 HTTP 快 50 倍）
  smoke_test.py   冒烟测试
docs/
  ARCHITECTURE.md 设计细节
  API.md          接口契约
  ROADMAP.md      已发现问题与下一步
```

## License

MIT
