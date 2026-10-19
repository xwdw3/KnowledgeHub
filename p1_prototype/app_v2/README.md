# P2 工程版 · 后端服务（生产就绪）

私有化企业知识库的完整后端实现：**FastAPI 接口 + 自动接口文档 + 文档入库 + Qdrant/PostgreSQL 双存储 + 混合检索 RAG + Agent Function Calling + API Key 鉴权 + 两级缓存 + 全链路审计 + Prometheus 监控 + Docker Compose 部署**。

> P1 证明"链路能跑通"，P2 证明"敢放到生产"。四项能力按风险从高到低：安全 → 缓存 → 审计 → 监控。

---

## 1. 快速启动

### 方式 A：Docker Compose（推荐，一键起齐）

```bash
cd p1_prototype/app_v2
cp .env.example .env           # 填入模型密钥
docker compose up -d --build

# 查看状态
docker compose ps

# 首次启动会在日志打印初始管理员密钥，请立即保存
docker compose logs api | grep -A2 "初始管理员"

# 打开接口文档（点 Authorize 填入密钥）
# http://127.0.0.1:8012/docs
# 运行看板
# http://127.0.0.1:8012/dashboard#key=<你的密钥>
```

容器：`rag_p2_qdrant`（向量库）、`rag_p2_postgres`（业务库）、`rag_p2_redis`（缓存）、`rag_p2_api`（服务）。
**全部数据组件不映射宿主机端口**，仅在内网 `rag_p2_net` 可达，生产只暴露 `8012`。

### 方式 B：本地直跑（无 Docker，内存 + 离线模式）

```bash
cd p1_prototype
pip install -r app_v2/requirements.txt
python run_p2.py               # 默认 8012 端口

# 指定端口并走纯本地模式（验证鉴权/缓存/审计/监控）
PORT=8022 META_BACKEND=memory VECTOR_BACKEND=local MODEL_MODE=offline python run_p2.py
```

本地模式下 Qdrant / PostgreSQL / Redis 不可用时**自动降级**为本地向量索引 + 内存存储 + 仅 L1 缓存，接口行为完全一致。

---

## 2. 鉴权：第一次调用

所有业务接口需要 `Authorization: Bearer <API Key>`。

```bash
KEY=rag_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx

# 查当前身份与权限
curl -H "Authorization: Bearer $KEY" http://127.0.0.1:8012/api/v1/auth/me

# 问答
curl -X POST http://127.0.0.1:8012/api/v1/chat \
  -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d '{"question":"住宿费标准是多少","top_k":3}'
```

公开路径（无需密钥）：`/`、`/docs`、`/redoc`、`/openapi.json`、`/api/v1/health`、`/api/v1/ready`、`/metrics`、`/dashboard`。

### 角色与权限点

| 角色 | 权限点 |
| --- | --- |
| `admin` | `kb:read` `kb:write` `kb:admin` `audit:read` `user:admin` `metric:read` |
| `editor` | `kb:read` `kb:write` |
| `viewer` | `kb:read` |

### 签发与管理密钥

```bash
# 签发一个只能访问 hr 权限组、7 天后过期的编辑密钥
curl -X POST http://127.0.0.1:8012/api/v1/auth/keys \
  -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d '{"owner":"zhangsan","role":"editor","perm_group":"hr","expires_in_days":7}'

# 列出（不含明文）
curl -H "Authorization: Bearer $KEY" http://127.0.0.1:8012/api/v1/auth/keys

# 吊销（立即失效，无需等缓存过期；不能吊销自己正在用的）
curl -X DELETE -H "Authorization: Bearer $KEY" \
  http://127.0.0.1:8012/api/v1/auth/keys/<key_id>
```

密钥存储：**PBKDF2-HMAC-SHA256 / 120000 轮 / 每密钥独立盐**，比对用 `hmac.compare_digest`（常量时间）。明文只在创建响应里出现一次。

### 知识权限组（重要）

文档按 `perm_group` 隔离（如 `hr`、`finance`、`all`）。**可访问范围由密钥绑定的权限组决定，请求体里的 `perm_group` 不生效**（仅保留字段兼容）。

