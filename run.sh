#!/usr/bin/env bash
# Memory Evidence Grid — 启动脚本
set -euo pipefail
cd "$(dirname "$0")"
export PYTHONPATH="$(pwd):${PYTHONPATH:-}"

# 必填：Embedding（官方要求 text-embedding-v4）与 LLM（官方要求 gpt-4o-mini）
: "${SCH_EMBED_KEY:?set SCH_EMBED_KEY (DashScope key for text-embedding-v4)}"
: "${SCH_LLM_KEY:?set SCH_LLM_KEY (OpenAI-compatible key serving gpt-4o-mini)}"

export SCH_PORT="${SCH_PORT:-8100}"
export SCH_KEY="${SCH_KEY:-sch-memory}"          # 支持逗号分隔多个 key
export SCH_LLM_FACTS="${SCH_LLM_FACTS:-1}"       # 1=用 LLM 抽非交易事实
export SCH_ASSESS="${SCH_ASSESS:-1}"             # 1=启用支持度判定（拒答）

exec python -m uvicorn app.api:app --host 0.0.0.0 --port "$SCH_PORT" --log-level info
