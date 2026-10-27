"""集中配置：只允许官方指定的模型。"""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_env_file(path: str | None = None) -> None:
    """从 .env.prod 之类的文件补齐缺失的环境变量（不覆盖已有值）。"""
    p = Path(path or os.path.join(os.path.dirname(ROOT), ".env.prod"))
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line.startswith("export ") or "=" not in line:
            continue
        k, v = line[len("export "):].split("=", 1)
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if "${" in v:
            continue
        os.environ.setdefault(k, v)


# 先补环境（.env.prod），再读常量
load_env_file()
DATA = ROOT / "data"
DATA.mkdir(parents=True, exist_ok=True)

DB_PATH = os.getenv("SCH_DB", str(DATA / "sch.db"))
BLOB_DIR = Path(os.getenv("SCH_BLOB_DIR", str(DATA / "blobs")))
BLOB_DIR.mkdir(parents=True, exist_ok=True)

# --- 官方允许的两个模型 -------------------------------------------------
EMBED_MODEL = os.getenv("SCH_EMBED_MODEL", "text-embedding-v4")
EMBED_DIM = int(os.getenv("SCH_EMBED_DIM", "1024"))
EMBED_BASE = os.getenv("SCH_EMBED_BASE", "https://dashscope.aliyuncs.com/compatible-mode/v1")
EMBED_KEY = os.getenv("SCH_EMBED_KEY", "") or os.getenv("DASHSCOPE_API_KEY", "")

LLM_MODEL = os.getenv("SCH_LLM_MODEL", "gpt-4o-mini")
LLM_BASE = os.getenv("SCH_LLM_BASE", "https://api.zhaomoxing.com/v1")
LLM_KEY = os.getenv("SCH_LLM_KEY", "") or os.getenv("SCH_LLM_API_KEY", "")

# --- 服务 --------------------------------------------------------------
PORT = int(os.getenv("SCH_PORT", "8100"))
# 允许逗号分隔多个 key（评测 key 与本地调试 key 可共存）
API_KEYS = {k.strip() for k in (os.getenv("SCH_KEY", "sch-memory")).split(",") if k.strip()}
TOPK_MAX = 100
MAX_IMAGES_PER_ADD = 200
MAX_IMAGE_BYTES = 10 * 1024 * 1024

# 是否用 LLM 抽非交易类事实（批量灌数据时可关，只走规则+向量）
LLM_FACTS = os.getenv("SCH_LLM_FACTS", "1") not in ("0", "false", "False")

# 是否启用 LLM 支持度判定（关掉=永不拒答，用于对比"纯检索水平"）
ASSESS = os.getenv("SCH_ASSESS", "1") not in ("0", "false", "False")

# --- 检索参数 ----------------------------------------------------------
DENSE_TOP = int(os.getenv("SCH_DENSE_TOP", "60"))
BM25_TOP = int(os.getenv("SCH_BM25_TOP", "60"))
FACT_TOP = int(os.getenv("SCH_FACT_TOP", "40"))
RRF_K = int(os.getenv("SCH_RRF_K", "60"))


