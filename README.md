# 私有化企业智能知识库 · 代码库地图

> 整合说明（2026-10-18）：项目早期在仓库根目录的原型代码（`src/` 等）已整体归档至
> `archive/early_prototype/`（只移动、未删除）。当前**唯一有效代码库**是 `p1_prototype/`。

## 目录结构

```
knowledge/
├── README.md            ← 本文件（代码地图）
├── docs/                ← 全部文档（01~03 规划、04 迭代设计、05~12 各阶段/最终验收）
├── p1_prototype/        ← ★ 唯一有效代码库（P1~P4 全部交付物）
└── archive/
    └── early_prototype/ ← 最早期的根目录原型（src/、test_all.py、旧 compose 与数据卷），
                           仅作历史归档，不参与任何验收与部署
```

## p1_prototype/ 内部地图（最终交付代码）

| 目录/文件 | 内容 | 所属阶段 |
|-----------|------|----------|
| `app/` | P1 原型（零数据库依赖的最小实现） | P1 |
| `app_v2/` | **最终主力代码**：api / core / services / storage / worker / web / schemas / deploy + 自带 docker-compose.yml | P2~P4 |
| `scripts/` | 14 个验收脚本（verify*.py）+ 辅助工具（dev_idp.py 等） | P1~P4 |
| `k8s/` | 8 个 K8s 清单（HPA/KEDA/PDB/探针）+ README | P4 |
| `tests/` | Agent 熔断等单元测试 | P1 |
| `data/` `data_v2/` | 演示语料与本地索引数据 | P1~P2 |
| `run.py` `run_p2.py` `run_v2.py` | 各阶段本地启动入口（单机演示用） | P1~P2 |
| `docs/` | P1 演示脚本等阶段内文档 | P1 |

> 命名说明：目录名 `p1_prototype` 是 P1 阶段"代码新建目录存放"的历史决定；
> P2 起的工程版代码（`app_v2/`）继续放在同一目录下，故保留此名。
> **生产部署与全部验收都以 `app_v2/` 为准**（见 `docs/12-最终阶段验收文档.md`）。

## 快速上手

```bash
cd p1_prototype

# 本地降级模式启动（无需 Docker）
python run_v2.py          # 或: python -m uvicorn app_v2.main:app --port 8021

# 容器全栈启动（12 服务）
cd app_v2 && docker compose up -d

# 验收（14 个脚本 559 项断言，命令清单见 docs/11-整合版第八章）
python scripts/verify.py
python scripts/verify_k8s_manifests.py
```

## 配置（首次运行必读）

仓库**不含任何真实密钥**，模型与数据库配置需要自行填：

```bash
cd p1_prototype
cp .env.example .env          # 填入 EMBED_API_KEY / CHAT_API_KEY 等
cp app_v2/.env.example app_v2/.env
```

- 不填密钥也能跑：`MODEL_MODE=offline` 时使用离线哈希向量 + 模板答案，全部接口可用，只是答不出真实语义。
- 未配 PostgreSQL / Qdrant / Redis 时自动降级为本地索引 + 内存库，适合本地演示。
- JWT 密钥、API Key、metrics 令牌都在首次启动时随机生成并打印到日志，**不要**把它们提交回仓库。
