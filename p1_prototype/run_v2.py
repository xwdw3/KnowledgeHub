"""P1 工程版启动入口：python run_v2.py"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import uvicorn

from app_v2.core.settings import settings
from app_v2.storage.meta_store import meta_store
from app_v2.storage.vector_store import vector_store

if __name__ == "__main__":
    print("=" * 66)
    print(f"  {settings.app_name} · P1 工程版")
    print("=" * 66)
    print(f"  模型模式 : {'真实模型' if settings.resolved_model_mode() == 'real' else '离线兜底（未配置密钥）'}")
    print(f"  向量库   : {vector_store.backend}")
    print(f"  业务库   : {meta_store.backend}")
    print(f"  服务地址 : http://127.0.0.1:{settings.port}")
    print(f"  接口文档 : http://127.0.0.1:{settings.port}/docs")
    print("=" * 66)
    uvicorn.run("app_v2.main:app", host=settings.host, port=settings.port, reload=False, log_config=None)
