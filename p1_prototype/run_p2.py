"""P2 本地联调启动入口：python run_p2.py

默认走「内存 + 离线」纯本地模式（不依赖 Docker / 外部模型 API），
用于验证鉴权、缓存、审计、监控四条 P2 主链路。

如需连真实模型：设置 MODEL_MODE=real 与 DASHSCOPE/DEEPSEEK 相关环境变量。
如需连 Docker 内的 Qdrant / PG / Redis：VECTOR_BACKEND=qdrant、META_BACKEND=postgres、REDIS_URL=...
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import uvicorn

from app_v2.core.settings import settings
from app_v2.storage.meta_store import meta_store
from app_v2.storage.vector_store import vector_store

if __name__ == "__main__":
    print("=" * 70)
    print(f"  {settings.app_name} · {settings.app_version}")
    print("=" * 70)
    print(f"  模型模式 : {'真实模型' if settings.resolved_model_mode() == 'real' else '离线兜底（未配置密钥）'}")
    print(f"  向量库   : {vector_store.backend}")
    print(f"  业务库   : {meta_store.backend}")
    print(f"  鉴权     : {'开启' if settings.auth_enabled else '关闭'}")
    print(f"  缓存     : redis={settings.redis_url if settings.cache_enabled else 'off'}")
    print(f"  审计     : {'开启' if settings.audit_enabled else '关闭'}")
    print(f"  指标     : {'开启' if settings.metrics_enabled else '关闭'}")
    print(f"  服务地址 : http://127.0.0.1:{settings.port}")
    print(f"  接口文档 : http://127.0.0.1:{settings.port}/docs")
    print(f"  监控看板 : http://127.0.0.1:{settings.port}/dashboard")
    print("=" * 70)
    uvicorn.run("app_v2.main:app", host=settings.host, port=settings.port, reload=False, log_config=None)