上传时还有一条严格规则：**只能投放自己权限组内的值**。只有本身带 `all` 的密钥才能往 `all` 投放，否则受限密钥可以自我扩权把密件公开。

```bash
# 管理员（perm_group=all）上传公共文档
curl -X POST http://127.0.0.1:8012/api/v1/documents \
  -H "Authorization: Bearer $ADMIN_KEY" \
  -F "file=@制度.txt" -F "perm_group=all"

# editor(hr) 上传 hr 文档 —— OK
curl -X POST http://127.0.0.1:8012/api/v1/documents \
  -H "Authorization: Bearer $HR_KEY" \
  -F "file=@工资表.txt" -F "perm_group=hr"

# editor(hr) 试图投放到 all —— 403
# {"code":403,"message":"无权将文档归入权限组 all，当前密钥仅限 ['hr']"}
```

---

## 3. 缓存：两级结构

```
请求 → L1（进程内 OrderedDict + TTL，默认 2048 条，~1μs）
        ↓ 未命中
       L2（Redis，跨副本共享）
        ↓ 未命中
      真实检索 / LLM → 回填 L1 + L2
```

| 配置 | 默认 | 说明 |
| --- | --- | --- |
| `CACHE_ENABLED` | `true` | 总开关 |
| `REDIS_URL` | 空 | 留空则只用 L1；填 `redis://redis:6379/0` 启用 L2 |
| `CACHE_L1_SIZE` | `2048` | L1 容量 |
| `CACHE_TTL_RETRIEVAL` | `300` | 检索结果缓存秒数 |
| `CACHE_TTL_ANSWER` | `600` | 问答结果缓存秒数 |

设计要点：

- **key = 参数排序后 SHA256**，避免 dict 顺序不同算出不同 key
- **权限组纳入 key**，否则 A 部门提问可能命中 B 部门的答案
- **Redis 失败自动降级**为 L1-only，服务不中断
- **文档增删改后自动失效**（`invalidate_knowledge()`），不会返回过期知识

```bash
# 查看命中率与被分层情况
curl -H "Authorization: Bearer $KEY" http://127.0.0.1:8012/api/v1/metrics/cache

# 人工失效（外部数据同步等场景）
curl -X POST -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d '{"kinds":["chat"]}' http://127.0.0.1:8012/api/v1/metrics/cache/invalidate
```

---

## 4. 审计：全链路留痕

异步写入（`queue.Queue(2000)` + daemon 线程），队列满时降级同步写——不拖慢主链路，也不丢记录。**401/403 一律强制留痕**，因为只有被拒绝的尝试才能回答"是否有人在试探权限"。

```bash
# 查询（只读，系统不提供修改/删除接口）
curl -H "Authorization: Bearer $KEY" \
  "http://127.0.0.1:8012/api/v1/audit/logs?limit=50&result=denied"

# 24 小时概览与写入队列健康度
curl -H "Authorization: Bearer $KEY" http://127.0.0.1:8012/api/v1/audit/summary

# 停机前确保落库
curl -X POST -H "Authorization: Bearer $KEY" http://127.0.0.1:8012/api/v1/audit/flush
```

记录动作示例：`doc.upload`、`doc.delete`、`chat.query`、`key.create`、`key.revoke`、`cache.invalidate`、`auth.forbidden:kb:write`。

---

## 5. 监控

### 5.1 完整监控栈（P3）

`docker compose up -d` 会一并拉起 Prometheus 与 Grafana，**数据源与面板自动装配**，无需手工配置：

| 服务 | 地址 | 说明 |
| --- | --- | --- |
| Prometheus | 内网 `prometheus:9090` | 抓取 `api:8012/metrics`，保留 7 天，加载 8 条告警规则 |
| Grafana | http://127.0.0.1:3000 | 匿名只读；面板在「RAG 知识库」文件夹下 |

预置面板 `RAG 知识库 · P3 运行监控`（22 面板 / 5 分组）：流量、延迟、错误率与安全、依赖负载、检索质量。

```bash
# 验证抓取是否正常
docker exec rag_p3_prometheus wget -qO- 'http://127.0.0.1:9090/api/v1/targets' | head -c 400
```

### 5.2 指标端点

