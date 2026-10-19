"""P1 原型启动脚本：python run.py"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import uvicorn

from app import config

if __name__ == "__main__":
    print("=" * 62)
    print("  企业智能知识库 · P1 核心链路验证原型")
    print("=" * 62)
    print(f"  运行模式 : {'真实模型' if config.MODE == 'real' else '离线演示（未配置模型密钥）'}")
    print(f"  访问地址 : http://127.0.0.1:{config.PORT}")
    print(f"  接口文档 : http://127.0.0.1:{config.PORT}/docs")
    print(f"  示例文档 : {config.DOCS_DIR}")
    print("=" * 62)
    uvicorn.run("app.main:app", host=config.HOST, port=config.PORT, reload=False)
