"""P1 原型统一配置入口。

配置来源优先级：环境变量 > p1_prototype/.env > 项目根 .env > 默认值。
这样做的好处是原型可以独立运行，同时复用已有的模型密钥，无需重复配置。
"""
import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent      # p1_prototype/
PROJECT_DIR = BASE_DIR.parent                          # 项目根 E:/knowledge
DOCS_DIR = BASE_DIR / "data" / "docs"
INDEX_DIR = BASE_DIR / "data" / "index"
INDEX_FILE = INDEX_DIR / "index.npz"
META_FILE = INDEX_DIR / "meta.json"

# 先加载根目录 .env（已有密钥），再用原型自身的 .env 覆盖
load_dotenv(PROJECT_DIR / ".env", override=False)
load_dotenv(BASE_DIR / ".env", override=True)


def _get(key: str, default: str = "") -> str:
    return os.getenv(key, default).strip()


# ---------- 模型 ----------
EMBED_MODEL_NAME = _get("EMBED_MODEL_NAME", "BAAI/bge-m3")
EMBED_DIM = int(_get("EMBED_DIM", "1024"))
CHAT_MODEL = _get("CHAT_MODEL", "deepseek-flash")

EMBED_API_KEY = _get("API_KEY_SILICON") or _get("EMBED_API_KEY")
EMBED_BASE_URL = _get("BASE_URL_SILICON", "https://api.siliconflow.cn/v1")
LLM_API_KEY = _get("DEEPSEEK_API_KEY")
LLM_BASE_URL = _get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")

# ---------- 切片 ----------
CHUNK_SIZE = int(_get("CHUNK_SIZE", "450"))
CHUNK_OVERLAP = int(_get("CHUNK_OVERLAP", "80"))

# ---------- 检索 ----------
RECALL_K = int(_get("RECALL_K", "10"))
RRF_K = int(_get("RRF_K", "60"))
FINAL_TOP_K = int(_get("FINAL_TOP_K", "3"))
MIN_SCORE = float(_get("MIN_SCORE", "0.25"))

# ---------- 演示模式 ----------
DEMO_MODE = _get("DEMO_MODE", "auto").lower()   # auto | real | offline

# ---------- 服务 ----------
HOST = _get("HOST", "0.0.0.0")
PORT = int(_get("PORT", "8011"))


def resolve_mode() -> str:
    """解析实际运行模式。

    - real   : 调用真实 Embedding / LLM
    - offline: 本地哈希向量 + 抽取式作答（零依赖、零费用，用于功能演示兜底）
    """
    if DEMO_MODE == "offline":
        return "offline"
    if DEMO_MODE == "real":
        return "real"
    # auto：密钥齐全才走真实模式
    return "real" if (EMBED_API_KEY and LLM_API_KEY) else "offline"


MODE = resolve_mode()

DOCS_DIR.mkdir(parents=True, exist_ok=True)
INDEX_DIR.mkdir(parents=True, exist_ok=True)