```bash
curl http://127.0.0.1:8012/metrics      # 公开，标准 Prometheus 文本格式
```

**应用指标**：`rag_requests_total`、`rag_request_duration_ms{quantile}`、`rag_chat_total`、`rag_chat_rejected_total`、`rag_llm_failures_total`、`rag_cache_hit_rate`、`rag_auth_failures_total`、`rag_forbidden_total`、`rag_rate_limited_total`

**依赖负载**（P3 新增，应用侧主动探测，不引 exporter）：

| 组件 | 指标 |
| --- | --- |
| PostgreSQL | `rag_db_up`、`rag_db_pool_used`、`rag_db_pool_max`、`rag_db_latency_ms`、`rag_db_documents`、`rag_db_chunks` |
| Qdrant | `rag_qdrant_up`、`rag_qdrant_points`、`rag_qdrant_latency_ms` |
| Redis | `rag_redis_up`、`rag_redis_used_memory_bytes`、`rag_redis_maxmemory_bytes`、`rag_redis_used_ratio`、`rag_redis_keys`、`rag_redis_evicted_total` |
| Rerank | `rag_rerank_total{mode}`、`rag_rerank_fallback_total`、`rag_rerank_latency_ms` |

> 探针只调用 O(1) 接口（`INFO`/`DBSIZE`/`get_collection`），不用 `SCAN` 与 `count(exact=True)`——指标采集不能反过来给被监控组件加压。

### 5.3 内置看板（免 Grafana）

浏览器打开 `http://127.0.0.1:8012/dashboard#key=<你的密钥>`。

密钥用 URL fragment 传（不会进入服务器日志与 Referer），首次输入后存 `localStorage`。看板单文件、无外部依赖，断网可用，3 秒自刷新。适合没有 Grafana 的轻量场景。

### 5.4 JSON 快照

```bash
curl -H "Authorization: Bearer $KEY" http://127.0.0.1:8012/api/v1/metrics/snapshot
```

> 自研指标实现，不引 `prometheus_client`。分位用 `deque(maxlen=1000)` 滚动窗口，内存恒定；路径中的长 ID 会归一化为 `{id}`，防止 label 基数爆炸。

### 5.5 结构化日志（P3）

```bash
LOG_FORMAT=json     # 默认；压缩成单行 JSON，供 Filebeat/Loki 采集
LOG_FORMAT=text     # 本地排查时改成人读格式
```

每个请求输出一行，含 `trace_id` / `user_id` / `role` / `status` / `latency_ms` / `client_ip`：

```json
{"ts":"2026-10-17T16:40:56.223","level":"INFO","logger":"access","trace_id":"641f46ac165d",
 "message":"POST /api/v1/chat -> 200 (1523ms)","event":"access","method":"POST",
 "path":"/api/v1/chat","status":200,"latency_ms":1522.8,"user_id":"admin","role":"admin",
 "client_ip":"127.0.0.1"}
```

**敏感字段自动脱敏**：按 key 名递归匹配（password / token / secret / api_key / authorization…）替换为 `***`，密码与密钥不会进入日志。

**异常落库**：5xx 会写入 `error_logs` 表（trace_id / path / exc_type / stack），可按 trace_id 反查：

```bash
curl -H "Authorization: Bearer $KEY" \
  "http://127.0.0.1:8012/api/v1/audit/errors?trace_id=641f46ac165d"
```

---

## 6. 统一响应约定

成功与失败对称，HTTP 状态码与 `code` 一致：

```json
// 成功
{"code": 200, "message": "ok", "data": {...}, "trace_id": "a1b2c3d4e5f6"}
// 失败
{"code": 403, "message": "角色 viewer 缺少权限 user:admin", "data": null, "trace_id": "..."}
```

包装由中间件在序列化之后统一完成，路由只返回业务数据。**不要用 `response_model=Envelope[T]` 声明包装层**——FastAPI 会按该模型重新序列化，`Dict[str, Any]` 类型的 `data` 会被过滤成 `null`，业务数据整体丢失（本项目踩过的真实坑，详见验收文档 §7.1）。

排除包装：`/docs`、`/redoc`、`/openapi.json`、`/metrics`、`/dashboard`。

---

## 7. 验收

