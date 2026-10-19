# 企业智能知识库 · P1 原型

验证 RAG 核心业务链路：**文档上传 → 解析 → 切片 → 向量化 → 双路检索 → 融合排序 → 基于原文作答 → 引用溯源 → 无答案拒答**。

零数据库依赖，`python run.py` 即可演示。

---

## 快速开始

```bash
cd p1_prototype
pip install -r requirements.txt
python run.py
```

打开 http://127.0.0.1:8011

> 依赖已就绪时可直接启动。首次运行会自动从 `data/docs` 加载演示语料索引。

---

## 运行模式

| 模式 | 触发条件 | 行为 |
| --- | --- | --- |
| 真实模型 | 根目录 `.env` 中有 `API_KEY_SILICON` + `DEEPSEEK_API_KEY` | BGE-M3 向量化 + DeepSeek 生成 |
| 离线演示 | 无密钥，或设置 `DEMO_MODE=offline` | 本地哈希向量 + 抽取式作答，链路完整可演示 |

修改模式：在 `p1_prototype/.env` 中设置 `DEMO_MODE=real|offline|auto`（默认 auto）。

---

## 端到端验收

```bash
python scripts/verify.py            # 用现有索引
python scripts/verify.py --rebuild  # 清空重建
```

输出 14 项检查：入库、切片质量、双路召回、库内问答、库外拒答、阈值分离、幂等覆盖、删除一致性。

---

## 目录结构

```
p1_prototype/
├── run.py                 启动入口（打印模式与地址）
├── app/
│   ├── config.py          配置集中管理，复用根目录 .env 密钥
│   ├── loader.py          解析 txt/md/pdf/docx，含表格保留与文本清洗
│   ├── splitter.py        段落优先递归切片 + jieba 分词
│   ├── embedder.py        BGE-M3 批量向量化 + 离线哈希兜底
│   ├── store.py           本地 numpy 双索引（向量 + BM25 统计），npz+json 落盘
│   ├── retriever.py       向量/BM25 双路召回 → RRF 融合 → 相关度
│   ├── rag.py             上下文拼装、生成、引用、拒答
│   ├── ingest.py          入库编排（幂等、批量、删除）
│   └── main.py            FastAPI 路由 + 演示页面
├── data/
│   ├── docs/              演示语料
│   └── index/             index.npz / meta.json
├── scripts/verify.py      端到端验收
└── docs/演示脚本.md        8~10 分钟讲解稿（含应答 Q&A）
```

---

## 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/` | 演示界面 |
| GET | `/api/status` | 运行模式、索引规模、当前参数 |
| POST | `/api/ask` | 问答，返回 `answer` / `sources` / `confidence` / `rejected` |
| POST | `/api/retrieve` | 仅检索，查看向量与 BM25 各自排名 |
| POST | `/api/upload` | 上传文档入库（multipart） |
| POST | `/api/ingest_dir` | 批量导入 `data/docs` |
| GET | `/api/docs` | 文档列表 |
| POST | `/api/docs/delete` | 按文件名删除 |
| GET | `/docs` | OpenAPI 文档 |

示例：

```bash
curl -X POST http://127.0.0.1:8011/api/ask \
  -H "Content-Type: application/json" \
  -d '{"query":"去上海出差住酒店能报多少钱一晚？"}'
```

响应片段：

```json
{
  "code": 200,
  "data": {
    "answer": "结论：去上海出差住宿，标准上限为 600 元/晚 [来源2]。...",
    "sources": [
      {"file_name": "差旅费用管理办法.md", "chunk_index": 1,
       "vec_rank": 1, "bm25_rank": 1, "rrf_score": 0.0323}
    ],
    "confidence": 0.31,
    "rejected": false
  }
}
```

---

## 与旧实现的差异（P1 修掉的问题）

| 问题 | 旧实现 | P1 |
| --- | --- | --- |
| RRF 融合 | 只对单路结果按位置计分 | 分别对向量、BM25 排名计算后相加 |
| 中文分词 | 正则整句切分 | jieba 搜索引擎模式 |
| 无答案处理 | 无阈值判断 | 三项加权相关度 + 阈值拒答 |
| 入库路径 | 两套实现并存 | 单一 `ingest.py` |
| 依赖 | Qdrant + PG 必须启动 | 零数据库，单文件索引 |

---

## 已知局限（P2 解决）

- 单文件索引不支持并发写
- 无用户体系与权限隔离
- 无缓存、无流式输出
- 会话不跨会话保存

详见 `../docs/04-分阶段迭代设计.md`。

---

## 阶段进度（截至 P4）

| 阶段 | 内容 | 验收 |
|------|------|------|
| P1 | RAG 核心链路：解析 / 切片 / 向量化 / 检索 / 问答 / Agent 熔断 | 13/13 |
| P2 | 企业必备安全与运维：API Key + RBAC、两级缓存、审计、Prometheus + Grafana | 36/36 |
| P3 | JWT 三角色、空间 + 密级正交隔离、混合检索 + Rerank、全链路日志 | 69/69 + 74/74 |
| P4 | 异步任务队列、MinIO、ELK、告警、前端系统、多租户、多实例、企业 SSO | 235/235 |

**当前代码是 P4 版本**，接口 57 个，容器服务 12 个。

### 快速开始

```bash
# 本地降级模式：不依赖 Docker，内存 + 本地索引，适合看代码逻辑
cd p1_prototype
python -m uvicorn app_v2.main:app --host 127.0.0.1 --port 8021
# 打开 http://127.0.0.1:8021/login  （管理员密码见启动日志）

# 容器全栈模式：PG + Qdrant + Redis + MinIO + ES + 监控栈
cd app_v2 && docker compose up -d
# 打开 http://127.0.0.1:8012/login
```

### 前端

三个页面全部免认证（页面不含数据），数据一律走带鉴权的 API：

| 路径 | 页面 |
|------|------|
| `/login` | 登录（账号密码 + 企业 SSO） |
| `/app` | 智能问答（会话、流式回答、来源溯源） |
| `/admin` | 知识库管理后台（文档/空间/用户/租户/审计/告警/任务） |

### 验收脚本

```bash
python scripts/verify_p4_sso.py             # SSO 安全边界 95/95（内置假 IdP）
python scripts/verify_multi_instance.py     # 多实例 34/34（自动拉起 Redis + 两副本）
python scripts/verify_p4_alerts.py          # 告警 39/39
```

完整清单见 `../docs/10-P4阶段验收文档.md`，多实例设计见 `../docs/09-P4多实例部署设计.md`。