```bash
# ---- 宿主机直跑（服务监听 127.0.0.1:8012）----
# P2 四条主链路（安全/缓存/审计/监控），61 项断言，纯标准库
ADMIN_KEY=rag_xxx python scripts/verify_p2.py

# P1 回归（核心 RAG 链路），36 项断言
ADMIN_KEY=rag_xxx python scripts/verify_v2.py

# ---- 容器内直跑（推荐：完全贴合生产网络）----
docker cp scripts rag_p2_api:/app/scripts
docker exec -w /app -e BASE=http://127.0.0.1:8012 -e ADMIN_KEY=rag_xxx \
  -e PYTHONPATH=/app rag_p2_api python /app/scripts/verify_p2.py

# Agent 熔断单元测试
pytest tests/test_agent_breaker.py -v
```

两个脚本都支持 `ADMIN_KEY`：P2 起所有业务接口需鉴权，不传只能跑通公开接口。
`verify_v2.py` 内的权限隔离用例会临时创建一个 `perm_group=finance` 的 editor 密钥，
跑完自动吊销，不会污染密钥表。

### 缓存分层怎么验才算数

L1 是进程内缓存，同进程重复请求永远先命中 L1，`l2_hit` 恒为 0——**此时 Redis 即使
完全没工作，用例也会"看起来通过"**。要证明 L2 真实生效：

```bash
# 1. 先请求一次，让结果写进 Redis
# 2. 重启 API（L1 随进程消失，Redis 容器不动）
docker compose restart api
# 3. 再请求同一 query，应 cached=true 且 l2_hit 从 0 变 1
curl -H "Authorization: Bearer $KEY" http://127.0.0.1:8012/api/v1/metrics/cache
```

详见 `docs/06-P2阶段验收文档.md`。

---

## 8. 目录结构

```
app_v2/
├── main.py                 应用装配、中间件、异常处理、lifespan
├── core/
│   ├── settings.py         全部配置（P1 + P2 两段）
│   ├── security.py         PBKDF2 密钥原语、RBAC、权限组解析
│   ├── response.py         统一响应约定与业务异常
│   ├── logging.py          trace_id、认证中间件、统一响应包装
│   └── metrics.py          自研 Prometheus 指标注册表
├── api/
│   ├── deps.py             身份依赖、权限依赖工厂
│   └── routes/
│       ├── chat.py         问答 / 检索 / 会话
│       ├── documents.py    上传 / 列表 / 删除 / 重建 / 清理
│       ├── auth.py         身份 / 密钥管理 / 角色
│       ├── audit.py        审计查询 / 概览 / flush
│       ├── metrics.py      Prometheus / 快照 / 缓存管理 / 看板
│       └── system.py       健康 / 就绪 / 配置 / 统计
├── services/
│   ├── auth_service.py     认证（缓存+限流）、密钥生命周期
│   ├── cache_service.py    L1 LRU + L2 Redis + 指标
│   ├── audit_service.py    异步审计队列
│   ├── rag_service.py      RAG 管道
│   ├── retrieval_service.py 混合检索（向量+BM25+RRF）
│   ├── agent_service.py    Agent 循环与三重熔断
│   ├── ingest_service.py   解析/切片/入库编排
│   ├── embedder.py         BGE-M3 向量化
│   └── llm.py              DeepSeek 对话（含离线兜底）
├── storage/
│   ├── meta_store.py       PG / 内存双实现（文档、密钥、审计）
│   └── vector_store.py     Qdrant / 本地双实现
└── schemas/models.py       全部请求/响应模型
```

---

## 9. 生产上线检查

- [ ] `ENV=prod`、`AUTH_ENABLED=true`（**绝不能为 false**）
- [ ] `BOOTSTRAP_ADMIN_KEY` 设为强随机值并妥善保管
- [ ] `PG_PASSWORD` 已改强密码
- [ ] 前置网关配置 HTTPS（明文 HTTP 传 API Key 等于泄露）
- [ ] Qdrant / PostgreSQL / Redis 未映射宿主机端口
- [ ] Prometheus 已接入并配置告警
- [ ] 已演练三种降级：Redis 宕机、PG 宕机、模型 API 不可用
